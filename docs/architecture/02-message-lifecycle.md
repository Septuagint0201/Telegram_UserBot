# Message Lifecycle

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的消息范围、事件归一化、状态机、并发门禁、媒体边界、投递语义和崩溃恢复规则。

总体产品目标见 `docs/Design.md`，运行组件和所有权见 `docs/architecture/01-runtime-topology.md`。数据库物理字段、具体队列库、媒体保留天数和部署参数由后续 Data Model、Operations 和 Test Strategy 文档细化。

当前状态：V1 架构基线。

## 2. 已确认决策

| 决策 | V1 选择 |
|---|---|
| 对话范围 | 只处理非 Bot 用户的一对一 private chat |
| 图片输入 | 下载 Telegram photo 和 MIME/实际内容均为图片的 document |
| 图片模型预算 | canonical `detail=auto` |
| 其他媒体 | 语音、音频、视频、video note、贴纸和非图片 document 只存元数据，不下载 |
| 出站媒体 | V1 只发送文本，不生成或发送媒体 |
| debounce | sliding 3 秒，首条消息起 hard cap 10 秒，均可配置 |
| 生成期间新消息 | API 在本次 run 开始后 3 秒内完整返回则允许旧结果发送，否则 supersede、尽力取消、合并后重生成 |
| AUTO read/typing | 生成开始时标记本 turn 消息已读，并维持 typing 至发送、取消或失败 |
| 非 AUTO read/typing | HUMAN、COPILOT 和 PAUSED 不自动已读，也不发送 typing |
| edit | 发送前使 turn 失效并重建；发送后只记录，不自动追发 |
| delete | 建立 tombstone，不自动追发；立即排除后续上下文并触发记忆对账 |
| reaction | 保存为 interaction event，不触发回复 |
| service message | 只保存必要元数据，不调用模型 |

本文中的 3 秒是新 incoming content 到达时采用的 supersede 分界，不是模型请求的通用 timeout，也不是从新消息到达时重新开始的 3 秒宽限。

## 3. 目标与非目标

本设计需要保证：

- Telegram update 重复、乱序或重放时不会重复回复。
- AI、proactive AI、COPILOT approved 和真人共享同一账号时，outgoing source 仍可确定并对账。
- 同一 conversation 同时只有一个有权创建自动回复副作用的协调者。
- 模型结果即使无法取消，也不能绕过 mode、revision 和 intent 门禁发送。
- 进程在持久化、模型调用、Telegram RPC 或确认阶段崩溃后可以安全恢复。
- 图片只在通过类型、大小和解码验证后进入模型，其他媒体二进制不落盘。
- Memory Pipeline 异步运行，不阻塞实时回复，同时 edit/delete 可以触发后续修正。

V1 不包含：

- 群组、supergroup、channel、forum topic、secret chat 或 Bot peer 的自动对话。
- 语音转写、音频理解、视频抽帧、OCR 专用流水线或贴纸理解。
- 模型生成图片、文件、语音或视频并发送到 Telegram。
- 对已发送 AI 回复进行自动撤回或自动补充说明。
- 在本阶段固定媒体字节、像素、保留期和重试次数的生产数值。

## 4. Peer 范围与摄取边界

### 4.1 支持的 conversation

只有同时满足下列条件的消息可以进入 Conversation Engine：

```text
peer_type = user
is_private = true
peer_is_bot = false
peer_id != managed_account_id
contact policy = allowed
```

每个支持的对话映射到：

```text
conversation_key = account_id + chat_id
```

`chat_id` 必须使用一种稳定的 canonical Telegram peer ID 表示，不得混用 raw user ID、marked peer ID 和临时实体对象。

### 4.2 不支持的 peer

group、supergroup、channel、topic、secret chat、Bot peer 和 self chat 的 update 仍可保存以下最小摄取元数据，用于审计过滤行为和推进 Telegram update watermark：

```text
account_id
peer_type
chat_id_hash 或受控内部 ID
event_kind
telegram_event_time
observed_at
ignored_reason
update_fingerprint
```

不支持的 peer 不保存消息正文、caption、文件名、forward 内容或媒体二进制，不创建 conversation turn，不标记已读，不发送 typing，也不调用模型。

## 5. 归一化事件模型

### 5.1 EventEnvelope

Telethon update 先归一化为不可变的事件 envelope，再由投影器更新 canonical message、turn 和 intent 状态：

```text
event_id
event_kind
account_id
chat_id
telegram_message_id?
grouped_id?
telegram_event_time
observed_at
update_fingerprint
ordering_key
payload_version
payload
```

