# Runtime Topology

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的运行时拓扑、进程所有权、模块依赖、网络边界和故障隔离规则。

总体产品目标与概念架构见 `docs/Design.md`。消息状态机、数据库字段、队列具体实现和部署命令由后续详细架构文档定义。

当前状态：V1 架构基线。

## 2. 目标

Runtime Topology 需要保证：

- Telethon Session 始终只有一个明确持有者。
- Control Bot 在聊天主进程故障时仍有机会报告状态和执行控制操作。
- 实时聊天不被记忆、摘要或主动性后台任务阻塞。
- 后台 worker 可以扩展，而不会产生重复调度或绕过 Telegram 发送门禁。
- Telegram Web App 只暴露设置、替换和删除模型 API key 所需的凭据能力。
- PostgreSQL、Redis、Telethon Session 和模型凭据不直接暴露到公网。
- 任一服务故障时，其影响范围和恢复路径可预测。

## 3. 范围

本文档覆盖：

- 单台 Ubuntu Server 上的 Docker Compose 拓扑。
- `https-gateway`、`app`、`control`、`worker`、`postgres`、`redis` 和 `migrate`。
- Python 模块边界和允许的依赖方向。
- 服务之间的同步调用、持久化通信和通知方式。
- 进程启动、停止、健康状态、扩展和故障隔离。
- Telethon Session、模型凭据和控制面的所有权。

## 4. 非目标

V1 不包含：

- 多主机编排、Kubernetes 或跨地域部署。
- PostgreSQL、Redis 或 Control Bot 的高可用集群。
- 同时运行多个 Telegram 真人账号。
- 对外提供通用管理 API。
- 在本阶段确定 Redis 任务队列的具体 Python 库。
- 在本阶段确定 Caddy、Nginx 或其他 HTTPS gateway 产品。
- 把 Main AI、Memory Agent 和 Proactive Agent 拆成独立微服务。
- 让 worker 或 control 直接持有 Telethon Session 或发送用户账号消息。

Ubuntu 原生进程运行仅用于开发和故障排查，不作为与 Docker Compose 对等维护的一等部署方式。

## 5. 已确认决策

| 决策 | V1 选择 |
|---|---|
| 主要部署方式 | 单台 Ubuntu Server + Docker Compose |
| Telegram 账号 | 单账号运行，数据模型预留多账号边界 |
| 业务进程 | `app`、`control`、`worker` |
| Control Bot | 独立 `control` 进程 |
| Telethon Session | `app` 专用持久化 `.session` 文件 |
| Scheduler | 位于 `worker`，使用单例租约 |
| 管理入口 | Control Bot 管理非密钥配置 + key-only Telegram Web App |
| 通用管理 API | 不提供 |
| 模型凭据 | API key 只写不读，数据库密文与主密钥分离 |
| 时间存储 | 持久化时间统一使用 UTC |

## 6. 总体拓扑

```text
                              Internet
                                  |
              +-------------------+-------------------+
              |                   |                   |
       Telegram MTProto     Telegram Bot API      HTTPS :443
              |                   |                   |
              |                   |            +------v-------+
              |                   |            | https-gateway|
              |                   |            +------+-------+
              |                   |                   |
        +-----v------+      +-----v-------------------v--+
        |    app     |      |          control          |
        | singleton  |      | Bot + Web App + Status    |
        +-----+------+      +-------------+-------------+
              |                           |
              +-------------+-------------+
                            |
                  +---------+---------+
                  |                   |
             +----v-----+       +-----v----+
             | postgres |       |  redis   |
             +----+-----+       +-----+----+
                  |                   |
                  +---------+---------+
                            |
                      +-----v------+
                      |   worker   |
                      | 1..N       |
                      +------------+

External model endpoints are called by app, control, and worker as permitted.
```

只有 `https-gateway` 向公网发布业务入口 TCP 443。证书签发工具如需 TCP 80，只允许用于 ACME challenge 或跳转到 HTTPS，不得在 80 上提供管理 API。Control Bot 使用出站 Bot API long polling，因此不需要公开 Telegram Bot webhook。

Telegram Web App 在用户的 Telegram 客户端中运行，通过 `https-gateway` 访问 `control` 提供的静态资源和 key-only 凭据 API。endpoint、protocol、model、生成参数和启用状态等非密钥配置只通过 Control Bot 命令与短生命周期输入会话管理。

## 7. 服务清单

