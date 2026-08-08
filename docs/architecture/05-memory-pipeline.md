# Memory Pipeline

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的 Memory Pipeline：episode extraction、rolling summary、daily/weekly consolidation、reconciliation、触发与范围合并、Memory Agent 输入输出、proposal 验证、证据与冲突、summary watermark、embedding 重建、遗忘淡化、积压降级、并发恢复和人工审计契约。

总体设计见 `docs/Design.md`；消息 revision、媒体和 memory refresh 入口见 `docs/architecture/02-message-lifecycle.md`；持久化骨架见 `docs/architecture/03-data-model.md`；模式控制和来源语义见 `docs/architecture/04-conversation-orchestrator.md`。本文不重新定义 Telegram update 摄取、Main AI 上下文总预算、Proactive 候选算法或 Operations 的最终 retention 数值。

当前状态：V1 架构基线。

## 2. 已确认决策

| 主题 | V1 决策 |
|---|---|
| 实时路径 | Memory Pipeline 异步运行；Main AI 不等待记忆处理 |
| 触发关系 | 所有触发条件均为 OR；事件范围先持久化，再异步执行 |
| 安静窗口 | 默认 45 秒，新的 eligible event 可以后移 quiet deadline |
| 硬阈值 | 20 条 eligible revision、约 6000 input tokens 或首条未处理事件等待 10 分钟，任一达到即绕过安静窗口 |
| 补偿扫描 | 默认每 5 分钟扫描未覆盖范围、过期 lease 和遗漏任务 |
| proposal | Memory Agent 只提出候选；应用层验证并事务提交正式记忆 |
| 自动接受 | 默认 `confidence >= 0.85`，且证据可信、无未解决冲突、通过全部确定性校验 |
| candidate | 默认 `0.60 <= confidence < 0.85` 或需要人工判断的合法提案 |
| 拒绝 | `confidence < 0.60` 或确定性校验失败；失败原因使用稳定 code |
| 冲突 | 同命题合并证据，明确时间变化 supersede，歧义冲突保持旧 active 并把新提案留为 candidate |
| 图片 | caption 按文本处理；视觉内容仅在 Memory Agent 支持图片时输入，image-only 推断默认只能成为 candidate |
| summary | rolling 默认每 50 条 revision 或约 12000 tokens；daily/weekly 使用有效时区切片 |
| summary 证据 | summary 是派生上下文；正式记忆最终必须追溯到未删除 canonical message revision |
| embedding | active memory、active summary、eligible canonical message chunk 建向量；不同 embedding space 不混查 |
| 淡化 | 时间影响检索分数，不自动删除或原地改写记忆 |
| 人工管理 | Control Bot 提供查看、接受、拒绝和 forget；高影响动作需预览和二次确认 |
| 积压 | Context 标记 `fresh/degraded/stale` 并扩大 recent canonical window，不读取未接受 candidate |

这些数值是带版本的服务器默认值，不是写死在 prompt 中的常量。修改阈值只影响之后创建或刷新时取得新 policy snapshot 的 job；已经 running 的 job 保持原快照。

## 3. 目标、范围与非目标

### 3.1 目标

V1 Memory Pipeline 必须保证：

- 任意 committed canonical event 范围都能重复处理；
- 相同输入、pipeline/prompt/schema 版本的重放不会重复创建正式记忆；
- 每个正式 memory version 都有可验证且未 redacted 的证据链；
- edit/delete/forget/purge 后，失效内容立即退出 Context，派生内容随后确定性重建或清除；
- summary watermark 只在对应版本和 source membership 成功提交后推进；
- embedding 更换模型或维度时不会把不同向量空间混合检索；
- Memory Agent 失败或积压不会阻塞 Telegram 摄取、真人发言或 Main AI 实时回复；
- 低置信度、冲突和 image-only 推断不会静默成为长期事实；
- 人工审计动作经过管理员、版本、状态和一次性 token 门禁。

### 3.2 非目标

V1 不包含：

- 让 Memory Agent 直接修改数据库或调用 Telegram；
- 把模型 confidence 当作真实概率或唯一接受条件；
- 从语音、音频、视频、video note、stickers 或非图片 document 二进制提取记忆；
- 自动 OCR 后把图片文字当作已验证事实；
- 使用 summary 代替 canonical history 或保留已被要求删除的旧正文；
- 因记忆变旧而自动物理删除稳定身份、核心关系或核心偏好；
- 在 Control Bot 中显示完整聊天历史、provider raw body、chain-of-thought 或 API key；
- 定义 Main AI 的最终 token 分配、向量召回公式或 HNSW/IVFFlat 参数；
- 让 Memory 成功、失败或人工接受行为自动授权聊天回复。

## 4. 组件职责与依赖方向

### 4.1 `app`

`app` 在 canonical message ingest 事务中：

- 写入 message event/revision/source reconciliation；
- 推进 conversation content revision；
- 创建或刷新待处理 memory range；
- 对 edit/delete 创建高优先级 reconciliation job；
- 通过 transactional outbox 发布轻量唤醒事件。

`app` 不同步调用 Memory Agent，也不等待 summary 或 embedding。

### 4.2 `worker`

`worker` 是 Memory Pipeline 的执行所有者：

- 领取带数据库 lease 的 memory/background job；
- seal immutable input manifest；
- 构造受版本控制的 Memory Agent 请求；
- 验证、持久化和裁决 proposal；
- 生成 summary、source membership 和 watermark；
- 提交 memory/current-version 变化与派生 event/intention；
- 生成、失效和重建 embedding；
- 运行补偿、冲突 consolidation 和 reconciliation。

多个 worker 可以并行处理不同 conversation；同一 conversation 的可变 memory/summary 提交仍由 PostgreSQL lock、CAS 和幂等键串行化。

### 4.3 `control`

`control` 只处理 allowlisted 管理员命令与审计动作：

- 查看 active memory 的受限摘要；
- 列出 candidate；
- 接受或拒绝精确 proposal revision；
- 对精确 memory identity 执行 forget 预览和确认；
- 查看 pipeline freshness、watermark 和稳定错误码。

`control` 不运行 Memory Agent，不直接写 active memory payload，也不处理 embedding vector。接受和 forget 通过 durable command 交给 worker/app 侧事务执行。

### 4.4 PostgreSQL 与 Redis

PostgreSQL 是以下状态的唯一正确性来源：

- canonical revision 和 source；
- memory job、input manifest 和 watermark；
- proposal、证据、正式 version 和 relation；
- summary version/source/watermark；
- embedding space/record；
- review command、审计和 erasure 状态。

Redis 只负责唤醒、短期节流和非权威缓存。Redis 丢失、重复通知或重启不得改变最终结果。

依赖方向保持：

```text
canonical ingest
  -> memory job refresh/outbox
  -> worker input manifest
  -> model adapter
  -> proposal validation
  -> memory/summary transaction
  -> embedding/outbox

control command
  -> durable review command
  -> worker validation/transaction
```

Memory Pipeline 不能反向调用 Conversation Orchestrator 获得发送权限，也不能直接创建 outbound intent。

## 5. 术语与 source of truth

### 5.1 Eligible event

eligible event 是可能改变长期记忆或 summary 的 committed canonical 变化：

- 支持的一对一 private conversation 中的 text/caption message revision；
- 已验证图片及其所属 message revision；
- 已确认 source 的 outgoing message；
- message edit/delete；
- album 封口后的稳定 item 集合；
- 与 message evidence 直接相关的 source correction；
- 已提交的人工 memory review/forget 动作。

reaction、service message、typing/read、无 caption 的 metadata-only 非图片媒体、unsupported peer body 和 `system_pending` outgoing 不进入普通 episode。`system_pending` 对账为确定 source 后，才按最终 source 刷新范围。

