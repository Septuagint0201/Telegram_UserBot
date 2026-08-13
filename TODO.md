# V1 Development TODO

## 1. 当前状态

架构设计与M0—M4已经完成。M4 Conversation Orchestrator与Main AI的continuation final gate、durable Control Bot backend、command/outbox→app executor权限边界和JUnit-derived evidence已由签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`及GitLab pipeline [#2758187631](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758187631)验证，M4-001—M4-011全部关闭。当前进入M5 Media与Context Contract，尚未完成任何M5实现项。Windows本机无Docker；真实Telegram/provider live、应用容器、真实AUTO、生产部署、backup/restore、production performance和live smoke仍为`NOT RUN`。

- [V1 Implementation Plan](docs/Implementation-Plan.md)定义 milestone 范围、顺序和边界。
- 本文件是日常执行清单：issue 必须按稳定 ID 跟踪，并记录依赖、交付物和验证结果。
- [Detailed Architecture](docs/architecture/01-runtime-topology.md)、[Design](docs/Design.md)和[ADR](docs/adr/README.md)是行为与约束的权威来源；实现不能通过修改测试来绕开这些契约。

证据状态只使用 `PASS`、`FAIL`、`NOT RUN` 或 `BLOCKED`。复选框只有在代码、测试、文档和证据全部满足后才能勾选；“已编写代码”不等于任务完成。

## 2. 执行规则

1. 每个 issue 使用下列固定 ID；拆分出的子 issue 必须保留父 ID，例如 `M3-005a`。
2. 默认只使用 synthetic fixture、fake provider 和 fake Telegram，不提交或复制真实 Session、API key、Bot token、私聊正文或生产配置。
3. 新表或持久字段必须同时提供 Alembic migration、fresh install、upgrade 和中断恢复测试。
4. 新的外部副作用必须先建立 durable intent、幂等键、最终门禁、结果回填和 crash reconciliation。
5. 新的敏感字段必须定义分类、加密/脱敏、审计、保留和删除行为，并加入 sentinel 扫描。
6. 语义发生变化时同步更新架构、ADR、Disclosure、acceptance traceability 和部署 manifest；不能只改代码。
7. 每个 milestone 关闭前运行适用的 unit、property、contract、integration 和恢复测试，未运行项目必须显式保留状态。
8. commit 使用 GPG 签名；公开 push、PR、release 或部署是独立动作，必须获得明确授权并重新执行 dual-use public release review。

## 3. 依赖顺序与并行边界

```text
M0 -> M1 -> M2 ----+
          \         +-> M4 -> M5 -> M6 -> M7 -> M8 -> M9
           -> M3 ---+