| 服务 | 副本 | 核心职责 | 明确不负责 |
|---|---:|---|---|
| `https-gateway` | 1 | TLS 终止；转发 key-only Web App 页面和凭据 API | 不访问数据库；不持有业务或模型密钥 |
| `app` | 1 | Telethon；消息接收；Conversation Engine；Context Builder；Main AI；最终发送 | 不运行 Control Bot；不执行记忆整理和 proactive 判断 |
| `control` | 1 | Control Bot；Web App；模型配置；模式控制；`/server_status` | 不持有 Telethon Session；不发送真人账号消息 |
| `worker` | 1..N | Memory、summary、embedding、proactive、补偿任务 | 不直接调用 Telethon；不直接产生 Telegram 副作用 |
| `postgres` | 1 | 持久化事实源；pgvector；配置版本；审计 | 不对公网开放 |
| `redis` | 1 | 任务队列；缓存；短期心跳；通知；短期租约 | 不作为原始消息或长期记忆的唯一事实源 |
| `migrate` | 按需一次 | 获取 migration lock；执行 Alembic migration；退出 | 不常驻；不处理业务流量 |

`postgres` 和 `redis` 在 V1 中均为单实例。可用性依赖备份、自动重启和补偿流程，而不是集群切换。

## 8. 进程所有权

### 8.1 app

`app` 是 Telegram 真人账号运行时的唯一 owner。

它独占：

- Telegram API ID、API Hash 和账号 Session 使用权。
- Telethon `.session` 文件的读写挂载。
- 账号级运行所有权锁。
- Telegram incoming/outgoing update 接收。
- 真人、AI 和 proactive outgoing 来源对账。
- 会话 debounce、模式检查、Context 构建和 Main AI 调用。
- 所有真人账号消息的最终发送动作。

启动时，`app` 必须通过不归还普通连接池的专用 PostgreSQL 长连接，取得以 Telegram account ID 为键的 session-level advisory lock。无法取得时应 fail closed，不得连接 Telethon 或尝试共享 `.session` 文件。

如果持有 advisory lock 的数据库连接断开，`app` 必须停止新的自动发送，重新确认数据库连接和账号所有权后才能恢复。

初次 Telegram 登录由显式的一次性 bootstrap 流程完成。生产 `app` 不在无人值守启动期间交互式等待手机号、验证码或 2FA。

### 8.2 control

`control` 是独立控制面，负责：

- 通过 Telegram Bot API long polling 运行 Control Bot。
- 校验 Control Bot 管理员白名单。
- 提供 `/server_status`、模式控制、`/models`、模型非密钥配置命令和 `/model_key <role>` Web App 入口。
- 通过绑定管理员、logical role 和随机 session ID 的短生命周期 Bot 输入会话管理 endpoint、protocol、model、生成参数、超时和启用状态；API key 输入必须拒绝。
- 提供 Telegram Web App 静态资源和只允许设置、替换或删除 API key 的凭据 API。
- 校验 Telegram Web App `initData`、时效和管理员身份。
- 创建、验证和激活模型配置版本。
- 接收一次性输入的 API key 并加密保存。
- 聚合服务心跳和依赖探测结果。

`control` 不挂载 Docker Socket，不具有启动、停止或 exec 容器的权限。`/server_status` 通过应用心跳、直接依赖探测和持久化状态判断服务状态。

`control` 写入模式或模型配置后，通过 PostgreSQL 提交正式状态，并使用 Redis 通知加速其他进程的缓存失效。Redis 通知不是事实源；通知丢失时，消费者通过配置版本检查恢复一致性。

### 8.3 worker

`worker` 负责可重试、非实时或周期性任务：

- Episode memory extraction。
- Memory proposal validation 后的持久化编排。
- Rolling summary 和 daily/weekly consolidation。
- Embedding 生成和重建。
- Proactive 候选处理和模型判断。
- 未处理 message watermark、未知 outbound intent 等补偿扫描。
- Scheduler tick 发布。

worker 可以增加副本。任务交付语义为 at-least-once，因此每种任务都必须拥有稳定幂等键。

所有 worker 实例都可以消费普通任务，但只有通过专用 PostgreSQL 长连接取得 Scheduler session-level advisory lock 的实例可以发布周期任务。非 leader worker 保持正常消费能力。leader 退出或该专用连接断开后锁自动释放，由其他实例重新竞争。

worker 产生主动发送决策后，只能保存 decision 并提交发送请求。最终规则检查、conversation lock、outbound intent 和 Telethon 发送仍由 `app` 执行。