`event_id` 是内部 ID。`update_fingerprint` 是摄取幂等键，由账号、update 类型和 Telegram 提供的稳定标识构造；不能只对序列化 JSON 做哈希，因为同一 update 的非语义字段可能变化。

`payload` 只保存归一化业务字段和恢复所需的最小 Telegram 标识。默认不长期保存完整 raw update；如为故障排查临时启用 raw capture，必须经过正文脱敏、访问控制和短期保留策略。

### 5.2 事件类型

| `event_kind` | 说明 | 是否可能改变 conversation revision | 是否触发回复 |
|---|---|---:|---:|
| `message.incoming.created` | 联系人新消息 | 是 | AUTO 下是 |
| `message.outgoing.observed` | 共享账号 outgoing update | 是 | 否 |
| `message.edited` | incoming 或 outgoing edit | 是 | 仅发送前重建，不单独追发 |
| `message.deleted` | 消息删除 | 是 | 否 |
| `album.item_observed` | 带 `grouped_id` 的消息项 | 是 | 由所属 turn 决定 |
| `reaction.changed` | reaction 增删或变更 | 否 | 否 |
| `service.observed` | Telegram service action | 否 | 否 |
| `read.observed` | 对端或本账号 read 状态 | 否 | 否 |
| `typing.observed` | 对端 typing 状态 | 否 | 否 |
| `outbound.random_id_mapped` | Telegram random ID 到 message ID 的映射 | 否 | 否 |

`read.observed` 和 `typing.observed` 可以采用短期保留；它们不是长期人格记忆证据。系统自身发出的 read acknowledgement 和 typing action 记录在 operation/audit 记录中，不伪装为 Telegram message。

### 5.3 Canonical message

消息当前投影至少表达：

```text
message_key = account_id + chat_id + telegram_message_id
direction = incoming | outgoing
role = user | assistant | system
source = telegram_user | ai | proactive_ai | copilot_approved | human | system_pending | system
text_content?
caption?
reply_to_message_id?
forward_metadata?
grouped_id?
content_revision
created_at
edited_at?
deleted_at?
media_status
metadata
```

`message_key` 是业务唯一键。重复 create 只能补全或对账同一行；不能新建第二条业务消息。edit 递增 `content_revision`，delete 设置 tombstone。delete 的状态优先级高于迟到的 create/edit，迟到 update 不得使 tombstone 复活。

`forward_metadata` 只保存显示给本账号的来源类型、可见名称和 Telegram 标识，不主动抓取源聊天历史。forward 内容与普通联系人输入一样属于不可信用户内容，不能获得系统指令权限。

### 5.4 Reply 与 album

reply 通过同一 chat 内的 `reply_to_message_id` 关联。目标消息缺失时保留 unresolved reference，不因补抓历史阻塞当前摄取。

album 使用：

```text
album_key = account_id + chat_id + grouped_id
```

同一 album 的 item 仍各自保留独立 `message_key`。稳定顺序使用 `(telegram_event_time, telegram_message_id)`；两者相同才使用 `observed_at` 和内部 ID 作为最终 tie-breaker。album 在所属 debounce collection seal 时形成快照，迟到 item 按普通新 content 规则更新 revision，不能改写已完成 outbound intent 的历史输入。

## 6. 内容与媒体边界

### 6.1 文本

普通 text 和 caption 保存为 UTF-8 文本，保留 Telegram entity 的归一化区间和类型。输入模型前由 Context Contract 再进行长度预算和信任标记；摄取层不把 URL、mention、code block 或 forward 文本解释为系统命令。

只有 caption 的视频或其他 metadata-only 媒体可以凭 caption 进入文本 turn。模型获得结构化的媒体类型提示，但不得声称已经看过未下载的媒体。没有 text/caption 的 metadata-only 消息单独到达时不触发 Main AI。

### 6.2 下载矩阵

| Telegram 内容 | 保存正文/caption | 保存元数据 | 下载二进制 | 可进入 Main AI |
|---|---:|---:|---:|---:|
| text | 是 | 是 | 不适用 | 是 |
| photo | caption | 是 | 是 | 是，验证成功且模型支持图片时 |
| image document | caption | 是 | 是 | 是，验证成功且模型支持图片时 |
| voice | caption（如有） | 是 | 否 | 仅 caption |
| audio | caption（如有） | 是 | 否 | 仅 caption |
| video | caption（如有） | 是 | 否 | 仅 caption |
| video note | 无 | 是 | 否 | 否 |
| non-image document | caption（如有） | 是 | 否 | 仅 caption |
| sticker / animated sticker | 无 | 是 | 否 | 否 |

metadata 可以包含 Telegram file ID/reference 的受控表示、声明 MIME、字节数、宽高、时长和原始文件名的清洗版本。file reference 不视为永久可用下载 URL，也不得写入日志。