### 5.2 Canonical event range

job 范围使用单调 committed `message_events.id` 的 inclusive `[range_start_event_id, range_end_event_id]`。范围表示 worker 必须扫描并裁决其中所有事件，不表示每个事件都要生成记忆。

event time、Telegram message ID 和 event ID 含义不同：

- event ID 决定处理顺序和 watermark；
- Telegram created/edit time 用于事实时间解析和 daily/weekly 归档；
- Telegram message ID 用于业务身份和 evidence 定位。

迟到事件拥有较新的 event ID，但可能归入较早的日历区间；summary source membership 必须显式记录，不能仅靠两个时间戳猜测。

### 5.3 Episode

episode 是一次 input manifest 中按 canonical event/revision 顺序选择的连续对话片段。它不是新的聊天副作用，也不等于 Conversation Orchestrator 的 reply turn。

一个 episode 可以包含：

- 多条 incoming；
- human/ai/proactive_ai/copilot_approved outgoing；
- 一个或多个 album；
- 为理解代词、时间或关系所需的有限前置上下文；
- 与范围内命题可能冲突的 active memory。

前置上下文只帮助理解，不自动成为 proposal evidence；模型必须明确引用证据 revision。

### 5.4 Formal memory 与 candidate

formal memory 是 `memories` 的 active stable identity 加 current immutable `memory_version`。只有 formal memory 可以进入正式长期记忆层。

candidate 是 `memory_proposals.state=candidate` 的待审结果：

- 不创建 active memory；
- 不进入 Main AI Context；
- 不生成正式 memory embedding；
- 可以在 retention 到期前由管理员接受或拒绝；
- source revision 被 edit/delete 后立即 invalidated，不再允许接受。

### 5.5 Watermark

watermark 表示某种 pipeline 已经确定性裁决到的最高连续 event ID。它不表示“其中每条消息都产生了记忆”，也不表示 Main AI 已回复。

```text
episode watermark
summary watermark by kind
reconciliation watermark/generation
embedding space build watermark
```

任何存在未处理 hole 的范围都不能越过 hole 推进连续 watermark。明确判定 `ineligible/no_change` 也是有效裁决结果。

## 6. 四类领域任务

### 6.1 Episode extraction

目的：从新的 conversation episode 提出以下 typed memory 变化：

```text
identity
relationship
fact
preference
event
intention
style
```

episode extraction 是高频任务，主要消费尚未越过 episode watermark 的 canonical range。单次模型输出可以包含零个或多个 proposal。

### 6.2 Rolling summary

目的：将 watermark 之后、达到阈值的 canonical history 压缩为可重建的 conversation summary。默认 eligibility：

```text
new eligible revisions >= 50
OR estimated unsummarized input tokens >= 12000
```

rolling summary 不是事实提取的替代品。它保留对话主题、未完事项、情绪/关系变化和重要时间点，但必须区分联系人陈述、真人陈述和 AI 生成内容。

### 6.3 Consolidation

consolidation 包含三个有界目的：

- 为已经结束且有内容的本地日生成 daily summary；
- 在 daily summaries 齐备后生成 weekly summary；
- 合并重复 memory evidence、识别明确时间变化、标记歧义冲突和计算检索淡化特征。

daily period 在有效时区的自然日结束且 conversation 已安静至少 15 分钟后 eligible；weekly 使用 ISO week，在对应周结束且所含 daily source 已完成后 eligible。Scheduler 默认每小时扫描 eligible period，不为空 period 生成 summary。

有效时区选择：

1. 已验证的 contact timezone override；
2. account timezone；
3. 部署默认 timezone。

每个 period 保存 IANA timezone 和 UTC boundary snapshot。时区之后变化不默认重切历史 period；管理员显式 rebuild 才使用新规则重建。

### 6.4 Reconciliation

以下情况创建高优先级 reconciliation：

- evidence message edit/delete；
- `system_pending` source 被纠正；
- image 被判定损坏、隔离或删除；
- memory forget/contact purge/account wipe；
- summary source revision 失效；
- embedding source hash 与 current content 不一致；
- backup restore 后 erasure ledger 要求再次清除。

reconciliation 不等待 45 秒安静窗口。它首先隔离受影响 active derived data，再决定 replacement、supersede、invalidate 或 redaction；不能先重建、后隐藏旧内容。

## 7. 触发、安静窗口与硬阈值

### 7.1 所有触发条件为 OR

以下任一条件可以创建、刷新或使 pending episode job 到期：

```text
confirmed AI/COPILOT reply reconciled
OR confirmed human outgoing
OR effective HUMAN/COPILOT/PAUSED/BLOCKED 下收到 eligible incoming
OR confirmed proactive outgoing
OR eligible revision count reaches 20
OR estimated unprocessed input reaches 6000 tokens
OR oldest unprocessed event reaches 10 minutes
OR compensation scan finds an uncovered range
```

AUTO incoming 立即持久化并扩大未处理范围；正常情况下等待本轮 confirmed outgoing 后形成完整 episode。若生成失败、没有 outgoing 或 conversation 持续活跃，revision/token/time 硬阈值和补偿扫描仍会使其得到处理。

edit/delete、forget/purge 和 source correction 直接触发 reconciliation，不与普通 episode 触发互相等待。

### 7.2 默认 deadline

```text
quiet_window = 45 seconds after latest eligible event
hard_event_count = 20 eligible revisions
hard_token_estimate = 6000 input tokens
hard_age = 10 minutes after first uncovered eligible event
compensation_scan_interval = 5 minutes
```

quiet deadline 可以后移；`hard_due_at` 在 pending generation 创建时基于最早未处理 event 固定，后续刷新不得延后。

任一硬条件达到时：

- pending job 立即 runnable；
- 新消息不能再把该 generation 推回等待；
- worker seal 当前范围；
- 同时到来的更晚 event 进入下一 pending generation。

硬阈值是 pipeline 调度阈值，不是 provider timeout。模型 timeout、retry 和 circuit breaker 由 model config/Operations 管理。

### 7.3 哪些事件刷新 quiet window

会刷新：

- 新 text/caption current revision；
- image message 的 validated media 从 pending 变为 ready；
- confirmed outgoing source；
- album 最终 membership/顺序确定。

不刷新：

- duplicate update；
- reaction/service/read/typing；
- 已判定 ineligible 的 metadata-only event；
- retry notification；
- 同一 source correction 的重复对账。

### 7.4 Completed turn 与 memory range

completed conversation turn 可以刷新 `completed_turn_watermark` 并提高调度优先级，但不作为唯一边界：

- HUMAN/COPILOT 可能没有 reply turn；
- AUTO 可能失败或被真人抢占；
- proactive outgoing 不是 reactive turn；
- edit/delete 必须独立修正。

因此 memory correctness 只依赖 canonical event range，不依赖 turn 是否成功。

## 8. Pending job 刷新与范围合并

### 8.1 Pending generation

同一 `(account_id, conversation_id, job_kind)` 同时最多一个可刷新的 `pending` generation。ingest 事务在 conversation scope 内：

1. 读取 episode watermark 和最新 eligible event；
2. 锁现有 pending job；
3. 若不存在，创建 `[watermark + 1, latest_event]`；
4. 若存在，扩大 `range_end_event_id`，但不改变 start；
5. 更新 eligible count/token estimate 和 quiet deadline；
6. 保留最早的 hard deadline；
7. 递增 `job_version` 并写 outbox；
8. 与 canonical ingest 一起 commit。

duplicate Telegram event 不增加 count、不扩大范围、不重置 deadline。