### 8.4 migrate

`migrate` 是一次性进程：

1. 等待 PostgreSQL 可用。
2. 获取全局 migration advisory lock。
3. 检查当前 schema revision。
4. 执行向前 migration。
5. 成功退出 `0`，失败以非零状态退出。

`app`、`control` 和 `worker` 在 schema revision 不满足代码要求时不得进入 ready 状态。

## 9. Python 模块拓扑

所有业务进程共享一个 Python 代码库和一组领域模块，通过不同 composition root 启动。

建议包结构：

```text
src/telegram_userbot/
    processes/
        app.py
        control.py
        worker.py
        migrate.py

    domain/
        conversation/
        memory/
        proactive/
        model_config/
        control/
        shared/

    application/
        commands/
        queries/
        services/
        ports/

    adapters/
        telegram_user/
        telegram_bot/
        webapp/
        persistence/
        queue/
        llm/
        embedding/

    platform/
        config/
        crypto/
        logging/
        health/
        time/
```

依赖方向固定为：

```text
processes -> adapters + application -> domain
                           |
                           +-> application ports

adapters -> application ports + domain contracts
domain   -> Python standard library and domain-local types
```

约束：

- `domain` 不导入 Telethon、Bot framework、SQLAlchemy、Redis client 或具体模型 SDK。
- `application` 定义事务、队列、模型、时钟、加密和 Telegram gateway 的 ports。
- `adapters` 实现 ports，不允许 adapter 直接依赖另一个 adapter 的内部实现。
- `processes` 只负责装配依赖、生命周期和信号处理，不承载业务规则。
- 跨领域修改通过 application command、query 或 domain event 完成。
- `app`、`control` 和 `worker` 不复制业务实现，只选择不同的 entrypoint 和 adapter 组合。

## 10. 状态所有权

| 状态 | 事实源 | 写入者 | 读取者 |
|---|---|---|---|
| Telethon Session | `app` 专用 volume | `app` | `app` |
| Telegram 原始消息 | PostgreSQL | `app` | `app`、`worker`、`control` 的受限查询 |
| Outbound intent 和发送结果 | PostgreSQL | `app` | `app`、`worker`、`control` 状态查询 |
| Conversation mode | PostgreSQL | `control`、授权的 `app` 流程 | `app`、`control`、`worker` |
| 长期记忆和 summary | PostgreSQL | `worker` | `app`、`worker`、受限的 `control` 查询 |
| 模型非密钥配置 | PostgreSQL | `control` | `app`、`control`、`worker` |
| 模型凭据密文 | PostgreSQL | `control` | `app`、`control`、`worker` |
| 队列消息 | Redis，PostgreSQL watermark 可补偿 | `app`、`control`、`worker` | `app`、`worker` |
| 短期缓存和通知 | Redis | 全部业务进程 | 全部业务进程 |
| 服务心跳 | Redis TTL | 对应服务自身 | `control` |
| 服务状态变更审计 | PostgreSQL | `control` 或状态记录器 | `control`、运维查询 |

Redis 丢失后允许缓存、通知和心跳丢失，但不能造成 PostgreSQL 中原始消息、正式记忆或活动配置丢失。后台任务通过 watermark 和补偿扫描重新发布。

## 11. 服务通信

### 11.1 同步通信

- `app -> Telegram MTProto`：接收和发送真人账号消息。
- `control -> Telegram Bot API`：Control Bot long polling 和消息回复。
- `Telegram Web App -> https-gateway -> control`：key-only 凭据页面和专用 API。
- `app/control/worker -> PostgreSQL`：事务状态和持久化查询。
- `app/control/worker -> Redis`：队列、缓存、通知和短期状态。
- `app -> Main AI endpoint`：实时聊天生成。
- `worker -> Memory/Proactive/Embedding endpoints`：后台模型任务。
- `control -> configured model endpoint`：显式配置验证；不得使用付费生成作为周期健康检查。

业务进程之间不使用未经持久化的直接 RPC 完成关键状态变更。

### 11.2 异步通信

```text
app
  -> refresh memory job
  -> summary eligibility job
  -> outbound reconciliation job

control
  -> mode/config invalidation notification
  -> explicit maintenance task

worker
  -> proactive send request
  -> retry/recovery job

app
  <- proactive send request
```

队列负责及时投递，PostgreSQL 中的消息 sequence、job record、decision 和 watermark 负责恢复。关键任务不能只存在于 Redis 的瞬时消息中。