M8 的 Compose 骨架可在 M0 后提前建立，但完成门禁必须等待 M7。
```

- `M0` 完成后才能写持久化或 provider/Telegram adapter。
- `M1` 的 schema、transaction 和 job/outbox 基础稳定后，`M2` 与 `M3` 可以有限并行。
- `M4` 同时依赖 `M2` 和 `M3`；`M5` 依赖 `M2`、`M3`、`M4`。
- `M6` 依赖 `M1`、`M2`、`M5`；`M7` 依赖 `M4`、`M6`。
- `M9` 只有在 `M0`—`M8` 全部关闭后才能开始。

| Milestone | 范围 | 状态 | Runtime evidence |
|---|---|---|---|
| M0 | 工程脚手架与测试基础 | COMPLETE | WINDOWS PASS / GITLAB LINUX PASS |
| M1 | PostgreSQL、Redis与 durable state | COMPLETE | WINDOWS STATIC/UNIT PASS; GITLAB SERVICE INTEGRATION PASS |
| M2 | 模型配置、adapter与 key-only 控制面 | COMPLETE | WINDOWS PASS / GITLAB LINUX PASS |
| M3 | Telegram ingest 与 outbound intent | COMPLETE | WINDOWS PASS / GITLAB LINUX SERVICE INTEGRATION PASS |
| M4 | Conversation Orchestrator 与 Main AI | COMPLETE | WINDOWS PASS / GITLAB LINUX SERVICE INTEGRATION PASS |
| M5 | Media 与 Context Contract | IN PROGRESS | NOT RUN |
| M6 | Memory、Summary 与 Embedding Pipeline | WAITING | NOT RUN |
| M7 | Proactive Pipeline | WAITING | NOT RUN |
| M8 | Production Compose 与 Operations | WAITING | NOT RUN |
| M9 | Release candidate 验证 | WAITING | NOT RUN |

## 4. 共享完成定义

每个 work package 必须同时满足：

- [ ] 实现遵守 package/import boundary，不向 domain 引入 framework 或 provider SDK。
- [ ] happy path、边界、失败、重试、并发和 crash-after-side-effect 路径具有相称测试。
- [ ] 日志、异常、metrics、audit 和测试 artifact 不含 secret 或用户正文。
- [ ] 数据库、队列或外部协议变更具有版本化兼容与回滚说明。
- [ ] acceptance manifest 能把需求、测试、commit、镜像/依赖摘要和证据关联起来。
- [ ] 受影响文档已同步，残留的 `NOT RUN`/`BLOCKED` 没有被静态检查替代。

## 5. M0 — 工程脚手架与测试基础

目标：建立可安装、可测试、可静态检查且默认没有真实外部副作用的 Python 工程。

- [x] **M0-001 固定 Python 与构建元数据**（依赖：无）— 选择并固定 CPython minor、构建 backend、runtime/dev lockfile 和升级策略；验证 clean environment 可重复安装且锁文件 hash 一致。
- [x] **M0-002 建立 source package 与依赖边界**（依赖：M0-001）— 创建 `src/telegram_userbot` 分层目录、process entrypoint skeleton 和 import rule；验证 domain 不导入 Telethon、Bot framework、SQLAlchemy、Redis client 或 provider SDK。
- [x] **M0-003 定义共享核心类型**（依赖：M0-002）— 实现强类型 ID、UTC timestamp、monotonic deadline、revision/version、result/error 与 redaction 类型；用 property test 覆盖序列化、时区和非法值。
- [x] **M0-004 定义 application ports**（依赖：M0-003）— 定义 Clock、RandomSource、IdFactory、UnitOfWork、Queue、TelegramGateway、ModelGateway、EmbeddingGateway 接口；提供 deterministic fake contract test。
- [x] **M0-005 建立安全结构化日志**（依赖：M0-003）— 实现字段 allowlist、correlation ID、正文/secret 拒绝和 sentinel scanner；验证异常链、测试输出和 CI artifact 不泄露 sentinel。
- [x] **M0-006 建立 typed configuration**（依赖：M0-003）— 定义环境分层、safe default、placeholder/default-secret 拒绝和 production startup validation skeleton；M0 不接受真实 credential。
- [x] **M0-007 建立测试工具链**（依赖：M0-001）— 配置 pytest、async test、Hypothesis、coverage、unit/contract/integration/recovery/soak markers；验证 marker 隔离与固定随机种子重放。
- [x] **M0-008 建立 synthetic fixture 与状态机 harness**（依赖：M0-004、M0-007）— 提供 account、peer、message、clock、provider、Telegram fixture factory；验证 fixture 不含真实标识且并发调度可复现。
- [x] **M0-009 定义 acceptance manifest**（依赖：M0-003、M0-007）— 建立 requirement→test→artifact→commit schema、validator 和示例；验证缺失证据或未知状态会 fail closed。
- [x] **M0-010 建立无 secret CI**（依赖：M0-005、M0-007、M0-009）— 配置 format、lint、type、unit、property、link、secret、artifact 扫描；CI 默认不访问 Telegram/provider 或公网 credential。
- [x] **M0-011 建立仓库静态门禁**（依赖：M0-010）— 检查 Markdown link、Disclosure、private-key/API-key pattern、签名状态和生成 artifact；验证故意注入 sentinel 时 pipeline 失败。
- [x] **M0-012 关闭 M0**（依赖：M0-001—M0-011）— 汇总签名 commit、依赖锁、测试与 manifest 证据，更新 README 和 M1 启动条件；所有适用门禁为 `PASS` 才能关闭。

### M0 退出门禁

- [x] clean checkout 可按hash lock确定性安装、import、lint、type-check和test（Windows/CPython 3.14.7）。
- [x] fake-only测试证明默认入口不连接Telegram、provider、数据库或Redis，测试只允许asyncio所需loopback。
- [x] acceptance manifest 与安全扫描为 `PASS`；其他 runtime 证据仍明确为 `NOT RUN`。

## 6. M1 — PostgreSQL、Redis与 Durable State

目标：建立 schema、事务、锁、lease、job/outbox 和可恢复的持久化基础。

- [x] **M1-001 固定兼容矩阵**（依赖：M0-012）— 固定 PostgreSQL、pgvector、Redis 与 testcontainer image digest；记录 Ubuntu/Windows 测试边界和升级窗口。
- [x] **M1-002 建立 Alembic baseline**（依赖：M1-001）— 创建 extension、schema、migration naming 与 checksum policy；验证 empty database 升级到 head 和 downgrade 边界。
- [x] **M1-003 建立 account/peer/conversation schema**（依赖：M1-002）— 落地 owner、Telegram peer、mode、overlay、`mode_version`、时区和状态约束；测试唯一键、CAS 与非法转换。
- [x] **M1-004 建立 message/revision/media schema 基础**（依赖：M1-003）— 落地业务键、event、revision、tombstone、album、media metadata 与内容分类；测试重复、乱序、编辑和删除。
- [x] **M1-005 建立 run/control/job/outbox/audit/erasure 基础表**（依赖：M1-003）— 定义 lease、attempt、intent、reservation、audit 和删除账本；约束状态、幂等键和保留边界。
- [x] **M1-006 实现 SQLAlchemy mapping、UoW 与 repository**（依赖：M1-003—M1-005）— 事务内聚合读取/写入并禁止隐式跨事务对象；运行 repository contract test。
- [x] **M1-007 实现 advisory lock、lease 与 CAS**（依赖：M1-006）— 支持 conversation/account/job ownership、到期接管和 fencing token；测试双 worker、时钟推进和失主恢复。
- [x] **M1-008 实现 Redis/arq dispatch 与 durable outbox recovery**（依赖：M1-005—M1-007）— Redis 只作加速/唤醒，PostgreSQL 保持事实来源；测试丢通知、重复投递和 Redis 重启。
- [x] **M1-009 实现删除与脱敏原语**（依赖：M1-004—M1-006）— 提供 tombstone、one-way redaction、erasure marker 和递归派生数据排队；验证正文不可从常规读路径恢复。
- [x] **M1-010 收紧 DB role、index 与 query budget**（依赖：M1-006）— 分离 migration/runtime/backup 权限，定义关键索引与 EXPLAIN baseline；记录数据规模和 `NOT RUN` 的生产证据。
- [x] **M1-011 验证 migration 与中断恢复**（依赖：M1-002—M1-010）— 覆盖 empty→head、previous→head、transaction interruption、worker crash 和兼容窗口；保存 migration manifest。
- [x] **M1-012 关闭 M1**（依赖：M1-001—M1-011）— 汇总 schema、migration、EXPLAIN、并发与恢复证据；未验证的生产负载不得标 `PASS`。

### M1 退出门禁

- [x] PostgreSQL 是 durable truth；Redis 清空后可从数据库恢复未完成工作。
- [x] duplicate delivery、lease expiry 和 crash recovery 不产生重复事实或丢失 intent。
- [x] fresh/upgrade migration test、关键约束和 synthetic EXPLAIN 为 `PASS`。

### M1 完成证据

- 签名提交 `9c2dbf61c8b67e75182f47abbb419ae82773678a` 的 GitLab Linux pipeline [#9](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2747812423)与 `m0-preflight`、`m1-postgres-redis` 作业均为 `PASS`。
- GitLab 在 digest-pinned PostgreSQL 17.10/pgvector 0.8.6 和 Redis 8.2.8 上执行的96个测试全部通过；M1 acceptance manifest 绑定相同 commit/tree，并将 M1-001—M1-012 全部记录为 `PASS`。
- Migration manifest 记录 23 张表、零匿名约束、empty→head、head→base→head 和中断恢复为 `PASS`；首个 baseline 的 previous→head 为 `NOT_APPLICABLE_BASELINE`，production load 保持 `NOT RUN`。

## 7. M2 — 模型配置、Adapter与 Key-only 控制面

目标：完成三个独立 generation profile、协议适配、credential 加密和安全配置入口。

签名提交`def4ff1f846307a7ea428de3c048616601cab7a4`对应的GitLab Linux pipeline [#2748486868](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2748486868)已经提供migration、browser和acceptance evidence，M2-001—M2-011全部关闭。

- [x] **M2-001 建立 model profile/version schema**（依赖：M1-012）— 为 `main_ai`、`memory_agent`、`proactive_agent` 建立独立配置、版本、capability 和 activation 状态；embedding 保持独立配置。
- [x] **M2-002 实现 canonical model configuration domain**（依赖：M2-001）— 统一 endpoint、protocol、model、temperature、maximum output tokens、timeout、enable 和协议扩展项；版本不可变且切换用 CAS。
- [x] **M2-003 实现 generation/embedding adapters**（依赖：M0-004、M2-002）— 支持 `openai_responses`、`openai_chat_completions`、`anthropic_messages` 与 embedding；明确拒绝 legacy text `/completions`。
- [x] **M2-004 建立 provider fake 与 wire contract fixture**（依赖：M2-003）— 覆盖 canonical→wire 字段、流式/非流式响应、usage、timeout、429/5xx、malformed response 和 cancel。
- [x] **M2-005 实现 API key envelope encryption**（依赖：M2-001）— 使用 AES-256-GCM、per-record nonce/AAD、key version 和 rotation seam；数据库、日志、audit 和响应永不返回明文。
- [x] **M2-006 实现 endpoint normalization 与 SSRF/TLS policy**（依赖：M2-002）— 规范 scheme/host/path，限制危险网络目标与 redirect，production 默认验证 TLS；用恶意 URL corpus 测试。
- [x] **M2-007 实现 Control Bot 非敏感配置命令**（依赖：M2-002）— `/models` 展示 profile 状态，通过短生命周期多步 session 配置 endpoint/protocol/model/参数；并发编辑检测版本冲突。
- [x] **M2-008 实现 key-only Web App**（依赖：M2-005、M2-007）— `/model_key <role>` 只打开 set/replace/delete API key 页面；校验 Telegram init data、nonce、过期、CSRF 和一次性提交，禁止消息输入 key。
- [x] **M2-009 实现 capability validation 与 activation gate**（依赖：M2-003—M2-008）— 激活前验证必需字段、协议能力、key 存在性和允许的 image/text 能力；失败保留旧 active version。
- [x] **M2-010 完成 browser/contract/security tests**（依赖：M2-004—M2-009）— 覆盖三个 profile、三种 generation 协议、embedding、key 替换/删除、重放、日志与 SSRF。
- [x] **M2-011 关闭 M2**（依赖：M2-001—M2-010）— 生成配置/协议矩阵与安全证据；真实 provider smoke 仍为 `NOT RUN`，除非在 M9 获得授权。

### M2 退出门禁

- [x] 三个 generation profile 可独立版本化、验证和切换，canonical 字段不泄露协议差异。
- [x] 非 secret 配置只经 Bot 命令/session；API key 只经 key-only Web App。
- [x] fake wire contract、加密、SSRF、重放和 secret sentinel 测试为 `PASS`。

### M2 完成证据

- Pipeline [#2748486868](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2748486868)的`m0-preflight`、`m1-postgres-redis`、`m2-model-control`、`m2-browser`和`m2-acceptance`全部为`PASS`。
- Linux/CPython 3.14.7在PostgreSQL 17.10/pgvector 0.8.6和Redis 8.2.8上运行157个测试，零失败、零跳过；line coverage 92.11%，branch coverage 81.61%。
- Migration manifest记录Alembic head `0002_m2_model_control`、34张表、零匿名约束，以及empty→head、head→base→head、`0001`→head和中断恢复全部`PASS`。
- Chromium 151.0.7922.34 browser manifest记录零外部请求、零browser storage entry、无credential echo；acceptance manifest绑定commit `def4ff1f846307a7ea428de3c048616601cab7a4`、tree `afca1b77506911d82f2424be8bf5b686ce4cf4ce`并将M2-001—M2-011全部标为`PASS`。
- 真实Telegram/provider、Ubuntu production、backup/restore和production load仍为`NOT RUN`。

## 8. M3 — Telegram Ingest与 Outbound Intent

目标：规范化 Telegram 事件，建立消息事实、来源核对和可恢复发送基础。

M3-001—M3-010已经由签名证据提交、disposable PostgreSQL/Redis、migration、12项content-free replay与acceptance manifest关闭。真实Telegram账号测试不属于M3关闭条件，并继续为`NOT RUN`。

- [x] **M3-001 实现 TelegramGateway port adapter boundary**（依赖：M1-012）— 隔离 Telethon entity/event 类型和 Session owner；只有 `app` process 能持有 Session。
- [x] **M3-002 实现 private one-to-one scope 与事件规范化**（依赖：M3-001）— 接收 incoming/outgoing/edit/delete/reaction/service；群组、频道和 unsupported peer 不保存正文且不触发模型。
- [x] **M3-003 实现 message business key 与 revision/tombstone**（依赖：M1-004、M3-002）— 使用 `account_id + chat_id + telegram_message_id`，保留 fingerprint、编辑 revision 和删除 tombstone；测试重复/乱序事件。
- [x] **M3-004 实现 album 与 media metadata**（依赖：M3-003）— 记录 `grouped_id`、稳定排序、Telegram photo/image document 元数据；语音、音频、视频、video note、非图片文档和 sticker 不下载。
- [x] **M3-005 实现 outbound group/intent 与 stable random ID**（依赖：M1-005、M3-003）— 持久化分片顺序、`telegram_random_id`、payload hash、状态和 attempt；重试复用同一 ID。
- [x] **M3-006 实现消息来源 reconciliation**（依赖：M3-005）— 将 observed outgoing 与 human/system/AI intent 核对；未知来源保持显式状态，不能猜测归因。
- [x] **M3-007 实现 read acknowledgement 与 typing port**（依赖：M3-001）— 提供幂等开始/续租/停止接口；policy 决策留给 M4。
- [x] **M3-008 建立 Telegram fake、replay 与 event fixture**（依赖：M3-002—M3-007）— 可重放 album、edit/delete、重复 update、disconnect 和进程崩溃时间点。
- [x] **M3-009 验证发送失败与恢复**（依赖：M3-005—M3-008）— 覆盖 success、FloodWait、transient、permanent、send-unknown、partial group 和 crash-after-send-before-ack。
- [x] **M3-010 关闭 M3**（依赖：M3-001—M3-009）— 汇总 replay、幂等和 reconciliation 证据；真实账号 ingest/send 仍默认 `NOT RUN`。

### M3 退出门禁

- [x] 重放相同 Telegram update 不产生重复 message fact 或 outbound send。
- [x] send-unknown 与 crash-after-send 不会盲目生成新 random ID 重发。
- [x] unsupported peer 和非图片媒体不落正文/二进制，不调用模型。

### M3 完成证据

- 签名提交`41f4160a6d53bdd34e2654f08a90a4b61b6675e8`、tree `768633185cc5f8362e18d7dc149868c7b1796bdb`对应的pipeline [#2751916211](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2751916211)及全部7个作业为`PASS`。
- `m3-telegram-fake`在Linux/CPython 3.14.7、PostgreSQL 17.10/pgvector 0.8.6与Redis 8.2.8上运行202个测试，1个browser测试按策略deselect；line coverage 93.22%，branch coverage 84.65%。
- Migration manifest记录Alembic head `0003_m3_telegram_lifecycle`、40张表、零匿名约束和4条migration路径全部`PASS`；12项replay matrix与M3-001—M3-010 acceptance manifest全部`PASS`。
- Windows本地179个默认测试为`PASS`，24个非默认测试按marker deselect；line coverage 91.41%，branch coverage 81.13%。本机PostgreSQL/Redis因无Docker为`NOT RUN`，真实Telegram ingest/send与Session owner runtime仍为`NOT RUN`。

## 9. M4 — Conversation Orchestrator与 Main AI

目标：完成 AUTO/HUMAN/COPILOT/PAUSED 状态、turn/run lifecycle 和最终发送门禁。

M4-001—M4-011已经关闭。多分片continuation、Control Bot持久后端与证据真实性补强均已完成；Control Bot命令边界按选择A落定：control role只写入command与requested outbox，app role消费、锁定目标状态、执行并写回终态与completed outbox。签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`的GitLab disposable PostgreSQL/Redis、migration、JUnit-derived race和acceptance全部`PASS`；真实Telegram/provider与真实AUTO继续为`NOT RUN`。