### 6.3 图片验证与存储

V1 图片格式基线为 JPEG、PNG 和 WebP。Telegram photo 以及声明为图片的 document 都必须完成服务端内容 sniff、完整解码和限制检查；仅信任扩展名或 MIME 不足以放行。动画格式、SVG、损坏图片、格式与 MIME 不一致、超过字节/像素限制或疑似解压炸弹的内容标记为 `rejected`，不传给模型。

下载流程：

```text
metadata persisted
        |
download to private temporary file
        |
byte limit + content sniff + full decode + pixel limit
        |
normalize orientation and produce metadata-free provider copy
        |
fsync + atomic rename into media-data
        |
media_status = ready
```

要求：

- 文件名使用不可预测内部 ID，不使用联系人提供的路径或原文件名。
- 临时文件和最终文件都位于非公开 `media-data` volume，权限只授予 `app` 写入和授权 worker 只读。
- 数据库保存相对 object key、内容 SHA-256、验证后的 MIME、尺寸、字节数和状态，不保存宿主机绝对路径。
- provider copy 必须剥离 EXIF 和其他非必要 metadata；原图是否短期保留由 Operations 的保留策略决定。
- `https-gateway` 和 `control` 不得提供通用媒体读取 URL。
- 删除消息后媒体立即从模型可见集合移除，并创建幂等清理任务。

具体字节、像素、下载 timeout 和保留期在 Data Model/Operations 中确定，但必须是服务器配置的有限值，不能由 Telegram 消息或模型请求提高。

### 6.4 模型图片契约

内部模型输入使用：

```text
content_part.type = input_image
content_part.media_ref = validated internal reference
content_part.detail = auto
```

protocol adapter 将 canonical `detail=auto` 映射到 provider 支持的对应字段。协议没有显式 detail 字段时，只有 adapter 明确声明 provider 默认行为与 `auto` 等价才可以省略 wire 字段；否则能力校验失败。adapter 和活动 Main AI profile 都必须声明图片输入能力，不能识别能力时按不支持处理。

包含图片的 turn 在图片仍为 `pending` 时不能开始生成。图片下载/验证失败或模型不支持图片时，整个包含该图片的自动回复 turn 标记为 `blocked_unsupported_media`，不调用纯文本模型猜测图片内容，也不自动发送降级回复。caption 与 canonical message/media metadata 仍保存，真人可在 HUMAN/COPILOT 流程处理。

## 7. 顺序、幂等与投影

### 7.1 事务摄取

每个 update 的最小事务为：

1. 按 `update_fingerprint` 插入或读取 event。
2. 按 `message_key` upsert canonical message 或 tombstone。
3. 如内容语义变化，原子递增 conversation `content_revision`。
4. 创建带唯一业务键的后续 job 或更新 pending turn。
5. 提交事务后才确认本地摄取完成并发布 Redis 通知。

Redis 只负责唤醒。通知丢失时，PostgreSQL pending records 和 watermark 扫描必须能够重新发布任务。

### 7.2 迟到和乱序

canonical 对话顺序使用 Telegram event time、message ID 和内部 tie-breaker，不使用 worker 实际处理完成时间。

- edit 先于 create 到达时创建 placeholder，再由 create 补全，但保持 edit 后的最高 `content_revision`。
- delete 先于 create 到达时创建 tombstone；后续 create 只能补足非正文审计字段。
- album item 迟到时按第 5.4 节处理。
- 已由 completed turn 覆盖的历史消息迟到时，只进入历史/记忆修复，不产生追溯式自动回复。
- 尚未完成的连续 incoming 由 replacement turn 按消息稳定顺序重新聚合。

### 7.3 至少一次处理

update、queue job、model completion callback 和 outgoing observation 都按 at-least-once 处理。每个副作用必须有独立幂等键：

```text
event ingest: update_fingerprint
message projection: message_key + content_revision
turn membership: turn_id + message_id + message_revision
model run: turn_id + generation_number
outbound intent: turn_id + generation_number + source
memory refresh: conversation_id + completed_turn_watermark
media cleanup: media_object_id + deletion_generation
```

## 8. Outgoing 来源识别与对账

### 8.1 系统发送

`app` 调用 Telegram 前必须持久化 outbound intent，并为该 intent 生成稳定的 Telegram `random_id`。所有重试必须复用同一 intent 和 random ID，不得重新生成文本后当作同一次发送。

用于来源与发送幂等的最小标识包括：

```text
intent_id
account_id
conversation_id
turn_id
generation_number
source
content_hash
telegram_random_id
telegram_message_id?
```