### 8.2 Seal 后不可改写

worker 使用 row lock/CAS 把 job 从 `pending` 变为 `leased/running` 时，固定：

```text
range start/end
threshold policy version and values
pipeline version
prompt version
output schema version
active memory model config/credential version
```

running job 的范围和 input manifest 不可修改。新 event 创建下一 pending generation；前一 job 成功后，下一 job 的 start 通过最新 contiguous watermark 校正，避免重复或 hole。

### 8.3 Job 状态机

```text
pending
  -> leased
  -> running
       -> succeeded
       -> retry_wait -> leased
       -> dead_letter
       -> cancelled
```

允许的取消仅包括 purge/wipe、范围已被更高 generation 完全重建，或管理员明确停止任务。普通 worker shutdown 使用 lease expiry/retry，不把任务标记 cancelled。

### 8.4 幂等键

job identity 与 model/proposal identity 分层：

```text
job_generation_key = SHA256(
  account_id | conversation_id | job_kind |
  range_start | range_end | input_revision_set_hash |
  pipeline_version | policy_version
)

model_input_key = SHA256(
  job_generation_key | prompt_version | output_schema_version |
  model_config_version | ordered_manifest_sha256
)

proposal_key = SHA256(
  model_input_key | proposal_ordinal | operation |
  normalized_target_set | normalized_semantic_key
)
```

hash 输入使用版本化 canonical serialization；不使用模型自由文本、处理时间或随机 UUID 作为业务幂等语义。

同一 provider attempt 重试复用 logical model run；显式使用新 prompt/model config 重新处理时产生新 model input key，但 acceptance validator 仍按 target/semantic slot 去重，不能创建重复 active memory。

### 8.5 Watermark 推进

episode job 只有在以下全部完成后才能 CAS 推进 watermark：

- 范围内每个 event 已归类 eligible/ineligible；
- 所有模型输出已保存为 proposal 或记录 deterministic no-change；
- 应自动接受的 proposal 已事务提交；
- candidate/rejected/error 有 terminal/可恢复状态；
- input manifest 和 model provenance 已保存；
- 没有更早的未完成 hole。

embedding job 不阻塞 episode watermark。embedding freshness 使用独立 target/space 状态表达。

## 9. Input manifest 与可重放性

### 9.1 Seal input manifest

在调用 provider 前，worker 创建 immutable `memory_input_manifest`。至少包含：

```text
account_id / conversation_id / memory_job_id
job kind and generation
range start/end
ordered canonical source items
supporting context items
active related memory versions
active summary versions
validated image references
source/trust/inclusion role per item
pipeline/prompt/input/output schema versions
model config and credential version references
timezone and threshold snapshots
token/image estimates and omission reason codes
manifest SHA-256
```

manifest item 只保存 typed source ID、ordinal、hash 和选择理由，不复制正文。正文通过未 redacted immutable revision/version 在执行时读取；读取后必须再次验证 hash。

### 9.2 Ordered item 类型

```text
episode_message
episode_image
supporting_message
related_memory
prior_summary
profile_identity
```

`episode_message/episode_image` 可以成为 evidence；supporting context 默认不能。Memory Agent 即使引用 supporting item，validator 也必须确认它满足范围、scope 和信任规则后才能升级为 evidence。

### 9.3 Snapshot 变化

input seal 后发生 edit/delete/purge 时：

- source row 立即 redacted/invalidated；
- 对应 manifest generation 标记 stale；
- running provider request best-effort cancel；
- 迟到输出只能保存最小 usage/error 并 discarded；
- 新 content generation 创建新 reconciliation/episode job。

Memory Pipeline 没有类似聊天发送的“3 秒允许旧结果”例外。任何 evidence revision 变化都使旧 proposal 无法接受。

## 10. Memory Agent 输入契约

### 10.1 顶层输入

canonical Memory Agent request 使用版本化结构：

```json
{
  "schema_version": 1,
  "purpose": "episode_extraction",
  "scope": {
    "account_ref": "opaque-account-id",
    "contact_ref": "opaque-contact-id",
    "conversation_ref": "opaque-conversation-id"
  },
  "episode": [],
  "supporting_context": [],
  "related_active_memories": [],
  "prior_summaries": [],
  "policy": {},
  "required_output_schema": "memory-proposals-v1"
}
```

opaque IDs 用于引用，不允许模型改变 scope。系统 prompt、typed policy 和 source content 分区由 protocol adapter 保持等价；消息正文永远作为不可信数据，而不是 developer/system instruction。

### 10.2 Episode item

每个 episode item 至少提供：

```text
message_revision_id
event_id and stable ordinal
direction / role / source
source_status=resolved
telegram time in UTC
canonical text or caption
reply/forward/album metadata when allowed
validated image content part when allowed
trust class
```

不向模型提供：

- Telethon Session、Bot token、API key 或 credential ciphertext；
- 联系人的电话号码、username 等非任务必要标识；
- raw Telegram update；
- provider raw request/response；
- deleted revision 正文；
- non-image media binary 或本地绝对路径；
- Control Bot callback token；
- 其他 conversation 的正文。

### 10.3 Related memory selection

episode extraction 可以读取：

- 同 contact、同 typed semantic slot 的 active memory；
- 可能冲突的 active event/intention/preference；
- 有助于代词消解的受限 relationship/identity；
- 最近 active rolling summary。

candidate、superseded、invalidated、forgotten、redacted memory 不进入普通输入。为 reconciliation 提供旧 version 时必须明确标记 `historical_invalidated_source`，且不得让模型把它当 current fact。

具体 token 配额和召回排序由 Context Contract 定义；Memory Pipeline 要求选择结果全部进入 manifest。

### 10.4 图片输入

caption 始终按 canonical text 处理。图片内容只有同时满足以下条件才进入 Memory Agent：

- media object 已通过 MIME、magic bytes、大小、像素和解码验证；
- 当前 revision 未 delete/redact；
- Memory Agent profile 声明支持 image input；
- adapter 支持 canonical `detail=auto` 的等价映射；
- manifest 记录 media object ID、content hash 和 inclusion reason。

不支持视觉时，不用 Main AI 的自由文本回复替代图片证据，也不把模型猜测写入 memory。只处理 caption 和其他文本。

由视觉内容单独产生的 proposal 设置 `visual_only=true`，默认只能进入 candidate。存在独立可信文本证据或管理员明确接受精确 proposal 后，才可以正式提交。

V1 不为原始图片像素建立 text embedding，也不执行自动 OCR 事实提升。

## 11. Memory Agent 输出契约

### 11.1 Strict structured output

