# Telegram Personal AI Digital Twin

## Telegram 真人账号 AI 分身系统 — 项目总体设计

## 1. 项目定位

本项目的目标是在 Telegram 上运行一个长期存在的个人 AI 分身。

系统直接控制一个真实 Telegram 用户账号，使该账号能够同时接受 AI 和真人本人控制，并保持统一的身份、统一的聊天历史、统一的人格和长期记忆。

最终表现为：

- 对方始终是在和同一个 Telegram 真人账号聊天。
- 普通情况下由 AI 自动处理消息。
- 真人本人可以随时直接进入 Telegram 手动回复。
- 真人发送出去的内容自动进入 AI 上下文，并被视作该 AI 自己过去说过的话。
- 可以通过独立 Telegram 控制 Bot 切换 AI / HUMAN 等运行模式。
- AI 拥有长期记忆，并持续对记忆进行整理、压缩、更新和遗忘。
- AI 能理解时间流逝、事件、承诺、人与人之间多久没有联系等时间因素。
- AI 可以在适当情况下主动向联系人发送消息。
- AI 的语言风格、关系判断、主动程度和过去行为长期保持连续。

项目最终目标不是传统客服机器人，而是一个长期运行的：

**Personal AI Agent / Digital Twin / AI Persona**

---

# 2. 总体架构

核心结构：

```text
                                  Telegram
                                      |
                    +-----------------+-----------------+
                    |                                   |
          Telegram 真人用户账号              Control Bot / Web App
                    |                                   |
                 MTProto                         Bot API / HTTPS
                    |                                   |
                    v                                   v
          app（单实例）                         control（单实例）
     Telethon Session Owner              控制、配置、状态监控
     Conversation Engine                         |
     Context / Main AI                           |
     Response Engine                             |
                    |                            |
                    +-------------+--------------+
                                  |
                         PostgreSQL + Redis
                                  |
                                  v
                         worker（可扩展）
                 Memory Agent / Proactive Agent
                     Embedding / Scheduler
```

运行时采用模块化单体，而不是从一开始把每个逻辑 Agent 拆成独立微服务。

初期主要业务进程为：

```text
app
control
worker
```

其中：

- `app` 是 Telethon Session 的唯一持有者，初期固定单实例。
- `control` 独立运行 Control Bot、key-only Telegram Web App 凭据 API 和服务状态监控。
- `worker` 执行记忆、摘要、embedding 和主动性任务，并承载带单例租约的 Scheduler。
- PostgreSQL 保存持久化业务状态，Redis 提供任务队列、缓存、租约和短期服务心跳。
- V1 只运行一个 Telegram 真人账号，但数据库身份模型和模块接口不写死为单账号。
- 未来多账号采用每个账号一个独立 `app` 实例和独立 Session，共享 `control`、`worker`、PostgreSQL 和 Redis。

整个系统分为几个彼此独立但协同工作的部分：

1. Telegram 真人账号层
2. Telegram 控制 Bot
3. Conversation Engine
4. Main AI Agent
5. Memory Agent
6. Memory System
7. Time / Proactive Agent
8. Scheduler
9. Context Builder
10. 数据库
11. Vector Memory
12. Cache
13. Human Override 系统
14. 模型配置与密钥管理
15. 服务状态监控
16. 测试、恢复与发布证据

---

# 3. Telegram 真人账号

系统的主要 Telegram 身份是一个真实用户账号，而不是 Bot 账号。

实现方式：

```text
Telegram User Account
        |
      MTProto
        |
     Telethon
        |
     Backend
```

账号通过 Telegram MTProto 登录。

首次登录需要：

- Telegram API ID
- Telegram API Hash
- 手机号
- Telegram 登录验证码
- 如果开启，则需要 2FA 密码

成功后保存 Telethon Session。

之后 Ubuntu 服务器可以长期维持该 Telegram 用户会话。

这个账号既可以：

- 被后端程序控制
- 被 AI 自动回复
- 被真人从 Telegram 手机客户端直接操作
- 被真人从 Telegram Desktop 操作

因此 AI 和真人共享完全相同的 Telegram 身份。

---

# 4. 人机共用账号

这是系统最核心的设计原则之一。

无论消息实际来自：

```text
AI
真人
主动 AI
```

Telegram 对外显示的始终都是：

```text
同一个 Telegram User Account
```

系统内部才区分消息实际来源。

例如：

```text
Telegram 对方看到：

Alice:
好的，我晚上看看。
```

数据库可能记录：

```json
{
  "role": "assistant",
  "source": "human",
  "content": "好的，我晚上看看。"
}
```

或者：

```json
{
  "role": "assistant",
  "source": "ai",
  "content": "好的，我晚上看看。"
}
```

对 LLM 来说，两者原则上都会表现为：

```json
{
  "role": "assistant",
  "content": "好的，我晚上看看。"
}
```

这样可以保持身份连续性。

---

# 5. 真人消息回灌 AI 上下文

这是本项目的重要特性。

真人本人通过 Telegram App 直接发送出去的消息，Telethon 后端也需要捕获。

例如历史：

```text
对方：
最近项目怎么样？

AI：
还在继续推进。

真人随后手动发送：
今天刚把核心功能跑通。
```

数据库记录：

```text
user:
最近项目怎么样？

assistant / source=ai:
还在继续推进。

assistant / source=human:
今天刚把核心功能跑通。
```

下一次对方问：

```text
那下一步准备做什么？
```

发送给主模型的上下文为：

```json
[
  {
    "role": "user",
    "content": "最近项目怎么样？"
  },
  {
    "role": "assistant",
    "content": "还在继续推进。"
  },
  {
    "role": "assistant",
    "content": "今天刚把核心功能跑通。"
  },
  {
    "role": "user",
    "content": "那下一步准备做什么？"
  }
]
```

因此 AI 会认为：

> “今天刚把核心功能跑通”是自己以前说过的话。

这保证真人和 AI 共同塑造同一个长期人格。

`outgoing=true` 本身不能证明消息来自真人，因为 `app` 通过 Telethon 发送的 AI 消息同样属于 outgoing。系统发送前必须原子创建 outbound delivery group 和全部 ordered intents，发送成功后分别绑定 Telegram message ID。监听器通过 intent/random ID 映射识别 `ai`、`proactive_ai` 或 `copilot_approved`；无法匹配任何系统发送记录的 outgoing 消息才归类为 `human`。

消息表使用 Telegram 账号、会话和 message ID 组成业务唯一键。同一个 update 被重复接收时只允许对账和补全状态，不得重复触发回复或记忆处理。

---

# 6. 消息来源模型

数据库内部建议明确保存：

```text
role
source
```

其中：

```text
role:
user
assistant
system
```

`source` 可以包括：

```text
telegram_user
ai
human
proactive_ai
copilot_approved
control
system
```

典型记录：

```text
role=user
source=telegram_user
```

代表联系人发来的消息。

```text
role=assistant
source=ai
```

代表正常 AI 自动回复。

```text
role=assistant
source=human
```

代表真人本人手动回复。

```text
role=assistant
source=proactive_ai
```

代表 AI 主动发出的消息。

```text
role=assistant
source=copilot_approved
```

代表管理员在 Control Bot 中批准、由 `app` 代发的 COPILOT 草稿。它保留是否人工编辑的 provenance，不等同于纯 `human` 写作证据。

---

# 7. Conversation Engine

Conversation Engine 是实时消息系统的中心。

职责包括：

- 接收 Telegram 消息
- 识别会话
- 判断联系人
- 判断当前工作模式
- 保存归一化 canonical message/revision
- 获取最近聊天历史
- 获取最近一次已提交的 Memory System 状态
- 刷新异步记忆处理任务
- 构造主模型 Context
- 调用 Main AI
- 创建 outbound delivery group 和全部 ordered intents
- 将结果发送到 Telegram
- 对账并记录最终回复

核心流程：

```text
Telegram Message
        |
 Conversation Engine
        |
Idempotent Save Canonical Message
        |
Refresh Pending Memory Job ---------> Async Memory Pipeline
        |
  Check Chat Mode
        |
Build Context from Committed Memory
and Recent Canonical Messages
        |
  Main AI Agent
        |
Create Outbound Intent
        |
Telegram Send
        |
Reconcile Message ID and Source
```

Memory Agent 不位于 Main AI 的同步回复路径中。它暂时尚未提取的新内容仍然通过 recent canonical messages 进入 Context，因此异步记忆处理不会使 Main AI 丢失当前对话。

V1 的消息生命周期基线见 `docs/architecture/02-message-lifecycle.md`。自动对话只覆盖非 Bot 用户的一对一 private chat；支持 text、caption 和经过验证的 photo/image document 输入，图片模型预算使用 `detail=auto`。语音、音频、视频、video note、贴纸和非图片 document 不下载二进制，只保存允许的元数据；其中只有 caption 可以作为文本输入。

---

# 8. 会话模式

完整状态、优先级、命令、草稿、版本与恢复契约见 `docs/architecture/04-conversation-orchestrator.md`。

`AUTO/HUMAN/COPILOT` 是基础模式。account 保存默认基础模式，conversation 可以设置 override；maintenance、global/contact pause 和 temporary HUMAN 是不破坏基础模式的覆盖层。运行依赖不可用使用独立 `BLOCKED(reason)`，不把基础模式永久改成 PAUSED。

建议至少提供三种主要模式。

## AUTO

AI 全自动处理。

```text
对方消息
   |
Main AI
   |
自动发送
```

适合普通聊天。

---

## HUMAN

真人接管。

AI 不自动发送回复。

```text
对方消息
   |
保存
   |
通知真人
   |
真人 Telegram 回复
```

真人发送的内容仍然进入 AI 历史。

---

## COPILOT

