# Architecture Documentation TODO

本文档用于跟踪 Telegram Personal AI Digital Twin 的详细架构设计工作。

总体产品目标和概念架构以 `docs/Design.md` 为准；以下任务负责把概念设计细化为可以实现、测试和部署的工程契约。

## 已确认的基础决策

- [x] 使用 Python 实现，目标运行环境为 Docker Compose 或 Ubuntu Server。
- [x] 初期采用模块化单体，业务进程拆分为 `app`、独立 `control` 和 `worker`。
- [x] 同一个 Telethon session 只允许一个运行进程持有。
- [x] V1 只运行一个 Telegram 真人账号，但数据模型和接口为未来多账号保留边界。
- [x] 主要部署基线为单台 Ubuntu Server 上的 Docker Compose。
- [x] Telethon session 使用 `app` 专用持久化 `.session` 文件，备份单独加密。
- [x] Control Bot 独立运行，并通过 `/server_status` 监控业务服务状态。
- [x] Scheduler 运行在 `worker` 中，并使用单例租约避免重复调度。
- [x] 不提供通用管理 API，只暴露 Telegram Web App 所需的 key-only HTTPS 凭据入口。
- [x] 模型非密钥配置由 Control Bot 命令和短生命周期输入会话管理，Web App 只设置、替换或删除 API key。
- [x] Main AI、Memory Agent 和 Proactive Agent 使用三个独立生成 ModelProfile，默认协议均为 Responses。
- [x] 生成模型可独立选择 Responses、Chat Completions 或 Messages；Embedding 配置保持独立。
- [x] 模型配置保存 canonical 语义字段，由 protocol adapter 映射实际请求和响应字段。
- [x] 模型凭据应用层加密保存，数据库密文与主密钥分离。
- [x] `control`、`app` 和 `worker` 构成受信任计算边界并只读挂载主密钥，基础设施服务不具备解密能力。
- [x] Memory Agent 不阻塞 Main AI 的实时回复流程。
- [x] 记忆提取采用事件驱动、安静窗口合并、硬阈值触发和定期补偿扫描。
- [x] Main AI 使用最近一次已提交的长期记忆，同时直接读取当前会话最新的 canonical message revision。
- [x] Telegram 消息发送采用 outbound delivery group、ordered intent、幂等写入和发送结果对账。
- [x] AI 与真人 outgoing 消息通过系统发送记录和 Telegram message ID 对账区分。
- [x] 手动模式控制优先，使用会话模式版本门禁阻止过期 AI 结果发送。
- [x] 基础模式采用 account default + conversation override；pause、maintenance 和 temporary HUMAN 作为覆盖层。
- [x] 全局控制使用独立 `account_control_version`，不批量改写所有 conversation 的 `mode_version`。
- [x] 恢复 AUTO、pause 或 dependency 后默认不补回复 backlog；V1 只允许显式 `/reply_pending`。
- [x] COPILOT 响应式草稿仅由 `/draft` 触发，批准代发记录为 `source=copilot_approved`。
- [x] temporary HUMAN 默认关闭；启用后使用 10 分钟 inactivity window，恢复时不补 backlog。
- [x] V1 自动对话只覆盖非 Bot 用户的一对一 private chat。
- [x] V1 下载并理解验证后的图片，图片预算为 `auto`；语音、音频、视频和其他非图片媒体不下载二进制。
- [x] 生成期间收到新 incoming 时，完整 API 结果在 run 开始 3 秒内返回则允许发送，否则 supersede 并合并重生成。
- [x] AUTO 在生成开始时自动 read 并维持 typing；HUMAN、COPILOT 和 PAUSED 不自动执行两者。
- [x] Memory Agent 先生成候选变更，再由应用层验证并事务提交。
- [x] Memory episode 默认使用 45 秒安静窗口；20 条 revision、约 6000 tokens 或 10 分钟任一达到即硬触发，补偿扫描默认每 5 分钟运行。
- [x] Memory 自动接受默认要求 `confidence >= 0.85`、可信证据且无未解决冲突；`0.60–0.85` 或 image-only/歧义结果进入 candidate。
- [x] AI/proactive 输出不能单独证明联系人事实或真人风格；已编辑 COPILOT 草稿只能作为低于纯 human 的风格证据。
- [x] Rolling summary 默认每 50 条 eligible revision 或约 12000 tokens 触发，summary 必须保存 ordered source membership。
- [x] Memory candidate 通过 Control Bot 命令查看和裁决，接受与 forget 使用精确 version 门禁和二次确认。
- [x] 时间淡化只影响检索，不自动删除记忆；显式 forget、contact purge 和 account wipe 保持不同语义。
- [x] Embedding 更换模型时使用隔离 shadow space 完整重建并原子切换，不混合不同空间检索。
- [x] Proactive Agent 只处理规则层筛选后的候选，不负责常规记忆调度。
- [x] Main AI 默认输入上限为 24,000 token，并根据模型上下文窗口、输出上限和安全预留动态收紧。
- [x] 模型派生的 identity、personality、relationship、memory 和 summary 都属于数据，只有系统与人工配置 instruction 可以发出指令。
- [x] 长模型输出确定性拆分为一个 delivery group 下的有序幂等 outbound intent，部分失败只恢复未确认段落。
- [x] `/context` 默认只显示 manifest 元数据；完整 `/context_preview` 绑定精确 manifest 并要求短期一次性 token 二次确认，Bot 消息随后尽力删除。