- [x] **M4-001 实现 effective mode 与 overlay/version**（依赖：M1-003、M3-010）— 计算 base mode、manual override、maintenance、blocked 和 expiry；每次决策绑定 `mode_version`。
- [x] **M4-002 实现 conversation turn 与 debounce**（依赖：M4-001）— 使用 sliding 3 秒、hard cap 10 秒的可配置收集窗口，持久化 included message/revision 和 turn state。
- [x] **M4-003 实现 Main AI run/attempt lifecycle**（依赖：M2-011、M4-002）— 每次调用绑定 config version、input revision、deadline、usage 和结果 hash；失败分类可恢复。
- [x] **M4-004 实现条件式 3 秒 supersede**（依赖：M4-003）— 新输入到达时，run 开始后三秒内已完成则允许继续；否则 supersede、best-effort cancel、丢弃晚结果并合并重建。
- [x] **M4-005 实现 final send gate 与 splitter**（依赖：M3-005、M4-001—M4-004）— 发送前重读 mode/version、conversation/input revision、tombstone 和 ownership；创建持久 intent 后才执行发送。
- [x] **M4-006 实现 AUTO read/typing lifecycle**（依赖：M3-007、M4-002—M4-005）— generation 开始时标 included messages read，并维持 typing 到 send/cancel/failure；其他 mode 不自动 read/typing。
- [x] **M4-007 实现 human takeover、pause、maintenance 与 BLOCKED**（依赖：M4-001、M4-005）— 人工 outgoing 使陈旧 run 失效，门禁覆盖模型请求；恢复必须显式且版本化。
- [x] **M4-008 实现 COPILOT draft workflow**（依赖：M4-003、M4-005）— `/draft`、edit、approve、ignore、expiry 全部持久化；approve 仍通过最终门禁与 outbound intent。
- [x] **M4-009 实现 `/reply_pending`**（依赖：M4-002、M4-007）— 可检查/重试待处理 conversation，不绕过 mode、lease 或 revision gate。
- [x] **M4-010 完成 race/crash/state-machine tests**（依赖：M4-001—M4-009）— 覆盖 mode flip、new input、edit/delete、human send、timeout、duplicate worker、late result、ordered continuation 与 crash point；race/acceptance状态从JUnit stable test ID派生。
- [x] **M4-011 关闭 M4**（依赖：M4-001—M4-010）— 签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`的GitLab service、migration、race和acceptance全部`PASS`；真实 AUTO 保持 disabled。

### M4 退出门禁

- [x] 任意强失效（mode/control、原source edit/delete/redact、human outgoing、lease）都能在每段发送前阻止剩余结果；本group outgoing与新incoming不会误取消合法continuation。
- [x] debounce、三秒 supersede 与通用 provider timeout 被分别测试，语义不混淆。
- [x] AUTO/COPILOT/HUMAN/PAUSED 的 read、typing、draft 和 send 副作用符合设计。

### M4 完成证据

- 历史签名提交`6185c6ef0a33ca4d8fadc293b49a536a18d7e24a`及pipeline [#2752242512](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2752242512)保留为原关闭基线，不覆盖本轮代码。
- 本轮race manifest与M4-001—M4-010 acceptance改为读取JUnit stable test ID；测试缺失、skip或失败均产生`NOT RUN/FAIL`并使race writer失败。
- 本轮Windows/CPython 3.14.7有228个default测试`PASS`、31个非默认测试deselect；line coverage 89.53%、branch coverage 80.73%，Ruff、strict mypy、import boundary与compileall为`PASS`。原80% branch gate保持不变；本机无容器运行时，M4 PostgreSQL integration为`NOT RUN`，本地race manifest仍按缺失service证据fail closed。
- Control runtime权限边界已通过单元/静态契约；pipeline [#2758059918](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758059918)和[#2758149511](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758149511)先后暴露并帮助修复分片fixture、draft排序字段和SQL `NULL`/JSON `null`语义问题。最终签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`对应的pipeline [#2758187631](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758187631)九个作业全部`PASS`；M4 service job执行258个测试、1个deselect，line coverage 92.52%、branch coverage 84.50%，migration、race manifest、M4 acceptance和artifact scan全部`PASS`。
- 真实Telegram/provider、真实AUTO、Ubuntu production、backup/restore、production load和24小时soak仍为`NOT RUN`。

## 10. M5 — Media与 Context Contract

目标：安全处理图片并构建可审计、可重建、受 token 预算约束的上下文。

- [ ] **M5-001 实现图片 ingestion validation**（依赖：M3-004）— 下载 Telegram photo/image MIME document，默认限制 20 MiB、40 MP、单边 16384 px、30 秒、并发 1；拒绝伪 MIME 和解码炸弹。
- [ ] **M5-002 实现私有 media storage lifecycle**（依赖：M1-004、M5-001）— 在 10 GiB 私有卷保存原件/可选 provider copy、hash、EXIF 清理状态与引用；原件默认保留 30 天、provider copy 24 小时，原子写入且默认不备份、不公开访问。
- [ ] **M5-003 实现 context policy 与动态预算**（依赖：M4-011）— Main AI 默认输入上限 24,000 token，并受模型窗口、输出预算、安全余量和 section cap 约束。
- [ ] **M5-004 实现候选选择与确定性排序**（依赖：M5-003）— 组合 structured memory、vector、recent messages，进行 exact rerank、去重和稳定 tie-break；不同 memory space 不混合检索。
- [ ] **M5-005 实现 instruction/data isolation**（依赖：M5-004）— 来源内容只进入 data boundary，不升级为指令；用 prompt-injection corpus 验证隔离和 provenance。
- [ ] **M5-006 实现 context manifest/hash/rebuild**（依赖：M5-003—M5-005）— 记录选择原因、source revision、token estimate、policy/config version 和 payload hash；同一 snapshot 可确定性重建。
- [ ] **M5-007 实现三协议图片映射**（依赖：M2-003、M5-001、M5-006）— canonical image budget/detail 使用 `auto`，分别生成 Responses、Chat Completions、Messages wire payload；先做 capability gate。
- [ ] **M5-008 实现 `/context`**（依赖：M5-006）— 只展示来源类别、计数、预算、版本和 freshness，不返回聊天正文、memory 正文或 secret。
- [ ] **M5-009 实现 `/context_preview`**（依赖：M5-006—M5-008）— 管理员受控查看 synthetic/redacted preview，覆盖 token overflow、rebuild、delete 和 send_unknown 场景。
- [ ] **M5-010 实现 quota、retention 与磁盘清理接口**（依赖：M5-002）— 为 Operations 暴露容量、TTL、引用保护和删除 job；阈值与最终期限留在 M8 配置。
- [ ] **M5-011 关闭 M5**（依赖：M5-001—M5-010）— 图片安全、三协议 contract、预算 property、注入和 rebuild 测试为 `PASS`。

### M5 退出门禁

- [ ] 非图片媒体只保留元数据；图片超限、伪装或不受模型支持时 fail closed。
- [ ] context 可由 manifest 重建，且删除/编辑后的旧内容不会重新进入选择结果。
- [ ] 24,000 token 是可配置默认上限，不会覆盖更小模型窗口或输出安全余量。

## 11. M6 — Memory、Summary与 Embedding Pipeline

目标：异步提取、验证、版本化和检索记忆，并在编辑、删除或遗忘后完成派生数据对账。

- [ ] **M6-001 实现 memory trigger 与补偿扫描**（依赖：M5-011）— `45 秒 OR 20 revisions OR 6000 tokens OR 10 分钟` 触发，另有 5 分钟补偿扫描；不与 Main AI 发言同步。
- [ ] **M6-002 实现 range merge、lease 与幂等**（依赖：M1-007、M6-001）— 合并重叠 source ranges，绑定 revision watermark 和 fencing token；重复 job 只产生一个事实结果。
- [ ] **M6-003 实现 memory input manifest 与 provider fake**（依赖：M2-003、M5-006、M6-002）— 记录 source membership、config/prompt version、token 和 payload hash；测试 timeout/malformed/duplicate proposal。
- [ ] **M6-004 实现 proposal validator 与事务提交**（依赖：M6-003）— schema、长度、source coverage、时态、禁止指令和实体引用校验通过后一次性提交。
- [ ] **M6-005 实现 source trust/confidence/conflict/candidate**（依赖：M6-004）— 区分用户陈述、观察、推断和模型提议；冲突保留候选，不用新摘要静默覆盖事实。
- [ ] **M6-006 实现 memory lifecycle**（依赖：M6-005）— 支持 create/update/merge/supersede/invalidate、版本链和可审计 reason；读路径只返回当前有效版本。
- [ ] **M6-007 实现 rolling/daily/weekly summary**（依赖：M6-002—M6-006）— 明确 membership、watermark、覆盖区间和重建规则；防止 summary-of-summary 漂移掩盖 source。
- [ ] **M6-008 实现 embedding chunk/space/shadow rebuild**（依赖：M2-001、M6-006）— embedding space 绑定 provider/model/dimension/version；rebuild 写 shadow，验证后原子切换，不混合空间。
- [ ] **M6-009 实现 Control Bot candidate/forget flow**（依赖：M6-005—M6-008）— 支持查看/确认/拒绝候选与精确 forget；命令输出默认不回显敏感正文。
- [ ] **M6-010 实现递归 reconciliation**（依赖：M1-009、M6-006—M6-009）— edit/delete/forget/purge 使 memory、summary、embedding、context manifest 和缓存失效并可恢复重建。
- [ ] **M6-011 实现 freshness 状态**（依赖：M6-001、M6-010）— 暴露 fresh/stale/rebuilding/blocked；过期时扩大 recent-window 而非伪称最新 memory。
- [ ] **M6-012 完成 restore/erasure 测试并关闭 M6**（依赖：M6-001—M6-011）— 从备份恢复后重新应用 erasure ledger，验证被删内容及派生向量不复活。

### M6 退出门禁

- [ ] 全部触发条件为 OR，补偿扫描能发现漏发任务，重复处理保持幂等。
- [ ] memory proposal 在 validator 和 transaction 之前不成为可检索事实。
- [ ] edit/delete/forget/purge 后所有派生层可追踪、可重建且不会复活已删除内容。

## 12. M7 — Proactive Pipeline

目标：在严格规则、预算、时区和最终门禁下生成主动消息或 COPILOT 草稿。

- [ ] **M7-001 实现 durable due job 与补偿扫描**（依赖：M6-012）— 持久化 candidate membership、due time、reason 和版本；15 分钟扫描恢复丢失唤醒。
- [ ] **M7-002 实现 reason allowlist 与 typed evidence**（依赖：M7-001）— 规则层只产生允许原因和结构化证据；Proactive Agent 不负责常规 memory 调度或自行扩大候选范围。
- [ ] **M7-003 实现 IANA timezone/DST 计算**（依赖：M1-003、M7-001）— 保存本地意图和 UTC due，覆盖 gap/fold、时区改变和重复执行。
- [ ] **M7-004 实现 quiet/absolute quiet policy**（依赖：M7-003）— quiet hours 默认 22:00–08:00、absolute quiet 00:00–07:00；有限 bypass 必须有 allowlisted reason 和 audit。
- [ ] **M7-005 实现多层预算与 reservation**（依赖：M1-005、M7-001）— account/contact/bypass budget 先原子预留，send outcome 后结算；并发 worker 不超额。
- [ ] **M7-006 实现 Proactive Agent 严格决策 contract**（依赖：M2-003、M7-002—M7-005）— 模型只能在候选内返回 send/skip、reason 和受限 context request；解析失败默认 skip。
- [ ] **M7-007 实现 Main AI proactive text generation**（依赖：M5-006、M7-006）— 仅在 Proactive Agent 允许后生成文本，使用独立 manifest/config version，不追加未授权候选。
- [ ] **M7-008 实现 activity/draft/takeover suppression**（依赖：M4-007—M4-008、M7-007）— 新消息、活跃 conversation、已有 draft、人工接管、pause/maintenance 会失效候选。
- [ ] **M7-009 实现 AUTO final gate 与 COPILOT draft**（依赖：M4-005、M7-005—M7-008）— AUTO 才能创建 outbound intent；COPILOT 只产生待批准 draft；HUMAN/PAUSED 不发送。
- [ ] **M7-010 实现 send-unknown 保守结算**（依赖：M3-009、M7-005、M7-009）— unknown 暂按已消费预算处理，reconciliation 后修正；禁止盲目重发。
- [ ] **M7-011 实现审计、设置与 Control Bot 命令**（依赖：M7-002—M7-010）— 管理 enable、时区、quiet hours、预算、候选和状态；每次变更版本化并可追责。
- [ ] **M7-012 关闭 M7**（依赖：M7-001—M7-011）— DST、预算并发、quiet bypass、takeover 和 crash state-machine test 为 `PASS`；真实主动发送仍 disabled。

### M7 退出门禁

- [ ] 规则层筛选、Proactive Agent 决策、Main AI 文本和 final gate 四层不可绕过。
- [ ] quiet hours、absolute quiet、预算 reservation 和 mode/version 在发送前重新校验。
- [ ] COPILOT 只产生草稿，send-unknown 不会造成重复消息或预算免费重试。

## 13. M8 — Production Compose与 Operations

目标：在 2 vCPU、4 GiB RAM、40 GiB SSD 基线内完成可部署、可备份、可观测和可恢复的 Ubuntu 运行环境。

- [ ] **M8-001 固定 production image 与 SBOM**（依赖：M0-012，可提前）— 固定 app/PostgreSQL/Redis/Caddy/backup image digest，生成 dependency inventory、license 和 SBOM。
- [ ] **M8-002 建立 production Compose topology**（依赖：M1—M7、M8-001）— 配置 internal/public network、Session/DB/media/backup volume、resource limit、healthcheck 和 restart policy；仅 Caddy 暴露 443。
- [ ] **M8-003 收紧 secret、mount 与 runtime identity**（依赖：M8-002）— 使用 file secret、最小只读 mount、固定 UID/GID、drop capability 和 non-root；Session、keyring 与 backup 权限分离。
- [ ] **M8-004 配置 Caddy key-only Web App 路由**（依赖：M2-008、M8-002）— 只公开 API-key 页面/API，设置 TLS、HSTS、安全 header、body/rate limit；不公开 Bot/control 内部接口。
- [ ] **M8-005 建立 migration one-shot、readiness 与 rollback**（依赖：M1-011、M8-002）— app 不自动并发迁移；schema 不兼容时拒绝 ready，记录 expand/contract 与回滚边界。
- [ ] **M8-006 建立 PostgreSQL pgBackRest/WAL 备份**（依赖：M8-002）— 定义 full/diff/incr、WAL archive、加密、保留、校验和定期 restore drill。
- [ ] **M8-007 建立 Telethon Session restic 备份**（依赖：M8-002）— 仅在 app 停止且 Session owner 释放后快照；测试备份期间拒绝双 owner。
- [ ] **M8-008 建立 erasure ledger restore gate**（依赖：M6-012、M8-006—M8-007）— 恢复后先重放删除账本并完成 reconciliation，未完成前 app 保持 maintenance/not ready。
- [ ] **M8-009 实现 retention、media 与磁盘水位**（依赖：M5-010、M8-002）— 70/80/90/95% 分级告警、限流、清理和 fail-safe；引用中的 media 不误删，低空间不破坏数据库。
- [ ] **M8-010 建立日志、metrics、alerts 与 `/server_status`**（依赖：M8-002）— 监控 process、queue、lease、provider、backup、disk 和 freshness；输出脱敏且 Control Bot 不泄露正文/secret。
- [ ] **M8-011 建立 age 加密导出与传输**（依赖：M8-003、M8-006）— 导出到 root-only staging，使用 age recipient 加密后经 SSH/SFTP 传输；失败清理明文临时文件。
- [ ] **M8-012 验证 shutdown/restart/dependency recovery**（依赖：M8-002—M8-011）— 覆盖 SIGTERM、Telegram disconnect、Redis/PostgreSQL/provider/Caddy 中断和宿主重启。
- [ ] **M8-013 编写 install/upgrade/rollback/patch/restore runbook**（依赖：M8-005—M8-012）— 命令、前置检查、维护模式、证据与停止条件可由新 Ubuntu 主机复现。
- [ ] **M8-014 完成 Compose/security/recovery integration**（依赖：M8-001—M8-013）— 验证 network exposure、mount ownership、backup restore、erasure gate、alerts 和 migration failure。
- [ ] **M8-015 完成 2/4/40 基线 soak**（依赖：M8-014）— 使用单 worker、concurrency 2 和定义的 synthetic workload 运行 24 小时，记录 CPU/RAM/disk/queue/latency/error；未跑满不得标 `PASS`。
- [ ] **M8-016 关闭 M8**（依赖：M8-001—M8-015）— 汇总 digest、SBOM、Compose、restore、security、soak 和 runbook evidence。

### M8 退出门禁

- [ ] fresh Ubuntu 能按 runbook 安装、迁移、启动、健康检查、备份、恢复和回滚。
- [ ] 只有 443 对外，Session owner 唯一，secret 不在镜像、Compose、日志或 artifact 中。
- [ ] 2 vCPU/4 GiB/40 GiB 的 24 小时 soak 和 restore/erasure drill 取得实际 `PASS`。

## 14. M9 — Release Candidate验证

目标：冻结可审计候选，在受控环境完成发布前的全部证据；M9 不自动授权公开 push 或真实账号启用。

- [ ] **M9-001 冻结候选与 manifest**（依赖：M0—M8）— 记录精确 commit、lockfile、migration head、image digest、SBOM、配置 schema 和 acceptance manifest hash。
- [ ] **M9-002 运行全部 required CI suites**（依赖：M9-001）— unit/property/contract/integration/recovery/security 全部针对同一候选；失败后修改必须重新冻结。
- [ ] **M9-003 执行受控 Telegram/Web App smoke**（依赖：M9-002）— 只用专用 staging account 和授权 peer，先 HUMAN/COPILOT 后审慎验证 AUTO；保留脱敏证据。
- [ ] **M9-004 执行已启用 provider synthetic smoke**（依赖：M9-002）— 对每个启用协议/profile 验证 text、image capability、usage、timeout 和错误映射；不使用真实私聊正文。
- [ ] **M9-005 执行 backup/restore/erasure drill**（依赖：M9-001）— 从冻结候选的备份恢复到隔离环境，证明 erasure ledger 阻止已删数据复活。
- [ ] **M9-006 执行 migration/upgrade/rollback drill**（依赖：M9-001）— 覆盖上一个受支持版本、失败中断、maintenance 和回退边界。
- [ ] **M9-007 重跑 2/4/40 24 小时 soak**（依赖：M9-002—M9-006）— 使用 release workload 和实际 digest；任何候选变更都使结果失效。
- [ ] **M9-008 完成 dependency/license/security/provenance 检查**（依赖：M9-001、M9-002）— 校验依赖、镜像、SBOM、漏洞、license、签名和构建来源；例外必须有 owner、期限和风险记录。
- [ ] **M9-009 核对 README、runbook、Disclosure 与实际状态**（依赖：M9-002—M9-008）— 所有能力、限制、默认关闭项和 `NOT RUN`/`BLOCKED` 与实际候选一致。
- [ ] **M9-010 执行最终 dual-use public release review**（依赖：M9-009）— 按精确 artifact、remote、可见性、分支保护和当前官方政策 fail closed 复核。
- [ ] **M9-011 等待明确发布授权**（依赖：M9-010=PASS）— 未收到仓库 owner 的明确公开 push/release 指令时停止，不以“候选完成”推定授权。
- [ ] **M9-012 执行并验证公开动作**（依赖：M9-011）— 推送/发布后 fetch 并核对 remote commit、签名、artifact digest、可见性和 Disclosure；失败立即停止后续推广。

### M9 退出门禁

- [ ] 所有 required evidence 绑定同一签名 commit 和不可变 artifact digest。
- [ ] staging live smoke、provider smoke、restore/erasure、migration/rollback 和 24 小时 soak 均为实际 `PASS`。
- [ ] public release review 为 `PASS` 且发布动作有本次明确授权；否则状态保持 `BLOCKED` 或 `NOT RUN`。

## 15. 持续性工作

这些任务贯穿所有 milestone，不单独替代 milestone 门禁：

- [ ] **X-001 Migration hygiene** — 每次 schema 变化更新 migration、compatibility window、fresh/upgrade/interruption test 和 restore 假设。
- [ ] **X-002 Documentation/ADR hygiene** — 语义决策进入 ADR，流程与运维变化同步 Design、Architecture、Implementation Plan、README 和 Disclosure。
- [ ] **X-003 Threat review** — 每个新入口、credential、side effect、文件/网络解析器和权限边界更新 threat cases 与负向测试。
- [ ] **X-004 Privacy/observability review** — 新日志、metric、audit、backup 和 Control Bot 输出定义敏感等级、retention、redaction 与 erasure 行为。
- [ ] **X-005 Acceptance traceability** — 每项已完成任务能追溯到需求、测试、证据、签名 commit 和 artifact digest。
- [ ] **X-006 Performance budget** — 测量 query、token、queue、memory、disk 和 latency，区分 synthetic 基线与生产证据。
- [ ] **X-007 Dependency maintenance** — 通过单独变更升级依赖/镜像，重跑兼容、安全、migration 和 recovery 门禁。

## 16. 必须在对应阶段确定的实现参数

以下内容必须在各自截止 milestone 内记录为代码、manifest 或 ADR 中的精确值：

| 决策 | 截止点 |
|---|---|
| CPython minor、build backend、lock/format/lint/type 工具与版本 | M0-001/M0-010 |
| PostgreSQL、pgvector、Redis 与 testcontainer digest | M1-001 |
| Alembic previous supported revision | 第二次 schema release 前 |
| Web App browser automation driver | M2-008 前 |
| provider endpoint、model 与 capability allowlist | 对应 adapter 启用前 |
| staging Telegram account 与授权测试 peer | M9-003 前 |
| Caddy、backup、base image digest 与 protected runner | M8-001/M8-014 前 |
| 2/4/40 performance/soak 的 synthetic workload 规模 | M8-015 前 |
| off-host backup target、retention 与 restore owner | M8-006/M9-005 前 |

## 17. 下一步

M4已经关闭，当前进入M5 Media与Context Contract，从M5-001图片ingestion validation开始。真实Telegram、真实provider、真实AUTO、自动发送和生产部署仍禁止启用，直到后续milestone取得各自授权与证据。
