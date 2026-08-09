#!/usr/bin/env python3
# ============================================================================
# tests/e2e/e2e_webhook_oidc.py
#
# 针对 commit 95f9ba5 "Add Authentication & authorization (OAuth 2.0 / OIDC)"
# —— webhook 入口可以要求一个 GitLab 签名的 OIDC id_token，而不是接受匿名 POST。
#
# 这个 commit 是 **additive**：一个开关 `WEBHOOK_AUTH_MODE=none|oidc`，默认
# `none` 且承诺与加认证之前**逐字节一致**。所以两种状态都必须测 —— 只测 `oidc`
# 证明不了"开关关掉时什么都没变"，只测 `none` 则等于没测这个 commit。
#
#   arm=none  开关关掉时是**真 no-op**：匿名 POST 照常接受；连**带一个垃圾
#             Authorization 头**的 POST 也照常接受（证明那个头被完全忽略，
#             而不是"宽松地校验了一下"）；拒绝计数器保持为 0；全链路照常出 MR。
#   arm=oidc  开关打开时真的挡住：匿名 → 401；垃圾 bearer → 401；**我们自己
#             签的合法 RS256 JWT** → 401（证明确实对着 GitLab 的 JWKS 验签，
#             而不是"能解析就放行"）；`alg:none` 伪造 → 401；
#             **真 GitLab 签发的 token + 被篡改的 project.id → 403**；
#             真实通知 → 全链路出 MR。
#
# 403 那一条是这个 commit 的**核心**：docs/auth.md 说 project_id claim 与载荷
# 的对比"才是选 OIDC 而不是共享密钥的全部理由"。它必须用**真 GitLab 签发**的
# token 才算数，否则只是把单测重跑一遍。做法是往 `.gitlab-ci.yml` 里加一个测试
# 专用作业（独立 marker 块，测完移除），它用同样的 aud 申请 id_token、POST 一个
# project.id 被换掉的载荷，并把 HTTP 状态码打进作业日志 —— token 全程留在 CI 里。
#
# ⚠️ 这个 commit 改了 `.gitlab-ci.yml`，而**不能假设**目标项目里已经有那些改动。
#    脚本对 CI 文件一律走检测 → 修改 → 按**原始字节**还原，两个 arm 需要的 CI
#    状态恰好相反（none 要 snippet 缺席，oidc 要它在场），这也是两个 arm 放在
#    同一个脚本里的原因之一。
#
# 详细用法/退出码/产物见 --help；共用约定与受管资源契约见 tests/e2e/README.md。
# ============================================================================

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (  # noqa: E402
    ACCESS_DEVELOPER, ACCESS_REPORTER, AssertionFailure, GitLab, OIDC_BEGIN,
    OIDC_END, PreflightError, Stack, UndoStack,
    EXIT_FAIL, EXIT_PASS, EXIT_PREFLIGHT, EXIT_RESTORE_INCOMPLETE, EXIT_TIMEOUT,
    close_logging, create_project_access_token, ensure_ci_marker_block,
    ensure_no_competing_stack, ensure_project_hook, ensure_project_variable,
    ensure_redis, find_new_auto_mrs, grep_stream, init_logging, log,
    newest_mr_iid, purge_gateway_stream, read_env_value, redact,
    register_secret, resolve_llm_route, resolve_project, section, sh,
    sha256_short, trigger_pipeline, verify_inbound_reachable,
)

REPO_DIR = Path(__file__).resolve().parents[2]

DEFAULT_PROJECT_PATH = "root/sdlcma-fix-f01-off-by-one"
DEFAULT_PAT_ENV_FILE = "settings/worker_local_multi_process-pat.env"
WORKER_ENV_FILE = "settings/worker_local_multi_process.env"
GATEWAY_ENV_FILE = "gateway/gateway_local_multi_process.env"
SNIPPET_FILE = "infra/oidc-webhook/gitlab-ci-snippet.yml"
REDIS_URL = "redis://localhost:6379/15"
GATEWAY_PORT = 8000
DEFAULT_TIMEOUT_S = 900

# 测试专用的 403 探测作业，用**自己的** marker 块管理（和官方 snippet 分开，
# 这样两者的安装/移除互不干扰，还原也各还各的）。
PROBE_BEGIN = "# >>> sdlcma-e2e-authz-probe (managed by tests/e2e/e2e_webhook_oidc.py)"
PROBE_END = "# <<< sdlcma-e2e-authz-probe"
PROBE_JOB_NAME = "sdlcma_e2e_authz_probe"
# 一个几乎肯定不存在的项目 id：即使网关（有 bug 地）放行了这个载荷，它也碰不到
# 任何真实项目。
FOREIGN_PROJECT_ID = "999999"