## 文档编写顺序

### 1. Runtime Topology

目标文档：`docs/architecture/01-runtime-topology.md`

- [x] 定义 Docker Compose 中的 `https-gateway`、`app`、`control`、`worker`、`postgres`、`redis` 和一次性 `migrate` 任务。
- [x] 定义 Python 包和模块边界，以及允许的依赖方向。
- [x] 明确 Telethon session、Control Bot 和 scheduler 的进程所有权。
- [x] 定义 Telegram Web App 静态页面和 key-only 凭据 API 的网络入口。
- [x] 定义各服务心跳、readiness 和 `/server_status` 状态聚合方式。
- [x] 定义进程启动、初始化、迁移、停止和重启顺序。
- [x] 定义进程间通信方式、队列边界和共享状态归属。
- [x] 定义单实例组件与允许水平扩展的组件。
- [x] 记录主要故障模式及其隔离范围。
- [x] 定义 incoming 图片持久化 volume 的所有权和非公开访问边界。

完成标准：每个运行组件只有一个明确职责和状态所有者，不存在多个进程同时使用同一 Telethon session 的路径。

### 2. Message Lifecycle

目标文档：`docs/architecture/02-message-lifecycle.md`

- [x] 定义 V1 支持的 private chat、group、channel、topic 和 bot peer 范围。
- [x] 定义 incoming、outgoing、edit、delete、reply、forward、album 和 media 事件模型。
- [x] 定义 text、caption、photo、audio、voice、video、document 和 sticker 的保存与下载边界。
- [x] 定义 reaction、service message、read acknowledgement 和 typing action 的行为。
- [x] 定义 Telegram 消息的业务唯一键和重复 update 处理方式。
- [x] 定义 AI、真人和 proactive outgoing 消息的来源识别与对账流程。
- [x] 定义消息持久化、debounce、turn 聚合、生成、发送和确认状态机。
- [x] 定义 AI 生成期间出现新 incoming message 时的 supersede 或排队策略。
- [x] 定义 AI 已回复后联系人 edit/delete 原消息时是否触发后续动作。
- [x] 定义 outbound delivery group、ordered intent、发送结果记录和启动后对账流程。
- [x] 定义同一 conversation 的串行化和锁租约规则。
- [x] 定义模型请求取消与 `mode_version` 发送门禁。
- [x] 定义超时、重试、死信、补偿和不可恢复失败的处理方式。
- [x] 定义消息顺序以及迟到、乱序和重复 update 的处理规则。

完成标准：任意处理步骤崩溃或重复执行时，不会重复回复，也不会把 AI 消息误判为真人消息。

### 3. Data Model

目标文档：`docs/architecture/03-data-model.md`