AI 只生成建议回复，不直接发送。响应式 incoming 不自动生成草稿，管理员执行 `/draft <contact>` 后才生成一次。

例如：

```text
对方：
晚上有空吗？

AI Draft：
应该有，怎么了？
```

真人可以：

- 直接发送
- 修改
- 忽略

Control Bot 通过 Send/Edit/Ignore card 完成审批；Send 由 `app` 在完整版本门禁后代发，最终进入 `assistant` 历史并记录 `source=copilot_approved`。直接在普通 Telegram 客户端发送的内容仍是 `source=human`。

这个模式非常适合训练 AI 学习真实表达风格。

---

# 9. Telegram Control Bot

另外建立一个独立 Telegram Bot：

```text
@xxx_control_bot
```

它不承担联系人聊天，只承担系统控制、状态监控和管理入口分发。

Control Bot 运行在独立的 `control` 进程中，不持有 Telethon Session。即使 `app` 进程不可用，只要 `control`、Telegram Bot API 和基础设施仍然可用，管理员仍可查看状态并修改全局控制状态。

例如命令：

```text
/ai
/human
/copilot
```

按联系人：

```text
/ai 123456789
/human 123456789
/copilot 123456789
/mode_inherit 123456789
```

不带联系人时修改 account default，只影响没有 override 的 conversation；带联系人时设置 override，`/mode_inherit` 清除 override。

其他建议命令：

```text
/status
```

查看当前模式。

```text
/server_status
```

查看 `app`、`control`、`worker`、Scheduler、PostgreSQL、Redis 和模型端点的状态摘要。

状态信息来自各服务写入 Redis TTL 的短期心跳和 PostgreSQL 中的重要状态转换，并结合 `control` 对 PostgreSQL、Redis 和配置中模型端点的直接探测。依赖不可达时返回 `down` 或 `unknown`，而不是依赖 Docker 容器查询。Control Bot 不挂载 Docker Socket，也不直接获得宿主机容器管理权限。

```text
/memory
```

查看该联系人的 active 长期记忆最小摘要。

```text
/memory_candidates [contact]
/memory_accept <proposal-short-id>
/memory_reject <proposal-short-id>
/memory_status [contact]
```

`/memory_candidates` 只列待审 candidate；接受/拒绝绑定精确 proposal、evidence 和 target version，使用短期一次性 action token。`/memory_status` 只显示 freshness、watermark lag、队列和稳定错误码，不显示正文。

```text
/context [contact]
```

默认只查看最新/当前 Main AI context manifest 的元数据：purpose、构建时间、token/image预算、各层source数量、memory freshness、选择/省略reason和版本，不显示私聊、memory、summary或system prompt正文。

```text
/context_preview <contact>
```

完整预览是高敏感操作。命令先展示精确manifest、联系人、内容范围和“正文将复制到管理员Telegram会话”的警告；管理员通过私聊内的Confirm按钮回调提交绑定本人、Bot chat、manifest hash和短期deadline的一次性token，token不进入Bot消息正文或可手输命令参数，Control Bot随后才重建并发送完整canonical文本。图片只显示media reference、hash、尺寸和`detail=auto`，不向Bot重传二进制。

确认token默认5分钟过期且只能使用一次；preview消息默认10分钟后尽力删除，并持久化Bot message ID和删除结果。Telegram消息删除不能保证清除通知、客户端缓存、转发、截图或平台副本，删除失败必须告警，不能宣称已经可靠擦除。Preview不调用模型、不创建Telegram真人账号outbound intent，也不能恢复已经delete/forget/purge/redact的正文。

```text
/pause
```

暂停 AI。

```text
/resume
```

重新开放对应 pause gate 并恢复原基础模式，不自动回复暂停期间积压消息。`/pause <contact>` 和 `/resume <contact>` 操作联系人覆盖层；无参数操作 global gate。

```text
/draft <contact>
/reply_pending <contact>
/cancel <contact>
```

`/draft` 只在 COPILOT 手动生成一次响应式草稿；`/reply_pending` 是恢复 AUTO 后显式补一次未回应片段的唯一 V1 路径；`/cancel` 失效当前 pre-send work 但不改变基础模式。

```text
/forget <memory-short-id>
```

预览并二次确认后忘记特定 stable memory。forget 清除 memory payload、派生正文和 embedding，但默认不删除 canonical chat；contact purge/account wipe 由独立高影响流程处理。

```text
/proactive off <contact>
/proactive on <contact>
```

关闭或启用某联系人主动消息；`off`立即使首项副作用前的主动run、draft和reservation失效。

```text
/proactive status [contact]
/proactive limits [contact]
/proactive limits_set <contact>
/proactive quiet [contact]
/proactive quiet_set <contact>
/proactive decisions [contact]
```

查看/修改account或联系人预算、间隔和quiet policy，以及查看不含私聊正文的decision摘要。修改使用短期多步输入、字段校验、范围预览和最终确认，不接受任意JSON规则。

```text
/models
```

显示三个生成模型 profile 和独立 Embedding 配置的活动版本、启用状态、credential 是否已配置，以及可用的模型配置命令；不显示 API key。

```text
/model_show <role>
/model_config <role>
/model_cancel
/model_validate <role>
/model_activate <role>
/model_key <role> [set|replace|delete]
```

`/model_show` 查看某角色的非密钥配置；`/model_config` 启动最长15分钟的多步输入会话，用于修改 endpoint、兼容协议、模型名称、temperature、最大输出 token、超时、启用状态和受控协议选项；`/model_cancel` 显式取消当前会话；`/model_validate` 用无私人数据 capability probe 验证 draft；`/model_activate` 以 profile/draft CAS 激活已验证版本；`/model_key` 打开只处理该角色 API key 的 Telegram Web App。API key 禁止通过命令参数、Bot 消息或多步输入会话提交。

多步输入会话绑定管理员 Telegram User ID、目标 logical role 和随机 session ID，同一管理员同一时间只允许一个活动配置会话；会话必须支持显式取消、超时失效、逐项校验和最终确认。Bot 删除消息不作为秘密清除机制，因此任何可能包含密钥的输入都必须直接拒绝且不得落入配置或审计记录。

Control Bot 必须设置管理员白名单。

例如：

```text
allowed_admin_ids
```

只有指定 Telegram User ID 可以控制服务器。

## 模型配置与 key-only Web App

模型配置以 Control Bot 命令和输入控件为主。命令使用按钮、选择菜单或 ForceReply 驱动短生命周期会话；适合单条表达的非密钥字段也可使用带参数命令。所有修改先写入目标 role 的 draft，经校验和显式确认后再激活。

Web App 只提供受限的 API key 凭据功能：

```text
按一次性launch限定的模型逻辑角色
API key 设置、替换或删除
```

Web App 不提供 credential 读取或状态查询接口；“已配置/未配置”、版本和非密钥配置只由 `/models`、`/model_show` 显示。

生成模型固定为三个彼此独立的逻辑配置：

```text
main_ai
memory_agent
proactive_agent
```

每个逻辑角色都拥有独立的 endpoint、model、credential reference、兼容协议、生成参数、草稿版本和活动版本。修改或激活其中一个角色的配置，不得隐式改变另外两个角色。

Embedding 是独立的非生成模型配置，不使用本节的 Responses、Chat Completions 或 Messages 对话协议，也不计入这三个生成模型 profile。

Control Bot 允许三个生成模型分别选择：

```text
openai_responses
openai_chat_completions
anthropic_messages
```

这里的 Completions 指 Chat Completions，不支持旧式纯文本 `/completions` 协议。

Web App 浏览器直接通过 HTTPS 连接服务器上的 `control` 凭据 API。该入口只接受 credential 的设置、替换或删除，不得读取或修改 endpoint、protocol、model、生成参数、启用状态、draft 或活动版本；它不扩展为通用业务管理 API，也不直接访问 `app`、worker、PostgreSQL 或 Redis。

每次 Web App 会话必须：

- 校验 Telegram Web App `initData` 签名。
- 校验认证数据的新鲜度，默认5分钟，未来时钟偏差最多30秒，拒绝过期重放。
- 再次检查 Telegram User ID 是否位于管理员白名单。
- 使用绑定管理员、Bot/chat、logical role和action set的256-bit一次性launch token，默认5分钟过期。
- 对凭据写入执行审计和速率限制，审计中只记录 role、动作、结果和 credential version，不记录 key。
- 不使用第三方analytics/CDN、cookie或browser storage保存key；request body最大16 KiB。

API key 采用只写不读语义：

- 管理员在 Web App 中输入一次后，通过 HTTPS 发送给 `control`。
- 服务端使用AES-256-GCM、随机96-bit nonce、绑定deployment/role/version的AAD和版本化master key keyring加密。
- 主密钥以root控制、仅`app/control/worker` reader GID可读的host file作为Compose secret挂载，不与数据库密文或backup credential存放在一起；必须验证非root容器实际可读且其他服务未挂载。
- Web App 成功响应只有空的204结果，不返回 credential 状态、版本、原key、密文、nonce或认证片段；状态只在Control Bot查看。
- 更换 key 必须重新输入；日志、审计记录、异常和 Telegram 消息中不得出现 key。

V1 将 `control`、`app` 和 `worker` 视为受信任计算边界，并向这三个服务只读挂载同一主密钥。只有 `control` 可以接受和写入模型配置；`app` 与 `worker` 只能按 credential reference 读取密文，并在发起模型请求时于进程内存中短暂解密。`https-gateway`、PostgreSQL 和 Redis 不挂载主密钥，也不能获得明文 API key。

模型端点默认只允许解析到公网unicast的HTTPS。私有HTTP(S)必须由root-only policy精确允许scheme/host/port/CIDR；Control Bot不能修改allowlist。请求禁用redirect和environment proxy，每次解析/连接重验全部A/AAAA，拒绝loopback、private、link-local、CGNAT、IPv6 ULA、云metadata和DNS rebinding；TLS验证不能由Bot关闭。