PROBE_JOB_YAML = f"""\
# 测试专用：验证"authn 通过但 authz 不通过"确实被 403 拒绝。
# 用**真 GitLab 签发**的 id_token（和官方 notifier 同一个 aud），但把载荷里的
# project.id 换成别人的。这正是 docs/auth.md 说的那条 —— 一个完全合法的、由
# 项目 A 签发的 token，也不能触发针对项目 B 的运行。
{PROBE_JOB_NAME}:
  stage: .post
  image: alpine:3
  when: always
  allow_failure: true
  variables:
    GIT_STRATEGY: none
  id_tokens:
    SDLCMA_E2E_TOKEN:
      aud: "$SDLCMA_AUDIENCE"
  before_script:
    - apk add --no-cache curl jq >/dev/null
  script:
    - |
      jq -n --argjson pid "$SDLCMA_E2E_FOREIGN_PROJECT_ID" \\
        '{{object_kind:"pipeline",
          object_attributes:{{id:0, ref:"main", status:"failed"}},
          project:{{id:$pid, web_url:"http://example.invalid/not-our-project"}},
          builds:[{{id:0}}]}}' > probe.json
      # 只取状态码，不让 curl 因为 4xx 而失败 —— 4xx 正是我们要的结果。
      CODE=$(curl -sS -o /dev/null -w '%{{http_code}}' -X POST \\
        "$SDLCMA_GATEWAY_URL/webhook" \\
        -H "Authorization: Bearer $SDLCMA_E2E_TOKEN" \\
        -H "Content-Type: application/json" \\
        --data-binary @probe.json)
      echo "SDLCMA_E2E_AUTHZ_PROBE_STATUS=$CODE"
      # 000 = 连不上（网络/防火墙），和 403（授权拒绝）是完全不同的结论，
      # 所以把它单独喊出来，别让脚本把网络问题误读成认证问题。
      if [ "$CODE" = "000" ]; then
        echo "SDLCMA_E2E_AUTHZ_PROBE_NOTE=runner 连不上 $SDLCMA_GATEWAY_URL" >&2
      fi
"""


# ============================================================================
# 伪造用的 JWT（本地生成，用于证明网关真的在验签）
# ============================================================================

def _real_kid(issuer: str) -> str:
    """从 GitLab 的 JWKS 里取一个**真实存在**的 kid。

    ⚠️ 这一步是伪造探测有没有意义的关键。第一版给伪造 token 编了个假 kid，结果
    两条伪造**都**在"查 JWKS 找不到这个 kid"那一步就被挡了（reason=unknown_key），
    **根本没走到验签和算法固定**。也就是说它们证明的只是"不认识这个 kid"，而
    注释却写着"证明确实在验签" —— 断言比它声称的弱，是最坏的一种弱。
    带上真 kid，网关就会找到密钥、真的去验签，伪造才会撞在它该撞的那堵墙上。
    """
    r = requests.get(f"{issuer.rstrip('/')}/oauth/discovery/keys", timeout=15)
    keys = (r.json() or {}).get("keys") or []
    if not keys or not keys[0].get("kid"):
        raise PreflightError(f"从 {issuer} 的 JWKS 里取不到 kid: {redact(r.text[:200])}")
    return str(keys[0]["kid"])


def _claims(issuer: str, audience: str, project_id: str) -> dict:
    now = int(time.time())
    return {"iss": issuer, "aud": audience, "sub": "project_path:x/y:ref:main",
            "project_id": project_id, "iat": now, "nbf": now, "exp": now + 300}


def _forged_rs256(issuer: str, audience: str, project_id: str, kid: str) -> str:
    """claims 全对、**kid 是真的**，但用我们自己的私钥签。

    网关必须以 `invalid_signature` 拒绝：它找得到那把公钥，于是真的去验了签，
    而签名对不上。这条把"验签"和"仅仅解析 claims"彻底分开 —— 一个只解析不验签
    的实现会放行它。
    """
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return jwt.encode(_claims(issuer, audience, project_id), key,
                      algorithm="RS256", headers={"kid": kid})


def _unknown_kid_rs256(issuer: str, audience: str, project_id: str) -> str:
    """kid 是编的 —— 走的是 JWKS 未知 kid 的重取路径（有速率限制那条）。"""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return jwt.encode(_claims(issuer, audience, project_id), key,
                      algorithm="RS256", headers={"kid": "e2e-nonexistent-kid"})


def _alg_none_token(issuer: str, audience: str, project_id: str, kid: str) -> str:
    """手工拼一个 `alg: none` 的 token，**kid 用真的**。

    必须手工拼：PyJWT 拒绝**签发** alg=none（这是它的好设计），所以这条伪造只能
    自己 base64 拼出来。kid 用真的，是为了让它越过密钥查找、真正撞上网关写死的
    `algorithms=["RS256"]` —— 那才是这条探测要测的东西。
    """
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()
    header = b64({"alg": "none", "typ": "JWT", "kid": kid})
    body = b64(_claims(issuer, audience, project_id))
    return f"{header}.{body}."          # 第三段（签名）为空


# ============================================================================
# 上下文
# ============================================================================

@dataclass
class Ctx:
    op: GitLab = None                    # type: ignore[assignment]
    proj: Any = None
    undo: UndoStack = None               # type: ignore[assignment]
    logdir: Path = Path(".")
    timeout: int = DEFAULT_TIMEOUT_S
    base_child_env: dict = field(default_factory=dict)
    hook_url: str = ""
    issuer: str = ""
    audience: str = ""
    results: dict = field(default_factory=dict)


def _fake_pipeline_payload(project_id: int, web_url: str, ref: str = "main") -> dict:
    """一个形状正确、但**不会**被当成真 bug 处理的载荷。

    刻意用 `status: "success"` + 一个 `auto/` 前缀的 ref：orchestrator 的 parser
    把任何 `auto/*` 开头的 ref 当成 worker 自己的命名空间，绝不会当成 bug 源。
    这样即使这条消息漏进了 stream，也不会凭空起一个 worker。
    """
    return {"object_kind": "pipeline",
            "object_attributes": {"id": 0, "ref": f"auto/e2e-probe/{ref}",
                                  "status": "success"},
            "project": {"id": project_id, "web_url": web_url},
            "builds": [{"id": 0}]}


# ============================================================================
# Arm A —— WEBHOOK_AUTH_MODE=none（开关关掉必须是真 no-op）
# ============================================================================

