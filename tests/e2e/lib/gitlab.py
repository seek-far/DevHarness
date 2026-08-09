"""
tests/e2e/lib/gitlab.py — GitLab 客户端 + GitLab 侧的"受管资源"。

**受管资源** 是这套 e2e 脚本的核心抽象，每个 `ensure_*` / `create_*` 都遵守
同一份契约：

  1. 先**检测**当前状态；
  2. 只在与期望不符时才**修改**；
  3. **只在真的改了**才往 undo 栈里登记逆操作；
  4. 逆操作执行后**回查**结果，不只是发出动作。

第 3 条不是洁癖：在真实系统上无谓地"删了又建"一条本来就正确的配置，是在制造
本可以不存在的写操作和竞态。第 4 条同样有真实教训——发出 DELETE 拿到 204，
不等于那个凭据真的失效了。

⚠️ 恢复顺序：undo 栈是 LIFO，**调用顺序决定恢复顺序**。凡是恢复动作本身有副
作用的资源（改 `.gitlab-ci.yml` 会触发流水线），必须在启动本地栈**之前**调用，
这样弹栈时才会先停栈、后还原它。
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

from .core import (
    AssertionFailure, PreflightError, UndoStack,
    log, redact, register_secret,
)

# GitLab 成员访问级别
ACCESS_GUEST = 10
ACCESS_REPORTER = 20
ACCESS_DEVELOPER = 30
ACCESS_MAINTAINER = 40
ACCESS_OWNER = 50


# ============================================================================
# 客户端
# ============================================================================


class GitLab:
    """只做三件事：拼 URL、带 PRIVATE-TOKEN 头、把 token 登记进脱敏表。

    刻意不用 python-gitlab：这里要断言的恰恰是**原始 HTTP 语义**（403 vs 404、
    `permissions` 块的形状、events 的 author），封装库会把这些抹平。
    """

    def __init__(self, api: str, token: str, label: str = ""):
        self.api = api.rstrip("/")
        self.token = token
        self.label = label            # "operator" / "bot" —— 只用于日志
        register_secret(token)

    def request(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 30)
        return requests.request(method, f"{self.api}{path}",
                                headers={"PRIVATE-TOKEN": self.token}, **kw)

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw) -> requests.Response:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw) -> requests.Response:
        return self.request("DELETE", path, **kw)

    def json_or_raise(self, resp: requests.Response, what: str) -> Any:
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{what} 失败: HTTP {resp.status_code} — {redact(resp.text[:300])}")
        return resp.json()


def enc(s: str) -> str:
    """URL 路径段编码（项目路径、分支名都要）。"""
    return urllib.parse.quote(s, safe="")


# ============================================================================
# 项目事实
# ============================================================================


@dataclass
class Project:
    id: int = 0
    path: str = ""
    web_url: str = ""
    default_branch: str = "main"
    gitlab_version: str = ""
    online_runners: list[int] = field(default_factory=list)


def _unreachable_hint(host: str) -> str:
    """GitLab 不可达时给出**顺链诊断**，而不是干巴巴一句"不可达"。

    真实事故（2026-08-09）：GitLab@minus 起不来的根因是中国那台 k3s server
    (ls4900) 关机 → minus 上的 k3s-agent（TimeoutStartSec=infinity）永远等不到
    它 → multi-user.target 永不激活 → gitlab-runsvdir（After=multi-user.target）
    的 start job 永远 waiting → GitLab 的 runit supervisor 根本没起。
    症状是 8929 接受 TCP、零字节、约 2.1s 后干净关闭。
    只报"不可达"的脚本会让人把这条链从头再查一遍。
    """
    return (
        f"GitLab 在 {host} 上不可达。\n"
        "\n"
        "  ⚠️ 不要用 /-/health 判断存活 —— 它对不在 GitLab 监控白名单里的 IP\n"
        "     返回 404，看起来像'服务没起'。用 /api/v4/version。\n"
        "\n"
        "  已知的高频根因（2026-08-09 实测）：GitLab@minus 是 omnibus 装在 WSL 里，\n"
        "  启动被 k3s-agent 挡住 —— 只要 k3s server(ls4900) 关机，k3s-agent\n"
        "  (TimeoutStartSec=infinity) 就永远等不到它，multi-user.target 永不激活，\n"
        "  而 gitlab-runsvdir 是 After=multi-user.target。\n"
        "\n"
        "  在 minus 上诊断（ssh -i ~/.ssh/minus_sidekiq user@<tailscale ip -4 minus>）：\n"
        "    systemctl list-jobs            # 一眼看到卡住的 running job + 一串 waiting\n"
        "    systemctl is-active gitlab-runsvdir\n"
        "  恢复（⚠️ 直接 start gitlab-runsvdir 无效，After= 排序照样生效）：\n"
        "    sudo systemctl stop k3s-agent\n"
        "    sudo systemctl start gitlab-runsvdir\n"
        "    sudo gitlab-ctl status\n"
    )


def resolve_project(gl: GitLab, project_path: str, *,
                    require_runner: bool = True) -> Project:
    """探测 GitLab 可达性 + 项目 + runner。任何一项不满足 → PreflightError。"""
    host = urllib.parse.urlparse(gl.api).netloc

    try:
        resp = gl.get("/version")
    except requests.RequestException as exc:
        raise PreflightError(
            _unreachable_hint(host) + f"\n  原始错误: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code == 401:
        raise PreflightError(
            "operator PAT 被拒（HTTP 401）。它过期/被吊销/写错了。")
    if resp.status_code != 200:
        raise PreflightError(
            _unreachable_hint(host) + f"\n  GET /version → HTTP {resp.status_code}")

    p = Project(gitlab_version=resp.json().get("version", "?"))
    log(f"  ✓ GitLab {p.gitlab_version} 可达（operator PAT 有效）")

    resp = gl.get(f"/projects/{enc(project_path)}")
    if resp.status_code in (403, 404):
        visible = gl.get("/projects", params={"simple": "true", "per_page": 50,
                                              "membership": "true"})
        names = ([x.get("path_with_namespace") for x in visible.json()]
                 if visible.status_code == 200 else [])
        raise PreflightError(
            f"项目 {project_path} 不可见（HTTP {resp.status_code}）。\n"
            f"  GitLab 对'你无权看见'的资源返回 404 而不是 403，所以这可能是权限\n"
            f"  问题而不是'项目不存在'。\n"
            f"  这个 token 可见的项目: {names or '(一个都没有)'}"
        )
    data = gl.json_or_raise(resp, "读取项目")
    p.id = data["id"]
    p.path = data["path_with_namespace"]
    p.web_url = data["web_url"]
    p.default_branch = data.get("default_branch") or "main"
    log(f"  ✓ 项目 id={p.id} path={p.path} default_branch={p.default_branch}")
    log(f"    web_url={p.web_url}")

    # ⚠️ web_url 里的主机名必须从**本机**解析得到正确地址：worker clone 的就是
    # 这个 URL。而本机 DNS 里 `minus` 还有一条路由器给的 minus.speedport.ip
    # 链路本地 IPv6 记录 —— 解析到哪个是要**验证**的事实，不是可以假设的。
    parsed = urllib.parse.urlparse(p.web_url)
    try:
        probe = requests.get(f"{parsed.scheme}://{parsed.netloc}/api/v4/version",
                             headers={"PRIVATE-TOKEN": gl.token}, timeout=15)
        ok = probe.status_code == 200
    except requests.RequestException:
        ok = False
    if not ok:
        raise PreflightError(
            f"web_url 的主机名 {parsed.netloc} 从本机访问不通。\n"
            f"  worker clone 用的就是这个主机名，所以它必须能解析并连通。\n"
            f"  若它解析到了路由器的链路本地地址而不是 tailnet IP，在 /etc/hosts\n"
            f"  里加一条指向 `tailscale ip -4 <host>` 的记录。"
        )
    log(f"  ✓ web_url 主机名 {parsed.netloc} 从本机可达")

    if require_runner:
        runners = gl.json_or_raise(gl.get(f"/projects/{p.id}/runners"), "读取 runner")
        online = [r for r in runners if r.get("online") and not r.get("paused")]
        p.online_runners = [r["id"] for r in online]
        if not online:
            raise PreflightError(
                f"项目 {project_path} 没有 online 且未 paused 的 runner。\n"
                f"  CI 永远不会跑，wait_ci_result 会一直等到超时 —— 那个失败看起来\n"
                f"  像 worker 卡住，实际是环境问题。\n"
                f"  已注册的 runner: {[(r.get('id'), r.get('status')) for r in runners]}"
            )
        log(f"  ✓ {len(online)} 个 online runner: {p.online_runners}")
    return p


# ============================================================================
# 受管资源 1：project access token（bot 身份）
# ============================================================================


@dataclass
class Bot:
    token: str = ""
    token_id: int = 0
    username: str = ""
    scopes: list[str] = field(default_factory=list)
    access_level: int = 0
    git_user: str = "root"


def create_project_access_token(gl: GitLab, undo: UndoStack, project_id: int, *,
                                name: str, scopes: list[str],
                                access_level: int, expires_at: str) -> tuple[Bot, GitLab]:
    """建一个 project access token，登记吊销动作，并等它的成员关系生效。"""
    resp = gl.post(f"/projects/{project_id}/access_tokens", json={
        "name": name, "scopes": scopes,
        "access_level": access_level, "expires_at": expires_at,
    })
    if resp.status_code == 404:
        raise PreflightError(
            "创建 project access token 返回 404。\n"
            "  gitlab.com 的 Free tier 不能创建 project/group access token（套餐\n"
            "  限制，不是设计选择）；自托管没有这个限制。"
        )
    data = gl.json_or_raise(resp, "创建 project access token")
    bot = Bot(token=data["token"], token_id=data["id"])
    register_secret(bot.token)

    def _revoke() -> None:
        gl.delete(f"/projects/{project_id}/access_tokens/{bot.token_id}")
        # 回查**结果**：用这个 token 自己去打 /user，必须 401。
        # 只看 DELETE 的返回码不够 —— 那只说明请求发出去了。
        probe = requests.get(f"{gl.api}/user",
                             headers={"PRIVATE-TOKEN": bot.token}, timeout=15)
        if probe.status_code != 401:
            raise RuntimeError(
                f"token 吊销后仍可用（GET /user → {probe.status_code}）—— 这是个敞口，"
                f"请手工到项目 Settings → Access Tokens 吊销 id={bot.token_id}")
        # 注：吊销后 GitLab 仍把它留在 access_tokens 列表里（revoked=True），
        # 对应的 bot 用户由后台任务异步删除，所以 /members/all 里那一行会残留
        # 一阵。那是 GitLab 自己的生命周期，不是没清理干净。

    undo.push(f"吊销 project access token id={bot.token_id}", _revoke)
    log(f"  ✓ 已创建 token id={bot.token_id} name={name} expires_at={expires_at}")

    bot_gl = GitLab(gl.api, bot.token, "bot")

    # ⚠️ 等成员关系传播。刚建出来的 project access token，其 bot 用户的成员关系
    # 对授权检查**不是立即可见**的：同一秒内用它打 GET /projects/{id} 会拿到
    # 404（GitLab 对"你无权看见"返回 404 而不是 403，所以看起来像"项目不存在"）。
    # 实测间歇性出现。产品侧含义：轮换脚本若刚建完 token 就起 worker，
    # gitlab_token_check._probe_project 会以 "cannot reach project" 误报 abort。
    for attempt in range(20):
        if bot_gl.get(f"/projects/{project_id}").status_code == 200:
            if attempt:
                log(f"  ✓ bot 的项目成员关系在约 {attempt * 2}s 后可见（最终一致窗口）")
            break
        time.sleep(2)
    else:
        raise AssertionFailure(
            f"新建的 bot 在 40s 内仍然看不到项目 {project_id}（HTTP 404）")

    who = bot_gl.json_or_raise(bot_gl.get("/user"), "bot GET /user")
    bot.username = str(who.get("username", ""))

    meta = bot_gl.get("/personal_access_tokens/self")
    if meta.status_code == 200:
        bot.scopes = sorted(meta.json().get("scopes") or [])
    else:
        # 老版本 GitLab 没这个端点；产品里的 preflight 对此也是"跳过而非失败"。
        log(f"  ! /personal_access_tokens/self → HTTP {meta.status_code}，跳过 scopes 读取")

    proj = bot_gl.json_or_raise(bot_gl.get(f"/projects/{project_id}"), "bot 读项目")
    perms = (proj.get("permissions") or {}).get("project_access") or {}
    bot.access_level = perms.get("access_level", 0)
    return bot, bot_gl


# ============================================================================
# 受管资源 2：protected 分支
# ============================================================================


def protect_payload(entry: dict) -> dict:
    """把 GET 回来的 protected_branch 条目还原成 POST 需要的参数。

    自托管 CE（/version 的 enterprise=false）只有简单的 push/merge access level，
    没有 Premium 的 per-user/per-group 规则 —— 所以取每个数组的第一个
    access_level 是**精确**还原，不是近似。
    """
    def lvl(key: str, default: int) -> int:
        arr = entry.get(key) or []
        return arr[0].get("access_level", default) if arr else default
    return {
        "name": entry["name"],
        "push_access_level": lvl("push_access_levels", ACCESS_MAINTAINER),
        "merge_access_level": lvl("merge_access_levels", ACCESS_MAINTAINER),
        "allow_force_push": bool(entry.get("allow_force_push", False)),
    }


def branch_pattern_matches(pattern: str, probe: str) -> bool:
    """GitLab 的分支保护支持 `*` 通配。翻成正则拿一个代表性分支名去试。"""
    import re as _re
    regex = "^" + ".*".join(_re.escape(p) for p in pattern.split("*")) + "$"
    return _re.match(regex, probe) is not None


def ensure_protected_branches(gl: GitLab, undo: UndoStack, project_id: int, *,
                              desired: dict, unprotect_matching: str = "") -> bool:
    """把保护分支规整到期望态。返回是否真的改过。

    desired              —— 一条期望的保护规则（protect_payload 的形状）
    unprotect_matching   —— 一个代表性分支名；任何能匹配到它的规则都会被删掉
                            （例：worker 要往 auto/bf/* 推，挡路的规则必须让开）
    """
    before = gl.json_or_raise(
        gl.get(f"/projects/{project_id}/protected_branches"), "读取保护分支")
    log(f"  当前保护规则: {[e['name'] for e in before] or '(无)'}")
    saved = [protect_payload(e) for e in before]
    modified = False

    def _restore() -> None:
        now = gl.get(f"/projects/{project_id}/protected_branches")
        if now.status_code == 200:
            for e in now.json():
                gl.delete(f"/projects/{project_id}/protected_branches/{enc(e['name'])}")
        for payload in saved:
            r = gl.post(f"/projects/{project_id}/protected_branches", params=payload)
            if r.status_code >= 400:
                raise RuntimeError(f"重建保护规则 {payload['name']} 失败: "
                                   f"HTTP {r.status_code} {redact(r.text[:200])}")
        after = gl.get(f"/projects/{project_id}/protected_branches")
        names = sorted(e["name"] for e in after.json()) if after.status_code == 200 else []
        if names != sorted(p["name"] for p in saved):
            raise RuntimeError(f"保护规则恢复后不一致: {names}")

    def _mark() -> None:
        nonlocal modified
        if not modified:
            # 只在真的要改时才登记 —— 无谓地"删了又建"一条本来就正确的规则，
            # 是在真实系统上制造本可以不存在的写操作。
            undo.push("恢复 protected 分支设置", _restore)
            modified = True

    if unprotect_matching:
        for e in before:
            if branch_pattern_matches(e["name"], unprotect_matching):
                log(f"  规则 {e['name']!r} 会挡住 {unprotect_matching} → 删除（测完重建）")
                _mark()
                gl.delete(f"/projects/{project_id}/protected_branches/{enc(e['name'])}")

    cur = next((e for e in before if e["name"] == desired["name"]), None)
    if cur and protect_payload(cur) == desired:
        log(f"  ✓ {desired['name']} 的保护设置已经合理，不动它")
    else:
        _mark()
        if cur:
            log(f"  {desired['name']} 保护设置不符 ({protect_payload(cur)}) → 改成 {desired}")
            gl.delete(f"/projects/{project_id}/protected_branches/{enc(desired['name'])}")
        else:
            log(f"  {desired['name']} 未受保护 → 加上保护")
        gl.json_or_raise(
            gl.post(f"/projects/{project_id}/protected_branches", params=desired),
            f"保护 {desired['name']}")
        log(f"  ✓ {desired['name']} 已保护: {desired}")
    return modified


# ============================================================================
# 受管资源 3：项目 webhook
# ============================================================================


def ensure_project_hook(gl: GitLab, undo: UndoStack, project_id: int, *,
                        url: str, enabled: bool = True) -> int:
    """把项目 webhook 指向 url（或按 enabled=False 停用它）。返回 hook id。

    ⚠️ `enabled=False` 的用法：在 WEBHOOK_AUTH_MODE=oidc 下，GitLab 自己的
    webhook 是匿名的，每次投递必被 401。GitLab 会在连续投递失败后**自动禁用**
    项目 webhook，所以让它一直撞墙不只是噪音，还会留下一个被停用的 hook 影响
    后续所有测试。
    """
    hooks = gl.json_or_raise(gl.get(f"/projects/{project_id}/hooks"), "读取 hooks")

    if not hooks:
        created = gl.json_or_raise(
            gl.post(f"/projects/{project_id}/hooks",
                    json={"url": url, "pipeline_events": True}), "创建 hook")
        hid = created["id"]
        log(f"  项目原本没有 hook，已创建 id={hid}（测完删除）")
        undo.push(f"删除测试创建的 hook id={hid}",
                  lambda: gl.delete(f"/projects/{project_id}/hooks/{hid}"))
        return hid

    hook = hooks[0]
    hid = hook["id"]
    prior = {"url": hook["url"], "pipeline_events": bool(hook.get("pipeline_events"))}
    want = {"url": url, "pipeline_events": bool(enabled)}
    log(f"  已有 hook id={hid} url={hook['url']} pipeline_events={prior['pipeline_events']}")

    if prior == want:
        log("  ✓ hook 已经是期望状态，不动它")
        return hid

    def _restore() -> None:
        gl.put(f"/projects/{project_id}/hooks/{hid}", json=prior)
        now = gl.get(f"/projects/{project_id}/hooks/{hid}")
        if now.status_code == 200:
            cur = now.json()
            if (cur.get("url") != prior["url"]
                    or bool(cur.get("pipeline_events")) != prior["pipeline_events"]):
                raise RuntimeError("hook 没还原成功")

    undo.push(f"还原 hook id={hid} → {prior}", _restore)
    gl.json_or_raise(gl.put(f"/projects/{project_id}/hooks/{hid}", json=want), "改 hook")
    log(f"  hook → {want}（测完还原）")
    return hid


# ============================================================================
# 受管资源 4：项目 CI/CD 变量
# ============================================================================


def ensure_project_variable(gl: GitLab, undo: UndoStack, project_id: int, *,
                            key: str, value: str, masked: bool = False) -> bool:
    """把一个 CI/CD 变量设成 value。返回是否真的改过。

    ⚠️ masked 变量的当前值**读不回来**（GitLab 只返回 masked 的元数据，
    `value` 字段仍会给出明文——但我们不打印它）。恢复策略：
      - 变量原本不存在 → 逆操作是删除；
      - 变量原本存在   → 逆操作是写回原值（原值当作秘密登记，绝不回显）。
    """
    cur = gl.get(f"/projects/{project_id}/variables/{enc(key)}")
    if cur.status_code == 200:
        prior = cur.json()
        prior_value = str(prior.get("value", ""))
        register_secret(prior_value) if prior.get("masked") else None
        if prior_value == value and bool(prior.get("masked")) == masked:
            log(f"  ✓ CI 变量 {key} 已经是期望值，不动它")
            return False

        def _restore() -> None:
            gl.put(f"/projects/{project_id}/variables/{enc(key)}",
                   json={"value": prior_value, "masked": bool(prior.get("masked"))})

        undo.push(f"还原 CI 变量 {key}", _restore)
        gl.json_or_raise(
            gl.put(f"/projects/{project_id}/variables/{enc(key)}",
                   json={"value": value, "masked": masked}), f"改 CI 变量 {key}")
        log(f"  CI 变量 {key} 已改（测完还原）")
        return True

    undo.push(f"删除测试创建的 CI 变量 {key}",
              lambda: gl.delete(f"/projects/{project_id}/variables/{enc(key)}"))
    gl.json_or_raise(
        gl.post(f"/projects/{project_id}/variables",
                json={"key": key, "value": value, "masked": masked}),
        f"建 CI 变量 {key}")
    log(f"  CI 变量 {key} 已创建（测完删除）")
    return True


# ============================================================================
# 受管资源 5：.gitlab-ci.yml 里的 marker 块
# ============================================================================

# 与 infra/oidc-webhook/install_snippet.sh 使用**同一对 marker**，这样两边看到
# 的是同一个块，不会各装各的。
OIDC_BEGIN = "# >>> sdlcma-oidc-notify (managed by infra/oidc-webhook/install_snippet.sh)"
OIDC_END = "# <<< sdlcma-oidc-notify"


def read_file(gl: GitLab, project_id: int, path: str, ref: str) -> str | None:
    """读仓库文件原文；不存在返回 None。"""
    r = gl.get(f"/projects/{project_id}/repository/files/{enc(path)}/raw",
               params={"ref": ref})
    return r.text if r.status_code == 200 else None


def commit_file(gl: GitLab, project_id: int, *, branch: str, path: str,
                content: str, message: str, ci_skip: bool = True,
                exists: bool = True) -> None:
    """提交一个文件。

    ⚠️ `ci_skip` 默认 **True**，理由是双重的：
      1. 一次提交会**触发一条流水线**。F01 的 main 流水线是故意失败的，所以
         每次改 CI 文件都会凭空多出一次触发 —— 要么白跑一个 worker，要么在
         栈没起时把 webhook 投递打成失败。
      2. GitLab 会在**连续投递失败**后自动禁用项目 webhook。让测试脚本反复
         制造失败投递，会留下一个被停用的 hook，影响之后所有测试。
    需要触发时，用 `POST /projects/:id/pipeline` 显式触发 —— 那是可控的。
    """
    msg = f"{message} [ci skip]" if ci_skip else message
    payload = {
        "branch": branch, "commit_message": msg,
        "actions": [{"action": "update" if exists else "create",
                     "file_path": path, "content": content}],
    }
    gl.json_or_raise(gl.post(f"/projects/{project_id}/repository/commits",
                             json=payload), f"提交 {path}")


def strip_marker_block(content: str, begin: str, end: str) -> str:
    """删掉 marker 块。与 install_snippet.sh 的正则同义。"""
    import re as _re
    pattern = _re.compile(_re.escape(begin) + r".*?" + _re.escape(end) + r"[^\n]*\n?",
                          _re.DOTALL)
    return pattern.sub("", content).rstrip("\n")


def ensure_ci_marker_block(gl: GitLab, undo: UndoStack, project_id: int, *,
                           branch: str, begin: str, end: str,
                           body: str | None, ci_path: str = ".gitlab-ci.yml") -> bool:
    """让 `.gitlab-ci.yml` 里的某个 marker 块处于期望状态。返回是否真的改过。

    body=None  → 块必须**不存在**（有就删掉）
    body=<str> → 块必须存在且内容为 body（没有就加、不一样就替换）

    恢复的是**整个文件的原始字节**，而不是"再执行一次逆向的块操作" —— 前者
    对得起"恢复环境"这四个字，后者在文件被别人同时改过时会把别人的改动一起
    带走或留下。
    """
    original = read_file(gl, project_id, ci_path, branch)
    exists = original is not None
    current = original or ""

    stripped = strip_marker_block(current, begin, end)
    if body is None:
        desired = stripped + ("\n" if stripped else "")
    else:
        desired = (stripped + ("\n\n" if stripped else "")
                   + begin + "\n" + body.strip("\n") + "\n" + end + "\n")

    if desired == current:
        log(f"  ✓ {ci_path} 的 {begin.split()[1]} 块已经是期望状态，不动它")
        return False

    had = begin in current
    log(f"  {ci_path}: {begin.split()[1]} 块 {'存在' if had else '不存在'} → "
        f"{'移除' if body is None else ('安装' if not had else '替换')}（测完按原始内容还原）")

    def _restore() -> None:
        now = read_file(gl, project_id, ci_path, branch)
        if now == original:
            return                        # 已经一致，不做无谓提交
        if original is None:
            gl.delete(f"/projects/{project_id}/repository/files/{enc(ci_path)}",
                      json={"branch": branch,
                            "commit_message": "chore: e2e restore — remove file [ci skip]"})
        else:
            commit_file(gl, project_id, branch=branch, path=ci_path,
                        content=original,
                        message="chore: e2e restore .gitlab-ci.yml", exists=True)
        back = read_file(gl, project_id, ci_path, branch)
        if back != original:
            raise RuntimeError(f"{ci_path} 没能还原成原始内容")

    undo.push(f"还原 {ci_path} 到原始内容", _restore)
    commit_file(gl, project_id, branch=branch, path=ci_path, content=desired,
                message=("chore: e2e remove notifier block" if body is None
                         else "chore: e2e install notifier block"),
                exists=exists)
    return True


# ============================================================================
# 观测：MR / 流水线 / push 事件
# ============================================================================


def newest_mr_iid(gl: GitLab, project_id: int) -> int:
    r = gl.get(f"/projects/{project_id}/merge_requests",
               params={"per_page": 1, "order_by": "created_at", "sort": "desc"})
    mrs = r.json() if r.status_code == 200 else []
    return mrs[0]["iid"] if mrs else 0


def find_new_auto_mrs(gl: GitLab, project_id: int, baseline_iid: int) -> list[dict]:
    """返回 baseline 之后新开的所有 auto/bf/* MR。

    刻意返回**列表**而不是第一个：只取第一个会看不见"一次触发产生了两个 worker"
    这种事。实测过 —— snippet 装着而 gateway 跑 mode=none 时，GitLab 的 webhook
    和 snippet 各 POST 一次，一次失败流水线开出两个 MR。
    """
    r = gl.get(f"/projects/{project_id}/merge_requests",
               params={"state": "opened", "order_by": "created_at",
                       "sort": "desc", "per_page": 20})
    if r.status_code != 200:
        return []
    return [m for m in r.json()
            if m["iid"] > baseline_iid and m["source_branch"].startswith("auto/bf/")]


def pusher_of_branch(gl: GitLab, project_id: int, branch: str,
                     attempts: int = 6, delay: int = 5) -> str:
    """谁把这条分支推上来的。

    ⚠️ 断言凭据身份要看 **pusher**，不是 git commit 的 author：
    bf_worker 的 commit_changes() 不设 user.name/user.email，所以 commit 的
    author 是**宿主机的 git 身份**。GitLab 单独记录"谁推的"，那才是凭据身份。
    events 相对 push 有几秒延迟，所以重试几次再下结论。
    """
    for _ in range(attempts):
        r = gl.get(f"/projects/{project_id}/events",
                   params={"action": "pushed", "per_page": 50})
        if r.status_code == 200:
            for ev in r.json():
                if ((ev.get("push_data") or {}).get("ref") or "") == branch:
                    return str((ev.get("author") or {}).get("username", ""))
        time.sleep(delay)
    return ""


def trigger_pipeline(gl: GitLab, project_id: int, ref: str) -> dict:
    return gl.json_or_raise(
        gl.post(f"/projects/{project_id}/pipeline", params={"ref": ref}),
        f"在 {ref} 上触发流水线")
