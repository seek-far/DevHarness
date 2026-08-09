# `tests/e2e/` — 重型端到端测试

这里的脚本**不属于全量回归**。它们需要真实的外部环境（另一台机器上的自托管
GitLab、在线的 GitLab Runner、本地 Redis、会真实花钱的 LLM 调用、入站 webhook
可达性），一次运行数分钟。

文件名**刻意不以 `test_` 开头**，所以 `uv run pytest tests/` 不会收集它们。
`tests/TESTING.md` 描述的回归底线不包含这一层。

每个脚本的**头部注释**说明三件事，用来判断将来要不要重跑它：

1. 它是针对哪个 commit / 哪个改动写的
2. 什么样的后续改动应该触发一次重跑
3. 它需要什么环境前提

## 共用库 `lib/`

通用逻辑在 `tests/e2e/lib/`，脚本只写 `from lib import ...`。**恢复逻辑一旦在
多个脚本里分叉，两份就会以不同的方式不完整，而"没还原干净"在真实系统上是无声
的** —— 这是它存在的唯一理由。

| 模块 | 内容 |
|---|---|
| `core.py` | 脱敏（`register_secret`/`redact`/`sha256_short`）、`UndoStack`、错误分层与退出码、`sh`、`read_env_value`、`grep_stream`（流式，绝不 `read_text`） |
| `gitlab.py` | `GitLab` 客户端 + GitLab 侧受管资源：project access token、protected 分支、项目 webhook、CI/CD 变量、`.gitlab-ci.yml` 的 marker 块；以及 MR / pusher / 流水线的观测 |
| `stack.py` | 本机侧受管资源：冲突进程（含 systemd 特例）、Redis、gateway/orchestrator 生命周期、`gateway:stream` 清空、`/metrics` 读数、入站可达性实证 |
| `llm.py` | LLM 路由与模型名决议（要求 8） |

### 受管资源契约

每个 `ensure_*` / `create_*` 都遵守同一份契约：

1. 先**检测**当前状态；
2. 只在与期望不符时才**修改**；
3. **只在真的改了**才往 undo 栈里登记逆操作；
4. 逆操作执行后**回查**结果，不只是发出动作。

第 3 条不是洁癖：在真实系统上无谓地"删了又建"一条本来就正确的配置，是在制造
本可以不存在的写操作和竞态。第 4 条同样有真实教训——发出 DELETE 拿到 204，
不等于那个凭据真的失效了。

### ⚠️ 两个必须记住的顺序规则

**undo 栈是 LIFO，调用顺序决定恢复顺序。** 凡是恢复动作本身有副作用的资源
（改 `.gitlab-ci.yml` 会触发流水线），必须在**启动本地栈之前**调用，这样弹栈时
才会先停栈、后还原它。

**改仓库文件一律 `[ci skip]`**（`commit_file(..., ci_skip=True)` 是默认）。理由
是双重的：一次提交会触发一条流水线（F01 的 main 是故意失败的，等于凭空多一次
触发）；而 GitLab 会在**连续投递失败**后自动禁用项目 webhook，让测试脚本反复
制造失败投递会留下一个被停用的 hook。需要触发时用 `POST /pipeline` 显式触发。

## 共同约定

| 约定 | 说明 |
|---|---|
| 探测 → 设置 → 记录 → **恢复** | 每个"修改"都登记一个逆操作，LIFO 回滚；PASS/FAIL/Ctrl-C 三条路径都会恢复 |
| 不做假设 | 不确定的事实一律探测；探测不了就报错要求人处理，不猜 |
| 永不回显凭据 | 所有 token 进脱敏表，任何日志输出先过 `redact()`；比较 token 只比 sha256 指纹 |
| 不动不是自己起的进程 | 检测到冲突的常驻服务就退出并说明，而不是替人停掉——脚本无法把它原样恢复 |
| 不写 `settings/*.env` | 配置改动一律走**进程环境变量**（天然无需恢复）。那些 env 文件是 per-host 的、gitignored 的，被覆盖过一次 |
| 退出码分层 | `0 PASS / 2 FAIL / 3 TIMEOUT / 4 环境前置失败 / 5 恢复不完整` |