模型配置采用草稿、连通性验证、激活三步流程。新配置验证失败时继续保留旧active配置；激活只影响新run。完整Caddy/Web App/key rotation/SSRF契约见`docs/architecture/08-operations.md`。

---

# 10. Main AI Agent

主模型：

**GPT 5.6 Luna**

Main AI 只负责高价值认知任务：

- 最终聊天回复
- 人格表达
- 复杂推理
- 语气和情绪
- 根据关系决定表达方式
- 消化长期记忆
- 使用相关历史
- 对主动消息进行最终生成
- 必要时对主动行为进行最终检查

Main AI 不承担高频数据库整理工作。

---

# 11. Main AI Context

详细装配、预算、检索、信任、adapter、版本和长输出契约见 `docs/architecture/06-context-contract.md`。

每次调用主模型之前，Context Builder 先固定 turn、source revision、模型能力、prompt、检索策略和 token policy，再构造 provider-independent canonical context：

```text
System / Developer Safety Instructions
Administrator-authored Identity Instructions
Descriptive Identity + Personality Data
Current Relationship + Current Time Data
Relevant Structured Memories
Relevant Semantic Memories
Conversation Summary
Recent Canonical Messages
Current Turn
```

稳定的可信 instruction 位于最前，动态 current turn 位于最后。只有 system/developer 和管理员人工配置的 instruction 可以改变模型行为；Memory Agent 提取的 identity、personality、relationship、memory、summary，以及消息、forward、reply quote 和图片中的文字，都属于带来源的数据，不能提升为 system instruction。

Main AI 默认输入上限为 24,000 token，并按下面公式收紧：

```text
safety_reserve = max(1024, ceil(max_context_tokens * 0.05))
effective_input_budget = min(24000,
  max_context_tokens - max_output_tokens - safety_reserve)
```

内容软配额为 current 20%、recent 30%、identity/personality/relationship/time 15%、structured memory 15%、semantic memory 10%、summary 10%。未使用预算按确定顺序借给 current 和 recent。Current turn 必须完整保留；即使移除所有可选层仍超限时 fail closed，不静默截断或猜测。

Context manifest 保存每个 source/revision、slice、trust、role、估算 token、图片、排序分数、选择/省略理由，以及 config、credential、prompt、builder、retrieval、adapter、capability、token estimator 和 embedding space 版本。相同输入与版本必须能重建同一 ordered manifest hash。

图片只自动选择 current turn 的 validated images，必要时追加直接 reply 的一张；不做历史图片广域召回。Canonical `detail=auto` 由三种协议 adapter 显式映射或通过已验证的 provider-native 等价默认处理。模型能力不足或 current required images 超限时，整个自动 turn 阻止，不静默丢图。

---

# 12. Memory Agent

详细的触发、输入清单、proposal、证据、summary、embedding、积压降级和恢复契约见 `docs/architecture/05-memory-pipeline.md`。

记忆模型：

**GPT 5.4 nano**

Memory Agent 是高频运行的小模型。

它不负责直接聊天。

它在异步 worker 中运行，不阻塞 Main AI 的实时回复。Main AI 使用最近一次成功提交的长期记忆，同时直接读取当前会话最新的 canonical message revision。

核心问题只有：

> 新发生的内容有什么值得长期保留？

Memory Agent 采用事件驱动与定期补偿结合的触发方式。

以下任一事件都会创建或刷新同一会话的 pending memory job：

```text
AI 回复成功
OR 真人手动回复
OR HUMAN / COPILOT / PAUSED / BLOCKED 下收到可提取的新消息
OR 主动对话产生新消息
OR 累计 revision 达到 20 条
OR 估算输入达到约 6000 tokens
OR 最早未处理事件等待达到 10 分钟
OR 补偿扫描发现遗漏范围
```

这些触发条件全部是 OR。正常任务在会话连续安静 45 秒后执行。安静窗口内出现新 eligible event 时，只扩大 pending range 并重新计时，不创建重复任务；hard deadline 不后移。20 条 revision、约 6000 tokens、10 分钟等待和默认每 5 分钟执行的补偿扫描都可以绕过安静窗口。

AUTO incoming 会立即进入未处理范围，通常等本轮 outgoing 确认后形成完整 episode；若生成失败或持续没有 outgoing，硬阈值和补偿扫描仍保证最终处理。已经 running 的范围不可扩张，新事件进入下一 job generation。

Proactive Agent 不负责常规记忆调度。它只消费已经提交的 event、intention 和关系状态，避免与 Memory Agent 形成循环依赖。

---

# 13. Memory Agent 输入

每次调用先持久化不可变 input manifest。典型输入：

```text
尚未处理的 conversation episode
该 episode 前后的必要上下文
当前联系人
可能重复或冲突的 active memory version
当前 active summary
当前长期 profile
来源 message revision / validated image IDs 和 event 范围
每项 source、trust、inclusion role 和 content hash
pipeline、prompt、schema、模型配置和时区版本
```

前置上下文只帮助理解，不能自动成为 evidence。消息、caption、memory 和 summary 都是数据而不是 system instruction；Memory Agent 不能借消息内容改变 scope、输出 schema 或请求 secret。

图片只有在媒体校验通过、revision 仍有效、Memory Agent profile 声明视觉能力且 adapter 支持 canonical `detail=auto` 时输入。caption 作为普通文本；纯图片推断默认只能进入 candidate。语音、音频、视频和其他非图片媒体不下载、不送入 Memory Agent。

---

# 14. Memory Agent 输出

输出必须是严格、版本化 JSON，一次可以提出零个或多个 proposal。

例如：

```json
{
  "schema_version": 1,
  "proposals": [
    {
      "proposal_ordinal": 0,
      "operation": "create",
      "memory_type": "event",
      "target_memory_ids": [],
      "semantic_key": "contact.relocation.plan",
      "payload": {
        "event": "plans_to_move",
        "destination": "Tokyo"
      },
      "rendered_text": "联系人计划下个月搬到东京",
      "importance": 0.82,
      "confidence": 0.88,
      "evidence": [
        {
          "message_revision_id": "revision-id",
          "evidence_role": "explicit_statement",
          "quoted_span_start": 0,
          "quoted_span_end": 9
        }
      ],
      "visual_only": false
    }
  ],
  "no_change_reason": null
}
```

也可以：

```json
{
  "schema_version": 1,
  "proposals": [],
  "no_change_reason": "no_long_term_value"
}
```

Memory Agent 的输出首先保存为 proposal，不能直接修改正式记忆。应用层负责验证 JSON Schema、证据消息、联系人范围、时间范围、引用关系和幂等键，然后在数据库事务中执行 create、update、supersede、invalidate 或 merge。

验证层不重新总结或改写模型语义。默认只有 `confidence >= 0.85`、证据可信、没有未解决冲突且全部确定性校验通过的结果可以自动接受；`0.60–0.85`、image-only 或歧义冲突保留为 candidate；低于 `0.60` 拒绝。不同记忆类型可以提高门槛，不能让模型自报高 confidence 绕过证据规则。candidate 不进入 Main AI 的正式长期记忆上下文，也不生成正式 memory embedding。

正式 memory 最终必须递归追溯到未删除的 canonical message revision。AI 和 proactive outgoing 只能证明系统实际发送过的内容或承诺，不能单独证明联系人事实、偏好或真人风格；已编辑的 `copilot_approved` 文本只能作为低于纯 `human` 的风格证据，未编辑草稿不作为真人风格证据。

---

# 15. 记忆类型

至少分为以下类型。

## Identity Memory

关于 AI 本人的稳定身份。

例如：

```text
职业
经历
兴趣
重要身份信息
```

---

## Relationship Memory

联系人关系。

例如：

```text
Bob 是大学同学。
认识 5 年。
平时聊天比较随意。
```

---

## Fact Memory

事实型记忆。

例如：

```text
Bob 有一只猫。
Bob 去年去了日本。
```

---

## Preference Memory

偏好。

例如：

```text
Bob 不喜欢太正式的语气。
Bob 喜欢摄影。
```

---

## Event Memory

事件。

例如：

```text
Bob 8 月 15 日面试。
```

---

## Intention / Promise Memory

承诺和待办。

例如：

```text
我答应晚上把文件发给 Bob。
```

---

## Style Memory

真人本人的表达方式。

例如：

```text
习惯短句。
很少用长篇解释。
喜欢使用“行”“嗯”“可以”。
```

---

# 16. Canonical 消息历史

所有受支持的 Telegram 消息先归一化、去重并按 revision 保存到：

```text
messages
```

除 Telegram delete、memory/contact/account 删除策略和显式 retention 边界外，canonical message 不自动过期，也不依赖 LLM 的总结版本。完整 Telegram raw update 不属于这份长期历史。

这样以后：

- Memory Agent 判断错了可以重新处理
- 可以重新生成 summary
- 可以更换 embedding 模型
- 可以更换主模型
- 可以审计历史

已归一化、可追踪 revision 的 canonical message history 是系统的 source of truth；默认不长期保存完整 Telegram raw update payload。

---

# 17. Structured Memory

长期结构化记忆可以类似目录管理：

```text
memory/

identity
profile
relationships
preferences
facts
events
intentions
style
summaries
```

概念上也可以表现为：

```text
identity.md
profile.md
relationships/
facts/
events/
daily/
weekly/
```

实际实现可以存 PostgreSQL。

---

# 18. Summary 系统

随着历史增长，需要持续压缩。

例如：

```text
Canonical Conversation
       |
   Daily Summary
       |
   Weekly Summary
       |
 Long-term Summary
```

建议层级：

```text
recent canonical messages
daily summary
weekly summary
relationship summary
long-term profile
```