Memory Agent 必须返回单个严格 schema 对象：

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
        "subject": "contact",
        "event": "plans_to_move",
        "destination": "Tokyo"
      },
      "rendered_text": "联系人计划下个月搬到东京",
      "confidence": 0.88,
      "importance": 0.82,
      "valid_from": null,
      "valid_to": null,
      "time_precision": "month",
      "evidence": [
        {
          "message_revision_id": "opaque-revision-id",
          "evidence_role": "explicit_statement",
          "quoted_span_start": 0,
          "quoted_span_end": 9,
          "media_object_id": null
        }
      ],
      "relations": [],
      "visual_only": false
    }
  ],
  "no_change_reason": null
}
```

没有变化时：

```json
{
  "schema_version": 1,
  "proposals": [],
  "no_change_reason": "no_long_term_value"
}
```

禁止把 explanation、chain-of-thought、Markdown code fence 或未声明字段混入输出。adapter 只做协议归一化，不修补语义错误。

### 11.2 Discriminated operation

| operation | target 要求 | 含义 |
|---|---|---|
| `create` | 无 target | 新命题或新事件 |
| `update` | 恰好一个 target | 同一事实 identity 的补充、更精确时间或非矛盾修正 |
| `supersede` | 恰好一个 target | 新事实明确替代旧事实，保留变化轨迹 |
| `invalidate` | 恰好一个 target | 证据明确表明旧记忆不成立，但没有替代事实 |
| `merge` | 至少两个 target | 多个同义 active memory 合并到一个 survivor/current version |

模型不能自行发明 UUID、version number、account/contact scope、accepted state 或 embedding vector。所有 target ID 必须原样来自输入中显式允许操作的 memory reference。

### 11.3 Typed payload

每个 `memory_type` 使用独立 versioned JSON Schema。共同字段不允许替代 typed payload：

- identity：属性、值、稳定性和归属；
- relationship：关系类型、起始/结束、互动语境；
- fact：subject、predicate、object、限定条件；
- preference：对象、倾向、强度、当前性；
- event：事件类型、参与者、时间/地点、状态；
- intention：承诺/待办、责任人、预期时间、状态；
- style：语言特征、适用场景、样本来源类别。

自由文本 `rendered_text` 只用于显示和模型输入，不能代替 typed validation 或 semantic matching。

## 12. Prompt、schema 与模型版本

每次 model run 必须快照：

```text
pipeline_version
prompt_version
input_schema_version
output_schema_version
typed_memory_schema_version
adapter_version
model_profile_id
model_config_version_id
credential_version_id
manifest_sha256
```

prompt 版本使用不可变、可部署的标识，例如 `memory-episode/1.0.0`，不能只记录 Git branch 或文件修改时间。

版本变更语义：

- retry：相同 manifest 和全部版本，复用 logical run；
- provider attempt retry：只增加 attempt number；
- prompt/schema/model config 改变：创建新 run generation；
- 批量 re-extract：显式 job kind/rebuild generation，不偷偷覆盖旧 proposal；
- acceptance policy 改变：已 accepted memory 不自动重裁决，除非显式 consolidation/review migration；
- candidate 使用接受时的最新 validator policy，但必须保留 proposal 生成时版本并显示差异。

prompt 本身不存 API key，也不把运营机密、内部路径或管理员身份传给 provider。

## 13. Evidence 与来源可信度

### 13.1 Source trust matrix

| canonical source | 联系人事实/偏好 | 本人 identity/承诺 | style-learning | 备注 |
|---|---:|---:|---:|---|
| incoming `telegram_user` | 是 | 仅联系人对本人的陈述，需标注 hearsay | 不作为本人 style | 联系人自述通常是 contact memory 主证据 |
| outgoing `human` | 是 | 是 | 高权重 | 只限 source reconciliation 已确认 |
| `copilot_approved`, edited | 受限 | 可证明已发送承诺 | 低于纯 human | 仍保留 AI draft provenance |
| `copilot_approved`, unedited | 不可单独证明 | 可证明已发送承诺 | 否 | 批准不等于真人写作 |
| `ai` | 不可单独证明 | 可证明系统已发送承诺 | 否 | 模型文本不能自我证明外部事实 |
| `proactive_ai` | 不可单独证明 | 可证明系统已发送承诺 | 否 | 必须保留 proactive provenance |
| `system_pending` | 否 | 否 | 否 | 对账完成前不处理 |

“可证明已发送承诺”只表示该 canonical outgoing 确实被发送，例如“我今晚会发文件”；它不证明承诺中的第三方背景事实一定真实。

forwarded body、reply quote 和引用的第三人文本必须保留原始 attribution，使用低信任 `quoted_external` 类别；它们不能因为由联系人转发就自动成为联系人自述或真人本人风格样本。联系人对转发内容新增的 caption/comment 可以作为独立 evidence，模型和 validator 不得把两者合并来源。

### 13.2 Evidence role

允许的稳定 role 至少包括：

```text
explicit_statement
explicit_correction
confirmed_commitment
observed_style_sample
temporal_update
supporting_context
visual_observation
manual_confirmation
```

`supporting_context` 不能单独激活记忆。`visual_observation` 默认触发 candidate policy。

### 13.3 Quote span

文本证据 quote 使用 canonical `content_text` 的 Unicode code point 半开区间 `[start, end)`，并记录 text normalization version。validator 必须确认：

- revision ID 属于本 account/conversation；
- revision current、未 redacted、未 tombstone；
- span 边界有效且非空；
- span 内容与模型声明一致；
- caption/text 类型允许；
- evidence role 与 source trust 组合合法。

quote 只保存在 proposal evidence 的 offset，不复制第二份正文。人工 UI 根据仍有效 revision 临时渲染最小片段。

### 13.4 Image evidence

image evidence 同时引用 message revision 和属于该 revision 的 validated media object。media hash、validation generation 或 revision 变化时，该证据失效。

正式 `memory_evidence` 仍绑定 canonical message revision；supplemental media reference 用于证明该 revision 中具体图片。删除 message 或 media 会通过双向索引定位所有 proposal/version/summary。

### 13.5 Recursive evidence root

正式 memory 可以直接引用 message revision，也可以引用 summary version 或 other memory version，但每条派生链必须：

- 无环；
- 最终到达至少一个 current、未 redacted canonical message revision；
- 每一跳 scope 一致；
- 每一跳 source membership 可查询；
- 不经过 forgotten/invalidated content；
- 遍历时有 visited set 和最大深度 8。

summary 自身不是不可质疑的事实。无法找到 root revision 的 proposal 必须拒绝为 `evidence_root_missing`。

## 14. Proposal 验证顺序

应用层按固定顺序验证，任何前置失败都不得靠后续高 confidence 覆盖：

1. model run、job、manifest 和 output schema/version 匹配；
2. JSON 严格解析、字段集合、长度、枚举和数值范围；
3. account/contact/conversation scope；
4. target memory 当前状态和 version；
5. evidence revision/media existence、current、hash、span 和 trust；
6. typed payload schema 和 canonical normalization；
7. temporal value、timezone、precision 和 interval；
8. semantic slot、重复命题和 target operation 规则；
9. conflict 与 source-specific policy；
10. confidence/importance policy；
11. proposal idempotency；
12. acceptance transaction 的 current-state recheck。

稳定 validation code 示例：

```text
schema_invalid
unknown_field
scope_mismatch
target_not_in_manifest
target_version_changed
evidence_missing
evidence_redacted
evidence_span_invalid
evidence_source_disallowed
evidence_root_missing
visual_only_requires_review
temporal_invalid
semantic_duplicate
ambiguous_conflict
confidence_candidate
confidence_too_low
stale_manifest
already_applied
```

validation code 可以进入日志和 Control Bot；正文、quote、模型原始输出和 provider error body不进入普通日志。

## 15. Confidence、importance 与时间

### 15.1 Confidence

Memory Agent 的 `confidence` 是建议值，不是校准概率。validator 结合证据信任、明确性、冲突和 memory type 得到 policy result：

```text
confidence >= 0.85
AND trusted evidence
AND no unresolved conflict
AND not visual-only
AND deterministic validation passes
  -> eligible for automatic acceptance

0.60 <= confidence < 0.85
OR valid but ambiguous/conflicting/visual-only
  -> candidate

confidence < 0.60
  -> rejected