- [x] 绘制核心实体关系并定义 account、peer、conversation 和 message 的身份模型。
- [x] 细化 message event/revision、media object、conversation turn、发送意图、模型运行、会话模式和后台任务表。
- [x] 细化 memory、memory proposal、memory evidence、summary 和 embedding 表。
- [x] 细化 event、intention、relationship state、proactive decision 和 audit log 表。
- [x] 细化 model endpoint、model profile、credential、config version 和服务状态数据。
- [x] 定义三个生成 ModelProfile 的独立活动版本约束，以及 Embedding 配置的独立模型。
- [x] 定义 canonical generation 字段和三种 protocol options 的 discriminated schema。
- [x] 定义 prompt、Context budget、retrieval policy 版本，以及长输出 delivery group/ordered intent 增量。
- [x] 定义主键、业务唯一键、外键、检查约束和必要索引。
- [x] 定义时间字段、时区、Telegram 原始数据和 JSONB 扩展字段规则。
- [x] 定义各业务流程的事务边界和并发更新策略。
- [x] 定义软删除、Telegram edit/delete 同步和历史版本保留策略。
- [x] 定义数据保留、导出、删除、备份和恢复边界。
- [x] 制定 Alembic migration 规则。

完成标准：Message Lifecycle、Memory Pipeline 和 Proactive Pipeline 所需状态都能持久化，并有明确的唯一性与事务约束。

### 4. Conversation Orchestrator

目标文档：`docs/architecture/04-conversation-orchestrator.md`

- [x] 定义 AUTO、HUMAN、COPILOT、PAUSED 和维护状态的语义。
- [x] 定义 Control Bot 命令、真人发言和系统故障触发的状态转换。
- [x] 定义全局模式、联系人模式和临时接管状态的优先级。
- [x] 定义 `mode_version` 的递增条件和发送前验证规则。
- [x] 定义 debounce 后的 conversation turn 输入和输出契约。
- [x] 定义 COPILOT 草稿的投递、接受、修改、忽略和过期流程。
- [x] 定义恢复 AUTO 的显式策略；自动恢复作为可配置能力，不作为默认假设。
- [x] 定义每次模式变更和 AI 决策所需的审计信息。

完成标准：任意模式下收到 incoming 或 outgoing 消息时，系统行为唯一且可预测。

### 5. Memory Pipeline

目标文档：`docs/architecture/05-memory-pipeline.md`

- [x] 定义 episode extraction、rolling summary 和 consolidation 三类任务。
- [x] 定义触发条件的或关系、安静窗口、硬阈值和补偿扫描。
- [x] 定义同一会话 pending memory job 的刷新、范围合并和幂等键。
- [x] 定义 Memory Agent 输入、结构化输出和 prompt 版本管理。
- [x] 定义 memory proposal 的校验、接受、拒绝和人工审计状态。
- [x] 定义 create、update、supersede、invalidate 和 merge 操作。
- [x] 定义 evidence、confidence、importance 和时间有效区间。
- [x] 定义 summary watermark、覆盖范围和重新生成规则。
- [x] 定义 embedding 的生成、版本、更换模型和重建流程。
- [x] 定义低置信度候选、冲突判断、遗忘和淡化策略。
- [x] 定义队列积压时 Context Builder 的记忆新鲜度降级策略。

完成标准：任意 canonical message/event 范围可以安全重放，重复处理不会重复创建记忆，所有正式记忆都能追溯到证据 revision。

### 6. Context Contract

目标文档：`docs/architecture/06-context-contract.md`