退出码 4 与 2 分开，是因为"环境没准备好"和"被测代码有问题"是两种完全不同的
结论，混在一起会让人去查错的地方。退出码 5 单列，是因为"结论正确但环境没还原
干净"必须有人去看。

产物落在 `evaluation/e2e_runs/<ts>_<name>/`：
`verdict.json`（逐条断言 + 环境快照）、`summary.log`、`gateway.log`、
`orchestrator.log`（worker 的日志继承在这里）、`journal/`（隔离的 RunRecord）。

---

## `e2e_gitlab_bot_identity.py`

针对 **commit `47e0e68`**（最小权限 GitLab bot 身份 / 3A outbound 半边）。

一个用例覆盖三处改动：`gitlab_private_token` 成为声明字段后 env var 压过
env file、启动 preflight `gitlab_token_check.py`、以及最小权限主张本身
（Developer + `api,write_repository` 足够跑完全链路，且 protected `main` 上
存在角色天花板）。

```bash
source .venv-linux/bin/activate
uv run python tests/e2e/e2e_gitlab_bot_identity.py --dry-run   # 只探测环境
uv run python tests/e2e/e2e_gitlab_bot_identity.py             # 完整跑
uv run python tests/e2e/e2e_gitlab_bot_identity.py --clean-artifacts
```

**环境前提**

| 项 | 说明 |
|---|---|
| GitLab@minus | 默认项目 `root/sdlcma-fix-f01-off-by-one`（F01 fixture）。⚠️ 见下面的"已知坑" |
| Runner | 该项目至少一个 online 且未 paused 的 runner，否则 `wait_ci_result` 会一直等到超时，症状看起来像 worker 卡住 |
| Redis | 本机 `redis://localhost:6379/15`；不通时脚本用 docker 起一个临时实例，测完删除 |
| 没有冲突的常驻栈 | 已在跑的 gateway/orchestrator 会抢同一个 Redis db 和同一个 consumer group |
| operator PAT | 默认从 `settings/worker_local_multi_process-pat.env` 的 `GITLAB_PRIVATE_TOKEN` 读，或用 `SDLCMA_OPERATOR_PAT` 覆盖 |
| 入站 webhook | GitLab 必须能 POST 回本机 `:8000`。脚本用 GitLab 的 hook test + gateway 的 `sdlcma_webhooks_received_total` 计数器**实证**这一点 |
| LLM | 会发生真实调用（一次 F01 约几分钱）。模型名按下面的规则自动决议 |

**脚本会改什么、怎么恢复**

| 修改 | 恢复 |
|---|---|
| 创建一个 project access token（Developer, `api`+`write_repository`, 1 天过期） | 删除，并用它自己回查 `GET /user` 必须 401 |
| `main` 加/改保护（push 需 Maintainer）、删除会挡住 `auto/bf/*` 的规则 | 按存档精确重建（CE 只有简单 access level，所以是精确还原） |
| 项目 webhook URL 指向本机 | 还原原 URL；原本没有 hook 则删除 |
| 起 gateway + orchestrator 子进程 | 按 **PID** 逐个终止并回查确认已死（绝不用 `pkill -f`——它会先杀掉自己） |
| 凭据/模型名注入 | 只走进程环境变量，磁盘零改动，天然无需恢复 |
| 临时 Redis 容器（仅当本机没有 Redis） | 删除容器并回查 |

默认**保留** MR 和 `auto/bf/*` 分支——它们是断言 A2/A3/A4 的证据，人要能点进去
看；`--clean-artifacts` 才清理。

---

## `e2e_webhook_oidc.py`

针对 **commit `95f9ba5`**（webhook 的 OIDC 认证与授权）。

这个 commit 是 **additive** —— 一个开关 `WEBHOOK_AUTH_MODE=none|oidc`，默认
`none` 且承诺与加认证之前逐字节一致。所以**两种状态都测**，放在一个脚本里，
因为"additive"是个**对比命题**：只有两个 arm 面对同一个项目、同一套 CI yaml
处理、同一次栈构建时，对比才成立。