```

per-type policy 可以提高门槛，不能低于部署的安全下限。identity、重要关系和涉及第三人的敏感事实默认不因单条间接证据降低门槛。

人工接受不会修改模型原始 confidence；正式 version 记录 proposal confidence、acceptance actor 和 `manual_confirmation` evidence/relation。

### 15.2 Importance

importance 表示未来检索和 proactive 考虑价值，不表示真假。建议语义：

```text
0.00-0.29 transient / low retrieval value
0.30-0.69 normal
0.70-0.89 important
0.90-1.00 identity-critical or major commitment/event
```

高 importance 不能绕过 evidence/confidence。consolidation 可以创建新 version 调整 importance，但必须记录 reason 和证据，不能原地改数字。

### 15.3 时间有效区间

时间字段分开保存：

```text
observed_at       证据被说出/观察的时间
valid_from/to     事实适用区间
event_start/end   事件发生区间
expected_at       intention 预期时间
time_precision    exact/day/week/month/relative/unknown
timezone          IANA name or unknown
```

“下个月”等相对时间以 evidence message 的 Telegram time 和有效时区解析，并保存解析规则版本和原始 typed expression。无法确定时区或日期时保留较粗 precision，不臆造精确 UTC。

`valid_to` 到期不自动忘记历史事实；Context/Proactive 读取时根据状态和时间选择 current relevance。

## 16. Operation 事务语义

### 16.1 `create`

适用于不存在语义等价 active memory 的新命题。事务：

1. 锁 account/contact scope 和可能匹配的 semantic slot；
2. 再次查重；
3. 创建 stable memory identity 和 version 1；
4. 写 evidence/relation；
5. 标记 proposal accepted；
6. 创建 embedding/outbox；
7. commit。

发现等价 active memory 时不创建第二条；转为 `merge/update` 或 `already_applied`。

### 16.2 `update`

适用于同一命题的非矛盾补充，例如更精确时间、补充地点或新增独立证据。创建同一 memory identity 的下一个 immutable version，current pointer CAS 前进。

旧 version 保留历史，除非 delete/forget/purge 要求 redaction。update 不能把“喜欢咖啡”改成“不再喝咖啡”；后者是 supersede。

### 16.3 `supersede`

仅当新证据明确表达时间变化或纠正时自动执行：

- 创建新 memory identity/current version；
- 将旧 memory 状态改为 superseded；
- 设置 `superseded_by_memory_id`；
- 建立 version relation `supersedes`；
- 旧 version 不进入 active Context，但历史仍可审计；
- 若旧证据之后被 delete/forget，旧正文仍按单向 redaction 清除。

### 16.4 `invalidate`

适用于证据明确否定旧事实且没有 replacement，或全部有效 evidence 消失。事务创建 invalidation version/history，将 memory status 改为 invalidated，立即使当前 payload 和 embedding 退出检索。

因 delete/forget 触发时，旧正文按 erasure policy 单向清除；不能仅打 `active=false` 后继续保留可恢复正文。

### 16.5 `merge`

适用于多个 active memory 表达同一命题：

- validator 选择确定性的 survivor，优先最早 stable identity；
- survivor 创建 merge version，合并去重后的有效 evidence；
- 其他 memory 标为 superseded by survivor；
- 建立 `merges` relations；
- confidence 不做简单相加，按独立证据数量和 source policy 重新计算；
- 重试看到相同 survivor/current version 时返回 `already_applied`。

### 16.6 多 proposal 原子性

同一 model output 的 proposals 默认逐条独立裁决；一个 proposal schema/conflict 失败不回滚其他无依赖 proposal。

若 proposal 显式依赖同一 output 中另一 proposal，validator 建立 DAG 并按拓扑顺序提交相关原子组。循环依赖整组拒绝。不能由数组顺序隐式表达事务依赖。

## 17. 冲突与 consolidation

### 17.1 Semantic slot

validator 从 typed payload 生成版本化 normalized semantic key，例如：

```text
scope + memory_type + subject + predicate + qualifiers
```

模型提供的 `semantic_key` 只是候选提示；应用层必须重新生成并保存 hash。自由文本相似度只能发现候选集合，不能单独决定 merge/supersede。

### 17.2 确定性分类

| 情况 | 动作 |
|---|---|
| typed proposition 相同、时间重叠、证据新增 | update/merge evidence |
| 新事实明确声明从某时起改变 | supersede |
| 新消息明确纠正旧消息 | supersede 或 invalidate |
| 两个值可在不同条件/时间同时成立 | 保留两者并补 qualifiers |
| 无法判断变化、例外还是矛盾 | 旧 active 保持；新 proposal candidate + `contradicts` |
| 仅 embedding 相似 | 不自动冲突 |

### 17.3 Consolidation 安全边界

consolidation 可以：

- 合并重复 evidence；
- 提议 merge/supersede/invalidate；
- 创建 relationship/long-term summary；
- 更新检索淡化特征；
- 关闭已完成/取消/过期 intention 的 current relevance。

consolidation 不可以：

- 在没有新 root evidence 时创造事实；
- 仅因模型认为“不重要”而 forget；
- 把歧义冲突自动覆盖；
- 恢复 forgotten/redacted content；
- 把 AI 自生成文本改标为 human style。

## 18. Proposal 状态与人工审计

### 18.1 状态机

```text
received
  -> validating
       -> accepted
       -> candidate
       -> rejected
       -> error

candidate
  -> accepted
  -> rejected
  -> invalidated
  -> expired