### 11.3 模型配置传播

`main_ai`、`memory_agent` 和 `proactive_agent` 分别拥有独立的生成 ModelProfile、credential reference、草稿版本和活动版本。三个 profile 默认使用 Responses adapter，但可以独立切换为 Chat Completions 或 Messages adapter。Embedding 使用独立的非生成配置，不参与这三个 profile 的协议选择。

模型配置采用版本化读取：

1. `control` 将 Bot 命令产生的非密钥 draft 和 key-only Web App 产生的 credential 分别写入 PostgreSQL。
2. `control` 验证端点、模型名称、兼容协议、canonical 参数和协议特有参数。
3. `control` 在一个事务中激活新版本。
4. 提交成功后发布 Redis invalidation 通知。
5. `app` 和 `worker` 在下一个模型请求开始时读取或确认活动版本。
6. 进行中的请求继续使用启动时记录的配置快照。

Redis 通知丢失时，本地缓存 TTL 和版本检查确保最终加载新配置。

## 12. 网络拓扑

Compose 至少定义两个逻辑网络：

```text
edge
backend
```

连接规则：

| 服务 | edge | backend | 公网入站 |
|---|---:|---:|---|
| `https-gateway` | 是 | 否 | TCP 443 |
| `control` | 是 | 是 | 否 |
| `app` | 否 | 是 | 否 |
| `worker` | 否 | 是 | 否 |
| `postgres` | 否 | 是 | 否 |
| `redis` | 否 | 是 | 否 |
| `migrate` | 否 | 是 | 否 |

`https-gateway` 只能访问 `control` 的 key-only Web App 内部端口。它不能解析或访问 PostgreSQL、Redis、`app` 或 `worker` 的内部服务名。

各服务可以发起必要的公网出站连接，但模型 endpoint 需要经过 adapter 的 URL policy 检查：

- 默认只允许 HTTPS。
- Docker 内部或宿主机私有 HTTP endpoint 必须在服务器侧 allowlist 中显式声明。
- 拒绝未允许的 loopback、link-local、云元数据地址和非 HTTP(S) scheme。
- DNS 解析和重定向后仍需满足地址策略。

## 13. Volume 和 Secret

### 13.1 持久化 volume

| Volume | 挂载者 | 权限 | 内容 |
|---|---|---|---|
| `postgres-data` | `postgres` | 读写 | PostgreSQL 和 pgvector 数据 |
| `redis-data` | `redis` | 读写 | AOF/持久化队列状态 |
| `telethon-session` | `app` | 读写、独占 | Telethon `.session` 文件 |

`telethon-session` 不挂载到 `control`、`worker`、`migrate` 或 gateway。镜像构建上下文和日志中都不能包含 Session 文件。

Session 备份必须使用与 SQLite 状态一致的受控快照，备份产物在离开主机前加密。不能在文件正在变化时使用无协调的普通复制作为唯一备份方案。

### 13.2 Secret 分配

| Secret | app | control | worker | gateway | postgres/redis |
|---|---:|---:|---:|---:|---:|
| Telegram API ID/Hash | 是 | 否 | 否 | 否 | 否 |
| Control Bot token | 否 | 是 | 否 | 否 | 否 |
| Credential master key | 只读 | 只读 | 只读 | 否 | 否 |
| TLS private key | 否 | 否 | 否 | 是 | 否 |
| PostgreSQL credential | 是 | 是 | 是 | 否 | 服务端自身 |
| Redis credential | 是 | 是 | 是 | 否 | 服务端自身 |

`control`、`app` 和 `worker` 构成 V1 的受信任计算边界：

- 只有 `control` 接收 API key 明文和写入 credential ciphertext。
- `app` 和 `worker` 仅按 credential reference 读取密文。
- 明文只在发起模型请求所需的进程内存中短暂存在。
- API key 不通过环境变量动态下发，不进入日志、trace、异常、审计内容或读取响应。
- gateway、PostgreSQL 和 Redis 不持有 credential master key。

主密钥使用 Docker Secret 或宿主机 root-only 文件只读挂载。主密钥轮换和恢复流程由 Operations 文档定义。

## 14. 启动顺序

逻辑启动顺序：

```text
postgres + redis
        |
      healthy
        |
      migrate
        |
 migration success
        |
app + control + worker
        |
individual readiness
        |
https-gateway routes key-only Web App traffic
```

具体要求：