```bash
uv run python tests/e2e/e2e_webhook_oidc.py --dry-run
uv run python tests/e2e/e2e_webhook_oidc.py            # 两个 arm，约 4 分钟
uv run python tests/e2e/e2e_webhook_oidc.py --arm oidc # 只跑一个
```

| Arm | 前置（检测→修改→恢复） | 断言 |
|---|---|---|
| **none** | snippet **缺席**、探测块缺席、hook 启用 | 匿名 POST → 200；**带垃圾 `Authorization` 头的 POST 也 → 200**（证明该头被完全忽略，而不是宽松校验）；拒绝计数器 = 0；全链路**恰好一个** MR + `outcome=fixed` |
| **oidc** | snippet **在场** + 测试专用探测块、CI/CD 变量、hook **停用** | 五条负向探测全部 401 且**各自命中不同的 reason**；**真 token + 被篡改的 `project.id` → 403**；全链路恰好一个 MR + `outcome=fixed`；`result=accept` 审计标记存在 |

### 为什么 oidc arm 要停用项目 webhook

在 `oidc` 下 GitLab 自己的 webhook 是匿名的，每次投递必被 401。GitLab 会在**连续
投递失败**后把 hook 置为不可执行（hook 对象上有 `alert_status` / `disabled_until`
两个字段），那会留下一个被停用的 hook 影响之后所有测试。真实的 oidc 部署里触发本
来就是**反转**的：失败的 CI 作业主动 POST 给我们，GitLab 的 webhook 子系统不再参与。

### 403 是怎么测的

`docs/auth.md` 说 `project_id` claim 与载荷的对比"才是选 OIDC 而不是共享密钥的
全部理由"，所以它必须用**真 GitLab 签发**的 token 才算数——用本地伪造的 token 测
只是把单测重跑一遍。做法是往 `.gitlab-ci.yml` 里加一个测试专用作业（独立 marker
块，测完移除）：它用同样的 `aud` 申请 id_token、POST 一个 `project.id` 被换成
`999999` 的载荷、把 HTTP 状态码打进作业日志。**token 全程留在 CI 作业里**，不会被
取出到脚本、日志或进程环境。作业还会把 `000`（连不上）单独喊出来，免得脚本把网络
问题误读成认证问题。

### ⚠️ 两条断言曾经比它们声称的弱

**伪造探测必须带真实的 `kid`。** 第一版给伪造 token 编了个假 kid，结果
`self_signed_rs256` 和 `alg_none` **都**在"查 JWKS 找不到这个 kid"那一步就被挡了
（`reason=unknown_key`），**根本没走到验签和算法固定**。注释写着"证明确实在验签"，
实际只证明了"不认识这个 kid"——断言比它声称的弱，是最坏的一种弱。现在从 GitLab
的 JWKS 取真 kid，五条探测命中五种不同的 reason：

```
missing_token / malformed_token / unknown_key / invalid_signature / invalid_token
```

`invalid_signature` 才是"真的验了签"，`invalid_token` 才是"撞上了写死的
`algorithms=["RS256"]`"。脚本对每条探测断言**具体的 reason**，不只是 401。

**`install_snippet.sh` 的交叉校验必须在只装了 OIDC 块的那一刻做。** 它会把自己
的块**追加到文件末尾**，所以只要文件里还有别的追加块，dry-run 就会报一个**纯粹
关于块先后顺序**的 diff——与"我们装的 snippet 内容对不对"毫无关系，会因为错误的
理由失败。

### 已知坑

**1. GitLab@minus 的可用性耦合在"ls4900(CN) 开没开"上。**
GitLab 是 omnibus 装在 minus 的 WSL 里（不是容器）。它的
`gitlab-runsvdir.service` 是 `After=multi-user.target`，而 minus 上的
`k3s-agent.service` 是 `Type=notify` + `TimeoutStartSec=infinity` —— 只要 k3s
server（ls4900）关机，k3s-agent 的 start job 就永远不完成，`multi-user.target`
永不激活，GitLab 的 runit supervisor 根本不会起。