- [x] 定义 identity、personality、relationship、memory、summary、recent messages 和 current message 的装配顺序。
- [x] 定义各上下文层的 token 预算、截断规则和总预算。
- [x] 定义结构化记忆、向量记忆和最近历史的选择与排序算法。
- [x] 定义记忆处理滞后时扩大 canonical message 窗口的降级策略。
- [x] 定义上下文快照、来源 ID、token 数和检索理由的记录方式。
- [x] 定义用户内容、记忆内容和系统指令之间的信任边界。
- [x] 定义 prompt injection、恶意转发内容和不可信历史的隔离方式。
- [x] 定义模型 provider adapter、能力声明、超时和结构化输出契约。
- [x] 定义 Responses、Chat Completions 和 Messages 的请求映射与响应归一化。
- [x] 定义 Chat Completions 的 `max_completion_tokens` / `max_tokens` 兼容策略。
- [x] 定义 text、caption、reply、forward、album 和 image content part 的选择、预算与来源记录。
- [x] 定义三种生成协议对图片输入和 canonical `detail=auto` 的能力校验、wire 映射及等价默认值。
- [x] 定义模型配置草稿、验证、激活、版本切换和运行中请求的快照语义。
- [x] 定义 `temperature`、`max_output_tokens` 和 Provider 特有参数的能力映射。
- [x] 定义 prompt、模型和检索算法的版本管理方式。
- [x] 定义 metadata-only `/context`、二次确认 `/context_preview`、一次性 token、正文不落库和 Bot 消息尽力删除边界。

完成标准：同一上下文输入和版本可以重建，且所有进入模型的非系统内容都保留明确来源和信任级别。

### 7. Proactive Pipeline

目标文档：`docs/architecture/07-proactive-pipeline.md`

- [x] 定义 SQL/规则层的候选联系人和候选事件筛选方式；stable due job 与 15 分钟补偿扫描为 OR 触发。
- [x] 定义 Proactive Agent 的 strict 输入、`send_now/defer_once/none` 输出和拒绝条件。
- [x] 定义 `promise_due/event_upcoming/event_followup/relationship_reconnect/explicit_followup` 的 event、intention、relationship time 和 conversation time 读取规则。
- [x] 定义联系人时区、DST、默认 `22:00-08:00` quiet hours、`00:00-07:00` 绝对禁发和受限高重要性例外。
- [x] 定义关系级每日预算、最小间隔、account 全局日限额和原子 reservation。
- [x] 定义 occurrence、candidate、decision、defer、draft、delivery group 和发送行为的幂等键。
- [x] 定义 Main AI 生成主动消息时使用的 text-only 最小上下文和 manifest。
- [x] 定义发送前在 conversation lock 内执行的最终安全闸门和原子 delivery group/intents。
- [x] 定义 HUMAN、COPILOT、PAUSED、活跃会话、冲突草稿和真人接管时的抑制规则；activity 窗口默认 30 分钟。
- [x] 定义主动消息决策、原因、证据、模型、预算、静默例外和最终结果的审计记录。

完成标准：worker 重试、scheduler 重复触发或并发执行时不会产生重复主动消息，任何主动发送都有完整决策依据。

### 8. Operations

目标文档：`docs/architecture/08-operations.md`

- [x] 定义 Caddy、Compose services、edge/backend/backup networks、volumes、healthcheck 和 digest pinning。
- [x] 定义 Ubuntu 24.04 amd64 + Docker Compose 生产基线，以及原生运行仅用于开发/排障。
- [x] 定义配置分层、deployment manifest、非密钥环境变量、Compose secrets 和启动校验。
- [x] 定义 Telethon Session、Bot token、AES-256-GCM 模型凭据、数据库/Redis/backup secret 的最小挂载矩阵。
- [x] 定义 key-only Telegram Web App HTTPS、5 分钟 `initData`/launch token、管理员/role 绑定、rate limit 和无回显。
- [x] 定义 API key set/replace/delete、版本化 master key keyring、在线轮换和离线恢复流程。
- [x] 定义 public HTTPS/private allowlist endpoint、DNS/redirect/TLS/proxy 和 SSRF fail-closed 防护。
- [x] 定义 Alembic one-shot migration、pgvector compatibility、expand/migrate/contract 和 rollback 边界。
- [x] 定义 pgBackRest full/differential/WAL、Session/restic、独立 erasure ledger、RPO/RTO 和全机恢复流程。
- [x] 定义 20 MiB、40 MP、16384 px、30 秒、10 GiB、original 30 天/provider copy 24 小时和不备份 media。
- [x] 定义 JSON stdout、Docker rotation、internal Prometheus metrics、独立告警和 2/4/40 下不常驻完整监控栈。
- [x] 定义 data export 只写 age 加密 root-only staging、仅经既有 SSH/SFTP 取回、24 小时清理，不扩展 Web App。
- [x] 定义 liveness/readiness、10/3/3 healthcheck、10/30 秒 heartbeat 和 `/server_status` 运维字段。
- [x] 定义 app/worker 90 秒、control/gateway 30 秒、Redis 60 秒、PostgreSQL 120 秒的 SIGTERM 流程。
- [x] 定义 2 vCPU/4 GiB/40 GiB limits、worker concurrency 2、图片并发 1、disk 70/80/90/95% 门禁和 provider retry。
- [x] 定义人工 digest-pinned upgrade、pre-upgrade backup、migration、兼容 rollback、Ubuntu security patch 和 DR 演练。