1. PostgreSQL 和 Redis 先启动并通过基础健康检查。
2. `migrate` 获取全局锁并完成 schema migration。
3. 业务进程检查 schema revision，错误时保持 not ready 并退出或重试。
4. `app` 检查 Session volume、取得 account advisory lock、验证 Session 已授权并连接 Telethon。
5. `control` 启动内部 Web 服务和 Bot API long polling。
6. `worker` 启动消费者；其中一个实例取得 Scheduler leader lock。
7. gateway 只有在 `control` ready 时才把 key-only Web App 请求视为可用。

不能依赖 Compose 的启动顺序等同于依赖已经 ready。每个进程都必须自行处理依赖暂时不可用。

## 15. 优雅停止

所有常驻 Python 进程必须处理 SIGTERM，并在有限时间内完成收尾。

### app

1. 将 readiness 设为 false。
2. 停止创建新的 AI 回复和 outbound intent。
3. 对正在执行的模型请求进行 best-effort cancel。
4. 等待已进入发送阶段的操作完成对账，或保留为可恢复的 unknown 状态。
5. 停止 Telethon update 消费并断开 Session。
6. 释放 account ownership lock 后退出。

### control

1. 停止接受新的 Bot 配置会话和 Web App credential 写入。
2. 完成已提交事务，未提交的 draft 不激活。
3. 停止 Bot API long polling 和内部 Web server。
4. 清理短生命周期管理会话后退出。

### worker

1. 停止领取新任务。
2. 允许已领取任务在 grace period 内完成。
3. 未完成任务释放或保留为可重试状态。
4. Scheduler leader 停止发布任务并释放 advisory lock。
5. 写入最终 heartbeat 状态后退出。

不能依赖固定容器停止顺序保证正确性。每个服务必须能够在其他依赖先行退出时安全失败。

## 16. 健康状态与 `/server_status`

### 16.1 内部健康接口

`app`、`control` 和 `worker` 各自提供仅 Compose 内网可访问的：

```text
/health/live
/health/ready
```

语义：

- `live`：进程事件循环和内部健康服务仍可响应。
- `ready`：服务拥有执行其核心职责所需的依赖和所有权。

readiness 条件：

| 服务 | ready 条件 |
|---|---|
| `app` | schema 正确；PostgreSQL/Redis 可用；account lock 有效；Session 已授权；Telethon 已连接 |
| `control` | schema 正确；Bot loop 可用；Web API 可用；关键配置存储可访问 |
| `worker` | schema 正确；PostgreSQL/Redis 可用；消费者运行；不要求当前实例是 Scheduler leader |

### 16.2 心跳

常驻服务定期向 Redis 写入带 TTL 的最小心跳：

```text
service_name
instance_id
started_at
last_heartbeat_at
readiness
last_successful_operation_at
```

建议默认每 10 秒刷新、30 秒过期，最终数值作为可配置运维参数。PostgreSQL 只记录重要状态转换，不保存每次高频 heartbeat。

### 16.3 server_status 输出

`/server_status` 使用以下状态：

```text
healthy
degraded
down
unknown
```

它至少报告：

- `app` heartbeat、Telethon 连接和最后 update 时间。
- `control` 自身状态和 Bot API 状态。
- worker 实例数、队列积压和最近成功任务时间。
- Scheduler leader 是否存在以及最近 tick 时间。
- PostgreSQL 和 Redis 直接探测结果。
- 各逻辑模型配置是否存在、最后验证结果和最近请求状态。
- pending/unknown outbound intent 数量。

模型端点状态优先使用显式验证结果和真实请求结果。周期状态检查不得为了显示“健康”而反复执行付费生成。

输出不得包含：

- API key、Bot token、Session 或环境变量。
- Prompt、联系人消息或长期记忆正文。
- 宿主机敏感路径、容器 inspect 内容或内部异常堆栈。

## 17. 扩展规则

### 17.1 V1 副本规则

| 服务 | V1 副本 | 是否可扩展 |
|---|---:|---|
| `app` | 1 | 否；一个账号只能有一个 owner |
| `control` | 1 | 否；V1 Bot long polling 和配置写入口保持单一 |
| `worker` | 1 起 | 是；依赖幂等任务和 Scheduler leader lock |
| `https-gateway` | 1 | V1 不要求 |
| `postgres` | 1 | V1 不要求 |
| `redis` | 1 | V1 不要求 |

worker 即使增加副本，也不能突破会话级锁、任务幂等键、proactive 幂等键或 Memory watermark。

### 17.2 未来多账号

未来多账号拓扑为：

