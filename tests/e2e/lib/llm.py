"""
tests/e2e/lib/llm.py — LLM 路由与模型名决议（通用要求 8）。

**背景是查明的根因，不是猜测。** 同一个 `LLM_MODEL` 被两个客户端读：

  langgraph worker   services/llm_client.py → ChatOpenAI(model=cfg.llm_model)
                     字符串**原样**发给 endpoint ⇒ 必须是后端认识的**裸名**
  mini / ver99       litellm，经 agents/mini_swe_agent.py::_litellm_model_name()
                     litellm **要求** provider 前缀，裸名会被**自动补成**
                     `openai/<name>`

⇒ 正确写法是 `LLM_MODEL` 保持**裸名**：mini 自己会补前缀，两条路径同一个值都对。
   带前缀的值是 mini 路径的遗留物，落到 langgraph 路径上会 400。

更糟的是 `LLM_VIA_GATEWAY=true` 会让 worker **跳过** llm_model_check（gateway
暴露的是多 backend 的并集，单名校验是范畴错误），所以模型名写错只在第一次真实
LLM 调用时才炸 —— 那时 fetch/parse/clone 都已经绿了。

本模块：检测 → 决议 → 以**进程环境变量**形式修改（天然无需恢复）。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .core import PreflightError, log, register_secret, redact, sh


@dataclass
class LlmRoute:
    route: str = ""                 # "direct" | "gateway"
    base_url: str = ""
    model_configured: str = ""
    model_effective: str = ""
    param_profile: str = "chat"
    via_gateway: bool = False
    served: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.model_effective != self.model_configured


def resolve_llm_route(repo_dir: Path) -> LlmRoute:
    """决议出这次运行真正会用的 LLM 路由和模型名，并用一次最小调用证实它。"""
    # 用**产品自己的**解析拿有效配置，而不是重新实现一遍 pydantic 的优先级。
    probe_src = (
        "import json;"
        "from settings.worker_settings import cfg;"
        "print(json.dumps({"
        "'base': cfg.llm_api_base_url, 'model': cfg.llm_model,"
        "'key': cfg.llm_api_key, 'via_gateway': bool(cfg.llm_via_gateway),"
        "'auth_mode': cfg.llm_auth_mode, 'param_profile': cfg.llm_param_profile}))"
    )
    # 清掉可能污染基线的 LLM_* 环境变量：要看的是 **env 文件本身**解析出什么。
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
    proc = sh([sys.executable, "-c", probe_src], cwd=repo_dir, env=clean_env)
    cfg = json.loads(proc.stdout.strip().splitlines()[-1])
    register_secret(cfg["key"])

    r = LlmRoute(
        base_url=cfg["base"].rstrip("/"),
        model_configured=cfg["model"],
        model_effective=cfg["model"],
        param_profile=cfg["param_profile"],
        via_gateway=bool(cfg["via_gateway"]),
    )
    log(f"  env 文件解析出: base={r.base_url} model={r.model_configured!r} "
        f"via_gateway={r.via_gateway} auth_mode={cfg['auth_mode']} "
        f"param_profile={r.param_profile}")
    if not r.base_url:
        raise PreflightError("LLM_API_BASE_URL 为空 —— 没法跑真实的 ReAct 循环")

    root = r.base_url[:-3] if r.base_url.endswith("/v1") else r.base_url
    headers = {"Authorization": f"Bearer {cfg['key']}"}

    # ── 判路由 ──────────────────────────────────────────────────────
    # 判据必须是**只有真目标才有的证据**（教训：端口占用会伪造 PASS）。
    # llm_gateway 有 /health 且返回自己的形状；DeepSeek 的 API 没有。
    r.route = "direct"
    try:
        h = requests.get(f"{root}/health", timeout=5)
        if h.status_code == 200 and isinstance(h.json(), dict) and "backends" in h.json():
            r.route = "gateway"
    except Exception:
        pass
    log(f"  路由判定: {r.route}")

    # ── 后端认识哪些模型 id ─────────────────────────────────────────
    for url in (f"{r.base_url}/models", f"{root}/v1/models"):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                r.served = [m.get("id", "") for m in (resp.json().get("data") or [])]
                if r.served:
                    log(f"  {url} → {len(r.served)} 个模型: {r.served[:8]}")
                    break
        except Exception as exc:
            log(f"  {url} 探测失败: {type(exc).__name__}")

    if r.route == "gateway":
        # gateway 会把 body 里的 model 重写成 backend 声明的模型，所以 LLM_MODEL
        # 只是占位符 —— **不要动它**。但它进 cache key，改了会让已录制的 replay
        # cache 全部失效（那是真金白银录出来的）。这里只告警，不修改。
        r.notes.append("gateway 路由：LLM_MODEL 是占位符，会被 gateway 重写；未做修改"
                       "（改动会让已录制的 replay cache 全部失效）")
        log(f"  → {r.notes[-1]}")
    elif r.served and r.model_configured not in r.served:
        bare = r.model_configured.split("/", 1)[-1]
        candidates = {m for m in r.served if m == bare or m.split("/", 1)[-1] == bare}
        if len(candidates) == 1:
            r.model_effective = candidates.pop()
            r.notes.append(
                f"直连路由：配置的 {r.model_configured!r} 不在后端模型列表里，去掉 "
                f"provider 前缀后唯一命中 {r.model_effective!r}；以进程环境变量 "
                f"LLM_MODEL 覆盖（不改磁盘，天然无需恢复）")
            log(f"  → {r.notes[-1]}")
        else:
            raise PreflightError(
                f"LLM_MODEL={r.model_configured!r} 不在后端提供的模型列表里，\n"
                f"  而候选也不唯一（{sorted(candidates) or '无'}）—— 不做假设，请人工确认。\n"
                f"  后端提供的模型: {r.served}\n"
                f"  提示：langgraph 路径要**裸名**（ChatOpenAI 原样发送）；带 provider\n"
                f"       前缀的写法是 mini/litellm 路径的遗留物。")
    elif not r.served:
        r.notes.append("后端没有可用的 /models 列表，跳过模型名比对，只做实调用验证")
        log(f"  → {r.notes[-1]}")

    # ── 终局验证：一次最小的真实调用 ────────────────────────────────
    # 这是唯一不依赖任何假设的检查。成本约几分之一分钱，换掉"跑了五分钟才在
    # 第一次 LLM 调用上 400"的失败模式。
    body: dict[str, Any] = {"model": r.model_effective,
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 1}
    if r.param_profile != "reasoning":
        body["temperature"] = 0
    try:
        resp = requests.post(f"{r.base_url}/chat/completions",
                             headers=headers, json=body, timeout=60)
    except requests.RequestException as exc:
        raise PreflightError(f"LLM 后端不可达: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise PreflightError(
            f"最小 LLM 调用失败: HTTP {resp.status_code}\n"
            f"  model={r.model_effective!r} base={r.base_url}\n"
            f"  后端原文: {redact(resp.text[:500])}\n"
            f"  （若是 model not found：langgraph 路径要裸名，见上面的说明）")
    log(f"  ✓ 最小 LLM 调用 200 —— 路由={r.route} model={r.model_effective!r} 已证实可用")
    return r