完成标准：全新 Ubuntu 主机可按文档完成部署，服务异常重启不会破坏 session、重复发送消息或丢失已接收的 canonical message。

### 9. Test Strategy

目标文档：`docs/architecture/09-test-strategy.md`

- [ ] 定义单元测试、契约测试、集成测试和端到端测试边界。
- [ ] 建立 Telegram gateway fake 和可重放 update fixtures。
- [ ] 建立 LLM、embedding 和时间服务 fake。
- [ ] 测试 key-only Telegram Web App 认证、过期重放、非管理员访问、越权字段拒绝和密钥不回显。
- [ ] 测试模型配置验证失败、原子激活、版本切换和进行中请求行为。
- [ ] 为 Responses、Chat Completions 和 Messages adapter 建立固定 wire contract fixtures。
- [ ] 测试三个生成 ModelProfile 独立修改和激活时互不影响。
- [ ] 测试 incoming/outgoing 来源识别、重复 update、edit/delete 和 album。
- [ ] 测试 Telegram random ID 映射、system pending source 和启动后 outgoing reconciliation。
- [ ] 测试图片格式校验、伪造 MIME、解码炸弹、模型能力门禁和 `detail=auto` 映射。
- [ ] 测试 Data Model 的 composite foreign key、partial unique、CHECK、CAS 和 one-way redaction 约束。
- [ ] 测试 memory forget、contact purge、account wipe、backup restore 后 erasure ledger 重放和数据不复现。
- [ ] 测试 Alembic 从空库、上一支持 revision、中断 backfill 到 head 的迁移路径。
- [ ] 测试 account default/override、pause overlay、maintenance、BLOCKED 和 control/mode version 优先级。
- [ ] 测试 HUMAN/COPILOT/PAUSED/temporary HUMAN/BLOCKED 恢复后不自动回复 backlog，及 `/reply_pending` 幂等范围。
- [ ] 测试 COPILOT manual `/draft`、30 分钟 expiry、edit revision、action token、approval 和 `copilot_approved` 对账。
- [ ] 测试 temporary HUMAN start/renew/expiry、停机补偿和 human outgoing 与 AI/COPILOT 发送竞态。
- [ ] 测试 debounce、顺序保证、会话锁和多 worker 竞争。
- [ ] 测试真人发送、模式切换和模型返回同时发生的竞态。
- [ ] 测试发送前、发送中、发送后各崩溃点的恢复行为。
- [ ] 测试 Memory Pipeline 重放、冲突、watermark 和 embedding 重建。
- [ ] 测试 Memory OR 触发、45 秒 quiet window、三类硬阈值、running range seal 和 5 分钟补偿扫描。
- [ ] 测试 human/AI/proactive/COPILOT 来源信任、candidate 门槛、image-only 审核和 evidence root 断链拒绝。
- [ ] 测试 rolling/daily/weekly summary source membership、迟到事件、时区快照、edit/delete 隔离与递归重建。
- [ ] 测试 Memory candidate 接受/拒绝、一次性 action token、旧 version/evidence 失效和 `/forget` 二次确认。
- [ ] 测试 Memory `fresh/degraded/stale` 积压降级，确保 Main AI 不等待且不读取 candidate/quarantined summary。
- [ ] 测试 Context Builder 总预算、软配额借用、current turn 超限、manifest hash 重建和 source/trust 完整性。
- [ ] 测试 structured/vector/recent 排序、ANN 精确重排、跨层去重、稳定 tie-break 和 memory lag canonical 扩展。
- [ ] 测试 prompt injection、恶意 forward/reply label、历史 AI 指令和图片文字始终保持数据权限。
- [ ] 测试长文本 deterministic splitter、grapheme 边界、delivery group 原子创建、逐段 random ID 对账和部分失败恢复。
- [ ] 测试 `/context` 无正文、preview token 过期/重放/越权/source redaction、Bot send unknown、定时删除失败和所有日志/队列不含 preview 正文。
- [ ] 测试 Proactive Pipeline 无 candidate 时零模型调用，due/补偿扫描与 worker 重试幂等。
- [ ] 测试联系人/account 时区、DST gap/fold、默认 quiet hours、绝对禁发和每日一次受限 bypass。
- [ ] 测试 account/contact/bypass 日预算、关系级最小间隔、并发 reservation 和 send-unknown 保守计费。
- [ ] 测试 30 分钟 activity、AUTO/COPILOT/HUMAN/PAUSED/BLOCKED、冲突草稿和真人接管抑制。
- [ ] 测试 `send_now/defer_once/none`、新 activity/mode/evidence 导致 stale，以及最终 conversation-lock 安全闸门。
- [ ] 测试 Caddy 仅 443、Compose 网络/secret/只读文件系统、无 Docker Socket 和非公开 health/metrics。
- [ ] 测试 key-only Web App `initData`/launch replay、role/admin binding、rate limit、API key 不回显/不入日志和 master key 轮换恢复。
- [ ] 测试 endpoint loopback/private/metadata/IPv6/DNS rebinding/redirect/proxy/CA 的 SSRF 和 TLS 门禁。
- [ ] 测试 arq/Redis 丢失后从 PostgreSQL outbox/job/watermark 恢复，以及 retry/dead-letter/FloodWait 上限。
- [ ] 测试 media 字节/像素/边长/timeout/炸弹、10 GiB quota、TTL 清理和 90/95% disk 降级。
- [ ] 测试 data export age recipient、受限 DB role、artifact hash、SSH/SFTP-only 边界和 24 小时清理。
- [ ] 测试 pgBackRest full/differential/WAL、Session/restic、旧库+最新 erasure ledger、RPO/RTO 和 restore maintenance gate。
- [ ] 测试 Alembic 从空库、上一支持版本、中断 backfill、兼容 app rollback 和不兼容 rollback BLOCKED。
- [ ] 在 2 vCPU/4 GiB/40 GiB Ubuntu 24.04 amd64 上执行 Compose 集成、SIGTERM/SIGKILL、升级和 24 小时资源 soak。
- [ ] 建立关键行为验收矩阵和回归测试数据集。

完成标准：所有消息副作用、跨进程竞态和崩溃恢复声明都有自动化测试或明确标记的人工验证步骤。

## 文档统一完成标准

每份详细架构文档完成前都需要包含：

- [ ] 目标、范围和非目标。
- [ ] 组件职责和依赖关系。
- [ ] 输入、输出和持久化契约。
- [ ] 正常流程、异常流程和恢复流程。
- [ ] 并发、幂等、重试和事务要求。
- [ ] 安全、隐私、审计和可观测性要求。
- [ ] 可测试的验收条件。
- [ ] 尚未决定的问题及其决策期限。
- [ ] 与 `docs/Design.md` 的术语和行为保持一致。

## 收尾任务

- [ ] 对全部详细架构文档进行交叉一致性审查。
- [ ] 根据详细设计修正 `docs/Design.md` 中过时或过于概念化的流程。
- [ ] 补充 README，包括项目状态、文档索引、开发环境和运行入口。
- [ ] 建立 Architecture Decision Records，记录关键取舍及后续变更。
- [ ] 从架构文档生成首个实现里程碑和代码任务列表。
