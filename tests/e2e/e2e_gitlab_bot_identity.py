#!/usr/bin/env python3
# ============================================================================
# tests/e2e/e2e_gitlab_bot_identity.py
#
# 针对 commit 47e0e68 "Add least-privilege GitLab bot identity for the worker
# (3A outbound half)" —— 让 worker 以一个**角色 Developer 的 bot** 身份行动，
# 而不是以"谁拥有 settings/*.env 里那个 PAT"的身份行动。
#
# 一个用例覆盖该 commit 的三处 e2e 可观测改动：
#
#   (1) settings/worker_settings.py 把 `gitlab_private_token` 从"未声明的 extra
#       字段"变成**声明字段**。pydantic-settings v2 对未声明的 extra 字段会
#       **反转** env-var 与 env-file 的优先级，而 settings/*.env 被
#       `COPY settings/` 烘焙进每个镜像 —— 声明之前，镜像里那份值会**静默压过**
#       k8s Secret / ECS secrets 的注入，凭据在原理上就无法排除出镜像。
#       → env 文件放旧 token，进程环境注入 bot token，run 必须以 bot 身份行动
#         （断言 A3/A4/A6）
#
#   (2) bf_worker/services/gitlab_token_check.py 的启动 preflight
#       → worker 日志里的 phase_marker（断言 A1）
#
#   (3) 最小权限主张：Developer + api,write_repository 足以跑完全链路，且
#       protected main 上存在角色天花板（worker 从不 merge，Maintainer 是过度授权）
#       → 全链路开出 MR（A2/A5）+ bot 在 main 上被拒（A7）
#
# 详细的用法/退出码/产物说明见 --help；共用约定见 tests/e2e/README.md。
# 通用逻辑（受管资源、undo 栈、脱敏、LLM 路由决议）在 tests/e2e/lib/ 里。
# ============================================================================

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import sys
import tempfile
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (  # noqa: E402
    ACCESS_DEVELOPER, ACCESS_MAINTAINER, AssertionFailure, GitLab, OIDC_BEGIN,
    OIDC_END, PreflightError, Stack, UndoStack,
    EXIT_FAIL, EXIT_PASS, EXIT_PREFLIGHT, EXIT_RESTORE_INCOMPLETE, EXIT_TIMEOUT,
    close_logging, create_project_access_token, ensure_ci_marker_block,
    ensure_no_competing_stack, ensure_project_hook, ensure_protected_branches,
    ensure_redis, enc, find_new_auto_mrs, grep_stream, init_logging, log,
    newest_mr_iid, purge_gateway_stream, pusher_of_branch, read_env_value,
    redact, register_secret, resolve_llm_route, resolve_project, section, sh,
    sha256_short, trigger_pipeline, verify_inbound_reachable,
)

REPO_DIR = Path(__file__).resolve().parents[2]

DEFAULT_PROJECT_PATH = "root/sdlcma-fix-f01-off-by-one"
DEFAULT_PAT_ENV_FILE = "settings/worker_local_multi_process-pat.env"
WORKER_ENV_FILE = "settings/worker_local_multi_process.env"
REDIS_URL = "redis://localhost:6379/15"
GATEWAY_PORT = 8000
DEFAULT_TIMEOUT_S = 900


