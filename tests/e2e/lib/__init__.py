"""
tests/e2e/lib —— 重型 e2e 脚本的共用库。

单一 import 面：脚本只写 `from lib import ...`，不必知道东西分在哪个模块。

设计契约（所有 `ensure_*` / `create_*` 都遵守）——**受管资源**：

  1. 先**检测**当前状态；
  2. 只在与期望不符时才**修改**；
  3. **只在真的改了**才往 undo 栈里登记逆操作；
  4. 逆操作执行后**回查**结果，不只是发出动作。

⚠️ undo 栈是 LIFO，**调用顺序决定恢复顺序**。凡是恢复动作本身有副作用的资源
（改 `.gitlab-ci.yml` 会触发流水线），必须在启动本地栈**之前**调用，这样弹栈时
才会先停栈、后还原它。
"""

from .core import (  # noqa: F401
    AssertionFailure, PreflightError, UndoStack,
    EXIT_PASS, EXIT_FAIL, EXIT_TIMEOUT, EXIT_PREFLIGHT, EXIT_RESTORE_INCOMPLETE,
    close_logging, grep_stream, init_logging, log, read_env_value,
    redact, register_secret, section, sh, sha256_short,
)
from .gitlab import (  # noqa: F401
    ACCESS_DEVELOPER, ACCESS_MAINTAINER, ACCESS_REPORTER,
    OIDC_BEGIN, OIDC_END,
    Bot, GitLab, Project,
    branch_pattern_matches, commit_file, create_project_access_token,
    enc, ensure_ci_marker_block, ensure_project_hook, ensure_project_variable,
    ensure_protected_branches, find_new_auto_mrs, newest_mr_iid,
    protect_payload, pusher_of_branch, read_file, resolve_project,
    strip_marker_block, trigger_pipeline,
)
from .llm import LlmRoute, resolve_llm_route  # noqa: F401
from .stack import (  # noqa: F401
    E2E_REDIS_CONTAINER, SYSTEMD_STACK_UNITS, Stack,
    ensure_no_competing_stack, ensure_redis, purge_gateway_stream,
    verify_inbound_reachable,
)