def run_arm_none(ctx: Ctx) -> dict:
    section("ARM none — 开关关掉时必须与加认证之前逐字节一致")
    res: dict[str, Any] = {}
    proj, op, undo = ctx.proj, ctx.op, ctx.undo

    # ── GitLab 侧的修改（必须排在起栈之前，见 lib 的顺序规则）──────────
    # snippet 必须**缺席**：它和 oidc 绑定，配 mode=none 时 GitLab 的 webhook 和
    # snippet 会各 POST 一次同一条失败流水线，开出两个 MR、烧两份 LLM 钱。
    log("  .gitlab-ci.yml：OIDC snippet 必须缺席")
    ensure_ci_marker_block(op, undo, proj.id, branch=proj.default_branch,
                           begin=OIDC_BEGIN, end=OIDC_END, body=None)
    log("  .gitlab-ci.yml：403 探测作业也必须缺席")
    ensure_ci_marker_block(op, undo, proj.id, branch=proj.default_branch,
                           begin=PROBE_BEGIN, end=PROBE_END, body=None)
    log("  webhook：启用并指向本机（这条路径就是 mode=none 的全部入口）")
    hook_id = ensure_project_hook(op, undo, proj.id, url=ctx.hook_url, enabled=True)

    # ── 起 gateway（orchestrator 先不起，见下）──────────────────────
    child_env = dict(ctx.base_child_env)
    child_env["WEBHOOK_AUTH_MODE"] = "none"
    stack = Stack(logdir=ctx.logdir / "none", repo_dir=REPO_DIR,
                  child_env=child_env, gateway_port=GATEWAY_PORT)
    stack.logdir.mkdir(parents=True, exist_ok=True)
    stack.start_gateway(undo)

    # ── 边界探测。刻意在 orchestrator 起来**之前**做 ────────────────
    # 这些 POST 会被 XADD 进 stream；orchestrator 若已在跑，会把它们当成消息去
    # 处理。载荷本身已经设计成 parser 不会当作 bug（auto/* 前缀 + success），
    # 但顺序上仍然先探测、后清空 stream、再起 orchestrator，双保险。
    payload = _fake_pipeline_payload(proj.id, proj.web_url)

    r1 = stack.post_webhook(payload)
    res["anonymous_status"] = r1.status_code
    if r1.status_code >= 400:
        raise AssertionFailure(
            f"mode=none 下匿名 POST 被拒（HTTP {r1.status_code}）—— "
            f"开关关掉时必须与加认证之前一致。\n  响应: {redact(r1.text[:200])}")
    log(f"  ✓ 匿名 POST 被接受: HTTP {r1.status_code}")

    # 带一个**垃圾** Authorization 头。这条比匿名那条更有力：它证明该头被
    # **完全忽略**，而不是"宽松地校验了一下"——后者会在某天变成一个安全断言。
    r2 = stack.post_webhook(payload, headers={"Authorization": "Bearer not-a-jwt"})
    res["garbage_bearer_status"] = r2.status_code
    if r2.status_code >= 400:
        raise AssertionFailure(
            f"mode=none 下带垃圾 Authorization 头的 POST 被拒（HTTP {r2.status_code}）"
            f"—— 说明 `none` 并不是真正的 no-op。\n  响应: {redact(r2.text[:200])}")
    log(f"  ✓ 带垃圾 bearer 的 POST 同样被接受: HTTP {r2.status_code}")

    rejected = stack.counter_total("sdlcma_webhook_auth_rejected_total")
    res["auth_rejected_total"] = rejected
    if rejected:
        raise AssertionFailure(
            f"mode=none 下 sdlcma_webhook_auth_rejected_total = {rejected}，应为 0")
    log("  ✓ 拒绝计数器为 0")

    verify_inbound_reachable(op, proj.id, hook_id, stack)

    n = purge_gateway_stream(REDIS_URL)
    log(f"  已清空 gateway:stream（{n} 条，含上面几次探测）")
    stack.start_orchestrator(undo)

    # ── 全链路：GitLab 自己的 webhook 一条路走到 MR ──────────────────
    baseline = newest_mr_iid(op, proj.id)
    pipe = trigger_pipeline(op, proj.id, proj.default_branch)
    log(f"  ✓ 已触发流水线 {pipe['id']}（基线 MR iid={baseline}）")
    mrs = wait_for_mrs(op, proj, baseline, ctx.timeout, stack.logdir)
    res["mr_iids"] = [m["iid"] for m in mrs]
    res["mr_urls"] = [m["web_url"] for m in mrs]
    if len(mrs) != 1:
        raise AssertionFailure(
            f"mode=none 一次触发开出了 {len(mrs)} 个 MR: {res['mr_iids']} —— "
            f"应当恰好一个")
    log(f"  ✓ 恰好一个 MR: !{mrs[0]['iid']} {mrs[0]['source_branch']}")
    res["outcome"] = read_outcome(stack.logdir / "journal")
    if res["outcome"] != "fixed":
        raise AssertionFailure(f"mode=none 的 RunRecord outcome={res['outcome']}，期望 fixed")
    log(f"  ✓ RunRecord outcome={res['outcome']}")

    stack.stop_all()
    return res


# ============================================================================
# Arm B —— WEBHOOK_AUTH_MODE=oidc（开关打开必须真的挡住）
# ============================================================================