旧聊天不需要每次完整送给主模型。

rolling summary 默认在 watermark 之后新增 50 条 eligible revision 或估算约 12000 tokens 时触发。daily/weekly 使用 contact override、account timezone、部署默认 timezone 的顺序选择有效 IANA 时区，并保存时区和 UTC period boundary 快照；时区之后变化不默认重切历史。

每个 immutable summary version 除覆盖 event range 外，还必须保存 ordered source membership，明确引用 message revision 或 prior summary version。summary 是派生上下文，不是独立事实源；正式 memory 即使引用 summary，也必须通过 source membership 递归到仍有效的 canonical revision。

迟到 event、edit 或 delete 会隔离受影响 current summary，重新生成对应 daily/weekly/rolling version，并让依赖旧 summary 的 embedding 和下游 summary 失效。summary version、source membership、current pointer 和 watermark 必须在同一事务提交。

---

# 19. Embedding / Vector Memory

Embedding 用于语义联想。

例如当前消息：

```text
最近工作还是挺累。
```

向量检索可以找到三个月前：

```text
刚换工作，有点担心适应不了。
```

即使关键词不同，也能建立语义关联。

Vector Memory 主要解决：

> “以前有没有发生过和现在类似的事情？”

V1 为 active memory current version、active summary current version 和允许检索的 current canonical text/caption chunk 建立 embedding；candidate、superseded、invalidated、forgotten、redacted 内容以及原始图片像素不进入 active vector retrieval。

每个 embedding space 固定模型、配置版本、维度、距离度量、归一化和 chunker generation。更换模型时创建独立 shadow space，完成全量构建、delta catch-up、数量/维度/hash 验证和抽样检索后原子切换；旧、新 space 的向量分数不得混合排序。

---

# 20. Vector Memory 与 Structured Memory 的区别

Structured Memory 负责：

```text
明确事实
稳定信息
联系人状态
事件
承诺
人格
```

Vector Memory 负责：

```text
模糊联想
语义相关
旧经历检索
类似事件
```

两者同时存在。

不能完全依赖 embedding。

---

# 21. Memory 生命周期

canonical message 提交以后，实时聊天和记忆处理分成两条流水线。记忆流水线按合并后的 conversation episode 工作：

```text
Canonical Message Revisions
     |
Pending Memory Job
     |
Memory Agent
     |
Memory Proposal
     |
Schema / Evidence / Scope Validation
     |
+----+-------------------------+
|                              |
Reject or Candidate         Commit in Transaction
                               |
                 +-------------+-------------+
                 |             |             |
          Structured Memory  Embedding  Event/Intention
```

Episode extraction 负责事实、偏好、事件、承诺和风格候选；rolling summary 在 50 条 revision 或约 12000 tokens 阈值时运行；daily/weekly consolidation 定期合并重复记忆、处理冲突和更新淡化特征。edit/delete、forget/purge 和 evidence source 变化进入不等待安静窗口的 reconciliation。

每个任务都记录覆盖的 event 范围、ordered input membership、manifest hash、pipeline/policy/prompt/schema/model config 版本。相同会话、相同输入和版本重复执行时必须幂等。定期补偿任务通过 contiguous memory watermark 查找未处理区间；存在 hole 时不能越过推进。

Memory freshness 分为：

```text
fresh      watermark 覆盖最新 eligible event，且无 reconciliation
degraded   积压不超过 20 条且最早未处理不超过 10 分钟
stale      超过上述任一阈值、出现 dead-letter 或 derived data 被隔离
```

Main AI 永不等待 Memory Agent。degraded/stale 时 Context Builder 在预算内扩大 watermark 之后的 recent canonical window，记录省略范围和原因，但不得读取 candidate 或 quarantined summary。

---

# 22. 记忆冲突

例如旧记忆：

```text
Bob 喜欢咖啡。
```

新消息：

```text
最近把咖啡戒了。
```

Memory Agent 可以提出：

```json
{
  "action": "supersede",
  "old_memory": "...",
  "new_memory": "Bob 已经停止喝咖啡"
}
```

validator 只有在新证据明确表达时间变化或纠正时才自动 supersede：

```text
active=false
superseded_by=id
```

保持历史变化轨迹。但 Telegram delete、memory forget、contact purge 或 account wipe 触发的内容 redaction 具有更高优先级，旧版本不得借“历史审计”继续保留已要求清除的正文。

相同 typed proposition 只合并证据或创建 update；不同条件/时间可以同时成立时补充 qualifiers；无法确定是变化、例外还是矛盾时保持旧 memory active，将新 proposal 置为 candidate 并建立 `contradicts` relation，不让模型静默覆盖。

---

# 23. 记忆淡化

记忆可以随时间降低检索权重。

例如：

```text
importance
confidence
recency
usage_count
```

综合决定检索优先级。

时间淡化不原地修改 immutable version 的 importance/confidence，也不自动删除事实。event/intention 到期或完成只改变 current relevance/status；明确新证据可以 supersede 或 invalidate。

但重要的稳定人格信息：

```text
身份
重要关系
核心偏好
重要经历
```

不应该因为时间简单删除。

显式 `/forget <memory>` 才清除该 stable memory 的所有 payload、rendered text、content-derived hash 和 embedding，并写 erasure ledger；默认不删除作为 source of truth 的 canonical message。contact purge 和 account wipe 是更大范围的独立操作。

---

# 24. Time / Proactive Agent

Proactive Pipeline 的规范见 `docs/architecture/07-proactive-pipeline.md`。其目标是在明确规则候选、有限频率和最终发送门禁内判断“现在是否值得主动联系”，而不是让模型自由浏览所有联系人或决定Telegram副作用。

`worker`运行Scheduler、候选规则、Time Context Builder、Proactive Agent和Main AI生成任务；`app`在conversation lock内执行最终门禁并独占Telethon发送。Proactive Agent不负责最终自然语言表达，也不能发明联系人、事件或候选原因。

---

# 25. Proactive Agent 的时间维度

规则层同时使用absolute、relationship、event、promise、conversation和social time，但只物化以下V1 reason：

```text
promise_due
event_upcoming
event_followup
relationship_reconnect
explicit_followup
```

来源必须是accepted active memory投影、explicit event/intention或已提交relationship state；模型不能把模糊聊天自动升级为发送理由。联系人timezone按contact override、account timezone、deployment timezone依次解析，保存IANA zone和UTC窗口；DST gap/fold使用确定且fail-closed的转换规则。

最近30分钟存在incoming、已对账outgoing、active turn/draft/delivery或真人接管时抑制主动行为。reaction、service metadata和无正文技术重试不刷新这一activity窗口。

---

# 26. Proactive Agent 不直接读取全部数据库

为了控制 token 和稳定性，在 Memory System 和 Proactive Agent 中间加入：

**Time Context Builder**

它只为已经通过确定性筛选的candidate构造text-only压缩输入，包括时间、关系快照、typed reason/evidence、预算状态、最近主动结果、最多8条structured memory和4条semantic memory。它不读取全量历史、图片、未确认outgoing、candidate/quarantined memory、被删除正文或任意数据库自由查询结果。

每次输入保存可重建manifest：canonical ID、revision/version、source actor、trust、选择理由、token estimate、policy/config/prompt版本和fingerprint。正文和隐私数据不进入普通日志。

---

# 27. Proactive Tick

Scheduler使用两个OR入口：精确的durable due job和默认每15分钟的补偿扫描。两者只物化仍在有效window内的stable occurrence；服务恢复不会补发已错过窗口的主动消息。只有明确reason、有效evidence和所有前置规则通过时才调用Proactive Agent：

```text
Scheduler
    |
SQL / Rules Candidate Filter
    |
No Candidate -> Stop
    |
Time Context Builder
    |
DeepSeek v4 Flash
    |
Decision
```

不能对所有联系人无条件调用模型。Occurrence、candidate membership、decision、one-time defer、reservation、draft和delivery group分别使用版本化HMAC幂等键及数据库唯一约束，避免Scheduler、worker重试、租约重领或并发触发重复生成/发送。

输出：

```json
{
  "action": "send_now",
  "priority": 0.81,
  "selected_occurrence_ids": ["..."],
  "decision_code": "event_followup",
  "topic": "job_interview",
  "defer_until": null
}
```

或者：

```json
{
  "action": "none",
  "selected_occurrence_ids": [],
  "decision_code": "not_natural_now",
  "topic": null,
  "priority": 0,
  "defer_until": null
}
```

第三种合法动作是`defer_once`：只能在同一candidate window内延期一次，到期后不再次调用Proactive Agent，只重跑确定性/授权门禁。

---

# 28. 主动消息最终生成

Proactive Agent 只决定：

```text
在已有candidate中是否说
在已有window中现在说或只延期一次
选择哪个typed reason和topic
```

Main AI 负责：

```text
具体怎么说
```

Main AI使用独立`purpose=proactive_final`的text-only最小上下文生成非空纯文本；不使用current reactive turn、图片、tool或reply target。最终delivery group引用Main AI run，decision单独引用Proactive Agent run，以保持人格生成与时间判断的来源可审计。

Main AI完成不等于获准发送。`app`在conversation lock内重新检查account control、mode、contact policy、activity、timezone/quiet、evidence、window、budget reservation和所有version snapshot。任一变化在首项副作用前都skip并释放reservation，不自动重新生成。

---

# 29. 主动消息预算

必须避免 AI 变成骚扰系统。

默认策略维护：

```text
proactive_enabled
daily_limit
minimum_interval
relationship_level
quiet_hours=22:00-08:00
absolute_no_send=00:00-07:00
account_daily_limit=10
```

例如：

```text
close: daily 2, minimum interval 6h, reconnect 3d
friend: daily 1, minimum interval 12h, reconnect 7d
acquaintance: daily 1, minimum interval 24h, reconnect disabled
```