```text
outbound intent persisted
        |
Telegram send with persisted random_id
        |
UpdateMessageID maps random_id -> telegram_message_id
        |
outgoing message update projected
        |
intent and message reconciled
```

Telegram gateway 必须向上层暴露或自行持久化 `random_id` 映射，不能只依赖消息正文和时间窗口猜测 source。

### 8.2 监听器分类

outgoing update 的分类顺序：

1. 按 `telegram_message_id` 命中已绑定 intent。
2. 按 `outbound.random_id_mapped` 命中 intent。
3. 若存在同 chat 的 `sending`/`unknown` intent，暂存为 `system_pending` 并立即运行 reconciler。
4. 不存在系统发送候选时，分类为 `human`。

`system_pending` 不进入长期上下文，直到解析为 `ai`、`proactive_ai`、`copilot_approved` 或 `human`。content hash、发送时间和 reply target 只能用于缩小候选或告警，不能在多个候选时自行决定 source。

真人 outgoing 一经确定：

- 保存为 `role=assistant, source=human`。
- 递增 `mode_version` 和 `content_revision`。
- 尽力取消本 conversation 的 active model run。
- 使尚未创建 Telegram 副作用的 AI turn/intent 失效。
- 刷新 Memory job。

该行为不强制持久切换会话模式；模式主要由 Control Bot 手动控制，但版本门禁必须覆盖真人接管动作。

## 9. Turn 聚合与 debounce

### 9.1 Collection

AUTO 下，可触发回复的 incoming content 进入一个 `collecting` turn。默认：

```text
sliding_debounce = 3 seconds
hard_collection_cap = 10 seconds from first included message
```

每条新的可触发 incoming 将 quiet deadline 后移 3 秒，但不能超过第一条后的 10 秒。达到任一 deadline 后 seal turn。两个值都由服务器配置并记录到 turn snapshot，不能由消息内容改变。

不触发回复的 reaction、service message 和无 caption 的 metadata-only 媒体不延长 debounce。album item 和 caption 作为 content 延长窗口。

### 9.2 Turn 输入快照

seal 时记录：

```text
ordered message IDs and content revisions
conversation content_revision
mode and mode_version
active Main AI config version
debounce policy snapshot
media readiness and hashes
context build version
```

turn 不复制长期记忆正文，但记录实际 Context manifest 或其不可变引用，以便复现模型输入。

## 10. 状态机

### 10.1 Conversation turn

```text
collecting -> ready -> generating -> output_ready -> sending -> completed
     |          |          |              |           |
     +----------+----------+--------------+-----------+-> cancelled
                           +----------------------------> superseded
                           +----------------------------> failed
                                                        -> dead_letter
```

| 状态 | 含义 |
|---|---|
| `collecting` | debounce 中，可增加 message membership |
| `ready` | 输入和媒体已 seal，等待获得执行权 |
| `generating` | 已创建 active model run |
| `output_ready` | 模型完整输出已验证，尚未创建发送副作用 |
| `sending` | 已创建 outbound intent，正在发送或对账 |
| `completed` | outbound intent 已 reconciled，或非自动模式的终态已记录 |
| `superseded` | 输入被新内容或 edit/delete 取代，结果不得发送 |
| `cancelled` | 模式、人工接管、全局 pause 或维护动作取消 |
| `failed` | 可观察的终态失败，不自动发送错误文本 |
| `dead_letter` | 自动恢复次数耗尽，需要人工检查 |

同一 turn 只允许递增 `generation_number`。replacement turn 必须显式引用 `supersedes_turn_id`，并继承尚未获得回复的 incoming membership。

### 10.2 Model run

```text
queued -> running -> succeeded
            |  |        |
            |  |        +-> discarded
            |  +-> cancel_requested -> cancelled
            +-> failed_retryable -> queued
            +-> failed_terminal
```

`succeeded` 表示完整 HTTP/stream 响应已结束、adapter 已归一化、schema/内容检查已通过。收到首 token、部分 stream 或 TCP 成功都不算完成。

取消是 best effort。`cancel_requested` 后返回的结果必须通过数据库 compare-and-set 检查；run 已 `cancelled`、turn 已 `superseded/cancelled` 或 generation 不再 active 时，结果转为 `discarded`。

### 10.3 Outbound intent

```text
pending -> sending -> sent_unconfirmed -> reconciled
   |          |              |
   |          +-> retry_wait-+
   |          +-> unknown ---+-> reconciliation
   +-> cancelled
   +-> failed_terminal -> dead_letter
```

`sent_unconfirmed` 表示 Telegram RPC 有成功证据但 message ID/source 投影尚未完整对账。`unknown` 表示进程或网络中断导致结果不确定。两者都禁止新建 replacement intent。

