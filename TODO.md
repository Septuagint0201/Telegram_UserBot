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
- [x] 不提供通用管理 API，只暴露 Telegram Web App 所需的专用 HTTPS 配置入口。
- [x] Telegram Web App 管理模型端点、模型名称、只写 API key 和生成参数。
- [x] 模型凭据应用层加密保存，数据库密文与主密钥分离。
- [x] `control`、`app` 和 `worker` 构成受信任计算边界并只读挂载主密钥，基础设施服务不具备解密能力。
- [x] Memory Agent 不阻塞 Main AI 的实时回复流程。
- [x] 记忆提取采用事件驱动、安静窗口合并、硬阈值触发和定期补偿扫描。
- [x] Main AI 使用最近一次已提交的长期记忆，同时直接读取当前会话的最新原始消息。
- [x] Telegram 消息发送采用 outbound intent、幂等写入和发送结果对账。
- [x] AI 与真人 outgoing 消息通过系统发送记录和 Telegram message ID 对账区分。
- [x] 手动模式控制优先，使用会话模式版本门禁阻止过期 AI 结果发送。
- [x] Memory Agent 先生成候选变更，再由应用层验证并事务提交。
- [x] Proactive Agent 只处理规则层筛选后的候选，不负责常规记忆调度。

## 文档编写顺序

### 1. Runtime Topology

目标文档：`docs/architecture/01-runtime-topology.md`

- [x] 定义 Docker Compose 中的 `https-gateway`、`app`、`control`、`worker`、`postgres`、`redis` 和一次性 `migrate` 任务。
- [x] 定义 Python 包和模块边界，以及允许的依赖方向。
- [x] 明确 Telethon session、Control Bot 和 scheduler 的进程所有权。
- [x] 定义 Telegram Web App 静态页面和专用配置 API 的网络入口。
- [x] 定义各服务心跳、readiness 和 `/server_status` 状态聚合方式。
- [x] 定义进程启动、初始化、迁移、停止和重启顺序。
- [x] 定义进程间通信方式、队列边界和共享状态归属。
- [x] 定义单实例组件与允许水平扩展的组件。
- [x] 记录主要故障模式及其隔离范围。

完成标准：每个运行组件只有一个明确职责和状态所有者，不存在多个进程同时使用同一 Telethon session 的路径。

### 2. Message Lifecycle

目标文档：`docs/architecture/02-message-lifecycle.md`

- [ ] 定义 incoming、outgoing、edit、delete、reply、forward、album 和 media 事件模型。
- [ ] 定义 Telegram 消息的业务唯一键和重复 update 处理方式。
- [ ] 定义 AI、真人和 proactive outgoing 消息的来源识别与对账流程。
- [ ] 定义消息持久化、debounce、turn 聚合、生成、发送和确认状态机。
- [ ] 定义 outbound intent、发送结果记录和启动后对账流程。
- [ ] 定义同一 conversation 的串行化和锁租约规则。
- [ ] 定义模型请求取消与 `mode_version` 发送门禁。
- [ ] 定义超时、重试、死信、补偿和不可恢复失败的处理方式。
- [ ] 定义消息顺序以及迟到、乱序和重复 update 的处理规则。

完成标准：任意处理步骤崩溃或重复执行时，不会重复回复，也不会把 AI 消息误判为真人消息。

### 3. Data Model

目标文档：`docs/architecture/03-data-model.md`

- [ ] 绘制核心实体关系并定义 account、peer、conversation 和 message 的身份模型。
- [ ] 细化消息、发送意图、模型运行、会话模式和后台任务表。
- [ ] 细化 memory、memory proposal、memory evidence、summary 和 embedding 表。
- [ ] 细化 event、intention、relationship state、proactive decision 和 audit log 表。
- [ ] 细化 model endpoint、model profile、credential、config version 和服务状态数据。
- [ ] 定义主键、业务唯一键、外键、检查约束和必要索引。
- [ ] 定义时间字段、时区、Telegram 原始数据和 JSONB 扩展字段规则。
- [ ] 定义各业务流程的事务边界和并发更新策略。
- [ ] 定义软删除、Telegram edit/delete 同步和历史版本保留策略。
- [ ] 定义数据保留、导出、删除、备份和恢复边界。
- [ ] 制定 Alembic migration 规则。

完成标准：Message Lifecycle、Memory Pipeline 和 Proactive Pipeline 所需状态都能持久化，并有明确的唯一性与事务约束。

### 4. Conversation Orchestrator

目标文档：`docs/architecture/04-conversation-orchestrator.md`

- [ ] 定义 AUTO、HUMAN、COPILOT、PAUSED 和维护状态的语义。
- [ ] 定义 Control Bot 命令、真人发言和系统故障触发的状态转换。
- [ ] 定义全局模式、联系人模式和临时接管状态的优先级。
- [ ] 定义 `mode_version` 的递增条件和发送前验证规则。
- [ ] 定义 debounce 后的 conversation turn 输入和输出契约。
- [ ] 定义 COPILOT 草稿的投递、接受、修改、忽略和过期流程。
- [ ] 定义恢复 AUTO 的显式策略；自动恢复作为可配置能力，不作为默认假设。
- [ ] 定义每次模式变更和 AI 决策所需的审计信息。

完成标准：任意模式下收到 incoming 或 outgoing 消息时，系统行为唯一且可预测。

### 5. Memory Pipeline

目标文档：`docs/architecture/05-memory-pipeline.md`

