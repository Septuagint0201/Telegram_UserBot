# Architecture Decision Records

本目录记录 Telegram Personal AI Digital Twin V1 已接受的关键架构取舍。详细行为仍以`docs/architecture/`中的契约为准；ADR解释为什么选择当前方向，以及什么变化要求重新决策。

## 状态

| 状态 | 含义 |
|---|---|
| Proposed | 正在讨论，不能作为实现依据 |
| Accepted | 当前实现必须遵守 |
| Superseded | 已由新ADR替代，保留历史 |
| Deprecated | 不再推荐，但尚未完全移除 |

## 记录

| ADR | 决策 | 状态 |
|---|---|---|
| [0001](0001-modular-monolith-and-session-owner.md) | 模块化单体、单Telethon Session owner | Accepted |
| [0002](0002-postgresql-durable-state-and-redis-dispatch.md) | PostgreSQL事实源、Redis dispatch、at-least-once恢复 | Accepted |
| [0003](0003-canonical-model-profiles-and-key-only-secrets.md) | Canonical模型配置、独立profile、key-only Web App | Accepted |
| [0004](0004-asynchronous-memory-and-trusted-context.md) | 异步Memory、可追溯Context和信任边界 | Accepted |
| [0005](0005-mode-gated-proactive-and-outbound-safety.md) | 模式门禁、主动消息预算和outbound intent | Accepted |
| [0006](0006-compose-operations-and-evidence-gates.md) | Compose生产基线、恢复与分层测试证据 | Accepted |

## 新增与变更规则

ADR使用四位递增编号。Accepted ADR不原地改写其核心决策；重大方向变化创建新ADR并在旧记录中标记`Superseded by`。拼写、链接或不改变含义的澄清可以原地修正。

下列变化必须创建或更新ADR：

- Telethon Session所有权、进程/容器边界或多账号拓扑；
- canonical事实源、消息副作用、幂等或恢复语义；
- 模型协议、credential输入/加密或配置控制面；
- Memory/Context来源信任、主动发送门禁或默认mode；
- 生产部署、公开端口、backup、RPO/RTO或release evidence门禁。

每次ADR变化都要同步受影响的详细架构、`docs/Design.md`、测试traceability、README和根`DISCLOSURE`（若行为、权限、数据或网络边界改变）。