只有 `reconciled` 允许 turn 进入 `completed`。异常情况下可由人工将 dead letter 结案，但必须留下审计原因，不能静默标记为成功。

## 11. 生成期间的新 incoming content

### 11.1 时间定义

```text
t0 = model provider request 实际开始时间
grace_deadline = t0 + 3 seconds
t_done = 完整响应结束且 adapter/schema 验证成功的时间
```

只要 model provider 请求已经开始、所属 turn 尚未创建 outbound intent，新的 `message.incoming.created` 到达时就应用本节规则。适用状态包括 `running`，也包括 API 已完成但仍停留在 `succeeded/output_ready` 的短暂窗口；已知 `t_done` 时直接与 deadline 比较。

### 11.2 条件式发送

当新 incoming 到达：

1. 先幂等保存消息并递增 conversation `content_revision`。
2. 将消息加入 pending replacement collection，不改写当前 run 的输入快照。
3. 若完整结果已经存在且 `t_done <= grace_deadline`，当前输出可获得一次 `grace_send_authorization`；结果尚未完成时只等待到既有 deadline，不从新消息到达时重新计时。
4. 当前结果通过所有其他发送门禁后照常发送；新 incoming 留在下一个 turn，不丢弃也不并入已发送结果。
5. 若到 `grace_deadline` 仍没有完整有效结果，或已知 `t_done > grace_deadline`，当前 turn 原子转为 `superseded`；仍在运行的 run 转为 `cancel_requested` 并立即尽力取消，已经完成的迟到结果转为 `discarded`。
6. replacement turn 合并原 turn 尚未获得回复的 incoming 与所有新 pending incoming，重新 debounce、构造 Context 和生成。

如果新 incoming 到达时已经 `t_new >= grace_deadline` 且 run 未完成，不再额外等待，立即执行第 5、6 步。

`grace_send_authorization` 只允许忽略由列明 `message.incoming.created` 事件造成的 `content_revision` 差异。以下事件没有 3 秒例外，始终使旧结果失效：

- 任一输入消息在发送前 edit 或 delete。
- 确认的真人 outgoing。
- mode 切换、global pause、维护门禁或联系人策略变化。
- 活动 turn/generation 被其他协调者替换。

API 通用 timeout 由 ModelProfile 配置，通常大于 3 秒；不能把 3 秒实现为强制终止所有正常模型请求。

## 12. Edit、delete 与 reaction

### 12.1 Edit

edit upsert 同一 `message_key`，递增消息 `content_revision` 和 conversation `content_revision`，并保存 `edited_at`。

- turn 仍在 `collecting`：更新 membership revision 并重新计算 sliding debounce，但 hard cap 不重置。
- turn 已 seal 且尚未 `reconciled`：旧 turn/run/未发送 intent 失效，尽力取消并创建 replacement turn。
- outbound 已 `reconciled`：只更新历史并创建 memory reconciliation job，不自动生成补充消息，不编辑或撤回已发送回复。
- outgoing human edit：保留 `source=human`，刷新上下文和记忆，不触发 AI 回复。

### 12.2 Delete

delete 创建或更新 tombstone：

```text
deleted_at set
text/caption no longer context-visible
media no longer context-visible
content_revision incremented
cleanup and memory reconciliation queued
```

逻辑删除必须在同一事务提交后立即对 Context 查询生效。物理正文清理、事件证据最小化和媒体删除按幂等 job 完成；引用被删 revision 的 memory、summary 和 embedding 同时退出 active Context，重建后对旧派生正文与向量执行单向 redaction。具体保留要求由 Data Model/Operations 定义，但任何 retained audit 都不得继续被模型检索。

发送前 delete 与 edit 一样使 turn 失效。发送后不自动追发，也不自动删除 AI 已发送消息。

### 12.3 Reaction 与 service message

reaction 保存目标 message、actor、reaction kind 和变化时间，用于关系信号和未来分析，但 V1 不创建 turn、不延长 debounce、不直接进入 Main AI。

service message 只保存 action kind、涉及的受控 peer reference 和时间。private chat 中不支持的 service action 不保存自由文本，不触发模型。

## 13. Mode、read acknowledgement 与 typing

### 13.1 Mode 行为

| Mode | 保存 incoming | 生成 | 自动发送 | 自动 read | 自动 typing |
|---|---:|---:|---:|---:|---:|
| AUTO | 是 | 是 | 是 | 是 | 是 |
| HUMAN | 是 | 否 | 否 | 否 | 否 |
| COPILOT | 是 | 响应式仅 `/draft`；proactive 可生成待批草稿 | 仅管理员批准后 | 否 | 否 |
| PAUSED/maintenance | 是，依赖持久化可用 | 否 | 否 | 否 | 否 |

