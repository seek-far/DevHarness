"""
tests/e2e/lib/stack.py — 本机侧的"受管资源"：Redis、冲突进程、gateway/orchestrator。

和 gitlab.py 同一份契约：检测 → 只在必要时修改 → 只在真的改了才登记逆操作 →
逆操作后回查。

这里集中了三条用真实事故换来的规则：

  * **绝不用 `pkill -f`** 停进程 —— 它的匹配串会命中脚本自己的命令行，先把
    自己的 shell 杀掉。一律按 PID，并回查确认真的死了。
  * **常驻栈若由 systemd 托管，`kill <pid>` 无效** —— Restart=always 会立刻拉回
    一个新 PID，看起来像"杀不死"。必须 `systemctl stop`。
  * **起 orchestrator 之前先清空 `gateway:stream`** —— 否则 webhook 可达性验证
    用的那条 hook-test 样例载荷、以及上次运行留在 PEL 里的条目（orchestrator
    启动时的 _drain_pending() 会重放它们），都会被当成真 bug 各起一个 worker。
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .core import PreflightError, UndoStack, log, redact, section, sh

# Option-1 harness (infra/local-gitlab/setup.sh) 装的常驻 systemd 单元。
# 它们是"冲突进程"里唯一**恢复有明确定义**的一类：停了之后 systemctl start
# 回去就是原样，不像手工起的进程那样丢失环境变量/cwd/启动方式。
SYSTEMD_STACK_UNITS = ("sdlcma-local-gateway", "sdlcma-local-orchestrator")

E2E_REDIS_CONTAINER = "sdlcma-e2e-redis"


# ============================================================================
# 受管资源 1：没有冲突的常驻栈
# ============================================================================


def ensure_no_competing_stack(undo: UndoStack, *, stop_systemd: bool = False) -> None:
    """检测已经在跑的 gateway/orchestrator。

    为什么必须拦：它们和测试共用同一个 Redis db、同一条 `gateway:stream`、
    同一个 consumer group。两个 orchestrator 抢同一个 group，webhook 会被随机
    一方消费掉，测试以"超时没等到 MR"的形式失败 —— 一个完全误导人的症状。

    默认**不**去停：进程不是脚本起的，脚本无从知道怎么把它原样恢复
    （环境变量、cwd、启动方式都拿不全）。systemd 单元是唯一的例外。
    """
    proc = sh(["ps", "-eo", "pid,args"], check=False)
    hits = [ln.strip() for ln in proc.stdout.splitlines()
            if ("gateway.gateway:app" in ln or "orchestrator.orchestrator" in ln)
            and "ps -eo" not in ln]
    if not hits:
        log("  ✓ 没有冲突的 gateway/orchestrator 进程")
        return

    active = [u for u in SYSTEMD_STACK_UNITS
              if sh(["systemctl", "is-active", u], check=False).stdout.strip() == "active"]

    if stop_systemd and active:
        log(f"  停止 systemd 单元 {', '.join(active)}（测完拉回来）")
        r = sh(["sudo", "-n", "systemctl", "stop", *active], check=False)
        if r.returncode != 0:
            raise PreflightError(
                f"停止 systemd 单元失败（多半是 sudo 需要密码）：\n"
                f"  {redact(r.stderr.strip())}\n"
                f"  请手工执行: sudo systemctl stop {' '.join(active)}")

        def _restart() -> None:
            rr = sh(["sudo", "-n", "systemctl", "start", *active], check=False)
            if rr.returncode != 0:
                raise RuntimeError(f"拉回 systemd 单元失败: {redact(rr.stderr.strip())}")
            for u in active:
                if sh(["systemctl", "is-active", u], check=False).stdout.strip() != "active":
                    raise RuntimeError(f"{u} 没能恢复成 active")

        undo.push(f"重新启动 systemd 单元 {', '.join(active)}", _restart)
        for _ in range(15):
            time.sleep(1)
            again = sh(["ps", "-eo", "args"], check=False).stdout
            if ("gateway.gateway:app" not in again
                    and "orchestrator.orchestrator" not in again):
                log("  ✓ 冲突的常驻栈已停止")
                return
        raise PreflightError("systemctl stop 之后进程仍在 —— 不敢继续")

    detail = "\n".join(f"    {h}" for h in hits)
    hint = (
        f"  它们由 systemd 管着（{', '.join(active)}），所以 `kill <pid>` 没用 ——\n"
        f"  Restart=always 会立刻拉回一个新 PID，看起来像'杀不死'。用：\n"
        f"    sudo systemctl stop {' '.join(active)}\n"
        f"  或者给脚本加 --stop-systemd-stack，由它停、由它拉回。"
        if active else
        "  （脚本不替你停 —— 它无法把不是自己起的进程原样恢复）"
    )
    raise PreflightError(
        "已经有 gateway/orchestrator 在跑，它们会和本测试抢同一个 Redis db\n"
        "  和同一个 consumer group（症状是'超时没等到 MR'，极具误导性）。\n"
        "  请先停掉它们再跑：\n" + detail + "\n" + hint)


# ============================================================================
# 受管资源 2：Redis
# ============================================================================


def ensure_redis(undo: UndoStack, redis_url: str, *, start_if_missing: bool = True) -> None:
    """Redis 探活；不通就用 docker 起一个临时的，并登记删除动作。"""
    import redis as redis_lib

    def ping() -> bool:
        try:
            redis_lib.from_url(redis_url, socket_connect_timeout=3).ping()
            return True
        except Exception:
            return False

    if ping():
        log(f"  ✓ Redis 已在: {redis_url}（不是脚本起的，测完不动它）")
        return
    if not start_if_missing:
        log(f"  ! Redis 不通: {redis_url} —— 正式跑时脚本会用 docker 起临时实例")
        return

    log("  Redis 不通 —— 用 docker 起一个临时实例")
    sh(["docker", "rm", "-f", E2E_REDIS_CONTAINER], check=False)
    sh(["docker", "run", "-d", "--name", E2E_REDIS_CONTAINER,
        "-p", "127.0.0.1:6379:6379", "redis:7-alpine"], timeout=180)

    def _rm() -> None:
        sh(["docker", "rm", "-f", E2E_REDIS_CONTAINER], check=False, timeout=60)
        out = sh(["docker", "ps", "-a", "--filter", f"name={E2E_REDIS_CONTAINER}",
                  "--format", "{{.Names}}"], check=False)
        if E2E_REDIS_CONTAINER in out.stdout:
            raise RuntimeError("临时 redis 容器没删掉")

    undo.push(f"删除临时 redis 容器 {E2E_REDIS_CONTAINER}", _rm)
    for _ in range(30):
        if ping():
            log(f"  ✓ 临时 Redis 就绪: {redis_url}")
            return
        time.sleep(1)
    raise PreflightError("临时 redis 容器起来了但连不上")


def purge_gateway_stream(redis_url: str, stream: str = "gateway:stream") -> int:
    """清空 gateway:stream，返回清掉了多少条。

    ⚠️ 必须在起 orchestrator **之前**做，理由有两个：
      1. webhook 入站可达性验证用的是 GitLab 的 hook test，它会真的 POST 一个
         样例 pipeline 载荷。orchestrator 若已在跑，会把这个样例**当成真 bug**
         去 spawn 一个 worker —— 白花一次 LLM 钱，还可能开出无关的 MR。
      2. db 是共享的开发库，上次运行留在 PEL 里的条目会被 orchestrator 启动时的
         `_drain_pending()` 重放（那是它刻意的崩溃恢复行为），同样凭空多一个 worker。

    删掉 stream 会一并删掉 consumer group，orchestrator 的 `_ensure_group()` 会
    在启动时重建。这里刻意**不**登记 undo：stream 装的是瞬时消息而不是配置，
    "恢复"一条已经被消费掉的旧 webhook 既做不到也没意义。
    """
    import redis as redis_lib
    r = redis_lib.from_url(redis_url, socket_connect_timeout=5)
    n = r.xlen(stream) if r.exists(stream) else 0
    r.delete(stream)
    return n


# ============================================================================
# 受管资源 3：gateway / orchestrator 生命周期
# ============================================================================


@dataclass
class Stack:
    logdir: Path = Path(".")
    repo_dir: Path = Path(".")
    child_env: dict = field(default_factory=dict)
    gateway_port: int = 8000
    procs: list[tuple[str, subprocess.Popen]] = field(default_factory=list)
    _undo_pushed: bool = False

    # ---- 启停 ----------------------------------------------------------

    def _launch(self, name: str, cmd: list[str], undo: UndoStack) -> subprocess.Popen:
        fh = open(self.logdir / f"{name}.log", "w", encoding="utf-8")
        # stderr 合并进 stdout：worker 的日志走 stdout（bf_worker.py 的
        # basicConfig(stream=sys.stdout)），而 spawner 用 create_subprocess_exec
        # 不重定向 ⇒ worker 的输出继承 orchestrator 的 stdout，落到这个文件里。
        p = subprocess.Popen(cmd, cwd=str(self.repo_dir), env=self.child_env,
                             stdout=fh, stderr=subprocess.STDOUT)
        self.procs.append((name, p))
        if not self._undo_pushed:
            undo.push("停止 gateway/orchestrator", self.stop_all)
            self._undo_pushed = True
        log(f"  启动 {name} pid={p.pid} → {self.logdir / (name + '.log')}")
        return p

    def start_gateway(self, undo: UndoStack) -> None:
        self._launch("gateway", [sys.executable, "-m", "uvicorn", "gateway.gateway:app",
                                 "--host", "0.0.0.0", "--port", str(self.gateway_port)],
                     undo)
        for _ in range(60):
            try:
                if requests.get(f"http://127.0.0.1:{self.gateway_port}/healthz",
                                timeout=3).status_code == 200:
                    log("  ✓ gateway /healthz 200")
                    return
            except Exception:
                pass
            for name, p in self.procs:
                if p.poll() is not None:
                    raise PreflightError(
                        f"{name} 启动即退出 rc={p.returncode} —— 看 "
                        f"{self.logdir / (name + '.log')}")
            time.sleep(1)
        raise PreflightError(f"gateway 60s 内没就绪 —— 看 {self.logdir / 'gateway.log'}")

    def start_orchestrator(self, undo: UndoStack) -> None:
        p = self._launch("orchestrator",
                         [sys.executable, "-m", "orchestrator.orchestrator"], undo)
        time.sleep(5)   # 建组要点时间；启动即退出要立刻发现，否则一路等到超时
        if p.poll() is not None:
            raise PreflightError(
                f"orchestrator 启动即退出 rc={p.returncode} —— 看 "
                f"{self.logdir / 'orchestrator.log'}")

    def stop_all(self) -> None:
        """按 PID 逐个停掉并**回查**确认真的死了。

        ⚠️ 绝不用 `pkill -f` —— 它的匹配串会命中脚本自己的命令行，先把自己的
        shell 杀掉（这个教训是真踩过的）。
        """
        for name, p in reversed(self.procs):
            if p.poll() is not None:
                # 可能是本 arm 结束时主动停的，也可能是它自己死的 —— 这里只陈述
                # "已不在运行"，不去替它编一个原因。
                log(f"    {name} 已不在运行 rc={p.returncode}")
                continue
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                log(f"    {name} 未响应 SIGTERM，升级为 SIGKILL")
                p.kill()
                p.wait(timeout=10)
            if p.poll() is None:
                raise RuntimeError(f"{name} (pid={p.pid}) 杀不掉")
            log(f"    {name} pid={p.pid} 已停止")

    # ---- 观测 ----------------------------------------------------------

    def metrics_text(self) -> str:
        try:
            r = requests.get(f"http://127.0.0.1:{self.gateway_port}/metrics", timeout=5)
            return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    def counter_total(self, metric: str) -> float:
        """把某个 counter 的所有 label 组合求和。"""
        total = 0.0
        for line in self.metrics_text().splitlines():
            if line.startswith(metric) and not line.startswith("#"):
                try:
                    total += float(line.rsplit(" ", 1)[1])
                except (ValueError, IndexError):
                    pass
        return total

    def counter_by_label(self, metric: str) -> dict[str, float]:
        """返回 {label 串: 值}，用于断言拒绝原因这类闭集枚举。"""
        out: dict[str, float] = {}
        for line in self.metrics_text().splitlines():
            if line.startswith(metric) and not line.startswith("#") and "{" in line:
                labels = line[line.index("{") + 1:line.index("}")]
                try:
                    out[labels] = float(line.rsplit(" ", 1)[1])
                except (ValueError, IndexError):
                    pass
        return out

    def assert_metrics_usable(self, metric: str) -> None:
        """确认 /metrics 本身可用。

        否则计数器永远读成 0，"没有增长"就分不清是'请求没到'还是'指标端点根本
        没工作' —— 一个环境问题会伪装成另一个环境问题。带 label 的 Counter 在
        第一次观测前没有 series，但 prometheus_client 始终会输出 # TYPE 行。
        """
        if metric not in self.metrics_text():
            raise PreflightError(
                f"gateway 的 /metrics 里找不到 {metric}。\n"
                f"  读不到它就无法区分'请求没到'和'指标端点没工作'。\n"
                f"  检查 gateway 是否装了 prometheus-client。")

    def post_webhook(self, payload: dict, *, headers: dict | None = None) -> requests.Response:
        """直接打本机 gateway 的 /webhook —— 用于认证相关的正/负向探测。"""
        return requests.post(f"http://127.0.0.1:{self.gateway_port}/webhook",
                             json=payload, headers=headers or {}, timeout=30)


def verify_inbound_reachable(gl, project_id: int, hook_id: int, stack: Stack) -> None:
    """用 GitLab 的 hook test 实证"webhook 真的能到达本机"。

    这是唯一不依赖假设的入站验证：GitLab 的 hook test 返回 2xx 只说明 GitLab
    自己发出去了，不说明它到了**这里**。（教训：localhost 上有别的进程在听时，
    冒烟测试会对着错的目标通过。）
    """
    stack.assert_metrics_usable("sdlcma_webhooks_received_total")
    before = stack.counter_total("sdlcma_webhooks_received_total")
    test = gl.post(f"/projects/{project_id}/hooks/{hook_id}/test/pipeline_events")
    log(f"  触发 hook test → HTTP {test.status_code}")
    for _ in range(15):
        time.sleep(1)
        after = stack.counter_total("sdlcma_webhooks_received_total")
        if after > before:
            log(f"  ✓ 入站可达: webhooks_received {before} → {after}")
            return
    raise PreflightError(
        f"GitLab 发出了 hook test，但本机 gateway 的 sdlcma_webhooks_received_total\n"
        f"  没有增长。说明它从 GitLab 那侧不可达。检查：\n"
        f"    * GitLab 侧 Admin → Settings → Network → Outbound requests 是否允许\n"
        f"      访问本地/内网地址（自托管默认**禁止**，这是最常见的原因）\n"
        f"    * 本机 :{stack.gateway_port} 是否被防火墙挡住\n"
        f"    * tailscale 在两侧是否都在线")
