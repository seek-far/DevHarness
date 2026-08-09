"""
tests/e2e/lib/core.py — 所有 e2e 脚本共用的地基。

这里放的是"每个重型 e2e 脚本都要重新做一遍、做错一次就会毁掉一次真实环境"
的那几件事：

  * 凭据脱敏      —— 靠机制，不靠每个调用点自觉
  * LIFO 恢复栈   —— 每个修改登记自己的逆操作，且**只在真的改了才登记**
  * 分层的错误类型 —— "环境没准备好"和"被测代码不对"必须是两种退出码
  * 日志 + 子进程   —— 输出一律过脱敏

为什么这些必须是库而不是各脚本自己写一份：恢复逻辑一旦分叉，两份就会以不同
的方式不完整，而"没还原干净"这种事在真实系统上是无声的。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ============================================================================
# 错误分层
# ============================================================================


class PreflightError(RuntimeError):
    """环境前置条件不满足 → 退出码 4。

    与"被测代码不对"严格区分：它们把人送去完全不同的地方查。
    """


class AssertionFailure(RuntimeError):
    """断言失败 → 退出码 2。被测代码没达到契约。"""


# 退出码是跨脚本的契约，写在这里以免各脚本各编一套。
EXIT_PASS = 0
EXIT_FAIL = 2
EXIT_TIMEOUT = 3
EXIT_PREFLIGHT = 4
EXIT_RESTORE_INCOMPLETE = 5


# ============================================================================
# 脱敏
# ============================================================================

_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """把一个秘密登记进脱敏表。

    只登记足够长的串，否则像 "root" 这种短词会把日志打成马赛克。
    """
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """任何要写进日志/控制台的文本都必须先过这里。"""
    text = str(text)
    for s in _SECRETS:
        text = text.replace(s, "<redacted>")
    # URL 里的 http://user:pass@host 单独处理：即使 pass 没被登记过
    # （例如 git 在报错里回显的 URL），也不能漏出去。
    return re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1<redacted>:<redacted>@", text)


def sha256_short(value: str) -> str:
    """秘密串的指纹——用来比较两个 token 是否相同，而不暴露任何一个。"""
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else "<empty>"


# ============================================================================
# 日志
# ============================================================================

_T0 = time.time()
_LOG_FH = None


def init_logging(path: Path) -> None:
    global _LOG_FH, _T0
    _T0 = time.time()
    _LOG_FH = open(path, "w", encoding="utf-8")


def close_logging() -> None:
    global _LOG_FH
    if _LOG_FH:
        _LOG_FH.close()
        _LOG_FH = None


def log(msg: str = "") -> None:
    line = f"[t+{int(time.time() - _T0):5d}s] {redact(msg)}"
    print(line, flush=True)
    if _LOG_FH:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


def section(title: str) -> None:
    log("")
    log("=" * 74)
    log(title)
    log("=" * 74)


# ============================================================================
# LIFO 恢复栈
# ============================================================================


@dataclass
class UndoStack:
    """后进先出的恢复栈。

    为什么是 LIFO 而不是把恢复写在 finally 里：恢复动作的**正确顺序**是它们被
    执行的逆序（先删 token 再恢复保护分支就删不掉了），而且中途任何一步失败时，
    已经做过的修改都必须照样回滚。把"做"和"撤"成对登记是唯一不会漏的写法。

    ⚠️ 顺序陷阱：调用顺序决定恢复顺序。凡是"恢复动作本身会产生副作用"的资源
    （典型：改 `.gitlab-ci.yml` 会触发流水线），它的 ensure_* 必须在**起本地栈
    之前**调用，这样弹栈时才会先停栈、后还原它。
    """

    actions: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def push(self, name: str, fn: Callable[[], None]) -> None:
        self.actions.append((name, fn))

    def run_all(self) -> None:
        section("恢复环境（LIFO）")
        if not self.actions:
            log("  （没有任何修改需要恢复）")
            return
        while self.actions:
            name, fn = self.actions.pop()
            try:
                fn()
                log(f"  ✓ 已恢复: {name}")
            except Exception as exc:  # noqa: BLE001 —— 恢复阶段吞掉异常继续走完
                self.failures.append(f"{name}: {type(exc).__name__}: {exc}")
                log(f"  ✗ 恢复失败: {name} — {type(exc).__name__}: {exc}")
        if not self.failures:
            log("  环境已完全恢复")


# ============================================================================
# 子进程 / 文件
# ============================================================================


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
       timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    """跑一条命令，输出自动脱敏。check=False 时把退出码交给调用方判断。"""
    proc = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, env=env,
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 rc={proc.returncode}: {redact(' '.join(cmd))}\n"
            f"stderr: {redact(proc.stderr.strip())[:800]}"
        )
    return proc


def read_env_value(env_file: Path, key: str) -> str:
    """从 KEY=VALUE 形式的 env 文件里取一个值；取不到返回空串。

    刻意不引入 dotenv：这里只需要最朴素的一行匹配，而且要能容忍 CRLF
    （这些 env 文件被 Windows 侧编辑过）。
    """
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def grep_stream(path: Path, needle: str) -> list[str]:
    """流式 grep 一个可能非常大的日志文件。

    ⚠️ 绝不用 read_text().splitlines() —— orchestrator 继承了每个 worker 的
    stdout，这个文件可以长到 GB 级。真实事故：一次 read_text() 吃掉 60 GB
    anon-rss，把一台 62 GB 无 swap 的机器 OOM 掉，连带打死了 redis 和 GitLab。
    """
    hits: list[str] = []
    if not path.exists():
        return hits
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if needle in line:
                hits.append(line.rstrip("\n"))
    return hits