COPILOT draft 默认不创建 Telegram outbound intent。管理员在 Control Bot 对精确 draft revision 执行 Send 后，由 `app` 在完整门禁下创建唯一 intent，最终 Telegram outgoing 按 `source=copilot_approved` 摄取并保留是否经过人工编辑的 provenance。真人在普通 Telegram 客户端独立发送仍是 `source=human`。响应式 COPILOT incoming 不自动生成草稿，具体状态机见 `docs/architecture/04-conversation-orchestrator.md`。

### 13.2 Read acknowledgement

AUTO 下只有在 turn 获得执行权并从 `ready` 进入 `generating` 时，才对本 turn 包含的 incoming 执行 read acknowledgement。Telegram read 是 high-watermark 语义，系统记录请求的最大 message ID、调用时间和结果。

摄取、debounce、media download 或排队本身不自动已读。read 失败不取消模型，但需要可观察并按 Telegram 限流策略有限重试；模式在调用前变化时不得发送 read。

### 13.3 Typing lifecycle

AUTO 下在 provider 请求开始前发送 typing，并按 Telegram 要求在后台刷新：

```text
start at generating
refresh while active and gates remain valid
stop on sending, completed, superseded, cancelled or failed
```

typing 刷新是可丢失的临时副作用，不进入可靠队列。进程崩溃后依赖 Telegram typing TTL 自然过期。发送 typing 失败不影响消息状态机，也不能触发高频重试。

## 14. Conversation 串行化与版本门禁

### 14.1 Lease 与事实源

同一 `conversation_key` 最多一个 active coordinator。Redis lease 使用随机 owner token、有限 TTL 和续租；只有 owner 可以续租或释放。lease 丢失时协调者必须停止创建新副作用。

Redis lease 不是正确性事实源。PostgreSQL 中的 account `control_version`、turn state、active generation、`mode_version`、`content_revision`、draft/approval revision、intent 唯一约束和 compare-and-set 更新共同防止重复发送。即使两个进程短暂都认为持有 lease，也只有一个能提交合法状态转换。

### 14.2 发送前检查

从 `output_ready` 创建 outbound intent 必须在 conversation 数据库锁/原子 CAS 内完成，并同时检查：

```text
effective mode permits intent source
account_control_version = run/decision/draft snapshot
mode_version = run.mode_version_snapshot
global pause and maintenance gates = open
turn = active and state = output_ready
generation_number = active generation
model run = succeeded and output validated
conversation content_revision = input revision
  OR valid grace_send_authorization covers every delta
no confirmed human outgoing after run start
contact policy still allows automation
no existing outbound intent for the idempotency key
lease owner token still valid
```

任一检查失败时不得创建 intent，输出转为 discarded/cancelled，并按原因决定是否创建 replacement turn。

intent 创建后到 Telegram RPC 前再执行一次轻量门禁。发生 mode、人类接管、edit/delete 或 lease 丢失时，尚未发送的 intent 转为 `cancelled`。已经进入结果不确定状态的 RPC 不能假定未发送，必须进入 reconciliation。

## 15. Retry、FloodWait 与失败恢复

### 15.1 模型请求

- DNS、连接、429 和可重试 5xx 使用有界 exponential backoff 和 jitter。
- 每次 attempt 记录 provider request ID、开始/结束时间、错误类别和配置快照，不记录完整 prompt 或凭据。
- 重试前重新检查 turn、generation、mode 和 revision；旧 run 不再 active 时直接丢弃。
- schema 错误、内容策略错误和确定性 4xx 默认为 terminal，不自动发送错误文本。
- provider 不支持幂等键时，重复模型调用仍不会直接造成 Telegram 副作用，因为只有 active generation 可以创建唯一 intent。

具体 attempt 上限在 Operations 中配置。达到上限后 turn 进入 `failed` 或 `dead_letter`，联系人侧保持不发送。

### 15.2 Telegram FloodWait

FloodWait 不进行忙循环。记录 Telegram 指示的等待时间，将 intent 转为 `retry_wait` 并在到期后重新检查门禁。

- 未执行 RPC 的 intent 可以在门禁仍有效时重试。
- 已可能执行的 RPC 必须先 reconciliation，并复用相同 `random_id`。
- 等待期间出现 human outgoing、mode 变化、edit/delete 或不被 grace 覆盖的 revision 时，未发送 intent 取消。
- 超过服务器允许的最大自动等待时进入 dead letter，由 `/server_status` 或后续运维命令显示摘要。

### 15.3 崩溃点