```text
app(account A) + session A
app(account B) + session B
app(account C) + session C
        |
shared control / workers / postgres / redis
```

每个 `app` 以 account ID 获取独立 ownership lock。V1 不实现账号注册、动态实例编排或 Control Bot 多账号切换界面，但当前表和接口不能依赖全局唯一账号常量。

## 18. 故障隔离

| 故障 | 影响 | 必须采取的行为 | 恢复 |
|---|---|---|---|
| `app` 崩溃 | AI 不再接收或发送；真人客户端仍可使用账号 | 不由 worker/control 代发 | 重启后取得 account lock，恢复 update 并对账 outbound intent |
| `control` 崩溃 | Control Bot、key-only Web App、状态查询不可用 | `app` 按最后已提交模式和模型配置继续运行 | 重启并恢复 Bot polling；不回滚已提交配置 |
| worker 崩溃 | 记忆、summary、proactive 延迟 | 实时聊天使用已提交记忆和 recent raw messages | 队列重试；watermark 补偿扫描 |
| Scheduler leader 崩溃 | 周期任务短暂不发布 | 普通 worker 继续消费 | advisory lock 释放后重新选 leader |
| PostgreSQL 不可用 | 无法保证事实持久化和所有权 | 自动发送 fail closed；worker 停止提交；control 报告 degraded/down | 重连、重新校验 schema/lock 后恢复 |
| Redis 不可用 | 队列、锁、通知、heartbeat 不可用 | 原始 update 尽可能先入 PostgreSQL；自动副作用暂停；不无锁发送 | Redis 恢复后根据 DB watermark 重新发布 |
| 模型端点不可用 | 对应模型调用失败 | Main AI 不发送；Memory 重试；Proactive 跳过 | 按模型调用策略重试或切换已验证配置 |
| gateway 不可用 | API key 无法设置、替换或删除 | Control Bot 的非密钥模型配置和其他文本控制仍可工作 | gateway 恢复后重新开放 key-only Web App |
| Session volume 丢失或损坏 | `app` 无法登录 | fail closed，不自动创建新 Session | 从加密备份恢复或显式重新登录 |
| Credential master key 不可用 | 无法读取模型凭据 | 模型调用和 credential 写入 fail closed；状态接口仍可报告原因 | 恢复同一主密钥或执行受控密钥恢复 |

## 19. 日志与可观测性边界

所有服务向 stdout 输出结构化 JSON 日志，并至少包含：

```text
timestamp
level
service
instance_id
event
correlation_id
conversation_id（适用时）
job_id（适用时）
config_version（模型调用适用时）
```

日志中禁止包含 Session、完整 API key、Bot token、完整 Prompt 和默认情况下的消息正文。

跨进程 correlation ID 必须随队列任务、proactive decision 和 outbound request 传播，便于从 Telegram update 追踪到模型调用、后台任务和最终发送。

## 20. 明确延后到后续文档的选择

以下选择不会改变本文件定义的服务边界，因此延后决定：

- Redis 任务队列具体采用的 Python 库。
- HTTPS gateway 使用 Caddy、Nginx 或其他实现。
- 内部 HTTP 端口号和 Compose service name 的最终拼写。
- CPU、内存、磁盘和 worker concurrency 数值。
- TLS 证书签发和更新工具。
- PostgreSQL、Redis 和 Session 的具体备份工具。
- 任务 retry、dead-letter 和补偿扫描的精确时间参数。

具体队列库必须满足后续 Message Lifecycle 定义的 at-least-once、延迟任务、重试、幂等和 asyncio 集成要求。

## 21. 验收条件

- [x] 每个运行组件都有唯一职责和明确状态所有者。
- [x] 只有 `app` 挂载和使用 Telethon Session。
- [x] `app` 无法取得 account ownership lock 时 fail closed。
- [x] `control` 独立于 `app`，可以根据心跳报告 `app` 故障。
- [x] worker 可以扩展，但只有一个 Scheduler leader。
- [x] worker 和 control 不能直接发送真人账号 Telegram 消息。
- [x] PostgreSQL 和 Redis 不发布公网端口。
- [x] 公网只暴露 gateway 的 key-only Telegram Web App HTTPS 路径。
- [x] API key 只写不读，且基础设施服务无法解密。
- [x] 服务启动和停止不依赖理想容器顺序。
- [x] 每个主要故障都有 fail-open 或 fail-closed 的明确选择。
- [x] Runtime Topology 未提前固定后续文档负责的实现库。