def run_arm_oidc(ctx: Ctx) -> dict:
    section("ARM oidc — 开关打开时必须真的挡住，且合法调用照常通过")
    res: dict[str, Any] = {}
    proj, op, undo = ctx.proj, ctx.op, ctx.undo

    # ── GitLab 侧的修改（同样排在起栈之前）─────────────────────────
    snippet_body = extract_snippet_body(REPO_DIR / SNIPPET_FILE)
    log("  .gitlab-ci.yml：安装官方 OIDC notifier 块")
    ensure_ci_marker_block(op, undo, proj.id, branch=proj.default_branch,
                           begin=OIDC_BEGIN, end=OIDC_END, body=snippet_body)

    # ⚠️ 交叉校验必须**在这一刻**做 —— 此时 OIDC 块是文件里唯一被加进去的块。
    # install_snippet.sh 会把它自己的块**追加到文件末尾**，所以只要文件里还有
    # 别的追加块（比如下面那个探测作业），它产出的顺序就会和现状不同，dry-run
    # 会报出一个**纯粹关于块先后顺序**的 diff —— 与"我们装的 snippet 内容对不对"
    # 毫无关系。放在这里，这条断言就与之后再加多少块无关。
    verify_matches_install_snippet(proj, res)

    log("  .gitlab-ci.yml：安装测试专用的 403 探测作业")
    ensure_ci_marker_block(op, undo, proj.id, branch=proj.default_branch,
                           begin=PROBE_BEGIN, end=PROBE_END, body=PROBE_JOB_YAML)

    log("  CI/CD 变量")
    ensure_project_variable(op, undo, proj.id,
                            key="SDLCMA_GATEWAY_URL", value=ctx.hook_url.rsplit("/", 1)[0])
    ensure_project_variable(op, undo, proj.id,
                            key="SDLCMA_AUDIENCE", value=ctx.audience)
    ensure_project_variable(op, undo, proj.id,
                            key="SDLCMA_E2E_FOREIGN_PROJECT_ID", value=FOREIGN_PROJECT_ID)
    if not read_variable(op, proj.id, "SDLCMA_CI_READ_TOKEN"):
        raise PreflightError(
            "项目缺少 SDLCMA_CI_READ_TOKEN。没有它，notifier 会把**自己的** job id\n"
            "  发给 worker（一个 curl 的 trace），worker 会去诊断一个 curl 而不是真\n"
            "  正的失败。snippet 会打警告但仍然发出去，所以这是个响一点的降级而不是\n"
            "  静默的降级 —— 但对本测试而言它会让全链路那条断言变得没有意义。\n"
            "  建一个 read_api、限本项目的 token 并设成这个变量。")
    log("  ✓ SDLCMA_CI_READ_TOKEN 存在（值不读取、不回显）")

    # ⚠️ 关掉 GitLab 自己的 webhook。在 oidc 模式下它是匿名的，每次投递必被 401，
    # 而 GitLab 会在**连续投递失败**后把 hook 置为不可执行（hook 对象上有
    # alert_status / disabled_until 两个字段），那会留下一个被停用的 hook 影响
    # 之后所有测试。真实的 oidc 部署里，触发是**反转**的：失败的 CI 作业主动
    # POST 给我们，GitLab 的 webhook 子系统不再参与。
    log("  webhook：停用 pipeline_events（oidc 下它只会一路 401）")
    ensure_project_hook(op, undo, proj.id, url=ctx.hook_url, enabled=False)
    res["hook_alert_status_before"] = hook_alert_status(op, proj.id)

    # ── 起 gateway（mode=oidc）──────────────────────────────────────
    child_env = dict(ctx.base_child_env)
    child_env["WEBHOOK_AUTH_MODE"] = "oidc"
    stack = Stack(logdir=ctx.logdir / "oidc", repo_dir=REPO_DIR,
                  child_env=child_env, gateway_port=GATEWAY_PORT)
    stack.logdir.mkdir(parents=True, exist_ok=True)
    stack.start_gateway(undo)

    # ── 负向探测（orchestrator 还没起）──────────────────────────────
    payload = _fake_pipeline_payload(proj.id, proj.web_url)
    kid = _real_kid(ctx.issuer)
    log(f"  取到 GitLab JWKS 的真实 kid（前 12 位）: {kid[:12]}…")

    # 每条探测都期望一个具体的 **reason**，而不只是"401"。
    # `expect_reason=None` 表示只要求"不是 unknown_key" —— 即它确实越过了密钥
    # 查找、撞在了自己该撞的那堵墙上，而不去假设 PyJWT 对 alg=none 的确切异常。
    probes: list[tuple[str, str | None, str | None]] = [
        ("no_token", None, "missing_token"),
        ("garbage_bearer", "Bearer not-a-jwt", "malformed_token"),
        # kid 编的 → 走未知 kid 的 JWKS 重取路径
        ("unknown_kid", "Bearer " + _unknown_kid_rs256(
            ctx.issuer, ctx.audience, str(proj.id)), "unknown_key"),
        # kid 是真的、claims 全对，签名是我们自己签的 → 必须栽在**验签**上
        ("forged_signature", "Bearer " + _forged_rs256(
            ctx.issuer, ctx.audience, str(proj.id), kid), "invalid_signature"),
        # kid 是真的、alg=none → 必须栽在**算法固定**上（写死 RS256，不可配置）
        ("alg_none", "Bearer " + _alg_none_token(
            ctx.issuer, ctx.audience, str(proj.id), kid), None),
    ]

    res["negative_probes"] = {}
    for name, auth, expect_reason in probes:
        before = stack.counter_by_label("sdlcma_webhook_auth_rejected_total")
        r = stack.post_webhook(payload,
                               headers={"Authorization": auth} if auth else None)
        after = stack.counter_by_label("sdlcma_webhook_auth_rejected_total")
        delta = [k for k in after if after[k] - before.get(k, 0.0) > 0]
        reason = (re.sub(r'^reason="|"$', "", delta[0]) if len(delta) == 1
                  else f"<{delta}>")
        res["negative_probes"][name] = {"status": r.status_code, "reason": reason}

        if r.status_code != 401:
            raise AssertionFailure(
                f"oidc 模式下 {name} 应当得到 401，实际 HTTP {r.status_code}\n"
                f"  响应: {redact(r.text[:200])}")
        if expect_reason and reason != expect_reason:
            raise AssertionFailure(
                f"{name} 拿到了 401，但拒绝原因是 {reason!r}，期望 {expect_reason!r}。\n"
                f"  原因不对意味着它没走到该走的那一步 —— 断言比它声称的弱。")
        if expect_reason is None and reason == "unknown_key":
            raise AssertionFailure(
                f"{name} 在密钥查找那一步就被挡了（reason=unknown_key），"
                f"根本没走到算法检查。\n"
                f"  这条探测必须带一个**真实的** kid 才有意义。")
        log(f"  ✓ {name} → 401 reason={reason}")

    # 拒绝原因是个**闭集枚举**：有了它，`received - forwarded = redis_failures +
    # auth_rejections` 才成立；没有它，一次拒绝激增读起来像 Redis 故障。
    reasons = {p["reason"] for p in res["negative_probes"].values()}
    log(f"  ✓ 五条探测覆盖了 {len(reasons)} 种拒绝原因: {sorted(reasons)}")

    n = purge_gateway_stream(REDIS_URL)
    log(f"  已清空 gateway:stream（{n} 条）")
    stack.start_orchestrator(undo)

    # ── 全链路：这次入口只剩 CI 作业带着真 token 那一条 ──────────────
    baseline = newest_mr_iid(op, proj.id)
    pipe = trigger_pipeline(op, proj.id, proj.default_branch)
    res["pipeline_id"] = pipe["id"]
    log(f"  ✓ 已触发流水线 {pipe['id']}（基线 MR iid={baseline}）")
    log(f"    {proj.web_url}/-/pipelines/{pipe['id']}")

    # 403 探测作业和 notifier 都在这条流水线的 .post 阶段
    res["authz_probe_status"] = wait_for_probe_status(op, proj, pipe["id"], timeout=420)
    if res["authz_probe_status"] == "000":
        raise PreflightError(
            f"403 探测作业连不上 {ctx.hook_url}（curl 返回 000）。\n"
            f"  这是**网络**问题不是认证问题：GitLab runner 到本机 :{GATEWAY_PORT}\n"
            f"  不通。检查 runner 容器的出网和 tailscale。")
    if res["authz_probe_status"] != "403":
        raise AssertionFailure(
            f"真 GitLab 签发的 token + 被篡改的 project.id 应当得到 **403**，"
            f"实际 {res['authz_probe_status']}。\n"
            f"  这是这个 commit 的核心断言：一个完全合法的、由项目 A 签发的 token，"
            f"也不能触发针对项目 B 的运行（docs/auth.md）。")
    log("  ✓ 真 token + 被篡改的 project.id → 403（authn 过、authz 不过）")

    mrs = wait_for_mrs(op, proj, baseline, ctx.timeout, stack.logdir)
    res["mr_iids"] = [m["iid"] for m in mrs]
    res["mr_urls"] = [m["web_url"] for m in mrs]
    if len(mrs) != 1:
        raise AssertionFailure(
            f"oidc 一次触发开出了 {len(mrs)} 个 MR: {res['mr_iids']} —— 应当恰好一个")
    log(f"  ✓ 恰好一个 MR: !{mrs[0]['iid']} {mrs[0]['source_branch']}")
    res["outcome"] = read_outcome(stack.logdir / "journal")
    if res["outcome"] != "fixed":
        raise AssertionFailure(f"oidc 的 RunRecord outcome={res['outcome']}，期望 fixed")
    log(f"  ✓ RunRecord outcome={res['outcome']}")

    # 接受路径也留下审计线索：那是被放行的触发唯一的记录
    accepts = grep_stream(stack.logdir / "gateway.log",
                          "phase_marker phase=webhook_auth result=accept")
    res["accept_markers"] = len(accepts)
    if not accepts:
        raise AssertionFailure(
            "gateway 日志里没有 phase=webhook_auth result=accept —— 被放行的触发"
            "没有留下任何审计记录（RunRecord 里没有 actor，这是唯一的线索）")
    log(f"  ✓ 审计标记 accept×{len(accepts)}: {accepts[-1].split('phase_marker ')[-1][:100]}")

    res["hook_alert_status_after"] = hook_alert_status(op, proj.id)
    stack.stop_all()
    return res