| 崩溃点 | 恢复行为 |
|---|---|
| event 提交前 | Telegram/update replay 后重新处理 |
| event 提交后、通知前 | watermark 扫描重新发布 pending work |
| model request 前 | active run lease 过期后重领 |
| model request 中 | 旧 attempt 超时；恢复者检查状态后决定重试，迟到结果受 generation gate 限制 |
| intent 提交前 | 没有 Telegram 副作用，可由 active turn 重试状态转换 |
| intent 提交后、RPC 前 | 门禁通过后使用原 intent/random ID 发送 |
| RPC 中或返回后、对账前 | 标记 `unknown/sent_unconfirmed`，先查 mapping/outgoing history，不创建新 intent |
| Telegram 已发送、listener 未投影 | UpdateMessageID、update replay 或受限 history reconciliation 补齐 message/source |

启动恢复顺序：

1. 取得 account ownership 和 conversation lease 后扫描 expired active runs。
2. 恢复 `system_pending` outgoing 和 `unknown/sent_unconfirmed` intents。
3. 重新发布 collecting/ready turns 和 media jobs。
4. 根据 PostgreSQL watermark 补发 memory refresh jobs。
5. 完成对账前不盲目重发 Telegram 消息。

## 16. Memory Pipeline 交互

实时路径只做可靠 job refresh，不同步等待 Memory Agent。

以下事件更新 memory pending watermark：

- incoming message/album 持久化。
- 确认的 human、ai、proactive_ai 或 copilot_approved outgoing 持久化。
- turn `completed`。
- 消息 edit/delete。
- system_pending source 完成对账。

同一 conversation 的 memory job 默认使用 45 秒安静窗口；20 条 eligible revision、约 6000 input tokens 或最早未处理 event 等待 10 分钟任一达到即硬触发，默认每 5 分钟的补偿扫描查找遗漏。所有触发条件是 OR。completed turn 可以提高优先级但不是 correctness 边界；已经 running 的 range 不可扩张，新 event 进入下一 generation。

Memory Agent 读取 immutable input manifest 指向的已提交 canonical revisions 和允许的 validated media reference，不读取未确认 source、tombstone 正文或临时下载文件。caption 按文本处理；纯图片推断默认只能成为 candidate。AI/proactive outgoing 不得单独证明联系人事实或真人 style，`copilot_approved` 必须继续区分是否编辑。

edit/delete 发生在已有 memory proposal、正式 memory 或 summary 之后时，创建不等待安静窗口的 reconciliation job。Memory Pipeline 通过 proposal evidence、formal evidence 和 summary source membership 先隔离受影响版本，再从剩余证据执行 invalidate、supersede 或重新提取；replacement 提交后清除旧派生正文和 embedding，而不是让已删除证据继续作为 active 或可恢复 memory。完整契约见 `docs/architecture/05-memory-pipeline.md`。

## 17. 隐私、安全与审计

- 所有联系人正文、caption、图片和 Telegram file reference 都属于敏感用户数据。
- 日志禁止包含消息正文、caption、图片 bytes、原始文件名、完整 peer 标识、Session、API key 或完整模型 prompt。
- outbound intent 可以记录 content hash、长度和 schema version，不默认记录生成正文到结构化日志；正文只进入受控业务表。
- 媒体 volume 不暴露给 gateway，不通过静态文件服务器发布，不写入镜像和普通备份日志。
- 模型 provider 只接收当前 Context manifest 明确选择的内容；metadata-only 媒体不得被 adapter 临时下载。
- Prompt injection 内容保留 user/forward 来源标记，不能改变 system/developer 指令层。
- operator audit 记录 mode 变化、dead-letter 处置和人工 source 修正，但不复制正文。
- unsupported peer 的正文在过滤阶段丢弃，不先完整持久化再清理。

## 18. 可观测性

消息路径的结构化指标至少包括：

```text
updates_ingested_total by event_kind
duplicate_updates_total
ignored_peer_updates_total by reason
conversation_turns_total by terminal_state
debounce_duration_seconds
model_runs_total by result
model_superseded_total by reason
grace_send_authorizations_total
outbound_intents_total by status/source
outbound_reconciliation_age_seconds
telegram_floodwait_total
media_download_total by status/type
media_validation_rejected_total by reason
memory_refresh_lag_seconds
conversation_lease_contention_total
```

correlation 至少可以从 event -> message -> turn -> model run -> outbound intent -> reconciled Telegram message 追踪，但外部日志只使用内部 ID 或不可逆短标识。

## 19. 自动化验收矩阵

后续 Test Strategy 必须至少覆盖：