- [ ] 定义 episode extraction、rolling summary 和 consolidation 三类任务。
- [ ] 定义触发条件的或关系、安静窗口、硬阈值和补偿扫描。
- [ ] 定义同一会话 pending memory job 的刷新、范围合并和幂等键。
- [ ] 定义 Memory Agent 输入、结构化输出和 prompt 版本管理。
- [ ] 定义 memory proposal 的校验、接受、拒绝和人工审计状态。
- [ ] 定义 create、update、supersede、invalidate 和 merge 操作。
- [ ] 定义 evidence、confidence、importance 和时间有效区间。
- [ ] 定义 summary watermark、覆盖范围和重新生成规则。
- [ ] 定义 embedding 的生成、版本、更换模型和重建流程。
- [ ] 定义低置信度候选、冲突判断、遗忘和淡化策略。
- [ ] 定义队列积压时 Context Builder 的记忆新鲜度降级策略。

完成标准：任意原始消息范围可以安全重放，重复处理不会重复创建记忆，所有正式记忆都能追溯到证据消息。

### 6. Context Contract

目标文档：`docs/architecture/06-context-contract.md`

- [ ] 定义 identity、personality、relationship、memory、summary、recent messages 和 current message 的装配顺序。
- [ ] 定义各上下文层的 token 预算、截断规则和总预算。
- [ ] 定义结构化记忆、向量记忆和最近历史的选择与排序算法。
- [ ] 定义记忆处理滞后时扩大原始消息窗口的降级策略。
- [ ] 定义上下文快照、来源 ID、token 数和检索理由的记录方式。
- [ ] 定义用户内容、记忆内容和系统指令之间的信任边界。
- [ ] 定义 prompt injection、恶意转发内容和不可信历史的隔离方式。
- [ ] 定义模型 provider adapter、能力声明、超时和结构化输出契约。
- [ ] 定义模型配置草稿、验证、激活、版本切换和运行中请求的快照语义。
- [ ] 定义 `temperature`、`max_output_tokens` 和 Provider 特有参数的能力映射。
- [ ] 定义 prompt、模型和检索算法的版本管理方式。

完成标准：同一上下文输入和版本可以重建，且所有进入模型的非系统内容都保留明确来源和信任级别。

### 7. Proactive Pipeline

目标文档：`docs/architecture/07-proactive-pipeline.md`

- [ ] 定义 SQL/规则层的候选联系人和候选事件筛选方式。
- [ ] 定义 Proactive Agent 的输入、输出和拒绝条件。
- [ ] 定义 event、intention、relationship time 和 conversation time 的读取规则。
- [ ] 定义联系人时区、DST、quiet hours 和适合联系的时间窗口。
- [ ] 定义每日预算、最小间隔、关系等级和全局限额。
- [ ] 定义 proactive decision 和发送行为的幂等键。
- [ ] 定义 Main AI 生成主动消息时使用的上下文。
- [ ] 定义发送前在 conversation lock 内执行的最终安全闸门。
- [ ] 定义 HUMAN、COPILOT、PAUSED、活跃会话和真人接管时的抑制规则。
- [ ] 定义主动消息决策、原因、证据、模型和最终结果的审计记录。

完成标准：worker 重试、scheduler 重复触发或并发执行时不会产生重复主动消息，任何主动发送都有完整决策依据。

### 8. Operations

目标文档：`docs/architecture/08-operations.md`

- [ ] 定义 Dockerfile、Compose 服务、volume、network 和 healthcheck。
- [ ] 定义 Ubuntu 原生运行方式与 Docker 运行方式的支持边界。
- [ ] 定义配置分层、环境变量、密钥注入和启动校验。
- [ ] 定义 Telethon session、Bot token、模型密钥和数据库凭据的保护方式。
- [ ] 定义 Telegram Web App HTTPS 发布、`initData` 验证和管理员授权。
- [ ] 定义 API key 只写不读、加密保存、轮换和主密钥恢复流程。
- [ ] 定义模型端点 URL 验证、私有 HTTP 允许列表和 SSRF 防护。
- [ ] 定义数据库和 pgvector migration 的部署流程。
- [ ] 定义 PostgreSQL、Telethon session 和必要配置的备份恢复流程。
- [ ] 定义结构化日志、敏感信息脱敏、metrics 和告警。
- [ ] 定义 liveness、readiness 和依赖服务状态检查。
- [ ] 定义 SIGTERM、队列任务收尾和 Telethon session 关闭流程。
- [ ] 定义资源限制、磁盘增长、队列积压和模型 API 故障的处理方式。
- [ ] 定义升级、回滚和灾难恢复演练流程。

完成标准：全新 Ubuntu 主机可按文档完成部署，服务异常重启不会破坏 session、重复发送消息或丢失已接收的原始消息。

### 9. Test Strategy

目标文档：`docs/architecture/09-test-strategy.md`

- [ ] 定义单元测试、契约测试、集成测试和端到端测试边界。
- [ ] 建立 Telegram gateway fake 和可重放 update fixtures。
- [ ] 建立 LLM、embedding 和时间服务 fake。
- [ ] 测试 Telegram Web App 认证、过期重放、非管理员访问和密钥不回显。
- [ ] 测试模型配置验证失败、原子激活、版本切换和进行中请求行为。
- [ ] 测试 incoming/outgoing 来源识别、重复 update、edit/delete 和 album。
- [ ] 测试 debounce、顺序保证、会话锁和多 worker 竞争。
- [ ] 测试真人发送、模式切换和模型返回同时发生的竞态。
- [ ] 测试发送前、发送中、发送后各崩溃点的恢复行为。
- [ ] 测试 Memory Pipeline 重放、冲突、watermark 和 embedding 重建。
- [ ] 测试 Proactive Pipeline 的时区、预算、幂等和最终安全闸门。
- [ ] 测试 migration、备份恢复和 Docker Compose 集成运行。
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