预算在Main AI调用前以数据库事务原子reservation，分别锁定account当地日、contact当地日和可选bypass bucket；第一项Telegram副作用commit一次，明确的副作用前失败释放，send-unknown或partial保守计一次。

Quiet例外仅允许`importance >= 0.90`的显式`event_upcoming`或`promise_due`，且最晚有用时间早于08:00；只可在`22:00-24:00`或`07:00-08:00`使用，每联系人当地日最多一次。`00:00-07:00`绝对禁发；relationship reconnect和一般follow-up永不绕过。

---

# 30. 主动消息状态

模式行为固定为：

```text
AUTO -> 全部门禁后自动发送
COPILOT -> 只创建一个待管理员审批的proactive draft
HUMAN / temporary HUMAN / PAUSED / maintenance / BLOCKED -> 不运行模型、不补跑
```

主动消息实际发送后：

```text
role=assistant
source=proactive_ai
```

之后它和正常聊天一样成为历史的一部分，但`proactive_ai`只能证明系统确实发送过该内容/承诺，不能单独证明联系人事实或真人风格。COPILOT批准消息保留`copilot_approved`及proactive provenance。

因此下一次 AI 会知道：

> “我之前主动问过他这个问题。”

Decision审计保存reason/evidence IDs与versions、membership hash、policy/timezone/mode/content/activity snapshots、两次model run/config/prompt versions、quiet/budget/final-gate结果、draft/group和terminal outcome；不保存chain-of-thought、API key或普通日志中的完整私密正文。

---

# 31. 真人行为学习

真人本人产生的数据价值非常高。

例如 AI 草稿：

```text
好的，我之后看一下。
```

真人实际发送：

```text
行 晚点看
```

长期积累以后 Memory Agent 可以提取：

```text
真人偏好：
短句
口语
较少使用完整书面句
```

这些内容更新：

```text
style profile
```

这样主模型会逐渐接近真人表达方式。

---

# 32. 模型分工

以下模型是当前默认配置。具体端点、模型名称和生成参数由运行时模型配置决定，不作为业务代码常量。

## Main AI

```text
GPT 5.6 Luna
```

职责：

- 最终聊天
- 人格
- 复杂推理
- 情绪表达
- 主动消息生成
- 高层决策

调用频率相对较低，但每次价值高。

---

## Memory Agent

```text
GPT 5.4 nano
```

职责：

- 记忆抽取
- 事实整理
- Summary
- 冲突判断
- 记忆更新
- Embedding 调度
- 时间相关信息抽取

调用频率高。

---

## Proactive Agent

```text
DeepSeek v4 Flash
```

职责：

- 时间感知
- 事件判断
- 关系时间
- 承诺检查
- 主动性评分
- 是否需要触发 Main AI

周期调用。

## 运行时模型配置

三个生成模型默认都使用 `openai_responses`，因为当前预设模型只需要 Responses 请求与响应格式。Chat Completions 和 Messages 作为可选兼容 adapter。

每个逻辑角色绑定自己的活动 ModelProfile 版本，例如：

```json
{
  "logical_role": "main_ai",
  "protocol": "openai_responses",
  "endpoint": {
    "base_url": "https://api.example.com/v1"
  },
  "model": "configured-model-name",
  "credential_ref": "credential-id",
  "generation": {
    "temperature": 0.8,
    "max_output_tokens": 1200
  },
  "timeout_seconds": 90,
  "protocol_options": {},
  "config_version": 3,
  "enabled": true
}
```

## Canonical 配置与协议字段映射

数据库和 Agent 代码使用统一语义字段，不直接保存某个 Provider 的实际请求字段名。内部请求至少包括：

```text
system_instructions
messages
temperature
max_output_tokens
response_schema
stream
```

`max_output_tokens` 始终表示单次生成允许的最大输出 token。输入上下文预算和模型上下文窗口由 Context Builder 与模型能力信息分别管理，不能与输出上限混为一项。

协议 adapter 在发送时完成字段映射：

| protocol | 默认路径 | 系统指令 | 对话输入 | 输出上限 wire 字段 | 主要响应位置 |
|---|---|---|---|---|---|
| `openai_responses` | `/responses` | `instructions` | `input` | `max_output_tokens` | `output` / `output_text` |
| `openai_chat_completions` | `/chat/completions` | `messages` 中的 system/developer message | `messages` | `max_completion_tokens`；兼容端点可选 `max_tokens` | `choices[0].message` |
| `anthropic_messages` | `/messages` | 顶层 `system` | `messages` | `max_tokens` | `content` blocks |

鉴权 header、请求路径和 Provider 版本 header 也由 endpoint 与 protocol adapter 生成，不由 Agent 拼接。

协议特有字段存入带类型的 `protocol_options`，使用 protocol 作为 discriminator，而不是允许任意请求 JSON。例如：

```text
ResponsesOptions
ChatCompletionsOptions
MessagesOptions
```

其中 Chat Completions adapter 可以配置：

```text
token_limit_field:
auto
max_completion_tokens
max_tokens
```

Messages adapter 可以配置 API version、认证方式和经过验证的请求路径。任意路径 override 仍必须经过 endpoint URL 与 SSRF 规则验证。

三个 adapter 的响应必须归一化为统一结果：

```text
text
structured_output
finish_reason
input_tokens
output_tokens
provider_request_id
diagnostic_capture_ref?
```

`diagnostic_capture_ref` 只在服务器显式开启加密、强制 TTL 的 debug capture 时存在。Agent 和 Memory proposal 逻辑只能读取归一化结果，不能依赖 `choices`、`content blocks` 或 Responses output item 等 wire 结构。

Control Bot 根据 protocol 只提供可用的字段和输入选项。切换 protocol 时创建新 draft，保留语义相同的 canonical 字段，重新校验协议特有字段、endpoint 和 credential；旧活动版本在新 draft 验证并激活前继续生效。Web App 不参与 protocol 或其他非密钥字段配置。

不同模型和 Provider 支持的参数不同，因此模型适配器必须声明能力：

```text
supported_protocols
supports_temperature
supports_structured_output
supports_streaming
supports_reasoning_effort
max_context_tokens
max_output_tokens_limit
supported_input_roles
supports_developer_role
supports_images
supported_image_mime_types
supported_image_detail_modes
max_images_per_request
image token estimator / reserve
Chat token limit fields
```

当模型不支持 `temperature` 或其他参数时，配置界面应禁用该字段或保存为 `null`，请求适配器不得强行发送不受支持的参数。Admission-critical capability 未知时配置验证失败，不能把 unknown 当作支持。

Chat Completions 的 `token_limit_field=auto` 在 draft validation 期间通过 catalog 或无私人数据 probe 固定为 `max_completion_tokens` 或 `max_tokens`，并写入不可变 capability/config snapshot。正式 model run 不允许因为 unsupported parameter 临时换字段重试。

非密钥配置与凭据分开保存。业务代码只能通过 credential reference 获取解密后的短生命周期内存值，不能查询或输出完整 API key。

每次模型调用需要记录：

```text
logical role
provider
protocol
endpoint id
model name
config version
prompt version
timeout
实际使用的非敏感生成参数
```

审计记录不得包含 API key。

Provider stream 可以在 adapter 内部用于降低延迟和响应 cancel，但只有完整 stream 结束、响应归一化和 purpose-specific schema 校验通过才算成功。Main AI V1 只接受最终文本且不执行 tool/function call；Memory/Proactive 必须得到完整 strict structured output。

Main AI 完整文本超过 Telegram 单条 policy 时，由 versioned deterministic splitter 生成一个 `outbound_delivery_group` 和多个 ordered intents。所有 intent 在发送第一段前原子创建，各自持有稳定 `telegram_random_id`；崩溃或 FloodWait 只恢复未确认段落，不重新调用模型或重新切段。

---

# 33. Embedding 模型

Embedding 建议独立。

Memory Agent 不负责真正“生成向量”。

流程：

```text
Accepted active memory / summary / eligible message revision
    |
Embedding eligibility + source hash
    |
Independent Embedding ModelProfile
    |
embedding_space + embedding_records in PostgreSQL/pgvector
```

这样未来可以单独替换 embedding 模型。

每个 embedding model/config/version/dimension/metric/chunker generation 对应独立 `embedding_space`。更换模型时创建 shadow space，对所有 eligible target 幂等重建、补齐 build snapshot 后的 delta、验证数量/维度/source hash 和抽样检索，再原子切换 active binding。构建失败时旧 active space 继续服务，不能在同一检索排序中混用不同 space 的向量。

proposal candidate、旧/invalidated/forgotten/redacted version 和 raw image pixels 不生成 active vector memory。edit/delete/forget 时先把关联向量标记 invalidated，使查询立即排除，再异步物理删除。

---

# 34. 数据库设计

使用PostgreSQL + pgvector。逻辑模型、约束和migration见`docs/architecture/03-data-model.md`；部署、retention、pgBackRest/WAL、restore和资源参数见`docs/architecture/08-operations.md`。

身份采用全局 `telegram_peers` 与账号级 `account_peers` 分离，所有主要业务表显式保存 `account_id` 并使用 composite foreign key 防止跨账号关联。核心业务实体使用 UUIDv7，高吞吐 append-only event/attempt/audit 使用 BIGINT identity。

消息、记忆和summary正文在PostgreSQL中使用可查询明文列，部署必须使用加密磁盘和加密off-host backup。PgBackRest持续WAL、每日differential、每周full，目标DB RPO 15分钟；恢复后最新erasure ledger覆盖旧快照，全部校验前保持bootstrap maintenance。Telegram/provider raw payload默认不保存；diagnostic默认关闭，显式启用最多1小时。