| 场景 | 必须结果 |
|---|---|
| 同一 incoming update 重放 10 次 | 一条 canonical message，最多一个有效 turn/intent |
| edit 先于 create | 最终为 edit 后内容，不重复回复 |
| delete 先于 create | tombstone 保持，不进入 Context |
| 三条消息间隔小于 3 秒 | 一个按稳定顺序聚合的 turn |
| 持续消息超过 10 秒 | hard cap seal，后续进入下一 turn |
| 新消息到达，API 在 run 开始后 3 秒内完成 | 旧结果允许发送，新消息保留为下一 turn |
| 新消息到达，API 超过 run 开始后 3 秒未完成 | 旧结果丢弃，replacement turn 合并重生成 |
| 新消息在 run 开始 3 秒后才到达 | 立即 supersede，不再等待 |
| streaming 3 秒内只有首 token | 不视为完成，执行 supersede |
| API 已在 3 秒内完成、intent 尚未创建时新消息到达 | 根据 `t_done` 授权旧结果，新消息进入下一 turn |
| API 超过 3 秒完成、intent 尚未创建时新消息到达 | 已完成结果 discarded，合并重生成 |
| 发送前 edit/delete | 旧 run/intent 失效并重建，不发送旧结果 |
| 发送后 edit/delete | 不自动追发，Context/Memory 进入修正流程 |
| 真人 outgoing 与模型返回竞态 | `mode_version` 使 AI 结果无法发送 |
| Control Bot 切 HUMAN 与模型返回竞态 | 非 AUTO 门禁阻止发送 |
| intent 提交后进程崩溃 | 使用同一 random ID 对账，不重复发送 |
| outgoing listener 先于 send caller 返回 | source 最终为 ai/proactive_ai/copilot_approved，不误判 human |
| photo 与 image document | 验证后以 `detail=auto` 进入支持图片的模型 |
| 伪造 image MIME 或超限图片 | 拒绝，不传 provider，不回复猜测内容 |
| voice/video/sticker | 不下载，只保存允许的元数据 |
| 无 caption 的 metadata-only 媒体 | 不触发 Main AI |
| HUMAN/COPILOT/PAUSED incoming | 不自动 read，不发送 typing |
| AUTO 生成取消或失败 | typing 停止，不发送错误文本 |
| Redis 通知丢失 | PostgreSQL 扫描恢复 pending work |
| 多 worker/重复 lease owner | CAS/唯一键确保最多一个 Telegram 副作用 |

测试需要使用可重放 Telegram update fixture、可控时钟、LLM fake、Telegram send fake 和崩溃注入点。仅通过单元测试不能证明发送恢复语义；必须包含容器内 PostgreSQL/Redis 集成测试。

## 20. 后续文档必须细化

Data Model：

- event、message、message revision、media object、turn、run、intent 和 watermark 的表、约束与索引。
- tombstone 正文清理和 evidence 关系。
- canonical Telegram peer ID 与 update fingerprint 的具体编码。

Context Contract：

- 文本、caption、forward、reply、album 和图片 content part 的选择与 token/image 预算。
- 三种模型协议对 `detail=auto` 和多模态 content part 的 wire 映射、等价默认值或不支持结果。
- 不支持图片时的能力校验与可观察错误。

Operations：

- media 字节/像素限制、download timeout、磁盘配额、保留期、清理和备份策略。
- retry/FloodWait/dead-letter 数值与 operator runbook。
- 启动 reconciliation 的扫描窗口和速率限制。

Test Strategy：

- 本文第 19 节的 fixture、race、crash 和端到端验收实现。
- Telegram random ID 映射与 Telethon gateway 的契约测试。
- 图片恶意样本、解码炸弹和媒体权限测试。

## 21. 完成检查

- [x] V1 自动处理范围固定为非 Bot 用户一对一 private chat。
- [x] incoming、outgoing、edit、delete、reply、forward、album、media、reaction 和 service event 已定义。
- [x] text/caption 与各类媒体的保存、下载和模型输入边界已定义。
- [x] 业务唯一键、event 幂等键和稳定顺序已定义。
- [x] AI、proactive AI、真人和 system pending outgoing 的对账流程已定义。
- [x] turn、model run 和 outbound intent 状态机已定义。
- [x] debounce 与条件式 3 秒 supersede 行为已定义。
- [x] edit/delete 在发送前后各自的行为已定义。
- [x] conversation lease、revision、mode version 和发送前门禁已定义。
- [x] read acknowledgement 与 typing 生命周期已定义。
- [x] retry、FloodWait、dead letter、reconciliation 和崩溃恢复已定义。
- [x] Memory refresh/reconciliation 触发点已定义。
- [x] 安全、隐私、可观测性和自动化验收矩阵已定义。