# ============================================================================
# 辅助
# ============================================================================

def extract_snippet_body(path: Path) -> str:
    """从 gitlab-ci-snippet.yml 取"第一个 YAML key 起"的正文。

    与 install_snippet.sh 同一条规则：文件头那一大段设计说明属于仓库，不属于
    每个被监控的项目。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln and not ln.startswith("#"))
    return "\n".join(lines[start:]).strip("\n")


def verify_matches_install_snippet(proj, res: dict) -> None:
    """跑 install_snippet.sh 的 dry-run，确认它认为"已经同步"。

    这是一条交叉验证：如果有人改了 gitlab-ci-snippet.yml 而本脚本的提取规则没跟上，
    dry-run 会打出 diff 而不是 "no change needed"。dry-run 是默认行为，不会提交。
    """
    r = sh(["bash", str(REPO_DIR / "infra/oidc-webhook/install_snippet.sh")],
           cwd=REPO_DIR, check=False, timeout=120,
           env={**os.environ, "PROJECT_PATH": proj.path})
    synced = "no change needed" in r.stdout
    res["install_snippet_dry_run_in_sync"] = synced
    if synced:
        log("  ✓ install_snippet.sh 的 dry-run 认为已同步（我们装的块与它一致）")
    else:
        log("  ! install_snippet.sh 的 dry-run 报告有差异 —— 提取规则可能已经分叉:")
        for ln in r.stdout.splitlines()[:20]:
            log(f"      {ln}")
        raise AssertionFailure(
            "脚本装进 .gitlab-ci.yml 的 notifier 块与 install_snippet.sh 会装的不一致。"
            "\n  两者必须一致，否则本测试测的就不是仓库里那个受支持的 snippet。")


def read_variable(op: GitLab, project_id: int, key: str) -> bool:
    """变量是否存在。**不返回值** —— 有些是 masked 的凭据。"""
    return op.get(f"/projects/{project_id}/variables/{key}").status_code == 200


def hook_alert_status(op: GitLab, project_id: int) -> str:
    hooks = op.get(f"/projects/{project_id}/hooks")
    if hooks.status_code != 200 or not hooks.json():
        return "<no hook>"
    return str(hooks.json()[0].get("alert_status", "?"))


def wait_for_probe_status(op: GitLab, proj, pipeline_id: int, timeout: int) -> str:
    """等 403 探测作业跑完，从它的日志里取回 HTTP 状态码。

    状态码留在作业日志里而不是回传给脚本，是因为 id_token **只应该存在于 CI
    作业内部** —— 把它取出来给脚本，等于把一个真凭据搬到日志和进程环境里。
    """
    deadline = time.time() + timeout
    job_id = None
    while time.time() < deadline:
        jobs = op.get(f"/projects/{proj.id}/pipelines/{pipeline_id}/jobs",
                      params={"per_page": 100})
        if jobs.status_code == 200:
            for j in jobs.json():
                if j["name"] == PROBE_JOB_NAME:
                    job_id = j["id"]
                    if j["status"] in ("success", "failed", "canceled"):
                        trace = op.get(f"/projects/{proj.id}/jobs/{job_id}/trace")
                        m = re.search(r"SDLCMA_E2E_AUTHZ_PROBE_STATUS=(\d+)", trace.text)
                        if m:
                            return m.group(1)
                        return f"<作业 {j['status']} 但日志里没有状态码>"
        log(f"  [...] 等 {PROBE_JOB_NAME} 完成（job_id={job_id}）")
        time.sleep(10)
    return f"<{timeout}s 内没等到 {PROBE_JOB_NAME}>"


def wait_for_mrs(op: GitLab, proj, baseline: int, timeout: int,
                 logdir: Path) -> list[dict]:
    """等到出现新的 auto/bf MR，然后再多等一会看会不会冒出第二个。

    为什么要多等：一次触发本应只产生一个 worker。snippet 在场而 gateway 跑
    mode=none 时会各 POST 一次，开出两个 MR —— 只取第一个匹配就永远看不见。
    """
    deadline = time.time() + timeout
    found: list[dict] = []
    while time.time() < deadline:
        found = find_new_auto_mrs(op, proj.id, baseline)
        if found:
            break
        marks = grep_stream(logdir / "orchestrator.log", "phase_marker phase=")
        last = (marks[-1].split("phase_marker ", 1)[-1][:80] if marks
                else "(还没有 phase_marker)")
        log(f"  [...] 还没有新 MR；最后一个标记: {last}")
        time.sleep(15)
    if not found:
        raise TimeoutError(f"{timeout}s 内没有等到新的 auto/bf MR")
    log(f"  ✓ 出现 MR !{found[0]['iid']}；再等 45s 确认没有第二个 worker…")
    time.sleep(45)
    return sorted(find_new_auto_mrs(op, proj.id, baseline), key=lambda m: m["iid"])


def read_outcome(journal: Path) -> str:
    if not journal.is_dir():
        return "<没有 journal 目录>"
    runs = sorted([d for d in journal.iterdir() if (d / "record.json").exists()],
                  key=lambda d: d.stat().st_mtime)
    if not runs:
        return "<没有 record.json>"
    return str(json.loads((runs[-1] / "record.json").read_text(encoding="utf-8"))
               .get("outcome"))


def check_oidc_preconditions(issuer_cfg: str, audience_cfg: str) -> tuple[str, str]:
    """网关的 OIDC 设置必须和 GitLab 的 discovery 对得上，逐字节。

    `iss` 是实例的 external_url，**经常不是**你访问它用的那个 URL；抄错会得到
    一个写着"token issuer does not match"的 401，看起来像坏 token 而不是坏配置。
    """
    if not issuer_cfg:
        raise PreflightError(f"{GATEWAY_ENV_FILE} 里 OIDC_ISSUER 为空 —— "
                             f"mode=oidc 下这是致命配置错误，不是回退到开放")
    if not audience_cfg:
        raise PreflightError(
            f"{GATEWAY_ENV_FILE} 里 OIDC_AUDIENCE 为空。它**没有默认值**是刻意的：\n"
            f"  GitLab 会为作业申请的任何 aud 签发 token，一个能猜到的 audience\n"
            f"  等于让实例上任何项目都能伪造触发。")
    disc = requests.get(f"{issuer_cfg.rstrip('/')}/.well-known/openid-configuration",
                        timeout=15)
    if disc.status_code != 200:
        raise PreflightError(f"OIDC discovery 取不到: HTTP {disc.status_code}")
    d = disc.json()
    if d.get("issuer") != issuer_cfg:
        raise PreflightError(
            f"OIDC_ISSUER={issuer_cfg!r} 与 GitLab 自报的 issuer={d.get('issuer')!r} "
            f"不符。必须逐字节相等 —— 不符会得到一个看起来像坏 token 的 401。")
    algs = d.get("id_token_signing_alg_values_supported") or []
    if "RS256" not in algs:
        raise PreflightError(
            f"GitLab 没有声明支持 RS256（{algs}）。网关把 algorithms=['RS256'] 写死，"
            f"修法**不是**放宽那个列表。")
    log(f"  ✓ OIDC discovery: issuer={d['issuer']} algs={algs} "
        f"jwks={d.get('jwks_uri')}")
    log(f"  ✓ OIDC_AUDIENCE={audience_cfg}")
    return issuer_cfg, audience_cfg


# ============================================================================
# main
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="""\
e2e：webhook 的 OIDC 认证与授权（commit 95f9ba5）

  这个 commit 是 additive —— 一个开关 WEBHOOK_AUTH_MODE=none|oidc，默认 none
  且承诺与加认证之前逐字节一致。所以**两种状态都测**：

    arm=none  匿名 POST 接受；带垃圾 Authorization 头的 POST **也**接受
              （证明该头被完全忽略，而不是宽松校验）；拒绝计数器为 0；
              全链路照常出一个 MR
    arm=oidc  匿名 / 垃圾 bearer / 我们自签的合法 RS256 / alg:none → 全部 401；
              **真 GitLab 签发的 token + 被篡改的 project.id → 403**；
              合法通知 → 全链路出一个 MR；接受路径留下审计标记

  403 那条是核心：docs/auth.md 说 project_id claim 与载荷的对比"才是选 OIDC
  而不是共享密钥的全部理由"。它由一个测试专用的 CI 作业发起，token 全程留在
  CI 里，不落任何日志。

  ⚠️ 这个 commit 改了 .gitlab-ci.yml，脚本**不假设**项目里已有那些改动：
     对 CI 文件一律检测 → 修改 → 按原始字节还原。两个 arm 需要的 CI 状态恰好
     相反（none 要 snippet 缺席，oidc 要它在场）。

  ⚠️ 重型测试，不在全量回归里。需要自托管 GitLab + online runner + Redis +
     两次真实计费的 LLM 运行 + 入站可达。约 5 分钟。

  什么时候该重跑：
    * gateway/webhook_auth.py 或 gateway/gateway.py 的 _enforce_webhook_auth
    * gateway/gateway_settings.py 的 OIDC 字段
    * infra/oidc-webhook/ 下的 snippet / setup.sh / install_snippet.sh
    * 换 GitLab 大版本（id_token claims、discovery、JWKS 路径都可能变）