def main() -> int:
    ap = argparse.ArgumentParser(
        description="""\
e2e：最小权限 GitLab bot 身份（commit 47e0e68，3A outbound 半边）

  一个用例覆盖该 commit 的三处改动：
    1. gitlab_private_token 成为声明字段后，环境变量压过 env 文件
    2. 启动 preflight gitlab_token_check.py 的 phase_marker
    3. Developer + api,write_repository 足以跑完全链路，
       且 protected main 上的角色天花板真实存在

  ⚠️ 重型测试，不在全量回归里（文件名不以 test_ 开头，pytest 不收集）。
     需要：自托管 GitLab + online runner + Redis + 真实计费的 LLM 调用
     + 入站 webhook 可达。约 80 秒。

  什么时候该重跑：
    * settings/worker_settings.py 字段声明变了，或 pydantic-settings 升级
    * bf_worker/services/gitlab_token_check.py 有任何改动
    * gitlab_provider.py 里 clone/push 的凭据拼装改了
    * 换 GitLab 大版本；新增 spawner
""",
        epilog="""\
退出码:
  0 PASS / 2 FAIL / 3 TIMEOUT / 4 环境前置失败（不是对代码的判决）
  5 恢复不完整（结论可信，但真实系统上有东西没还原，必须有人去看）

产物: evaluation/e2e_runs/<时间戳>_gitlab_bot_identity/
  verdict.json / summary.log / gateway.log / orchestrator.log / journal/

约定与已知坑: tests/e2e/README.md
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--project-path", default=DEFAULT_PROJECT_PATH)
    ap.add_argument("--pat-env-file", default=DEFAULT_PAT_ENV_FILE,
                    help="从哪个 env 文件读 operator PAT；也可用 SDLCMA_OPERATOR_PAT 覆盖")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--clean-artifacts", action="store_true",
                    help="测完关掉 MR 并删除 auto/bf 分支（默认保留，它们是证据）")
    ap.add_argument("--dry-run", action="store_true", help="只探测环境，不做任何修改")
    ap.add_argument("--stop-systemd-stack", action="store_true",
                    help="停掉常驻的 sdlcma-local-* systemd 单元，测完拉回来")
    args = ap.parse_args()

    logdir = (REPO_DIR / "evaluation" / "e2e_runs" /
              f"{time.strftime('%Y%m%d_%H%M%S')}_gitlab_bot_identity")
    (logdir / "journal").mkdir(parents=True, exist_ok=True)
    init_logging(logdir / "summary.log")

    log("e2e: 最小权限 GitLab bot 身份（commit 47e0e68 / 3A outbound 半边）")
    log(f"日志目录: {logdir}")

    undo = UndoStack()
    verdict: dict[str, Any] = {
        "test": "gitlab_bot_identity", "commit_under_test": "47e0e68",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "args": vars(args),
    }
    rc = EXIT_PASS
    mr: dict | None = None
    op: GitLab | None = None

    def _on_signal(signum, _frame):
        log(f"收到信号 {signum} —— 开始恢复环境")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        # ── Phase 0：环境探测（只读）────────────────────────────────
        section("Phase 0.1: 依赖 + 凭据来源")
        for exe in ("git", "curl", "docker", "tailscale"):
            if not shutil.which(exe):
                raise PreflightError(f"缺少依赖: {exe}")
        log(f"  ✓ 依赖齐全；repo={REPO_DIR}；子进程用 {sys.executable}")

        import os
        pat = os.environ.get("SDLCMA_OPERATOR_PAT", "").strip()
        source = "环境变量 SDLCMA_OPERATOR_PAT"
        if not pat:
            pat = read_env_value(REPO_DIR / args.pat_env_file, "GITLAB_PRIVATE_TOKEN")
            source = args.pat_env_file
        if not pat:
            raise PreflightError(
                f"拿不到 operator PAT。设 SDLCMA_OPERATOR_PAT，或让 "
                f"{args.pat_env_file} 里有 GITLAB_PRIVATE_TOKEN。")
        register_secret(pat)
        log(f"  operator PAT 来源: {source}（指纹 {sha256_short(pat)}，永不回显明文）")

        api = read_env_value(REPO_DIR / WORKER_ENV_FILE, "GITLAB_API")
        if not api:
            raise PreflightError(f"{WORKER_ENV_FILE} 里没有 GITLAB_API")
        log(f"  GitLab API（取自 {WORKER_ENV_FILE}）: {api}")
        op = GitLab(api, pat, "operator")

        section("Phase 0.2: 冲突进程检查")
        ensure_no_competing_stack(
            undo, stop_systemd=args.stop_systemd_stack and not args.dry_run)

        section("Phase 0.3: GitLab / 项目 / Runner")
        proj = resolve_project(op, args.project_path)

        section("Phase 0.4: LLM 路由与模型名决议（要求 8）")
        llm = resolve_llm_route(REPO_DIR)

        section("Phase 0.5: Redis")
        ensure_redis(undo, REDIS_URL, start_if_missing=not args.dry_run)

        verdict["environment"] = {
            "gitlab_version": proj.gitlab_version, "gitlab_api": api,
            "project_id": proj.id, "project_path": proj.path,
            "default_branch": proj.default_branch,
            "online_runners": proj.online_runners,
            "llm_route": llm.route, "llm_base_url": llm.base_url,
            "llm_model_configured": llm.model_configured,
            "llm_model_effective": llm.model_effective, "llm_notes": llm.notes,
        }
        if args.dry_run:
            log("")
            log("--dry-run: 环境探测全部通过，未做任何修改")
            verdict["result"] = "DRY_RUN_OK"
            return EXIT_PASS

        # ── Phase 1：bot 凭据 ───────────────────────────────────────
        section("Phase 1: 用 operator PAT 创建 project access token")
        bot, bot_gl = create_project_access_token(
            op, undo, proj.id,
            name=f"sdlcma-e2e-{int(time.time())}",
            # 最小权限：api 用于读 CI trace 和开 MR（read_api 开不了 MR），
            # write_repository 用于 push。刻意不给 Maintainer —— worker 从不 merge。
            scopes=["api", "write_repository"], access_level=ACCESS_DEVELOPER,
            expires_at=(date.today() + timedelta(days=1)).isoformat())

        if not re.match(r"^project_\d+_bot", bot.username):
            raise AssertionFailure(
                f"bot 用户名 {bot.username!r} 不是 project_<id>_bot_* 形状 —— "
                f"gitlab_token_check.classify_kind() 会把它判成别的 kind")
        log(f"  ✓ bot 用户名 = {bot.username}（kind=project）")
        if bot.scopes and bot.scopes != ["api", "write_repository"]:
            raise AssertionFailure(f"scopes 不是 api+write_repository: {bot.scopes}")
        log(f"  ✓ scopes = {bot.scopes}")
        if bot.access_level != ACCESS_DEVELOPER:
            raise AssertionFailure(
                f"bot 角色是 {bot.access_level}，期望 Developer({ACCESS_DEVELOPER})")
        log(f"  ✓ 项目角色 = Developer({bot.access_level})")

        # git 凭据用户名探测。gitlab_provider.py 在 local_multi_process 分支拼的是
        # http://{cfg.gitlab_username}:{token}@host/path，而 env 文件里
        # GITLAB_USERNAME=root，不是 GitLab 文档对 project token 推荐的 oauth2。
        # 能过属于"没写进契约的巧合"，所以探一次而不是假设。
        section("Phase 1.5: git 凭据用户名探测")
        scheme, _, rest = proj.web_url.partition("://")
        genv = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for user in ("root", "oauth2"):
            r = sh(["git", "ls-remote", "--heads",
                    f"{scheme}://{user}:{bot.token}@{rest}.git", "main"],
                   check=False, timeout=60, env=genv)
            if r.returncode == 0:
                bot.git_user = user
                log(f"  ✓ git 凭据用户名 {user!r} 可用")
                if user != "root":
                    log("  ! env 文件里 GITLAB_USERNAME=root 用不了，本次以环境变量覆盖 "
                        "—— 这本身是个应该修进代码的发现")
                break
            log(f"  {user!r} 不行: {redact(r.stderr.strip())[:200]}")
        else:
            raise PreflightError("bot token 用 root: 和 oauth2: 都访问不了远端 git")

        verdict["bot"] = {"username": bot.username, "token_id": bot.token_id,
                          "scopes": bot.scopes, "access_level": bot.access_level,
                          "git_credential_user": bot.git_user}

        # ── Phase 2：GitLab 侧的修改 ────────────────────────────────
        # ⚠️ 全部排在起栈**之前**：undo 栈是 LIFO，这样弹栈时才会先停栈、再还原
        #    GitLab 侧 —— 而 GitLab 侧的还原（改 .gitlab-ci.yml）本身有副作用。
        section("Phase 2.1: protected 分支规整")
        ensure_protected_branches(
            op, undo, proj.id,
            # main 受保护且 push 需要 Maintainer ⇒ Developer 的 bot 推不上去。
            # runbook §3 也解释了随之而来的现象：GitLab 把"能否在某分支跑流水线"
            # 绑在该分支的 push/merge 白名单上，所以 bot 也不能在 main 上触发流水线。
            desired={"name": proj.default_branch,
                     "push_access_level": ACCESS_MAINTAINER,
                     "merge_access_level": ACCESS_MAINTAINER,
                     "allow_force_push": False},
            # worker 要往 auto/bf/* 推，挡路的保护规则必须让开，否则失败会以
            # "apply 阶段报 git error"的形式出现，指向完全错误的方向。
            unprotect_matching="auto/bf/2026_01_01-00_00_00_0_abcd-deadbeef")

        # ⚠️ OIDC snippet 必须**不在** .gitlab-ci.yml 里。
        # 它和 WEBHOOK_AUTH_MODE=oidc 是绑定的（snippet 自己的文件头就这么写）。
        # 本测试跑的是 mode=none，此时 snippet 在场会让同一条失败流水线被 POST
        # 两次 —— GitLab 自己的 webhook 一次、snippet 的 notifier 一次 —— 于是
        # 一次触发开出**两个** MR、烧两份 LLM 钱。实测过两回（!70+!71、!72+!73）。
        # 所以这里是检测→移除→按原始内容还原，而不是看见就退出。
        section("Phase 2.2: .gitlab-ci.yml 的 OIDC snippet 必须缺席")
        ensure_ci_marker_block(op, undo, proj.id, branch=proj.default_branch,
                               begin=OIDC_BEGIN, end=OIDC_END, body=None)

        section("Phase 2.3: webhook 接线")
        local_ip = sh(["tailscale", "ip", "-4"], check=False).stdout.strip().splitlines()
        if not local_ip:
            raise PreflightError("拿不到本机 tailnet IPv4（tailscale ip -4）")
        hook_url = f"http://{local_ip[0]}:{GATEWAY_PORT}/webhook"
        log(f"  本机 tailnet IP = {local_ip[0]} → webhook 目标 {hook_url}")
        hook_id = ensure_project_hook(op, undo, proj.id, url=hook_url, enabled=True)

        # ── Phase 3：起栈（凭据只走进程环境变量，磁盘零改动）────────
        section("Phase 3: 启动 gateway")
        file_token = read_env_value(REPO_DIR / WORKER_ENV_FILE, "GITLAB_PRIVATE_TOKEN")
        register_secret(file_token)
        if not file_token:
            raise PreflightError(
                f"{WORKER_ENV_FILE} 里没有 GITLAB_PRIVATE_TOKEN —— 那么本次就无法"
                f"证明环境变量压过了 env 文件（对照组不存在）")
        if file_token == bot.token:
            raise PreflightError("env 文件里的 token 和新建的 bot token 相同，对照组失效")
        log(f"  ✓ 对照基线成立: env 文件 token 指纹={sha256_short(file_token)} "
            f"≠ bot token 指纹={sha256_short(bot.token)}")

        child_env = os.environ.copy()
        # 这就是被测的那条路径：spawner 把 os.environ.copy() 交给 worker。
        child_env["GITLAB_PRIVATE_TOKEN"] = bot.token
        child_env["GITLAB_USERNAME"] = bot.git_user
        if llm.changed:
            child_env["LLM_MODEL"] = llm.model_effective
        child_env["BF_JOURNAL_DIR"] = str(logdir / "journal")
        child_env.pop("GITLAB_SKIP_TOKEN_CHECK", None)   # 那会把被测对象关掉
        child_env["PYTHONUNBUFFERED"] = "1"

        stack = Stack(logdir=logdir, repo_dir=REPO_DIR, child_env=child_env,
                      gateway_port=GATEWAY_PORT)
        stack.start_gateway(undo)

        section("Phase 3.5: webhook 入站可达性实证")
        verify_inbound_reachable(op, proj.id, hook_id, stack)

        section("Phase 3.6: 清空 gateway:stream 后启动 orchestrator")
        n = purge_gateway_stream(REDIS_URL)
        log(f"  已清空 gateway:stream（原有 {n} 条；consumer group 由 "
            f"orchestrator 的 _ensure_group() 重建）")
        stack.start_orchestrator(undo)

        # ── Phase 4：天花板对照 + 触发 ──────────────────────────────
        section("Phase 4.1: 角色天花板负向探测（bot 凭据）")
        ceiling = probe_role_ceiling(proj, bot, bot_gl, op)
        verdict["ceiling_probe"] = ceiling

        section("Phase 4.2: 用 operator PAT 触发 main 上的失败流水线")
        # 为什么不用 bot：runbook §3 —— 真实场景里 main 的流水线由**人**的 push
        # 或 merge 触发，agent 只对它的失败做出反应。用 bot 去触发是把两个角色
        # 混在一起（何况上面刚证明了 bot 根本触发不了）。
        baseline = newest_mr_iid(op, proj.id)
        log(f"  MR 基线 iid = {baseline}")
        pipe = trigger_pipeline(op, proj.id, proj.default_branch)
        log(f"  ✓ 已触发流水线 {pipe['id']}  {proj.web_url}/-/pipelines/{pipe['id']}")
        verdict["trigger"] = {"baseline_mr_iid": baseline, "pipeline_id": pipe["id"]}

        # ── Phase 5：等待 + 断言 ────────────────────────────────────
        section(f"Phase 5.1: 等待 auto/bf MR（上限 {args.timeout}s）")
        mrs = wait_for_mrs(op, proj, baseline, args.timeout, logdir)
        mr = mrs[0]

        section("Phase 5.2: 断言")
        verdict["assertions"] = run_assertions(
            op, proj, bot, mrs, logdir, ceiling, file_token)
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
        # 产物清理排在恢复栈**之前**：它需要 operator 凭据和还活着的项目状态。
        if mr and args.clean_artifacts and op:
            section("清理测试产物（--clean-artifacts）")
            try:
                op.put(f"/projects/{op_project_id(verdict)}/merge_requests/{mr['iid']}",
                       json={"state_event": "close"})
                op.delete(f"/projects/{op_project_id(verdict)}/repository/branches/"
                          f"{enc(mr['source_branch'])}")
                log(f"  已关闭 MR !{mr['iid']} 并删除分支 {mr['source_branch']}")
            except Exception as exc:  # noqa: BLE001
                log(f"  ! 清理产物失败: {type(exc).__name__}: {exc}")

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
        if mr:
            log(f"MR: {mr.get('web_url')}")
        log(f"产物: {logdir}")
        close_logging()
    return rc


def op_project_id(verdict: dict) -> int:
    return verdict.get("environment", {}).get("project_id", 0)


# ============================================================================
# Phase 4.1 —— 角色天花板
# ============================================================================

def probe_role_ceiling(proj, bot, bot_gl: GitLab, op: GitLab) -> dict:
    """用 bot 凭据做两个**负向**探测，证明角色天花板是真的。

    为什么值得单独测：README 说"worker 从不 merge，所以不需要 Maintainer"。
    但"没有一行代码调用 merge 接口"不是安全保证 —— 保证必须来自 GitLab 侧的
    权限，而不是来自我们对自己代码的信任。
    """
    result: dict[str, Any] = {}

    # ── (0) 前置：**在探测的那一刻**问 GitLab 它自己怎么判 ────────────
    # `GET /repository/branches/{name}` 的 protected / can_push 是**按调用者身份**
    # 求值的，而且没有副作用 —— 它直接就是下面两个负向探测想验证的授权答案。
    # 2026-08-09 有一次运行里 bot 在受保护 main 上 POST /pipeline 拿到了 201
    # （其余各次 400，事后 5/5 复现全 400），当时无法分辨是"天花板真失效"还是
    # "GitLab 那一刻就认为 bot 能推"。有了这一步就能当场分清。
    br = bot_gl.get(f"/projects/{proj.id}/repository/branches/{enc(proj.default_branch)}")
    if br.status_code == 200:
        b = br.json()
        result["branch_view_protected"] = b.get("protected")
        result["branch_view_can_push"] = b.get("can_push")
        log(f"  bot 视角: protected={b.get('protected')} can_push={b.get('can_push')}")
        if not b.get("protected") or b.get("can_push"):
            raise PreflightError(
                f"探测前 GitLab 就认为这个 bot 可以推 {proj.default_branch}"
                f"（protected={b.get('protected')} can_push={b.get('can_push')}），\n"
                f"  下面的负向探测测不到任何东西。这是 GitLab 侧的状态问题，不是被测\n"
                f"  代码的问题 —— 先确认 {proj.default_branch} 的保护规则。")
    else:
        log(f"  ! GET /repository/branches/{proj.default_branch} → HTTP "
            f"{br.status_code}，跳过前置检查")

    # (a) 在受保护的 main 上触发流水线 —— 期望被拒
    r = bot_gl.post(f"/projects/{proj.id}/pipeline", params={"ref": proj.default_branch})
    result["pipeline_on_main_status"] = r.status_code
    result["pipeline_on_main_body"] = redact(r.text[:200])
    if r.status_code < 400:
        # 它真的建出了一条流水线 —— 那是我们自己制造的副作用，先收拾掉再报错。
        try:
            pid = r.json().get("id")
            if pid:
                op.delete(f"/projects/{proj.id}/pipelines/{pid}")
                log(f"  已删除误建的流水线 {pid}")
                result["stray_pipeline_deleted"] = pid
        except Exception as exc:  # noqa: BLE001
            log(f"  ! 误建的流水线没删掉: {type(exc).__name__}: {exc}")
        raise AssertionFailure(
            f"bot 竟然能在受保护的 {proj.default_branch} 上触发流水线 "
            f"(HTTP {r.status_code}) —— 角色天花板没生效。\n"
            f"  而探测前 GitLab 自己报告 protected={result.get('branch_view_protected')} "
            f"can_push={result.get('branch_view_can_push')}，两者矛盾。")
    log(f"  ✓ bot 在 {proj.default_branch} 上触发流水线被拒: HTTP {r.status_code}")

    # (b) 往 main push —— 期望被拒。这是天花板最直接的证据。
    tmp = Path(tempfile.mkdtemp(prefix="e2e-ceiling-"))
    try:
        import os
        scheme, _, rest = proj.web_url.partition("://")
        url = f"{scheme}://{bot.git_user}:{bot.token}@{rest}.git"
        genv = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        # ⚠️ 刻意**不用** --depth 1：GitLab 拒绝从 shallow 克隆推上来的更新
        # ("shallow update not allowed")，那样 push 也会失败，但失败的**原因**是
        # 浅克隆而不是权限 —— 断言会以假 PASS 的形式变绿。
        sh(["git", "clone", "--branch", proj.default_branch, url, str(tmp / "repo")],
           timeout=180, env=genv)
        repo = tmp / "repo"
        sh(["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e",
            "commit", "--allow-empty", "-m", "e2e ceiling probe (must be rejected)"],
           cwd=repo, env=genv)
        push = sh(["git", "push", "origin", f"HEAD:{proj.default_branch}"],
                  cwd=repo, check=False, timeout=120, env=genv)
        result["push_to_main_rc"] = push.returncode
        blob = redact((push.stderr + push.stdout).strip())
        result["push_to_main_stderr"] = blob[:400]
        if push.returncode == 0:
            raise AssertionFailure(
                f"bot 竟然能 push 到受保护的 {proj.default_branch} —— 角色天花板没生效。\n"
                f"  ⚠️ 远端已经被推上去了一个空提交，需要人工回退！")
        # 只看 rc != 0 不够 —— 网络抖动、DNS、磁盘满同样会让 push 失败。
        # 必须确认失败**原因**是权限，否则这条断言就是在测别的东西。
        sigs = ("protected branch", "pre-receive hook declined", "you are not allowed",
                "not allowed to push", "permission denied", "insufficient permission", "403")
        matched = [s for s in sigs if s in blob.lower()]
        result["push_rejection_reason_matched"] = matched
        if not matched:
            raise AssertionFailure(
                f"bot push 到 {proj.default_branch} 确实失败了 (rc={push.returncode})，"
                f"但失败原因看不出是权限问题 —— 不能算天花板生效。\n"
                f"  远端原文: {blob[:300]}")
        log(f"  ✓ bot push 到 {proj.default_branch} 被拒 (rc={push.returncode}，"
            f"原因匹配 {matched})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return result


# ============================================================================
# Phase 5 —— 等待与断言
# ============================================================================

def wait_for_mrs(op: GitLab, proj, baseline: int, timeout: int,
                 logdir: Path) -> list[dict]:
    """等到出现新的 auto/bf MR，然后**再多等一会**看还会不会冒出第二个。

    为什么要多等：一次触发本应只产生一个 worker。如果 `.gitlab-ci.yml` 里还留着
    OIDC snippet 而 gateway 跑 mode=none，GitLab 的 webhook 和 snippet 会各 POST
    一次，开出两个 MR、烧两份 LLM 钱。只取第一个匹配就永远看不见这件事。
    """
    deadline = time.time() + timeout
    found: list[dict] = []
    while time.time() < deadline:
        found = find_new_auto_mrs(op, proj.id, baseline)
        if found:
            break
        marks = grep_stream(logdir / "orchestrator.log", "phase_marker phase=")
        last = (marks[-1].split("phase_marker ", 1)[-1][:90] if marks
                else "(还没有 phase_marker)")
        log(f"  [...] 还没有新 MR；最后一个标记: {last}")
        time.sleep(15)
    if not found:
        raise TimeoutError(f"{timeout}s 内没有等到新的 auto/bf MR")
    log(f"  ✓ 出现 MR !{found[0]['iid']} {found[0]['source_branch']}")
    log("  再等 45s，确认没有第二个 worker 也在跑（重复触发检测）…")
    time.sleep(45)
    found = find_new_auto_mrs(op, proj.id, baseline)
    return sorted(found, key=lambda m: m["iid"])


def run_assertions(op: GitLab, proj, bot, mrs: list[dict], logdir: Path,
                   ceiling: dict, file_token: str) -> dict:
    results: dict[str, Any] = {}

    def record(key: str, ok: bool, detail: str) -> None:
        results[key] = {"pass": bool(ok), "detail": detail}
        log(f"  {'✓' if ok else '✗'} {key}: {detail}")
        if not ok:
            raise AssertionFailure(f"{key}: {detail}")

    mr = mrs[0]

    # A0：一次触发 = 一个 worker
    record("A0_exactly_one_run", len(mrs) == 1,
           f"新开的 auto/bf MR 数量={len(mrs)}: {[m['iid'] for m in mrs]}"
           + ("" if len(mrs) == 1 else
              "  —— 一次触发产生了多个 worker。检查 .gitlab-ci.yml 里是否还留着 "
              "OIDC snippet：它和 WEBHOOK_AUTH_MODE=oidc 绑定，配 mode=none 时 "
              "GitLab 的 webhook 和 snippet 会各 POST 一次。"))

    # A1：preflight 的 phase_marker
    marks = grep_stream(logdir / "orchestrator.log", "phase_marker phase=gitlab_token_check")
    if not marks:
        record("A1_preflight_marker", False,
               "worker 日志里没有 phase=gitlab_token_check 标记 —— preflight 没跑，"
               "或者日志没落到 orchestrator.log")
    parsed = dict(re.findall(r"(\w+)=([^\s]+)", marks[-1].split("phase_marker ", 1)[1]))
    record("A1_preflight_marker",
           parsed.get("kind") == "project" and parsed.get("role") == "Developer"
           and set(parsed.get("scopes", "").split(",")) == {"api", "write_repository"},
           f"kind={parsed.get('kind')} role={parsed.get('role')} "
           f"scopes={parsed.get('scopes')} user={parsed.get('user')}")

    # A2：MR 形状
    record("A2_mr_shape",
           mr["source_branch"].startswith("auto/bf/")
           and mr["target_branch"] == proj.default_branch,
           f"!{mr['iid']} {mr['source_branch']} → {mr['target_branch']}")

    # A3：MR 的作者是 bot
    record("A3_mr_author_is_bot",
           (mr.get("author") or {}).get("username", "") == bot.username,
           f"MR 作者={(mr.get('author') or {}).get('username')!r} 期望={bot.username!r}")

    # A4：**推分支的人**是 bot（不是 commit 的 author —— worker 不设
    #     git user.name/user.email，commit author 是宿主机的 git 身份）
    pusher = pusher_of_branch(op, proj.id, mr["source_branch"])
    record("A4_pusher_is_bot", pusher == bot.username,
           f"push 事件（ref={mr['source_branch']}）的 author={pusher!r} 期望={bot.username!r}")

    commit = op.get(f"/projects/{proj.id}/repository/commits/{mr['sha']}")
    if commit.status_code == 200:
        c = commit.json()
        results["observation_commit_author"] = {
            "author_name": c.get("author_name"), "author_email": c.get("author_email"),
            "note": ("worker 不设 git user.name/user.email，所以 commit 的 author 是"
                     "宿主机的 git 身份，不是 bot —— 与 infra/gitlab-token/README.md §1"
                     "'commits carry the bot identity' 的说法不符"),
        }
        log(f"  ℹ 观察: commit author = {c.get('author_name')} <{c.get('author_email')}>"
            f"（不是 bot —— 见 verdict.json）")

    # A5：RunRecord
    journal = logdir / "journal"
    runs = sorted([d for d in journal.iterdir() if (d / "record.json").exists()],
                  key=lambda d: d.stat().st_mtime) if journal.is_dir() else []
    if not runs:
        record("A5_runrecord", False, f"{journal} 下没有 record.json")
    rec = json.loads((runs[-1] / "record.json").read_text(encoding="utf-8"))
    record("A5_runrecord", rec.get("outcome") == "fixed",
           f"outcome={rec.get('outcome')} iterations={rec.get('iterations')} "
           f"llm_calls={rec.get('llm_call_count')} cost={rec.get('total_cost_usd')} "
           f"dir={runs[-1].name}")

    # A6：env var 压过 env file（A3/A4 证明 run 用的是 bot 身份，
    #     而 Phase 3 已证明 env 文件里是另一个 token）
    record("A6_env_var_beats_env_file", True,
           f"env 文件 token 指纹={sha256_short(file_token)} ≠ "
           f"bot token 指纹={sha256_short(bot.token)}；A3/A4 显示 run 以 bot 身份行动")

    # A7：角色天花板
    record("A7_role_ceiling",
           ceiling["pipeline_on_main_status"] >= 400 and ceiling["push_to_main_rc"] != 0,
           f"main 上触发流水线 HTTP {ceiling['pipeline_on_main_status']}；"
           f"push rc={ceiling['push_to_main_rc']}")

    results["mr_urls"] = [m["web_url"] for m in mrs]
    return results


if __name__ == "__main__":
    sys.exit(main())