消息 current projection 与 `message_revisions` 分离：edit 保留历史 revision；Telegram delete 时清除所有 revision 中的正文和媒体，并隔离、重建或擦除依赖该证据的记忆、summary 与向量，只保留 tombstone 与必要审计元数据。memory forget、contact purge 和 account wipe 是三个不同的 durable operation。

核心表族：

```text
identity:
  accounts, telegram_peers, account_peers, contacts, conversations

message lifecycle:
  message_events, messages, message_revisions, media_objects,
  message_media, message_reactions, conversation_turns, turn_messages,
  context_manifests, model_runs, copilot_drafts,
  copilot_draft_revisions, outbound_delivery_groups, outbound_intents

conversation control:
  account_orchestrator_states, account_control_history,
  conversation_mode_history, control_commands, orchestrator_blocks,
  copilot_action_tokens, copilot_edit_sessions

durable work:
  background_jobs, transactional_outbox, domain watermarks

memory:
  memories, memory_versions, memory_proposals, memory_proposal_targets,
  memory_proposal_evidence, memory_evidence, memory_relations,
  memory_jobs, memory_input_manifests, memory_input_manifest_items,
  memory_watermarks, summaries, summary_versions, summary_version_sources,
  summary_watermarks, embedding_spaces, embedding_records,
  memory_review_actions

proactive and state:
  life_events, intentions, relationship_states,
  proactive_decisions, agent_states, service_status_events

model control:
  model_endpoints, model_profiles, model_config_drafts,
  model_config_versions, model_credentials,
  model_credential_versions, model_capability_snapshots,
  control_input_sessions, model_key_launch_sessions,
  model_key_rate_limits,
  context_policies, context_policy_versions,
  retrieval_policies, retrieval_policy_versions, prompt_versions

governance:
  context_preview_requests, context_preview_tokens,
  context_preview_deliveries, audit_log,
  data_erasure_requests, erasure_ledger,
  data_export_requests, debug_payload_captures
```

普通状态机使用 `TEXT + CHECK`，安全关键字段不得隐藏在 JSONB。JSONB 只用于带 schema version 和大小限制的 Telegram/provider 扩展、protocol options 和 typed memory payload。

---

# 35. messages

示例字段：

```text
id
account_id
telegram_message_id
conversation_id
direction
role
source
source_status
current_revision_no
grouped_id
reply_to_telegram_message_id
telegram_created_at
edited_at
deleted_at
metadata
```

正文版本：

```text
message_revisions:
id
account_id
message_id
revision_no
body_kind
text_content / caption
entities
content_sha256
source_event_id
created_at
redacted_at
```

消息业务唯一键至少包含：

```text
account_id
chat_id
telegram_message_id
```

重复 Telegram update 必须命中同一记录并进行幂等更新。

消息通过独立 event/revision/media 记录表达 edit、delete tombstone、album membership、媒体验证状态和可重放投影。`message_events` 不复制正文；正文位于可单向 redaction 的 `message_revisions`。V1 的正文与自动回复范围、业务键和稳定排序规则由 `docs/architecture/02-message-lifecycle.md` 固定，物理结构由 `docs/architecture/03-data-model.md` 固定。

## outbound_delivery_groups / outbound_intents

一个完整 logical output 创建一个 delivery group；即使短文本也有 group，长文本按 deterministic splitter 生成多个 intents。Group 核心字段包括：

```text
id
account_id
conversation_id
source
state
account_control_version_snapshot
mode_version_snapshot
content_revision_snapshot
turn_id
proactive_decision_id
copilot_draft_id
approved_draft_revision_id
generation_no
logical_content_hash
normalizer_version
splitter_version
chunk_count
reconciled_chunk_count
```

每个 chunk 的 intent 保存：

```text
id
delivery_group_id
chunk_ordinal
chunk_count_snapshot
content_hash
telegram_random_id
telegram_message_id
attempt_count
created_at
sent_at
reconciled_at
last_error
```

`source` 在发送前已确定为 `ai`、`proactive_ai` 或 `copilot_approved`。COPILOT group 必须绑定唯一 draft 和 approved revision。Group 和全部 intents 在首段发送前原子持久化；每个 intent 有独立稳定 random ID，监听器逐段对账。部分失败只恢复未确认 ordinal，不能重新切段或从头发送。

---

# 36. memories

```text
memories:
id
account_id
contact_id
conversation_id
memory_type
status
current_version_no
superseded_by_memory_id
created_at
updated_at
forgotten_at

memory_versions:
id
account_id
memory_id
version_no
operation
payload
rendered_text
importance
confidence
valid_from
valid_to
model_run_id
prompt_version
created_at
redacted_at
```

`memories` 只保存稳定 identity 和 current version pointer，实际内容位于不可变 `memory_versions`。正式版本必须通过 `memory_evidence` 关联一个或多个来源 revision；没有证据链的模型输出不能直接进入正式长期记忆。forget 默认清除该 memory 的 payload、rendered text 和 embedding，但不等于删除来源消息。

Memory Pipeline 还需要：

```text
memory_watermarks:
  conversation_id, watermark_kind,
  last_scanned_event_id, last_contiguous_decided_event_id, version

memory_input_manifests/items:
  job generation, range, ordered typed source IDs,
  source hashes, trust/inclusion role, pipeline/prompt/schema/config versions

memory_proposals/targets/evidence:
  operation, target set, confidence, importance, time interval,
  visual_only, validation/decision state and policy version

summary_version_sources:
  ordered message revision or prior summary membership
```

这些记录使 episode、summary 和人工 candidate review 可重放。proposal candidate 仍是待审模型输出，不等同于 `memories.status=active`；只有 acceptance transaction 同时写入 version、evidence/relation、current pointer 和 embedding outbox 后才成为正式记忆。

---

# 37. life_events

```text
id
contact_id
title
start_time
end_time
importance
status
source_memory_id
```

---

# 38. intentions

```text
id
contact_id
owner
content
expected_time
status
importance
```

`owner` 可以是：

```text
self
contact
```

表示是谁的承诺。

---

# 39. relationship_states

```text
contact_id
relationship_level
last_contact_at
last_user_message_at
last_assistant_message_at
last_proactive_at
interaction_frequency
```

---

# 40. agent_states

安全关键的 conversation control 不放入 generic agent state；它使用 typed `account_orchestrator_states`：

```text
default_base_mode
global_paused
maintenance_state
temporary_takeover_enabled / seconds
resume_pending_policy
control_version
```

`agent_states` 只保存低频、非发送门禁的可解释拟人格状态，例如：

```text
current_social_energy
```

主动消息预算和全局 proactive 开关同样应使用 typed policy/budget 表，而不是任意 JSONB。拟人格状态应该保持简单和可解释。

---

# 41. Vector DB

可以使用：

```text
pgvector
```

这样 PostgreSQL 本身就可以同时承担：

- 普通数据库
- embedding vector storage

初期不一定需要单独部署 Pinecone、Milvus 等系统。

这可以显著降低复杂度。

---

# 42. Context Builder

Context Builder 是独立 domain service，不直接发送 HTTP。它读取 purpose、turn、active memory/summary、freshness、模型能力和所有版本快照，选择 source 后先持久化不可变 manifest，再由 provider adapter 映射 wire request。

固定顺序：

```text
trusted system/developer instructions
manual identity instructions
identity/personality data
relationship/current time data
structured memory
semantic memory
summary
recent canonical history
current turn
```

Structured memory 使用 current active version、validity、scope、importance、confidence、freshness、source quality 和 topic relevance 确定性排序，默认最多 12 项。Semantic retrieval 只查一个 active embedding space，ANN 取候选后按精确 distance 和固定公式重排，默认最多 8 项。跨层命中同一 source root 时正文只放一次，所有选择理由仍进入 manifest。

Recent selector 从 head 向前选取、最终按时间顺序装配，保持 reply、album 和 revision 边界。Memory `degraded/stale` 时 Main AI 不等待 worker，而是在预算内扩大 watermark 后的 canonical range；删除/隔离原因导致 stale 时先排除受影响 derived item。

---

# 43. 上下文预算

V1 默认：

```text
max_input_tokens = 24000
safety_reserve = max(1024, 5% of model context window)

current turn                         20%
recent canonical                    30%
identity/personality/relationship   15%
structured memory                   15%
semantic memory                     10%
summary                             10%
```

配额是软限制，借用顺序固定并版本化。Trusted instructions、framing 和 `detail=auto` 图片 reserve 都计入输入预算；`max_output_tokens` 单独从模型窗口中预留。Current turn 不做静默字符截断；单独超预算时不调用 provider。

Token estimator 优先使用匹配模型的 tokenizer，否则使用带 10% margin 的 versioned conservative fallback。Manifest 保存估算分解，run 保存 provider actual usage；估算偏差只能触发校准和告警，不能自动扩大预算。

---

# 44. Cache

因为个人 AI 大量 Prompt 内容重复，因此缓存是重要的成本优化手段。

建议至少四层缓存。Redis 和 provider prompt cache 都只负责加速，PostgreSQL 的 source/version/manifest 才是正确性事实源；cache hit 后仍需验证 current pointer、delete/redact、scope 和 policy version。

## L1 Identity Cache

几乎长期不变：

```text
identity
personality
core style
```

长时间缓存。

---

## L2 Relationship / Profile Cache

更新较慢：

```text
关系摘要
联系人基本信息
长期背景
```

---

## L3 Conversation Cache

最近会话。

生命周期较短。

---

## L4 Retrieval Cache

Embedding 检索结果。

相同或近似主题短时间内可以复用。

---

# 45. Prompt Cache 设计原则

为了提高缓存命中率，Prompt 尽量稳定排序。

例如固定：

```text
SYSTEM
IDENTITY
PERSONALITY
RELATIONSHIP
MEMORY
RECENT
USER
```