```

accepted/rejected/invalidated/expired 是 terminal decision。`error` 表示 pipeline/adapter/validator 故障；是否重试由 job generation 决定，不能把 error 当低置信度 candidate。

candidate 在以下变化后立即 invalidated：

- evidence revision edit/delete/redaction；
- target memory current version 改变；
- account/contact purge；
- proposal retention 到期；
- typed schema 不再受支持且没有明确 migration。

### 18.2 Control Bot 命令

```text
/memory [contact]
/memory_candidates [contact]
/memory_accept <proposal-short-id>
/memory_reject <proposal-short-id>
/forget <memory-short-id>
/memory_status [contact]
```

- `/memory` 展示 active memory 的最小摘要、类型、时间和 short ID；
- `/memory_candidates` 只列待审 candidate，不混入 active；
- `/memory_accept` 展示 proposal、证据最小片段、来源类别、confidence、冲突和将执行的 operation；
- `/memory_reject` 记录 reason code，可选短说明；
- `/forget` 明确说明只忘记该 memory，默认不删除 canonical message；
- `/memory_status` 显示 freshness、watermark lag、pending/dead-letter 数，不显示正文。

接受和 forget 需要第二次确认。callback data 只含短期 opaque token；token 绑定 admin、Bot chat、action、proposal/memory、expected version 和 expiry，只存 hash、单次使用。Control Bot 不能直接 UPDATE memory 表。

### 18.3 接受 candidate

worker 在 account/contact scope lock 下：

1. 验证 durable command actor 和一次性 token；
2. 锁精确 proposal 和所有 target；
3. 确认 candidate、未过期、evidence/current version 未变化；
4. 使用当前 validator policy 重跑确定性校验；
5. 添加 `manual_confirmation` provenance；
6. 按 operation 原子提交；
7. 标记 command/token used 并写 audit/outbox。

管理员确认不允许接受 scope mismatch、deleted evidence、invalid schema 或 forgotten target；人工动作只解决置信度/歧义判断，不能绕过数据完整性与删除门禁。

### 18.4 Bot 隐私边界

候选和 active memory 正文属于敏感数据。Control Bot：

- 只向 allowlisted admin 的绑定 private Bot chat 显示；
- 默认显示最小必要摘要，不推送完整 conversation；
- 不在 callback data、日志或 error 中放正文；
- 编辑/拒绝说明使用短生命周期 ForceReply session；
- Bot 消息删除只降低暴露，不作为服务端 erasure 证明；
- contact purge/account wipe 要清理仍受系统控制的 Bot-side durable notification references。

## 19. Summary Pipeline

### 19.1 Summary identity 与 source membership

每个 summary version 除 inclusive processing range 外，必须保存 ordered source membership：

```text
message_revision_id
OR prior_summary_version_id
ordinal
inclusion_role
source_content_sha256
```

CHECK 要求恰好一个 source。source membership 是 delete/edit reconciliation 和 evidence root traversal 的依据；只保存 `range_start/end` 不足以证明具体使用了什么。

### 19.2 Rolling summary

默认触发：

```text
50 new eligible revisions
OR 12000 estimated unsummarized tokens
```

worker 从 rolling watermark 之后选择连续已裁决 event，并可以读取前一个 active rolling summary作为前置压缩状态。新 version 的 source membership 必须同时记录 previous summary 和新增 revisions。

watermark CAS、summary version、source rows 和 current pointer 在同一事务提交。provider 成功但事务失败时，重试按 manifest/idempotency key 找回或重建，不能只推进 watermark。

### 19.3 Daily summary

daily identity 使用：

```text
conversation_id
period local date
timezone snapshot
period start/end UTC
summary kind=daily
```

迟到 event 落入已完成 period 时创建新 daily version，并递归失效依赖旧 daily 的 weekly/consolidated summary。不会原地改写旧 content。

### 19.4 Weekly summary

weekly 只消费已完成 daily versions 和必要的明确 root revisions，不重复无界读取整周 raw history。若某 daily stale/rebuilding，weekly job 等待或进入 retry_wait，不以不完整来源推进 watermark。

### 19.5 Summary 内容契约

summary 输出至少分区：

```text
topics
confirmed facts mentioned
open intentions/commitments
relationship changes
notable events/time anchors
conversation tone
uncertainties/conflicts
source coverage
```

必须保留“谁说的”和 source provenance，不能把 AI 生成的猜测压缩成联系人已确认事实。summary content 进入 Context 时为 `trusted_derived` 数据，不是 system instruction。

### 19.6 Edit/delete/rebuild

source edit/delete 的事务先：

1. 通过 membership index 找到依赖 summary versions；
2. 将受影响 current summary 标记 quarantined/invalidation pending；
3. 使其 embedding 立即不可检索；
4. 创建 reconciliation/rebuild jobs；
5. commit 后才运行模型。

重建从最近未受影响 predecessor 或 period root 开始。replacement 提交后，对旧 summary content/hash/embedding 单向 redaction；不保留能恢复 deleted source 的派生文本。

## 20. Embedding Pipeline

### 20.1 Eligible target

V1 建立 embedding 的 target：

- active memory current version；
- active rolling/daily/weekly/consolidated summary current version；
- Context Contract 允许向量召回的 current canonical text/caption message chunks。

不建立：

- proposal candidate/rejected/error；
- superseded/invalidated/forgotten/redacted version；
- deleted/tombstone message；
- raw image pixels、音视频或其他未下载媒体；
- Control Bot 草稿和 provider raw body。

### 20.2 Chunk 与幂等

每个 target 使用版本化 chunker：

```text
embedding key = embedding_space_id + target_version_id + chunk_index
source_sha256 = hash(normalized chunk text + chunker_version)
```

同 key/hash 已存在时返回成功。hash 变化时先创建新 current target/revision，再使旧 embedding invalidated；不能在同一个 immutable target ID 上静默替换来源内容。

### 20.3 Space 隔离

一个 embedding space 固定：

```text
provider/model/config version
dimensions
distance metric
normalization
chunker version
generation
```

检索 query 必须绑定单个 active space ID。禁止在一次排序中混合不同 model、dimension、metric 或 normalization 的向量分数。

### 20.4 更换模型和重建

```text
create new space state=building
  -> enumerate eligible targets at build snapshot
  -> generate/upsert chunks idempotently
  -> compensation scan for changes after snapshot
  -> verify count, dimensions, source hashes and sample retrieval
  -> acquire activation lock
  -> catch up final delta
  -> atomically set new active / old retired
  -> invalidate/delete old records per retention
```

build 失败时旧 active space 继续服务。没有完整 coverage、dimension 校验和 final delta catch-up 时不得激活。切换不重新接受 memory，也不改变 summary watermark。

### 20.5 Source 失效

edit/delete/forget/purge/wipe 时，embedding 在同一逻辑删除事务中先 `invalidated_at`，查询必须立即排除。物理向量删除可异步，但不能依赖 ANN index refresh 才实现不可见。

backup restore 后先重放 erasure ledger、重建 active-space eligibility，再开放模型检索。

## 21. 淡化、完成状态与 forget

### 21.1 淡化不改写历史

时间淡化是 Context/Proactive 的检索特征，不修改 immutable version 的原始 importance/confidence：

```text
base importance
source confidence/trust
recency since last supported observation
usage/retrieval count
valid interval/current status
memory type stability class
```

具体排序公式由 Context Contract 定义。Memory Pipeline 只提供可追溯特征和 consolidation proposal。

### 21.2 不简单衰减的类型

以下内容默认不因年龄自动失效：

- account identity/personality；
- 重要关系；
- core style；
- 核心偏好；
- 重要经历。

event/intention 到期或完成时改变 current relevance/status，不删除历史。新的明确证据可以 supersede/invalidate。

### 21.3 Memory forget

`/forget <memory>` 是精确 stable identity 的不可逆内容清除操作：

1. 展示 scope、type、最小摘要和“不会删除 canonical chat”说明；
2. allowlisted admin 二次确认；
3. 创建 durable erasure command；
4. 事务中将 memory 标为 forgotten，并立即退出 Context；
5. 清除全部 memory version payload/rendered text/content hash；
6. invalidate 并删除 embedding；
7. 清除 proposal/summary 中仅由该 memory 派生且可能恢复正文的内容；
8. 保留最小 non-content ledger 和 source revision ID 关系；
9. 向备份 erasure ledger 记录 generation。

forget 默认不删除仍合法存在的 canonical message revision。contact purge 和 account wipe 是不同、更大 scope 的操作，不能用循环 `/forget` 代替。

## 22. Freshness 与积压降级

### 22.1 Freshness 状态

按 conversation 计算：

```text
fresh:
  episode watermark covers latest eligible event
  AND no reconciliation pending

degraded:
  uncovered eligible revisions <= 20
  AND oldest uncovered age <= 10 minutes
  AND no dead-letter/reconciliation quarantine

stale:
  uncovered revisions > 20
  OR oldest uncovered age > 10 minutes
  OR required job dead-letter
  OR affected active memory/summary is quarantined
```

summary 和 embedding 另有独立 freshness；一个 conversation 可以 `memory=fresh, summary=degraded, embedding=building`。

### 22.2 Main AI 降级契约

Main AI 不等待 worker。Context Builder 至少接收：

```text
episode watermark
latest eligible event ID
freshness state and stable reason code
active memory/summary version IDs
uncovered canonical range
embedding active space/build state
```

存在 uncovered range 时，Context Builder 在自身 token/image budget 内扩大 watermark 之后的 recent canonical window。超过预算时：

- 优先保留 current message 和最新 canonical revisions；
- 记录被省略 range、event count 和 reason；
- 不用 candidate 或 stale quarantined summary 填补；
- 不声称已拥有未提取事实；
- manifest 明确标记 memory freshness。

具体装配顺序、截断和 token 数由 Context Contract 定义。

### 22.3 Backpressure

worker backlog 时：

- 新 ingest 仍只做有界 pending-range refresh；
- Scheduler 不为同一 range 产生重复 jobs；
- reconciliation、delete/forget 优先于普通 extraction；
- episode extraction 优先于 summary/consolidation/embedding rebuild；
- 每个 account/conversation 设置公平领取和最大并发；
- stale 状态告警但不自动切换 conversation base mode；
- provider 长期不可用时进入 operational dependency status，不丢 watermark。

## 23. 并发、锁与事务

### 23.1 Lock 顺序

跨 memory 事务统一使用：

```text
account scope
  -> contact
  -> conversation memory watermark/job
  -> target memories by sorted UUID
  -> proposal
  -> summary identity/version
  -> review command