外部症状是 8929 **接受 TCP、零字节、约 2.1s 后干净关闭**。诊断入口是
`systemctl list-jobs`（一眼看到一个 running job 和一串 waiting）。

⚠️ 恢复时**直接 `systemctl start gitlab-runsvdir` 无效** —— multi-user.target
的 start job 还在队列里，`After=` 排序照样生效。必须：

```bash
sudo systemctl stop k3s-agent      # 解掉那个永不完成的 job
sudo systemctl start gitlab-runsvdir
sudo gitlab-ctl status
```

治本：给 k3s-agent 加 `TimeoutStartSec=120` 的 drop-in。脚本的 Phase 0 在
GitLab 不可达时会直接把这段诊断打出来。

**2. 不要用 `/-/health` 判断 GitLab 存活。** 它对不在 GitLab 监控白名单里的
IP 返回 **404**，看起来像"服务没起"。用 `/api/v4/version`。

**3. LLM 模型名是否带 provider 前缀，取决于走哪条路径 —— 不能假设。**

| 路径 | 客户端 | 规则 |
|---|---|---|
| langgraph worker（本脚本测的） | `services/llm_client.py` → `ChatOpenAI(model=cfg.llm_model)`，原样发送 | **必须裸名**（`deepseek-v4-pro`）；带 `deepseek/` 前缀 → 后端 400 |
| mini / ver99 | litellm，经 `agents/mini_swe_agent.py::_litellm_model_name()` | litellm **要求**前缀；裸名会被**自动补成** `openai/<name>` |

⇒ 正确写法是 `LLM_MODEL` 保持**裸名**，两条路径同一个值都对。带前缀的值是
mini 路径的遗留物。更糟的是 `LLM_VIA_GATEWAY=true` 会让 worker **跳过**
`llm_model_check`，所以写错只在第一次真实 LLM 调用时才炸。

脚本的处理：判路由（用 `/health` 里只有 llm_gateway 才有的形状，而不是靠端口
号——端口占用会伪造 PASS）→ 探 `/models` → 候选**唯一**才自动采用 → 最后用一次
`max_tokens=1` 的真实调用做终局验证。gateway 路由下**不动** `LLM_MODEL`，因为
它会被 gateway 重写，但会进 cache key，改了会让已录制的 replay cache 全部失效。

**4. 角色天花板探测有一次未解释的 201（2026-08-09）。**
一次运行里 bot 在受保护的 `main` 上 `POST /pipeline` 拿到了 **201**（其余各次都是
400；事后立刻用同样的 token 连试 5 次全是 400）。唯一的环境差异是 minus 刚从
**休眠恢复**。查过并**排除**的：保护规则当时是正确的 `push=[40] merge=[40]`；
"delete+重建保护规则会开一个授权窗口"这个假说被实验证伪（重建后 +0/+5/+25/+75s
全部 400）。

为此脚本做了三件事，但**没有**把它糊过去：

- 探测前先用 bot 身份读 `GET /repository/branches/main`，它的 `protected`/`can_push`
  是**按调用者求值**且无副作用的。若 GitLab 当时就说 `can_push=true`，直接报
  **环境前置失败（退出码 4）**，不冤枉被测代码；否则才做负向探测。
- 万一探测真的建出了流水线，立刻删掉它（首次失败时漏了这步，在 `main` 上留了
  一条 api 触发的失败流水线）。
- 恢复动作改成**只在真的改过时才登记**。首跑里 Phase 2 判定"已经合理，不动它"，
  undo 却照样把 `main` 的保护规则删了又建 —— 不要动你没改过的东西。（顺带说明：
  这不是那次 201 的原因，已证伪。）

如果再次出现，`verdict.json` 里的 `ceiling_probe.branch_view_*` 会告诉你 GitLab
当时到底怎么想的。

**5. commit 的 git author 不是 bot。** `gitlab_provider.commit_changes()` 不设
`user.name`/`user.email`，所以 commit 的 author 是**宿主机的 git 身份**。GitLab
单独记录"谁推的"，脚本的 A4 断言的是那个（events API 的 `author.username`），
并把 commit author 作为**观察项**写进 `verdict.json`。