""",
        epilog="""\
退出码:
  0 PASS / 2 FAIL / 3 TIMEOUT / 4 环境前置失败（不是对代码的判决）
  5 恢复不完整（结论可信，但真实系统上有东西没还原，必须有人去看）

产物: evaluation/e2e_runs/<时间戳>_webhook_oidc/
  verdict.json / summary.log / none/{gateway,orchestrator}.log / oidc/…

约定与已知坑: tests/e2e/README.md
""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", choices=["both", "none", "oidc"], default="both",
                    help="跑哪个 arm（默认 both；none 先跑，它是基线）")
    ap.add_argument("--project-path", default=DEFAULT_PROJECT_PATH)
    ap.add_argument("--pat-env-file", default=DEFAULT_PAT_ENV_FILE)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--dry-run", action="store_true", help="只探测环境，不做任何修改")
    ap.add_argument("--stop-systemd-stack", action="store_true")
    args = ap.parse_args()

    logdir = (REPO_DIR / "evaluation" / "e2e_runs" /
              f"{time.strftime('%Y%m%d_%H%M%S')}_webhook_oidc")
    logdir.mkdir(parents=True, exist_ok=True)
    init_logging(logdir / "summary.log")
    log("e2e: webhook OIDC 认证与授权（commit 95f9ba5）")
    log(f"日志目录: {logdir}")

    undo = UndoStack()
    verdict: dict[str, Any] = {
        "test": "webhook_oidc", "commit_under_test": "95f9ba5",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "args": vars(args)}
    rc = EXIT_PASS

    def _on_signal(signum, _frame):
        log(f"收到信号 {signum} —— 开始恢复环境")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        section("Phase 0.1: 依赖 + 凭据来源")
        for exe in ("git", "curl", "docker", "tailscale"):
            if not shutil.which(exe):
                raise PreflightError(f"缺少依赖: {exe}")
        try:
            import jwt  # noqa: F401
            from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
        except ImportError as exc:
            raise PreflightError(
                f"缺少 PyJWT[crypto]（{exc}）—— 伪造探测需要它自己签一个 RS256 token。"
            ) from exc
        log(f"  ✓ 依赖齐全（含 PyJWT[crypto]）；repo={REPO_DIR}")

        pat = os.environ.get("SDLCMA_OPERATOR_PAT", "").strip()
        source = "环境变量 SDLCMA_OPERATOR_PAT"
        if not pat:
            pat = read_env_value(REPO_DIR / args.pat_env_file, "GITLAB_PRIVATE_TOKEN")
            source = args.pat_env_file
        if not pat:
            raise PreflightError("拿不到 operator PAT")
        register_secret(pat)
        log(f"  operator PAT 来源: {source}（指纹 {sha256_short(pat)}）")

        api = read_env_value(REPO_DIR / WORKER_ENV_FILE, "GITLAB_API")
        if not api:
            raise PreflightError(f"{WORKER_ENV_FILE} 里没有 GITLAB_API")
        op = GitLab(api, pat, "operator")

        section("Phase 0.2: 冲突进程检查")
        ensure_no_competing_stack(
            undo, stop_systemd=args.stop_systemd_stack and not args.dry_run)

        section("Phase 0.3: GitLab / 项目 / Runner")
        proj = resolve_project(op, args.project_path)

        section("Phase 0.4: OIDC 配置与 GitLab discovery 对账")
        issuer, audience = check_oidc_preconditions(
            read_env_value(REPO_DIR / GATEWAY_ENV_FILE, "OIDC_ISSUER"),
            read_env_value(REPO_DIR / GATEWAY_ENV_FILE, "OIDC_AUDIENCE"))

        section("Phase 0.5: LLM 路由与模型名决议（要求 8）")
        llm = resolve_llm_route(REPO_DIR)

        section("Phase 0.6: Redis")
        ensure_redis(undo, REDIS_URL, start_if_missing=not args.dry_run)

        local_ip = sh(["tailscale", "ip", "-4"], check=False).stdout.strip().splitlines()
        if not local_ip:
            raise PreflightError("拿不到本机 tailnet IPv4")
        hook_url = f"http://{local_ip[0]}:{GATEWAY_PORT}/webhook"
        log(f"  本机 tailnet IP = {local_ip[0]} → {hook_url}")

        verdict["environment"] = {
            "gitlab_version": proj.gitlab_version, "project_id": proj.id,
            "project_path": proj.path, "default_branch": proj.default_branch,
            "online_runners": proj.online_runners,
            "oidc_issuer": issuer, "oidc_audience": audience,
            "gateway_url": hook_url,
            "llm_route": llm.route, "llm_model_effective": llm.model_effective,
            "llm_notes": llm.notes,
        }
        if args.dry_run:
            log("")
            log("--dry-run: 环境探测全部通过，未做任何修改")
            verdict["result"] = "DRY_RUN_OK"
            return EXIT_PASS

        child_env = os.environ.copy()
        if llm.changed:
            child_env["LLM_MODEL"] = llm.model_effective
        child_env["BF_JOURNAL_DIR"] = ""      # 每个 arm 自己覆盖
        child_env["PYTHONUNBUFFERED"] = "1"

        ctx = Ctx(op=op, proj=proj, undo=undo, logdir=logdir, timeout=args.timeout,
                  base_child_env=child_env, hook_url=hook_url,
                  issuer=issuer, audience=audience)

        arms = ["none", "oidc"] if args.arm == "both" else [args.arm]
        for arm in arms:
            # 每个 arm 的 journal 各自隔离，便于分别断言 outcome
            ctx.base_child_env["BF_JOURNAL_DIR"] = str(logdir / arm / "journal")
            (logdir / arm / "journal").mkdir(parents=True, exist_ok=True)
            ctx.results[arm] = (run_arm_none(ctx) if arm == "none"
                                else run_arm_oidc(ctx))

        verdict["arms"] = ctx.results
        if args.arm == "both":
            # 两个 arm 的对比才是"additive"这个命题的意义所在：同一个项目、同一套
            # CI yaml 处理、同一次栈构建，只有开关不同 —— 两边都恰好一个 MR、都
            # outcome=fixed，而认证边界的行为完全相反。
            log("")
            log("  两个 arm 的对比:")
            log(f"    none: MR={ctx.results['none']['mr_iids']} "
                f"outcome={ctx.results['none']['outcome']} "
                f"匿名={ctx.results['none']['anonymous_status']} "
                f"垃圾bearer={ctx.results['none']['garbage_bearer_status']} "
                f"拒绝数={ctx.results['none']['auth_rejected_total']}")
            neg = {k: v["reason"]
                   for k, v in ctx.results["oidc"]["negative_probes"].items()}
            log(f"    oidc: MR={ctx.results['oidc']['mr_iids']} "
                f"outcome={ctx.results['oidc']['outcome']} "
                f"负向(全 401，reason)={neg} "
                f"authz探测={ctx.results['oidc']['authz_probe_status']}")

        verdict["result"] = "PASS"
        log("")
        log("=== PASS ===")

    except PreflightError as exc:
        verdict.update(result="PREFLIGHT_FAIL", error=str(exc))
        log("")
        log("=== 环境前置失败（不是被测代码的问题）===")
        log(str(exc))
        rc = EXIT_PREFLIGHT
    except AssertionFailure as exc:
        verdict.update(result="FAIL", error=str(exc))
        log("")
        log("=== FAIL ===")
        log(str(exc))
        rc = EXIT_FAIL
    except TimeoutError as exc:
        verdict.update(result="TIMEOUT", error=str(exc))
        log("")
        log("=== TIMEOUT ===")
        log(str(exc))
        rc = EXIT_TIMEOUT
    except KeyboardInterrupt:
        verdict["result"] = "INTERRUPTED"
        log("")
        log("=== 被中断 ===")
        rc = EXIT_FAIL
    except Exception as exc:  # noqa: BLE001
        import traceback
        verdict.update(result="ERROR", error=redact(f"{type(exc).__name__}: {exc}"))
        log("")
        log("=== 脚本自身异常 ===")
        log(redact(traceback.format_exc()))
        rc = EXIT_FAIL
    finally:
        undo.run_all()
        if undo.failures:
            verdict["restore_failures"] = undo.failures
            log("")
            log("=== 环境恢复不完整（退出码 5）===")
            for f in undo.failures:
                log(f"  {f}")
            rc = EXIT_RESTORE_INCOMPLETE if rc == EXIT_PASS else rc

        verdict["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        verdict["exit_code"] = rc
        (logdir / "verdict.json").write_text(
            json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8")
        log("")
        log(f"结论: {verdict.get('result')}  退出码={rc}")
        log(f"产物: {logdir}")
        close_logging()
    return rc


if __name__ == "__main__":
    sys.exit(main())