```

不得在持有数据库锁时调用 provider、Redis、Telegram 或文件系统。

### 23.2 Worker lease

generic background job 使用 `FOR UPDATE SKIP LOCKED` 领取，并保存 owner token/lease expiry。只有匹配 token 的 worker 可以续租或提交 terminal state。

memory domain job 在 model call 前已 seal；lease 过期后另一 worker可以恢复同 logical run 或创建允许的新 attempt。旧 worker 迟到提交时因 owner/generation CAS 失败，只能丢弃结果。

### 23.3 Proposal acceptance transaction

```text
BEGIN
  lock proposal and review command if any
  lock target/current memory identities in stable order
  revalidate manifest/evidence/target versions
  insert immutable memory version(s)
  insert evidence, target and relation rows
  update current pointer/status with CAS
  mark proposal/review command terminal
  enqueue embedding/outbox
COMMIT
```

任一步失败整体回滚；不能出现 active version 没有 evidence、accepted proposal 没有 result，或 watermark 越过未提交 proposal。

### 23.4 Summary transaction

```text
BEGIN
  lock summary identity and watermark
  verify source revisions/versions and manifest hash
  insert immutable summary version
  insert ordered source memberships
  update current pointer and watermark with CAS
  enqueue embedding/outbox
COMMIT
```

### 23.5 At-least-once 边界

队列、outbox、provider attempt 和 worker execution 都按 at-least-once 设计。正确性来自：

- canonical event unique；
- job/model/proposal idempotency key；
- immutable input manifest；
- target current-version CAS；
- sorted locks；
- unique embedding target key；
- watermark contiguous advancement。

## 24. 重试、死信与崩溃恢复

### 24.1 Retry 分类

可重试：

- provider timeout/429/5xx；
- transient network/DNS；
- lease expiry；
- serializable/deadlock retry；
- embedding batch partial failure；
- Redis/outbox notification failure。

不可原样重试：

- output schema invalid after bounded repair-free attempts；
- evidence redacted/scope mismatch；
- unsupported image/protocol capability；
- prompt/schema/config 被禁用；
- purge/wipe；
- deterministic policy rejection。

schema invalid 不通过自由文本“修复 prompt”直接改写原 proposal。可以创建有界的新 run generation，仍需完整 provenance。

### 24.2 Dead letter

超过 max attempts 的 job 进入 dead-letter，保存：

```text
job/run IDs
scope and range
stable error code
attempt count/times
model/config/prompt/schema versions
redacted provider metadata
next operator action
```

不保存 API key、headers、完整 prompt、message/memory 正文或 raw response。管理员 retry 创建新 generation 或重新开放原逻辑 job；不能直接把 dead-letter 标 succeeded。

### 24.3 Crash points

| 崩溃点 | 恢复行为 |
|---|---|
| ingest commit 前 | Telegram replay 重新处理，不存在 job 范围孤儿 |
| ingest commit 后、outbox 前 | 同事务 outbox 已存在；relay/scan 补发 |
| input manifest 前 | lease 过期后重新 seal current valid range |
| provider call 中 | attempt unknown/timeout；重领者按 run state 决定重试 |
| provider 成功、proposal 前 | 同 manifest/run 重放，proposal key 去重 |
| proposal 保存后、accept 前 | validator 扫描 `received/validating` 恢复 |
| memory commit 后、job success 前 | accepted result/idempotency 被发现，不重复创建 version |
| summary version 后、watermark 前 | 二者同事务，不产生该状态 |
| embedding build 中 | 按 space/target/chunk key 续建 |
| space activation 中 | 单事务 active binding；旧或新只有一个 active |

### 24.4 启动补偿顺序

1. 应用 erasure ledger，隔离恢复出来的 deleted/forgotten content；
2. 扫描 expired leases 和 `received/validating` proposal；
3. 补建 uncovered episode jobs；
4. 扫描 summary source/current/watermark 一致性；
5. 失效 source hash 不匹配的 embedding；
6. 继续 building space 或标记 failed；
7. 发布 freshness/health；
8. 之后才允许 derived memory 进入 Main AI Context。

## 25. Data Model 增量

本文要求 `docs/architecture/03-data-model.md` 的既有骨架增加以下持久化契约。

### 25.1 `memory_watermarks`

```text
account_id
conversation_id
watermark_kind episode|reconciliation
last_scanned_event_id
last_contiguous_decided_event_id
last_succeeded_job_id
version
updated_at
PRIMARY KEY (conversation_id, watermark_kind)
```

### 25.2 Memory job/input

`memory_jobs` 增加：

```text
generation
job_version
eligible_revision_count
estimated_input_tokens
pipeline_version
policy_version
prompt_version
input_schema_version
output_schema_version
sealed_at
input_manifest_id
```

新增 `memory_input_manifests` 与 `memory_input_manifest_items`，保存第 9 节 typed immutable membership 和 hash；`model_runs` 增加 nullable `memory_input_manifest_id`，仅 `logical_role=memory_agent` 时允许。

### 25.3 Proposal/evidence/targets

`memory_proposals` 增加：

```text
proposal_ordinal
semantic_key_hash
proposed_confidence
proposed_importance
proposed_valid_from/to
visual_only
accepted_memory_version_id?
decision_actor_type/actor_id?
decision_reason_code
validator_policy_version
```

新增 `memory_proposal_targets`，以 `(proposal_id, target_memory_id, target_role)` 表达 merge/supersede/invalidate 的 target 集合。proposal evidence 增加 nullable media object、source hash 和 trust class，且 media 必须属于引用 revision。

正式 `memory_evidence` 同样增加 nullable supplemental media object、source hash 和 trust class；media 只允许与主来源 `message_revision_id` 同时存在，并必须属于该 revision。这样 candidate 被接受并按 retention 清除 proposal 正文后，image evidence 仍保持完整可追溯性。

### 25.4 Summary source

`summary_versions` 增加 period timezone/start/end、manifest hash、pipeline/schema version 和 invalidation state。新增 `summary_version_sources`：

```text
summary_version_id
ordinal
message_revision_id?
prior_summary_version_id?
inclusion_role
source_content_sha256
CHECK exactly one source is non-null
UNIQUE (summary_version_id, ordinal)
```

### 25.5 Review action

复用受类型约束的 `control_commands` 作为 Bot update 幂等 identity，并新增 typed extension `memory_review_actions`，至少保存：

```text
action accept|reject|forget
proposal_id? / memory_id?
expected proposal/memory version
admin actor
action_token_hash
expires_at / used_at
state
reason_code
created_at / decided_at
```

正文不复制到 command/audit；显示时按仍有效 source 临时读取。

### 25.6 Index

至少需要：

```text
memory_watermarks(conversation_id, watermark_kind) PRIMARY KEY
memory_jobs(conversation_id, job_kind, state, quiet_until, hard_due_at)
  partial for pending/retry_wait