不要每次随机改变前缀内容。所有稳定排序和序列化规则都有 `builder_version`，不能为了提高 cache hit 而省略 source boundary、trust label 或删除门禁。

稳定部分尽量放在 Prompt 前方。

动态内容放后方。

这样更利于 provider 侧 prompt caching。

---

# 46. 调度

Operations规范见`docs/architecture/08-operations.md`。生产基线固定为Ubuntu Server 24.04 amd64上的：

```text
Docker Compose
2 vCPU / 4 GiB RAM / 40 GiB SSD
```

初期运行拓扑：

```text
https-gateway
app
control
worker
postgres
redis
migrate（一次性任务）
session-backup（ops profile 一次性任务）
data-export（ops profile 一次性任务）
```

其中：

- `https-gateway`使用Caddy，只把key-only Web App和credential API通过公网443暴露；不开放80、health、metrics或管理API。
- `app` 固定一个副本，独占挂载的 Telethon `.session` 文件。
- `control` 独立运行 Control Bot、Web App 和状态查询；不挂载 Telethon Session。
- `worker`初期concurrency 2，使用arq/Redis dispatch；durable job/outbox/watermark始终在PostgreSQL。
- Scheduler 位于 `worker` 中，通过专用 PostgreSQL 连接持有 advisory lock，保证同一调度任务只有一个发布者。
- `postgres` 和 `redis` 只加入 Compose 内部网络，不映射公网端口。
- `migrate`在业务服务启动或升级前执行Alembic，成功后退出。
- PostgreSQL通过pgBackRest持续WAL/每日differential/每周full备份；Session每日和升级前用独立restic repository加密备份。
- Data export使用维护者age public key加密到root-only staging，只通过既有SSH/SFTP取回，24小时内删除；不扩展key-only Web App。

Ubuntu 原生运行只作为开发和故障排查手段，不作为与 Docker Compose 对等维护的一等部署方式。

Telethon`.session`保存在`app`专用volume。只有app停止并释放account lock时，one-shot backup/restore helper可只读挂载；常驻服务不得共享。

---

# 47. Redis

Redis 可以承担：

```text
消息队列
任务锁
缓存
短期状态
rate limit
```

V1使用arq。Redis配置AOF everysec、192 MiB数据目标和`noeviction`；queue payload只传durable IDs/version，不传正文或secret。Redis丢失后从PostgreSQL transactional outbox、jobs和watermarks恢复，AOF不是DR事实备份。

例如：

```text
Telegram message
   |
Queue
   |
AI Worker
```

避免消息并发导致同一联系人回复顺序错乱。

---

# 48. 同一会话锁

必须保证：

```text
chat_id
```

级别串行。

否则：

用户连续发：

```text
A
B
C
```

可能发生：

```text
AI回复B
AI回复A
AI回复C
```

因此应设置：

```text
conversation lock
```

锁必须具有 owner token 和过期租约，只有持有者可以续租或释放。worker 崩溃后租约可恢复，但锁本身不能代替数据库幂等键和发送前状态检查。

---

# 49. 消息防抖

Telegram 真人聊天通常连续发送多条：

```text
你今晚
有没有空
一起吃饭？
```

不要每条调用一次主模型。

默认使用 sliding 3 秒 debounce，将连续消息合并；从首条可触发 incoming 开始最多收集 10 秒，达到 hard cap 后必须 seal 当前 conversation turn。两个数值均可由服务器配置，并保存到 turn snapshot。

reaction、service message 和没有 caption 的 metadata-only 媒体不延长 debounce。album item、text 和 caption 属于可触发 content。

如果新的 incoming content 在模型生成期间到达，以本次 model run 的请求开始时间 `t0` 为基准：完整且验证通过的 API 结果在 `t0 + 3 秒` 前返回时，允许旧结果通过其他门禁后发送，新消息进入下一 turn；到该时刻仍未完成时，旧 turn 必须 supersede、尽力取消并与 pending incoming 合并重生成。首 token 或部分 stream 不算完成，这个 3 秒分界也不是模型的通用 timeout。

---

# 50. 真人与 AI 并发冲突

这是必须处理的问题。

例如：

AI 正在生成回复。

真人此时手动发送消息。

系统检测到：

```text
outgoing human message
```

系统可以尽力取消该 AI 回复任务，但不能依赖模型请求一定可取消。

否则可能出现：

```text
真人：
行，没问题

AI 2 秒后：
好的，我看看时间。
```

造成身份割裂。

因此主要保护规则是 account control version 与会话模式版本的组合门禁：

```text
AI 开始生成 -> 记录 effective=AUTO,
                account_control_version=A, mode_version=N
        |
真人 outgoing 或 Control Bot 切换模式
        |
mode_version += 1，并尽力取消请求
        |
AI 发送前重新读取 effective mode 和两个 version
        |
不是 AUTO 或 A/N 任一不匹配 -> 丢弃结果，不发送
```

account default、global pause、maintenance 和 account/model 级 block 递增 account `control_version`，不批量改写所有 conversation；联系人 override、contact pause、temporary HUMAN、真人 outgoing 和显式 cancel 递增 conversation `mode_version`。发送前检查应在 account snapshot + conversation lock 内完成。

新的联系人 incoming 与真人 outgoing 采用不同规则。新的 incoming 可以按第 49 节的条件式 3 秒策略决定旧结果是否仍可发送；真人 outgoing、Control Bot 模式切换、global pause 以及发送前 edit/delete 没有宽限，始终使旧结果失效。

AUTO 只在 sealed turn 真正开始生成时标记本 turn 的 incoming 已读并启动 typing，typing 持续到发送、取消或失败。HUMAN、COPILOT 和 PAUSED 不自动标记已读，也不发送 typing。

---

# 51. Human takeover 自动判断

除了控制 Bot 手动 `/human`，也可以设置：

真人只要在该会话发一条消息：

```text
自动进入 temporary HUMAN
```

例如保持：

```text
10 分钟
```

10 分钟没有新的 confirmed human outgoing 后结束覆盖层，恢复原 account default 或 conversation override。

这个策略可以做成可配置项。到期、resume 或 dependency 恢复都会把 automation reply floor 推进到当前 event watermark，因此不会自动回复覆盖期间积压的旧消息；需要时由管理员显式执行 `/reply_pending <contact>`。

V1 以 Control Bot 的手动模式切换为主要接管方式，自动 temporary HUMAN 默认关闭。

---

# 52. 安全

Telethon Session 等价于账号登录权限。

必须：

- 不上传 Git
- 文件权限严格限制
- Session 加密备份
- 服务器使用 SSH Key
- Ubuntu数据盘加密，普通swap禁用或使用zram；安全更新自动安装但不自动reboot
- 只有Caddy公网443，禁止暴露80、管理、health、metrics、PostgreSQL和Redis端口
- Control Bot 设置 User ID 白名单
- 数据库禁止公网直连
- Redis 禁止公网直连
- Web App 必须校验 Telegram `initData` 签名、时效和管理员身份
- API key 只能通过 HTTPS Web App 提交，禁止通过 Bot 消息提交
- API key 必须应用层加密保存，数据库密文与主密钥分离
- API key使用AES-256-GCM和版本化keyring；master/backup/erasure/diagnostic keys用途分离
- API key 不得出现在日志、异常、审计记录或配置读取响应中
- 模型端点必须经过协议、地址和 SSRF 安全校验
- incoming图片限制为20 MiB、40 MP、16384 px、30秒、单并发，写入私有10 GiB`media-data`；original 30天、provider copy 24小时且默认不备份
- 容器固定digest、非root、只读rootfs、drop capabilities、no-new-privileges，不挂载Docker Socket

---

# 53. Telegram 风控

Userbot 属于用户账号自动化。

需要避免：

```text
大规模陌生人私聊
广告群发
极高发送频率
自动批量加群
批量添加联系人
```

本项目定位为个人 AI 分身，相对更接近正常个人通信模式。

发送速率需要符合正常人类行为。

---

# 54. 日志与审计

日志只保存可重建的metadata和状态，不保存AI request/response正文：

```text
AI request/response IDs, versions, usage and result codes
memory changes
proactive decision
mode changes
control commands
errors
```

特别是主动行为：

```text
为什么主动发送
使用了哪些记忆
由哪个模型决定
```

必须可追踪。

服务进程需要定期发布最小化状态信息，例如：

```text
service name
instance id
started_at
last_heartbeat_at
readiness
last_successful_operation_at
queue lag
scheduler lease status
last model endpoint check
```

`/server_status` 只返回运维所需的摘要，不返回环境变量、路径、凭据、Prompt 正文或联系人隐私数据。

服务输出JSON stdout并由Docker轮换；metrics只在内网，以低基数聚合暴露。2/4/40基线不常驻完整Prometheus/Grafana/Loki，生产还需一个独立于Control Bot/Telegram的外部健康告警通道。Raw diagnostic默认关闭，显式启用最多1小时。

---

# 55. Proactive Log

Proactive审计以ID、受控code、hash和version为主，不在普通日志复制联系人名称、evidence正文或最终消息：

```json
{
  "decision_id": "...",
  "contact_id": "...",
  "candidate_membership_hash": "...",
  "action": "send_now",
  "reason_codes": ["event_upcoming"],
  "evidence_version_ids": ["..."],
  "priority": 0.82,
  "quiet_result": "normal_window",
  "budget_reservation_id": "...",
  "proactive_model_run_id": "...",
  "main_model_run_id": "...",
  "outbound_group_id": "...",
  "terminal_result": "reconciled"
}
```

自由文本topic/brief和生成正文是敏感derived data，只在受控业务表有限保留；API key、provider header/raw body和chain-of-thought永不记录。这样仍可重建决策和调整策略，而不把私聊内容扩散到日志系统。

---

# 56. 失败保护

如果 Main AI API 失败：

```text
不发送
```

不要发送异常内容，并将模型运行记录为失败。

如果进程在创建 outbound delivery group/intents 后、发送确认前崩溃：

```text
保留 pending / partial / unknown group 和 intent
启动后查询或等待 Telegram update 对账
按 chunk ordinal 与稳定 random ID 恢复
无法确认当前 chunk 前不发送下一段或盲目重复发送
```

如果 Memory Agent 失败：

```text
未被删除的 canonical message/revision 仍然保存
sealed range、input manifest、watermark 和 model run 状态仍然保存
任务按稳定错误分类重试或进入 dead-letter
默认每 5 分钟的补偿扫描重建遗漏范围
Main AI 使用已提交 memory，并在预算内扩大 watermark 之后的 recent canonical window
```

Memory freshness 为 stale 时不读取 candidate、quarantined summary 或失效 embedding，也不因为 Memory 恢复而自动回复 backlog。edit/delete/forget reconciliation 优先于普通提取和 summary；恢复数据库备份后先重放 erasure ledger，再开放 derived memory 检索。

如果 Proactive Agent 失败：

```text
同 logical run 只对明确 retryable provider failure 有限重试
schema/tool/length/malformed 失败不使用部分输出
candidate 过窗即 expire，不发送固定替代文本
reservation 或 final gate 不确定时 fail closed 并进入恢复/对账
```

基础设施恢复遵守以下硬边界：

```text
Redis 丢失 -> 从 PostgreSQL outbox/jobs/watermarks 重建
PostgreSQL restore -> bootstrap maintenance + 最新 erasure ledger
Session backup -> app 停止后的一致性加密快照
disk 90% -> 停止 media/proactive/低优先后台工作
disk 95% -> operational BLOCKED 并安全断开 Telethon
upgrade -> signed revision + exact digest + DB/Session backup + one-shot migration
```

数据库目标RPO为15分钟、整机目标RTO为2小时；在实际restore drill返回证据前只能称为目标，不能声明已达成。

主动系统的原则应该是：

```text
宁可少发，不要乱发。
```

---

# 57. 主动性最终安全阀

即使 Proactive Agent 输出：

```text
action=send_now
```

也要经过规则层检查：

```text
proactive enabled?
reason/evidence/window still current?
quiet hours and absolute 00:00-07:00 no-send?
exact high-importance bypass eligibility?
account/contact/bypass reservation valid?
daily limit and minimum interval?
chat currently active?
human mode?
blocked contact?
account/global/contact pause or maintenance?
account_control/mode/content/activity/policy/timezone versions still match?
no active turn/draft/delivery or human takeover?
```

AUTO满足preliminary gate并取得原子budget reservation后才进入Main AI；COPILOT只生成待管理员审批的草稿；HUMAN、temporary HUMAN、PAUSED、maintenance和BLOCKED不运行两个模型。Main AI或草稿审批完成后，`app`仍要在conversation lock内重跑最终门禁，才可原子创建outbound delivery group与全部ordered intents。任何首项副作用前的失效都skip且不自动重生成。

---

# 58. 项目最终运行逻辑

普通被动聊天：

```text
Telegram Incoming
      |
Conversation Engine
      |
Idempotent Save Canonical Message
      |
Refresh Pending Memory Job --------> Async Memory Pipeline
      |
Debounce / Check Mode
      |
Context Builder
Committed Memory/Summary + Freshness + Recent Canonical Messages
      |
Main AI
      |
Check account control + mode/content versions in Conversation Lock
      |
Create Outbound Intent source=ai
      |
Telegram Send
      |
Reconcile Telegram Message ID
```

真人回复：

```text
Telegram Outgoing Update
      |
Telethon Listener
      |
Match Existing Outbound Intent?
      |
Yes -> Reconcile AI Source and Stop Duplicate Processing
No  -> Classify source=human
      |
Idempotent Save Message
      |
Increment mode_version / Best-effort Cancel
      |
Refresh Pending Memory Job --------> Async Memory Pipeline
```

主动消息：

```text
Scheduler
   |
Durable Due Job OR 15-minute Compensation Scan
   |
SQL / Rules Occurrence Filter
   |
No Candidate ----------------------> Stop, zero model calls
   |
Aggregate Stable Candidate
   |
Time Context Builder
   |
Proactive Agent
   |
send_now / defer_once / none
   |
Preliminary Gate + Atomic Budget Reservation
   |
Main AI text-only proactive_final
   |
Resolve mode: AUTO sends, COPILOT creates approval draft,
HUMAN/PAUSED/maintenance/BLOCKED skips
      |
App Final Gate + account/mode/content/activity/evidence versions
in Conversation Lock
   |
   +-- AUTO --> Atomically Create Delivery Group + Ordered Intents
   |            source=proactive_ai
   |                |
   |            Telegram Send
   |                |
   |            Reconcile Telegram Message ID
   |
   +-- COPILOT --> Create Approval Draft
   |                   |
   |              Control Bot Send/Edit/Ignore
   |                   |
   |              Approved -> source=copilot_approved
   |
   +-- HUMAN/PAUSED/maintenance/BLOCKED --> Skip, no backlog
   |
Refresh Pending Memory Job --------> Async Memory Pipeline
```

---

# 59. 测试与证据策略

详细契约见`docs/architecture/09-test-strategy.md`。测试按风险分为unit/property、contract、PostgreSQL/Redis integration、Compose E2E、migration/recovery、live smoke、backup/restore和2/4/40 resource soak。

自动门禁使用synthetic-only数据、virtual clock、确定性random/ID、Telegram/provider fake、可重放update fixture、显式barrier和命名crash point。PostgreSQL约束、事务、锁、CAS、outbox、pgvector和erasure必须在真实PostgreSQL/pgvector上验证，不能用SQLite或mock替代。

真实Telegram只在隔离主机、专用授权测试账号和allowlisted测试peer上人工执行；真实provider smoke只发送固定无私人数据文本和测试图片。Session、API key、Bot token、生产对话和生产数据库不能进入公开仓库、普通runner或artifact。

CI分为：

```text
commit/MR preflight -> static + unit/property + contract
protected pre-merge/main -> PostgreSQL/Redis integration + migration smoke
nightly/manual -> Compose + race/crash + migration/recovery + security lab
release/operations-sensitive -> live smoke + backup/restore + 2/4/40 24h soak
```

全局line coverage门槛为85%，branch为80%；send/auth/credential/SSRF/erasure/proactive final gate等安全关键不变量必须有正例、反例、边界和race/crash case。失败不能通过自动重跑改为PASS。

每个验收项使用稳定requirement/test ID，并输出JUnit、coverage和content-free acceptance manifest。结果只能是`PASS`、`FAIL`、`NOT RUN`或`BLOCKED`；普通证据保留30天，release/restore/soak证据保留365天。

当前仓库已经完成M0—M6。M6实现异步OR trigger与补偿扫描、pending range/lease、content-free input manifest、strict proposal/evidence validator、versioned memory lifecycle、immutable summary membership/watermark、single active embedding space的shadow rebuild、metadata-only Control Bot review和one-way erasure replay。M5的context继续只选择active validated memory/summary并记录freshness；candidate、rejected、redacted和forgotten派生数据不可检索。M4的Control Bot只写command/outbox，app role执行编排状态变更并回写终态；M6 control role同样只写review action，不直接修改derived truth。默认入口仍只执行安全配置检查，不创建Telegram client、不读取Session、不启动Control Bot polling、Memory worker或Orchestrator，也不产生自动消息。

M0—M6已有绑定签名commit/tree的GitLab Linux evidence。M5签名提交`9e6aeaf3a50ff58826a6830492c766a7983da9b6`的pipeline [#2758537825](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758537825)共11个作业全部`PASS`。M6签名提交`9be7012edf3aabe1dd5db7a325b0f36efce27063`、tree `5e2c7bda8b96ea9bb58e9a5c66f27b22e8fb6a87`的pipeline [#2762159878](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2762159878)共13个作业全部`PASS`：M6 service执行329个测试、deselect 1个，line/branch coverage为91.98%/84.32%；migration manifest记录80张表、零匿名约束、四条migration路径`PASS`，M6-001—M6-012 acceptance全部`PASS`。Windows本机无Docker，M6 PostgreSQL/Redis integration仍为`NOT RUN`。真实Telegram/provider、完整runtime、Compose、Ubuntu production、真实backup/restore、live smoke与soak仍为`NOT RUN`；任何文档中的最终流程都不能被误述为当前已部署能力。

---

# 60. 最终哲学

系统中的人格并不存在于某一个单独模型中。

人格由以下内容共同形成：

```text
长期身份
+
历史行为
+
真人消息
+
结构化记忆
+
关系记忆
+
语言风格
+
时间连续性
+
主动行为
```

Main AI 只是运行时的推理和表达核心。

Memory Agent 提供长期连续性。

Proactive Agent 提供时间和主动性。

Telegram 真人账号提供稳定的社会身份。

真人本人不断产生的真实消息则持续修正 AI。

最终形成：

```text
真人
  ↕
AI Digital Twin
  ↕
同一个 Telegram 身份
```

随着使用时间增加，AI 应逐渐获得：

- 更准确的个人语言风格
- 更完整的人际关系记忆
- 更稳定的长期人格
- 更自然的时间感
- 更合理的主动沟通能力
- 更接近真人本人的判断模式

整个系统最终可以概括为：

**一个由真实 Telegram 用户账号承载，以 GPT 5.6 Luna 为人格与推理核心、GPT 5.4 nano 为长期记忆管理员、DeepSeek v4 Flash 为时间与主动性管理器，并结合结构化记忆、语义向量记忆、真人实时接管以及 Telegram Control Bot 的长期运行个人 AI 数字分身系统。**