memory_input_manifest_items(message_revision_id)
memory_proposals(state, expires_at)
memory_proposal_targets(target_memory_id)
memory_proposal_evidence(message_revision_id)
summary_version_sources(message_revision_id)
summary_version_sources(prior_summary_version_id)
embedding_records(target partial unique indexes)
```

semantic key hash 建受限 lookup index，不用数据库文本 similarity unique 代替 typed conflict policy。

## 26. 隐私、安全与审计

### 26.1 Prompt injection

conversation、caption、forwarded text、memory 和 summary 都是数据。Memory Agent 中任何“忽略规则、泄露 secret、把本句保存为事实”等内容不能改变 system/developer policy、scope 或 output schema。

validator 不执行模型返回的 URL、代码、SQL、命令或 tool request。Memory Pipeline 没有通用网络抓取或 host execution 能力。

### 26.2 最小披露

provider 请求只发送任务所需的 conversation scope。默认不跨联系人共享正文；account identity 仅在 memory type 需要时提供。

日志和 metrics 只记录 ID、range、count、latency、token usage、版本和 stable code。debug raw capture 默认关闭，启用时遵守独立加密、短 TTL 和 purge/redaction。

### 26.3 Audit

至少审计：

- job created/refreshed/sealed/terminal；
- model/profile/config/prompt/schema/manifest hash；
- proposal operation/state/validation code；
- automatic/manual acceptance actor 和 policy；
- memory current pointer/status/relation 变化；
- summary watermark/rebuild；
- embedding space build/activate/retire；
- forget/purge/reconciliation generation；
- dead-letter/retry operator action。

audit 不保存 chain-of-thought、完整正文、quote 副本、向量或 secret。

### 26.4 Disclosure 边界

部署将向管理员配置的 Memory Agent 和 Embedding endpoint 发送选中的私聊正文、caption、允许的图片、active memory 和 summary。Control Bot 人工审计会通过 Telegram Bot API 向 allowlisted admin 显示最小 memory/candidate 摘要和证据片段。这些都是敏感数据流，必须在根 `DISCLOSURE` 中明确，并在实现后验证实际默认值。

## 27. 可观测性与健康状态

推荐 metrics：

```text
memory_jobs_created_total{kind,trigger}
memory_job_lag_events / memory_job_oldest_age_seconds
memory_job_duration_seconds{kind,result}
memory_proposals_total{operation,state,validation_code}
memory_auto_accept_total / manual_review_total
memory_conflicts_total{resolution}
summary_lag_events{kind}
summary_rebuild_total{reason}
embedding_records_total{space,target_type}
embedding_build_progress_ratio{space}
embedding_source_mismatch_total
memory_dead_letters_total{kind,error_code}
```

`/server_status` 只聚合：

```text
memory freshness counts
oldest pending age
pending/running/retry/dead-letter counts
memory model endpoint/config/credential readiness
embedding active/building space status
last successful compensation scan
```

不显示联系人、正文、semantic key、prompt 或 provider error body。

## 28. 自动化验收场景

### 28.1 Trigger 与范围

- confirmed AI、human、proactive outgoing 和非自动模式 incoming 任一触发 pending job。
- 45 秒 quiet window 被新 eligible event 刷新，hard due 不延后。
- 20 revisions、6000 tokens、10 minutes 任一硬阈值使 job runnable。
- duplicate update 不增加计数或刷新 deadline。
- running range 不扩张；新 event 创建下一 generation。
- compensation scan 能从 watermark 重建遗漏 job。

### 28.2 Replay 与幂等

- 相同 range/manifest/versions 重跑只产生同 proposal/result。
- provider 成功后 worker 崩溃，恢复不创建第二个 memory version。
- 新 prompt/model config 可重跑，但不会创建重复 active semantic memory。
- watermark 存在 hole 时不能前进。
- Redis 清空后 PostgreSQL scan 恢复全部 pending work。

### 28.3 Evidence 与 policy

- model 引用其他 conversation、旧 revision、越界 span 或 redacted source 时拒绝。
- forwarded/reply quoted content 保持第三人 attribution，不能被提升为联系人自述或 human style。
- `ai/proactive_ai` 不能单独激活联系人事实或 human style。
- edited COPILOT style 权重低于纯 human；unedited COPILOT 不作为 human style。
- image-only proposal 进入 candidate；无视觉能力时图片不传 provider。
- summary/other memory evidence 可递归到 root revision；循环或断链拒绝。
- 高 importance/高 confidence 不能绕过 evidence policy。

### 28.4 Conflict 与人工审计

- 同命题新增独立证据创建 update/merge，不重复 create。
- 明确“最近戒咖啡”可以 supersede 旧偏好并保留时间轨迹。
- 歧义冲突保持旧 active，新 proposal candidate。
- candidate evidence edit/delete 后 action token 即使未过期也不能接受。
- 非管理员、转发 callback、token 重放、旧 expected version 全部拒绝。
- `/forget` 二次确认后正文和 embedding 不可恢复，但 canonical chat 默认仍存在。

### 28.5 Summary

- 第 50 条 revision 或 12000 tokens 触发 rolling summary。
- summary version/source/watermark 同事务提交。
- 迟到 event 重建正确 daily，并递归失效 weekly。
- delete source 先隔离 summary，再重建和单向 redaction。
- empty day/week 不生成 summary。
- timezone 改变不静默重切历史 period。

### 28.6 Embedding

- candidate/redacted/forgotten target 从不进入 active retrieval。
- edit 产生新 revision/vector，旧 vector 立即 invalidated。
- shadow space 未完整时旧 space 继续 active。
- dimension/hash/count/final delta 通过后才原子切换。
- query 永远只绑定一个 active space。
- build crash 后按 target/chunk key 继续，不重复记录。

### 28.7 Freshness 与恢复

- memory lag 在阈值内为 degraded，超过硬阈值或 dead-letter 为 stale。
- Main AI 在 stale 时仍可运行，并扩大未处理 canonical window。
- candidate 和 quarantined summary 不进入降级 Context。
- backup restore 先执行 erasure reconciliation，再开放 memory retrieval。
- Memory provider 故障不阻止 ingest、human outgoing 或已有 committed memory 读取。

## 29. 后续文档边界

Context Contract 定义各 context layer 的精确 token/image 配额、structured/vector/recent 排序、freshness 降级截断、prompt trust boundary 和三种 generation protocol 的 wire mapping；必须消费本文的 manifest/source/freshness 契约。

Proactive Pipeline 只读取 accepted active memory、event/intention 和 freshness；不得消费 candidate、stale quarantined summary 或用 proactive 输出反向自证新事实。

Operations 确定 proposal/review/job/audit/old embedding space 的 retention 数值、provider timeout/retry、磁盘/备份加密、queue 配额、时钟和 alert threshold。

Test Strategy 将第 28 节实现为 fake Memory/Embedding provider、可重放 revision fixtures、并发事务、虚拟时钟、故障注入和 restore/erasure 测试。

## 30. 验收条件

- [x] episode extraction、rolling summary、consolidation 和 reconciliation 职责已定义。
- [x] OR 触发、45 秒 quiet window、三类硬阈值和 5 分钟补偿扫描已定义。
- [x] pending job 刷新、running 范围不可变、generation 和分层幂等键已定义。
- [x] Memory Agent input manifest、图片能力、strict output schema 和版本管理已定义。
- [x] proposal 验证顺序、状态、自动接受、candidate、拒绝和人工审计已定义。
- [x] create、update、supersede、invalidate 和 merge 的事务语义已定义。
- [x] evidence trust、recursive root、confidence、importance 和时间区间已定义。
- [x] rolling/daily/weekly summary source、watermark、迟到事件和重建规则已定义。
- [x] embedding target、chunk、space 隔离、shadow rebuild 和原子切换已定义。
- [x] 冲突、淡化、forget、edit/delete reconciliation 和 one-way redaction 已定义。
- [x] fresh/degraded/stale 和 Main AI 非阻塞降级契约已定义。
- [x] 数据模型增量、锁、事务、retry、dead-letter、恢复、审计和验收场景已定义。
