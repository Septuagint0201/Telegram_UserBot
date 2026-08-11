# Data Model

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的 PostgreSQL 逻辑数据模型、实体身份、版本化方式、业务唯一键、外键、检查约束、索引、事务边界、保留与删除语义，以及 Alembic migration 规则。

总体设计见`docs/Design.md`，运行组件所有权见`docs/architecture/01-runtime-topology.md`，消息状态机见`docs/architecture/02-message-lifecycle.md`，模式与控制状态机见`docs/architecture/04-conversation-orchestrator.md`，记忆运行语义见`docs/architecture/05-memory-pipeline.md`，上下文与adapter见`docs/architecture/06-context-contract.md`，主动门禁见`docs/architecture/07-proactive-pipeline.md`，retention、加密、backup和migration运行规则见`docs/architecture/08-operations.md`。后续实现不得绕过本文定义的账号边界、版本快照、证据链、删除语义、预算事务和发送幂等约束。

当前状态：V1 架构基线。

## 2. 已确认决策

| 决策 | V1 选择 |
|---|---|
| 多账号身份 | 全局 `telegram_peers` + 账号级 `account_peers`；主要业务表显式保存 `account_id` |
| 正文保护 | PostgreSQL 中使用可查询明文列；宿主机磁盘、数据库备份和导出产物必须加密 |
| edit 历史 | 保留 message content revision |
| delete 历史 | 清除所有正文 revision 和媒体，只保留 tombstone 与必要审计元数据 |
| raw payload | 默认不保存；仅允许显式启用的加密短期 debug capture |
| canonical retention | messages 和 memories 默认不自动过期；短期状态使用 retention class |
| 删除操作 | memory forget、contact purge 和 account wipe 是三个不同操作 |
| 模型配置 | 部署级 `main_ai`、`memory_agent`、`proactive_agent`、`embedding` 四个独立 role binding |
| 模型版本 | draft 可修改；验证后生成不可变 config version；每个 role 同时至多一个 active version |
| credential | 每个 role 独立 credential 与 credential version；endpoint 可以复用 |
| Memory | 稳定 memory identity + 不可变 memory version + typed JSONB + evidence join |
| Audit | 数据库 append-only audit，不复制正文；V1 不建立密码学 hash chain |
| 分区 | V1 不启用表分区，但高增长表保留未来按时间迁移的边界 |

## 3. 全局约定

### 3.1 PostgreSQL 与扩展

PostgreSQL 是所有长期业务状态的事实源。V1 使用：

```text
PostgreSQL
pgvector
```

Redis 只承载队列通知、缓存、短期 lease 和 heartbeat 加速。任何必须在 Redis 丢失后恢复的事实，都要存在于 PostgreSQL 的 domain row、job、watermark 或 transactional outbox 中。

### 3.2 ID 策略

| 数据类别 | 主键策略 |
|---|---|
| 稳定业务实体、跨表引用、配置版本 | UUIDv7，由应用层生成 |
| 高吞吐 append-only event、attempt、audit、outbox | `BIGINT GENERATED ALWAYS AS IDENTITY` |
| Telegram 原生 ID | `BIGINT`，作为业务键组成部分，不作为数据库主键 |

UUIDv7 在创建事务前生成，便于队列和日志提前关联；不能依赖随机 UUID 的物理顺序表达业务时间。所有顺序仍使用显式时间、Telegram message ID、revision number 或 sequence。

### 3.3 时间与时区

- 所有持久化时间使用 `TIMESTAMPTZ`，写入 UTC。
- Telegram 原始时间保存为 `telegram_event_at`，首次观测时间保存为 `observed_at`。
- 业务日期和联系人本地日历使用独立 `DATE`、`TIME` 和 IANA timezone 字段，不能把本地时间伪装成 UTC。
- 仅需要相对时长时使用整数秒或毫秒，并在字段名中写明单位。
- `created_at` 由数据库默认 `clock_timestamp()` 或 `now()` 生成；外部调用者不能覆盖审计时间。

### 3.4 状态与类型

V1 不使用 PostgreSQL native enum。封闭状态机使用 `TEXT NOT NULL` 加命名 `CHECK` constraint；可扩展业务类型使用 lookup table 或带 `schema_version` 的 typed JSONB。

原因：native enum 删除或重命名值需要特殊 migration，且后续架构文档仍可能增加状态。状态变更必须同时更新应用常量、CHECK constraint、迁移和契约测试。

### 3.5 账号隔离

除全局 `telegram_peers`、共享 `model_endpoints` 和纯 lookup table 外，主要业务表显式保存 `account_id`，即使可以通过 `conversation_id` 间接推导。

每个父表提供 `(id, account_id)` unique key，子表使用 composite foreign key：

```text
(conversation_id, account_id)
  -> conversations(id, account_id)
```

这样可以在数据库层阻止跨账号错误关联，并为未来 Row Level Security 和按账号导出/删除提供稳定过滤键。V1 暂不依赖 RLS 作为唯一安全边界。

当子表同时保存更细 scope 时，必须使用最宽 composite key，而不是多个彼此独立但可能交叉错配的 FK。例如：

```text
contacts(id, account_id, account_peer_id) UNIQUE
conversations(id, account_id, contact_id, account_peer_id) UNIQUE
conversation_turns(id, account_id, conversation_id) UNIQUE
messages(id, account_id, conversation_id) UNIQUE
message_revisions(id, account_id, message_id) UNIQUE
```

同时保存 contact 和 conversation 的 memory/proactive row 以 `(conversation_id, account_id, contact_id)` 引用 conversation；同时保存 turn 和 conversation 的 run/intent row 以 `(turn_id, account_id, conversation_id)` 引用 turn。不能只建立两个较窄 FK 后依赖应用代码判断它们属于同一 scope。

### 3.6 敏感数据类别

| 类别 | 示例 | 数据库表示 |
|---|---|---|
| credential secret | API key | 应用层密文，主密钥与数据库分离 |
| sensitive content | message、caption、memory、summary、outbound text | 可查询明文列；磁盘和备份加密 |
| sensitive derived data | embedding、relationship state、proactive reason | 受控业务表，不进入普通日志 |
| operational metadata | 状态、时间、hash、错误类别 | 结构化列，错误信息必须脱敏 |
| transient raw debug | Telegram/provider raw payload | 默认关闭；独立密文表和强制 TTL |

应用层字段加密只用于 credential 和临时 raw debug capture。不得使用 credential master key 加密 debug payload；两者使用不同 key purpose 和轮换记录。

正文或派生内容的普通 SHA-256 同样属于敏感指纹：它只用于同一生命周期内的一致性校验，必须随正文 redaction 一并清除。需要在删除后长期存在的防重复标识使用独立 purpose key 的 HMAC，并且不得由可枚举 Telegram ID 或短文本直接生成。

### 3.7 JSONB 边界

identity、foreign key、状态、排序、幂等键、时间、常用筛选字段和所有安全门禁必须使用普通列。

JSONB 只用于：

- Telegram 或 provider 的受控扩展 metadata；
- `protocol_options` 等 discriminated schema；
- typed memory payload；
- 低频、非安全关键的审计补充字段。

每个 JSONB 字段必须有对应 `schema_version`、应用层 JSON Schema、最大字节限制和未知字段策略。禁止把完整 raw update、完整模型请求、Prompt、API key 或任意未校验请求 JSON 塞入普通 metadata。

## 4. 实体关系总览

```text
accounts
  |--< account_peers >-- telegram_peers
  |          |
  |          +-- contacts
  |                 |
  |                 +-- conversations
  |                        |
  |                        +--< messages --< message_revisions
  |                        |       |             |
  |                        |       |             +--< message_media >-- media_objects
  |                        |       +--< message_reactions
  |                        |
  |                        +--< conversation_turns --< turn_messages
  |                        |          |
  |                        |          +--< context_manifests --< context_manifest_items
  |                        |          |                              +--< context_manifest_item_reasons
  |                        |          |                +--< context_manifest_omissions
  |                        |          +--< model_runs --< model_run_attempts
  |                        |          +--< copilot_drafts --< copilot_draft_revisions
  |                        |          +--< outbound_delivery_groups --< outbound_intents --< outbound_attempts
  |                        |
  |                        +--< memories --< memory_versions --< memory_evidence
  |                        |                    |
  |                        |                    +--< embedding_records
  |                        +--< memory_jobs --< memory_input_manifests
  |                        |                         +--< memory_input_manifest_items
  |                        +--< memory_proposals --< memory_proposal_targets/evidence
  |                        +--< memory_watermarks
  |                        +--< summaries --< summary_versions --< summary_version_sources
  |                        +--< context_preview_requests
  |                                      +--< context_preview_tokens
  |                                      +--< context_preview_deliveries
  |
  +--< audit_log
  +--< background_jobs
  +--< data_erasure_requests

model_endpoints --< model_capability_snapshots
       |                  |
       +--< model_config_drafts --< control_input_sessions
       +--< model_config_versions >-- model_profiles
                                             |
                                             +-- model_credentials --< model_credential_versions
                                             +--< model_key_launch_sessions
context_policies --< context_policy_versions
retrieval_policies --< retrieval_policy_versions
prompt_versions -> context_manifests / model_runs

message_events -> message projection / conversation revision
transactional_outbox -> Redis notification relay
life_events / intentions / relationship_states -> proactive_decisions -> outbound_delivery_groups -> outbound_intents
```

模型 profile、endpoint 和 credential 是部署级配置，不属于某个 Telegram account。箭头表示逻辑关系，不代表所有删除都使用 `ON DELETE CASCADE`；显式删除流程和外键策略分别见第 13、14 节。

## 5. Identity 与 tenancy

### 5.1 `accounts`

表示系统管理的 Telegram 真人账号。V1 只有一行，但所有业务逻辑按多账号建模。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `telegram_user_id` | BIGINT | `UNIQUE NOT NULL` |
| `display_label` | TEXT | 管理员可见非秘密标签 |
| `status` | TEXT | `bootstrap_required/active/disabled/deleting`；聊天 pause 由 orchestrator overlay 表达 |
| `default_timezone` | TEXT | IANA timezone |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | 受控更新 |
| `deleted_at` | TIMESTAMPTZ | account wipe 进行中或完成标记 |

Telethon API ID、API Hash、手机号、Session 路径和 2FA 不存入该表。它们属于部署 secret 或 Session volume。

### 5.2 `telegram_peers`

表示 Telegram 平台上的稳定 peer 身份，与当前受管账号无关。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `peer_type` | TEXT | `user/chat/channel`；V1 自动对话只允许 `user` |
| `telegram_peer_id` | BIGINT | Telegram 原生 ID |
| `is_bot` | BOOLEAN | 非 Bot private chat 门禁 |
| `created_at` | TIMESTAMPTZ | 首次建立全局 identity 的时间 |

唯一约束：

```text
UNIQUE (peer_type, telegram_peer_id)
```

`telegram_peers` 不保存 access hash、username、display name 或联系人备注，因为这些值按账号可见且会变化。

### 5.3 `account_peers`

表示某个受管账号视角下的 peer 投影。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `peer_id` | UUIDv7 | FK telegram_peers |
| `access_hash` | BIGINT | nullable，敏感 operational metadata，不进日志 |
| `username` | TEXT | nullable，非身份主键 |
| `display_name` | TEXT | nullable |
| `observed_is_contact` | BOOLEAN | Telegram 当前观察值 |
| `last_observed_at` | TIMESTAMPTZ | entity 刷新时间 |
| `metadata` | JSONB | 白名单扩展、schema version 独立列 |

唯一约束：

```text
UNIQUE (account_id, peer_id)
UNIQUE (id, account_id)
```

username 不能唯一，也不能用来关联历史消息。

### 5.4 `contacts`

保存本系统对联系人施加的产品策略，不等同于 Telegram 通讯录。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `account_peer_id` | UUIDv7 | composite FK 保证同账号 |
| `automation_status` | TEXT | `allowed/blocked/review/deleting` |
| `proactive_enabled` | BOOLEAN | 联系人主动消息总开关 |
| `timezone` | TEXT | nullable IANA timezone |
| `locale` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | 受控更新 |
| `deleted_at` | TIMESTAMPTZ | contact purge 标记 |

```text
UNIQUE (account_id, account_peer_id)
UNIQUE (id, account_id)
UNIQUE (id, account_id, account_peer_id)
```

### 5.5 `conversations`

V1 每个允许的一对一 private contact 对应一个 conversation。unsupported peer 只进入最小 message event 元数据，不创建 conversation。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `contact_id` | UUIDv7 | composite FK contacts |
| `account_peer_id` | UUIDv7 | composite FK account_peers |
| `telegram_chat_id` | BIGINT | canonical marked peer ID |
| `base_mode_override` | TEXT | nullable `AUTO/HUMAN/COPILOT`；空表示继承 account default |
| `contact_paused` | BOOLEAN | contact pause overlay，默认 false |
| `temporary_human_until` | TIMESTAMPTZ | nullable；可选 temporary takeover deadline |
| `mode_version` | BIGINT | 从 1 开始单调递增 |
| `content_revision` | BIGINT | 从 0 开始，内容语义变化时单调递增 |
| `automation_resume_floor_event_id` | BIGINT | nullable；恢复后不自动补回复的 event floor |
| `last_response_covered_event_id` | BIGINT | nullable；已明确回应范围 watermark |
| `last_message_at` | TIMESTAMPTZ | nullable projection |
| `last_completed_turn_at` | TIMESTAMPTZ | nullable projection |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | 受控更新 |
| `deleted_at` | TIMESTAMPTZ | contact purge 标记 |

约束：

```text
UNIQUE (account_id, account_peer_id)
UNIQUE (account_id, telegram_chat_id)
UNIQUE (id, account_id)
UNIQUE (id, account_id, contact_id, account_peer_id)
CHECK (mode_version >= 1)
CHECK (content_revision >= 0)
CHECK (base_mode_override IS NULL OR base_mode_override IN ('AUTO','HUMAN','COPILOT'))
```

conversation overlay、两个 version 和 reply watermark 位于同一行，是发送前 row lock/CAS 的事实源。effective mode 还需要组合 `account_orchestrator_states`；缓存中的状态不能覆盖数据库值。

## 6. Message、event 与 media

### 6.1 `message_events`

保存归一化 append-only update 元数据。消息正文不存入 event payload，避免 Telegram delete 后 append-only event 继续保留正文。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `event_uuid` | UUIDv7 | 对日志和 job 暴露的稳定 ID，UNIQUE |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | nullable，unsupported peer 没有 conversation |
| `event_kind` | TEXT | Message Lifecycle 定义的 event kind |
| `telegram_message_id` | BIGINT | nullable |
| `grouped_id` | BIGINT | nullable |
| `fingerprint_version` | SMALLINT | 幂等算法版本 |
| `update_fingerprint` | BYTEA | 固定长度摘要 |
| `telegram_event_at` | TIMESTAMPTZ | nullable |
| `observed_at` | TIMESTAMPTZ | DB default |
| `ordering_key` | TEXT | 版本化 canonical 排序编码 |
| `metadata_schema_version` | SMALLINT | NOT NULL |
| `metadata` | JSONB | 不含正文的归一化 metadata |
| `projected_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (account_id, fingerprint_version, update_fingerprint)
CHECK (octet_length(update_fingerprint) = 32)
```

对 unsupported peer，`metadata` 只能包含 peer type、受控内部/散列标识、ignored reason 和推进 watermark 所需值。

### 6.2 `messages`

保存 Telegram message 的稳定 identity 和当前 tombstone/source 投影，正文位于 `message_revisions`。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `telegram_message_id` | BIGINT | Telegram 业务 ID |
| `sender_account_peer_id` | UUIDv7 | nullable composite FK |
| `direction` | TEXT | `incoming/outgoing` |
| `role` | TEXT | `user/assistant/system` |
| `source` | TEXT | `telegram_user/ai/proactive_ai/copilot_approved/human/system_pending/system` |
| `source_status` | TEXT | `resolved/pending/corrected` |
| `current_revision_no` | INTEGER | 从 1 开始；tombstone 仍保留最后 revision number |
| `grouped_id` | BIGINT | nullable |
| `reply_to_telegram_message_id` | BIGINT | nullable，不要求目标已经存在 |
| `telegram_created_at` | TIMESTAMPTZ | Telegram message date |
| `edited_at` | TIMESTAMPTZ | nullable |
| `deleted_at` | TIMESTAMPTZ | nullable |
| `is_tombstone` | BOOLEAN | delete 后 true |
| `first_observed_at` | TIMESTAMPTZ | NOT NULL |
| `last_observed_at` | TIMESTAMPTZ | NOT NULL |
| `metadata_schema_version` | SMALLINT | NOT NULL |
| `metadata` | JSONB | forward/reply/media type 等白名单 metadata |

```text
UNIQUE (account_id, conversation_id, telegram_message_id)
UNIQUE (id, account_id)
UNIQUE (id, account_id, conversation_id)
CHECK (current_revision_no >= 1)
CHECK (is_tombstone = (deleted_at IS NOT NULL))
```

不能使用 `outgoing=true` 直接推导 source。`system_pending` 在对账完成前不能进入长期 Context 或 Memory evidence。

### 6.3 `message_revisions`

每次 create/edit 生成一个 revision。revision 创建后除单向 redaction 外不可修改。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `message_id` | UUIDv7 | composite FK messages |
| `revision_no` | INTEGER | 从 1 单调递增 |
| `body_kind` | TEXT | `text/caption/none` |
| `text_content` | TEXT | nullable sensitive content |
| `caption` | TEXT | nullable sensitive content |
| `entities_schema_version` | SMALLINT | NOT NULL |
| `entities` | JSONB | 归一化 Telegram entities |
| `content_sha256` | BYTEA | nullable；redaction 时清除 |
| `source_event_id` | BIGINT | FK message_events |
| `telegram_edited_at` | TIMESTAMPTZ | nullable |
| `created_at` | TIMESTAMPTZ | DB default |
| `redacted_at` | TIMESTAMPTZ | nullable，只能从 null 变为非 null |
| `redaction_reason` | TEXT | `telegram_delete/contact_purge/account_wipe/policy` |

```text
UNIQUE (message_id, revision_no)
UNIQUE (id, account_id)
UNIQUE (id, account_id, message_id)
CHECK body_kind 与 text_content/caption 一致
CHECK redacted_at IS NULL OR 正文、entities、content_sha256 全部已清空
```

`messages.current_revision_no` 通过 deferrable composite foreign key 指向同一 message 的 revision。应用查询必须同时检查 `messages.is_tombstone=false` 和 `message_revisions.redacted_at IS NULL`。

### 6.4 `debug_payload_captures`

默认不创建记录。只有服务器侧显式开启受控诊断时保存短期加密 raw payload。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `source_type` | TEXT | `telegram_update/provider_request/provider_response` |
| `source_ref_type` | TEXT | 受控关联类型 |
| `source_ref` | TEXT | canonical internal ID string，不存秘密作为 ref |
| `ciphertext` | BYTEA | 独立 diagnostic key 加密 |
| `nonce` | BYTEA | 加密参数 |
| `key_version` | INTEGER | diagnostic key version |
| `created_at` | TIMESTAMPTZ | DB default |
| `expires_at` | TIMESTAMPTZ | `NOT NULL`，强制短期 TTL |
| `redacted_at` | TIMESTAMPTZ | nullable |

普通业务角色没有读取权限；清理 job 必须按 `expires_at` 使用 partial index 领取。

### 6.5 `media_objects`

表示 `media-data` 中可验证和清理的物理对象，包括 original 和 metadata-free provider copy。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `object_kind` | TEXT | `original/provider_copy` |
| `status` | TEXT | `pending/ready/rejected/delete_pending/deleted/failed` |
| `storage_key` | TEXT | nullable relative opaque key，UNIQUE |
| `sha256` | BYTEA | nullable，ready 时固定长度 |
| `validated_mime` | TEXT | nullable allowlist |
| `byte_size` | BIGINT | nullable nonnegative |
| `width` | INTEGER | nullable positive |
| `height` | INTEGER | nullable positive |
| `parent_object_id` | UUIDv7 | provider copy 指向 original |
| `validation_error_code` | TEXT | nullable，不保存原始 bytes/路径 |
| `created_at` | TIMESTAMPTZ | DB default |
| `ready_at` | TIMESTAMPTZ | nullable |
| `delete_requested_at` | TIMESTAMPTZ | nullable |
| `deleted_at` | TIMESTAMPTZ | nullable |
| `retention_class` | TEXT | `media_original_30d/media_provider_copy_24h` |
| `expires_at` | TIMESTAMPTZ | nullable |

ready、rejected 和 deleted 状态使用 CHECK 保证字段组合一致。`storage_key` 不得包含绝对路径、`..` 或联系人原始文件名。

### 6.6 `message_media`

将某个 message revision 的媒体位置关联到 media object。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `message_revision_id` | UUIDv7 | composite FK revision |
| `media_object_id` | UUIDv7 | nullable，metadata-only 媒体为空 |
| `media_kind` | TEXT | `photo/image_document/voice/audio/video/video_note/document/sticker` |
| `position` | INTEGER | album/content part 顺序 |
| `telegram_file_ref` | TEXT | nullable，受控敏感 metadata |
| `declared_mime` | TEXT | nullable |
| `declared_size` | BIGINT | nullable |
| `duration_ms` | BIGINT | nullable |
| `original_name_sanitized` | TEXT | nullable，禁止路径字符 |
| `metadata_schema_version` | SMALLINT | NOT NULL |
| `metadata` | JSONB | 宽高等允许字段 |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (message_revision_id, position)
CHECK metadata-only media 的 media_object_id IS NULL
CHECK photo/image_document ready 输入必须关联 validated media object
```

Telegram delete 先使关联在 Context 查询中不可见，再由清理 job 删除物理对象。共享相同 SHA-256 不表示可以跨消息复用生命周期；V1 不做跨联系人内容去重。

### 6.7 `message_reactions`

保存 reaction 当前投影，历史仍由 `message_events` 表达。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `message_id` | UUIDv7 | composite FK messages |
| `actor_peer_id` | UUIDv7 | nullable FK telegram_peers |
| `reaction_key` | TEXT | 归一化 emoji/custom reaction key |
| `active` | BOOLEAN | 当前是否存在 |
| `updated_at` | TIMESTAMPTZ | 最后变化时间 |

```text
UNIQUE (message_id, actor_peer_id, reaction_key)
  WHERE actor_peer_id IS NOT NULL
UNIQUE (message_id, reaction_key)
  WHERE actor_peer_id IS NULL
```

`actor_peer_id IS NULL` 表示 Telegram 只提供匿名/聚合 reaction 投影，此时一条 `reaction_key` 只能有一行并由 metadata 记录计数。reaction 不创建 turn，不直接进入 Main AI。

## 7. Conversation、turn 与发送

### 7.1 `account_orchestrator_states`

每个 Telegram account 一行，保存不需要批量改写 conversation 的全局控制事实：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `account_id` | UUIDv7 | PK/FK accounts |
| `default_base_mode` | TEXT | `AUTO/HUMAN/COPILOT` |
| `global_paused` | BOOLEAN | 默认 false |
| `maintenance_state` | TEXT | `inactive/draining/active` |
| `temporary_takeover_enabled` | BOOLEAN | V1 默认 false |
| `temporary_takeover_seconds` | INTEGER | positive，默认 600 |
| `resume_pending_policy` | TEXT | V1 只允许 `ignore`；future 可加 `ask` |
| `resume_floor_event_id` | BIGINT | nullable account-level lazy floor |
| `control_version` | BIGINT | 从 1 开始单调递增 |
| `updated_by` | TEXT | internal admin/system ref |
| `updated_at` | TIMESTAMPTZ | 受控更新 |

```text
CHECK (default_base_mode IN ('AUTO','HUMAN','COPILOT'))
CHECK (maintenance_state IN ('inactive','draining','active'))
CHECK (resume_pending_policy = 'ignore')
CHECK (temporary_takeover_seconds > 0)
CHECK (control_version >= 1)
```

V1 将 future `ask` 保留为 migration 扩展点，但当前数据库 CHECK 明确拒绝它，避免配置出没有完整状态机的半成品功能。

### 7.2 `conversation_mode_history`

`conversations` 的 override/overlay 与 `mode_version` 保存当前事实，本表保存不可变变更历史。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `mode_version` | BIGINT | 与更新后的 conversation 一致 |
| `change_kind` | TEXT | `base_override/contact_pause/temporary_human/human_outgoing/cancel/policy` |
| `previous_state` | TEXT | nullable 受控状态，不放 JSON/正文 |
| `new_state` | TEXT | nullable 受控状态 |
| `reason` | TEXT | stable reason code |
| `actor_type` | TEXT | `admin/human/system` |
| `actor_ref` | TEXT | nullable internal ref，不存显示名 |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (conversation_id, mode_version)
```

同一事务必须同时更新 `conversations` 并插入 history；不允许只写 history。

`account_control_history` 以 `(account_id, control_version)` 唯一，记录 account default、global pause、maintenance、takeover policy 和 account block 版本变化。两类 history 都不复制 message/draft 正文。

### 7.3 `conversation_turns`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `state` | TEXT | Message Lifecycle turn 状态机 |
| `trigger_kind` | TEXT | `incoming/replacement/copilot/proactive` |
| `supersedes_turn_id` | UUIDv7 | nullable self FK |
| `collection_sequence` | BIGINT | conversation 内单调序号 |
| `active_generation_no` | INTEGER | 从 0 开始 |
| `base_mode_snapshot` | TEXT | seal 时 resolved base mode |
| `base_mode_source_snapshot` | TEXT | `account_default/conversation_override` |
| `effective_mode_snapshot` | TEXT | seal 时 overlay 解析后的模式 |
| `account_control_version_snapshot` | BIGINT | seal 时 account control version |
| `mode_version_snapshot` | BIGINT | seal 时版本 |
| `content_revision_snapshot` | BIGINT | seal 时 conversation revision |
| `resume_floor_event_id_snapshot` | BIGINT | nullable |
| `coverage_event_id_snapshot` | BIGINT | nullable |
| `debounce_seconds` | INTEGER | positive server policy snapshot |
| `hard_cap_seconds` | INTEGER | positive server policy snapshot |
| `collect_started_at` | TIMESTAMPTZ | NOT NULL |
| `quiet_deadline_at` | TIMESTAMPTZ | NOT NULL |
| `hard_deadline_at` | TIMESTAMPTZ | NOT NULL |
| `sealed_at` | TIMESTAMPTZ | nullable |
| `completed_at` | TIMESTAMPTZ | nullable |
| `terminal_reason` | TEXT | nullable code |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | CAS 更新 |

```text
UNIQUE (conversation_id, collection_sequence)
UNIQUE (id, account_id)
UNIQUE (id, account_id, conversation_id)
CHECK (quiet_deadline_at <= hard_deadline_at)
CHECK (active_generation_no >= 0)
```

对允许并存的状态分别建立 partial unique index，例如同一 conversation 至多一个 `collecting` turn、至多一个 `generating/output_ready/sending` turn。replacement collection 与正在等待 3 秒 deadline 的旧 run 可以同时存在，因此不能用一个过宽的“所有非终态唯一”索引。

### 7.4 `turn_messages`

保存 turn 输入 membership 的不可变快照。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `turn_id` | UUIDv7 | composite FK turns |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | 与 turn/message 一致的 composite scope |
| `message_id` | UUIDv7 | composite FK messages |
| `message_revision_id` | UUIDv7 | composite FK revisions |
| `ordinal` | INTEGER | turn 内稳定顺序 |
| `inclusion_kind` | TEXT | `trigger/context/replacement_carry_forward` |
| `created_at` | TIMESTAMPTZ | DB default |

```text
PRIMARY KEY (turn_id, message_id)
UNIQUE (turn_id, ordinal)
```

turn、message 和 revision 使用包含 account/conversation/message 的最宽 composite FK。message edit 后 replacement turn 必须引用新 revision；旧 turn 保持原 revision ref，以便解释 discarded run。

### 7.5 `context_manifests`

Context Contract 见 `docs/architecture/06-context-contract.md`。本表保存一次 provider-independent context build 的可复现 identity 和预算/version snapshot。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `owner_kind` | TEXT | `turn/background_job` |
| `turn_id` | UUIDv7 | nullable composite FK turns |
| `background_job_id` | UUIDv7 | nullable FK background_jobs；account-scoped job使用widest composite FK |
| `purpose` | TEXT | 受控业务 purpose |
| `logical_role` | TEXT | `main_ai/memory_agent/proactive_agent`；与 purpose 一致 |
| `builder_version` | TEXT | Context Builder 版本 |
| `prompt_version` | TEXT | system/developer prompt 版本 |
| `prompt_bundle_sha256` | BYTEA | ordered active prompt/manual instruction bundle hash |
| `context_policy_version_id` | UUIDv7 | FK immutable context policy version |
| `retrieval_policy_version_id` | UUIDv7 | FK immutable retrieval policy version |
| `retrieval_policy_version` | TEXT | structured/vector/recent 算法与权重 |
| `token_policy_version` | TEXT | 总预算、软配额和 splitter policy |
| `token_estimator_version` | TEXT | tokenizer/fallback framing 版本 |
| `capability_snapshot_sha256` | BYTEA | active config capabilities hash |
| `embedding_space_id` | UUIDv7 | nullable；当次只允许一个 active space |
| `memory_freshness` | TEXT | `fresh/degraded/stale` snapshot |
| `effective_input_budget` | INTEGER | positive |
| `safety_reserve_tokens` | INTEGER | nonnegative |
| `estimated_instruction_tokens` | INTEGER | nonnegative |
| `estimated_text_tokens` | INTEGER | nonnegative |
| `estimated_image_tokens` | INTEGER | nonnegative |
| `estimated_structural_tokens` | INTEGER | nonnegative |
| `input_token_estimate` | INTEGER | 上述输入估算总计，nonnegative |
| `image_count` | INTEGER | nonnegative |
| `omission_count` | INTEGER | nonnegative |
| `source_revision_vector_sha256` | BYTEA | selected/current source version vector hash |
| `manifest_sha256` | BYTEA | 对 ordered item manifest 的 hash |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (turn_id, logical_role, builder_version, manifest_sha256)
  WHERE turn_id IS NOT NULL
UNIQUE (background_job_id, logical_role, builder_version, manifest_sha256)
  WHERE background_job_id IS NOT NULL
CHECK ((owner_kind = 'turn' AND turn_id IS NOT NULL AND background_job_id IS NULL) OR
       (owner_kind = 'background_job' AND turn_id IS NULL AND background_job_id IS NOT NULL))
CHECK (input_token_estimate = estimated_instruction_tokens
       + estimated_text_tokens + estimated_image_tokens
       + estimated_structural_tokens)
```

Memory/Proactive 的 purpose-specific manifest 没有 reactive `turn_id` 时，以对应 sealed background job identity 建立唯一键；不能把 nullable `turn_id` 当作幂等约束。Memory Pipeline 的 evidence range仍由专用 `memory_input_manifests` 表达，context manifest只负责实际canonical model request选择。

### 7.6 `context_manifest_items`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `manifest_id` | UUIDv7 | FK context_manifests |
| `ordinal` | INTEGER | 总装配顺序 |
| `layer` | TEXT | `instruction/identity/personality/relationship_time/structured_memory/semantic_memory/summary/recent/current` |
| `canonical_role` | TEXT | `system/developer/user/assistant` |
| `source_actor` | TEXT | `server/admin/contact/human/ai/proactive_ai/copilot_approved/derived` |
| `source_type` | TEXT | typed discriminator |
| `message_revision_id` | UUIDv7 | nullable FK |
| `memory_version_id` | UUIDv7 | nullable composite FK current version |
| `summary_version_id` | UUIDv7 | nullable composite FK current version |
| `media_object_id` | UUIDv7 | nullable FK validated provider copy |
| `trust_level` | TEXT | `system/trusted_derived/trusted_history/model_generated_history/untrusted_user/untrusted_external` |
| `rank_position` | INTEGER | nullable positive；retrieval item 的稳定 rank |
| `base_score` | NUMERIC | nullable，范围 `[0,1]` |
| `final_score` | NUMERIC | nullable，范围 `[0,1]` |
| `score_features_schema_version` | SMALLINT | nullable |
| `score_features` | JSONB | nullable；只存版本化归一数值，不存正文 |
| `source_slice_start` | INTEGER | nullable nonnegative Unicode code-point offset |
| `source_slice_end` | INTEGER | nullable positive、exclusive |
| `image_detail` | TEXT | nullable；图片固定 `auto` |
| `token_estimate` | INTEGER | nonnegative文本/framing估算 |
| `estimated_image_tokens` | INTEGER | nonnegative |
| `content_sha256` | BYTEA | 完整 source content hash |
| `rendered_part_sha256` | BYTEA | slice/label/content part canonical serialization hash |

```text
UNIQUE (manifest_id, ordinal)
CHECK typed source foreign key 恰好符合 source_type
CHECK source_slice_start/source_slice_end 同时为空或同时非空且 start < end
CHECK score/rank 组合符合 layer
CHECK image_detail IS NULL OR image_detail = 'auto'
```

正文由 version FK 重建，manifest 本身不复制 message/memory text。若 source 后续被 Telegram delete/contact purge 擦除，manifest 仍能解释选择过哪个 ID，但不能恢复已删除正文。

多重命中理由和省略项不能塞进单个 reason 字符串：

```text
context_manifest_item_reasons(
  manifest_item_id BIGINT FK,
  reason_ordinal SMALLINT,
  reason_code TEXT,
  related_source_type TEXT?,
  related_source_id UUIDv7?,
  PRIMARY KEY (manifest_item_id, reason_ordinal)
)

context_manifest_omissions(
  id BIGINT IDENTITY PK,
  manifest_id UUIDv7 FK,
  layer TEXT,
  reason_code TEXT,
  source_type TEXT?,
  source_id UUIDv7?,
  range_start_event_id BIGINT?,
  range_end_event_id BIGINT?,
  omitted_count INTEGER?,
  estimated_tokens INTEGER?,
  created_at TIMESTAMPTZ
)
```

Reason/omission 使用受控类型和 typed scope 校验；不保存省略正文。`context_manifests.omission_count` 与 child count 在 seal transaction 中一致。

### 7.7 `model_runs`

表示一次逻辑模型运行，不保存完整请求或 raw response。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | nullable composite FK |
| `turn_id` | UUIDv7 | nullable composite FK |
| `logical_role` | TEXT | `main_ai/memory_agent/proactive_agent/embedding` |
| `model_profile_id` | UUIDv7 | deployment profile FK，与 logical role 一致 |
| `purpose` | TEXT | 细分业务目的 |
| `generation_no` | INTEGER | turn 内 generation；后台任务可为 1 |
| `state` | TEXT | Message Lifecycle model run 状态 |
| `account_control_version_snapshot` | BIGINT | nullable；conversation/proactive run 的 global gate snapshot |
| `config_version_id` | UUIDv7 | 不可变 config snapshot FK |
| `credential_version_id` | UUIDv7 | 同 role credential snapshot FK，不含 secret |
| `context_manifest_id` | UUIDv7 | nullable FK |
| `memory_input_manifest_id` | UUIDv7 | nullable FK；仅 Memory Agent run 使用 |
| `prompt_version` | TEXT | purpose-specific prompt snapshot |
| `prompt_bundle_sha256` | BYTEA | exact ordered trusted instruction bundle hash |
| `context_policy_version_id` | UUIDv7 | nullable FK；generation request required |
| `retrieval_policy_version_id` | UUIDv7 | nullable FK；Context retrieval purpose required |
| `retrieval_policy_version` | TEXT | nullable；与 Context manifest 一致 |
| `token_policy_version` | TEXT | nullable；与 Context manifest 一致 |
| `token_estimator_version` | TEXT | nullable；与 Context manifest 一致 |
| `capability_snapshot_sha256` | BYTEA | config capabilities snapshot hash |
| `input_fingerprint` | BYTEA | canonical request 的 keyed fingerprint；purge 时清除 |
| `output_fingerprint` | BYTEA | nullable normalized output keyed fingerprint；purge 时清除 |
| `adapter_version` | TEXT | protocol adapter 实现版本 |
| `request_schema_version` | SMALLINT | canonical request schema |
| `output_schema_version` | SMALLINT | normalized result schema |
| `normalizer_version` | TEXT | normalized text/structured result version |
| `finish_reason` | TEXT | nullable canonical finish reason |
| `result_kind` | TEXT | nullable `text/structured/refusal/error` |
| `is_complete` | BOOLEAN | nullable；只有完整且contract-valid才为 true |
| `provider_request_id` | TEXT | nullable，脱敏长度限制 |
| `input_tokens` | INTEGER | nullable nonnegative |
| `output_tokens` | INTEGER | nullable nonnegative |
| `started_at` | TIMESTAMPTZ | nullable |
| `completed_at` | TIMESTAMPTZ | nullable |
| `cancel_requested_at` | TIMESTAMPTZ | nullable |
| `error_code` | TEXT | nullable stable code |
| `error_detail_redacted` | TEXT | nullable 长度限制 |
| `redacted_at` | TIMESTAMPTZ | nullable；清除 content-derived fingerprint/error detail |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (turn_id, logical_role, generation_no) WHERE turn_id IS NOT NULL
UNIQUE (id, account_id)
UNIQUE (id, account_id, logical_role)
UNIQUE (id, account_id, conversation_id, logical_role)
UNIQUE (id, account_id, conversation_id, turn_id, logical_role)
CHECK (logical_role = 'memory_agent') = (memory_input_manifest_id IS NOT NULL)
```

`model_profiles` 提供 `(id, logical_role)` unique key，run 使用 composite FK 保证 role；config version 和 credential version 也分别通过 `(id, profile_id)` composite FK 绑定同一个 `model_profile_id`，数据库会拒绝把其他 role 的配置或 key 用于本次运行。

业务输出由目标表反向引用 `model_run_id`：Main AI 由 outbound delivery group/intents 引用，Memory Agent 由 proposal 引用，Proactive Agent 由 decision 引用。避免 `model_runs` 使用不可约束的 polymorphic output ID。

### 7.8 `model_run_attempts`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `model_run_id` | UUIDv7 | FK model_runs |
| `attempt_no` | INTEGER | 从 1 开始 |
| `state` | TEXT | `started/succeeded/retryable_failed/terminal_failed/cancelled/unknown` |
| `provider_request_id` | TEXT | nullable |
| `started_at` | TIMESTAMPTZ | NOT NULL |
| `completed_at` | TIMESTAMPTZ | nullable |
| `http_status` | INTEGER | nullable |
| `error_code` | TEXT | nullable |
| `retry_after_seconds` | INTEGER | nullable nonnegative，fractional provider value 向上取整 |
| `input_tokens` | INTEGER | nullable |
| `output_tokens` | INTEGER | nullable |

```text
UNIQUE (model_run_id, attempt_no)
```

attempt 不保存 API key、header、prompt、消息正文或 raw body。

### 7.9 `turn_grace_authorizations`

持久化条件式 3 秒发送例外，不能只保存在进程内存。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK，与 turn/run 一致 |
| `turn_id` | UUIDv7 | composite FK turns |
| `model_run_id` | UUIDv7 | composite FK runs，UNIQUE |
| `model_role` | TEXT | 固定为 `main_ai` |
| `run_started_at` | TIMESTAMPTZ | `t0` |
| `grace_deadline_at` | TIMESTAMPTZ | `t0 + policy snapshot` |
| `model_completed_at` | TIMESTAMPTZ | `t_done` |
| `authorized_at` | TIMESTAMPTZ | DB default |

CHECK 要求 `model_role = 'main_ai'` 且 `model_completed_at <= grace_deadline_at`。

authorization 使用 `(model_run_id, account_id, conversation_id, turn_id, model_role)` composite FK 保证 run 确属该 turn。`turn_grace_events` 以 `(authorization_id, message_event_id)` 为主键，列明唯一允许忽略的 `message.incoming.created` revision delta。edit、delete、human outgoing 或 mode change 不得插入该表。

### 7.10 COPILOT draft

`copilot_drafts` 保存 response/proactive draft identity 和审批状态，不把 draft 塞入 generic job JSONB：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `contact_id` | UUIDv7 | composite FK contacts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `turn_id` | UUIDv7 | composite FK turns |
| `model_run_id` | UUIDv7 | nullable composite FK；生成开始后绑定 Main AI |
| `model_role` | TEXT | nullable；run 存在时固定 `main_ai` |
| `proactive_decision_id` | UUIDv7 | nullable；proactive draft provenance |
| `draft_kind` | TEXT | `reactive/proactive` |
| `state` | TEXT | `requested/collecting/generating/ready/editing/approved/send_queued/send_unknown/sent/ignored/expired/invalidated/failed` |
| `current_revision_no` | INTEGER | nullable；ready 后从 1 开始 |
| `account_control_version_snapshot` | BIGINT | NOT NULL |
| `mode_version_snapshot` | BIGINT | NOT NULL |
| `content_revision_snapshot` | BIGINT | NOT NULL |
| `requested_by` | TEXT | internal admin/system ref |
| `approved_by` | TEXT | nullable internal admin ref |
| `requested_at` | TIMESTAMPTZ | DB default |
| `ready_at` | TIMESTAMPTZ | nullable |
| `approved_at` | TIMESTAMPTZ | nullable |
| `expires_at` | TIMESTAMPTZ | nullable；ready 时为 30 分钟 deadline |
| `terminal_at` | TIMESTAMPTZ | nullable |
| `terminal_reason` | TEXT | nullable stable code |

```text
UNIQUE (id, account_id, conversation_id)
UNIQUE (conversation_id)
  WHERE state IN ('requested','collecting','generating','ready','editing','approved','send_queued','send_unknown')
UNIQUE (proactive_decision_id)
  WHERE proactive_decision_id IS NOT NULL
CHECK ((model_run_id IS NULL AND model_role IS NULL) OR
       (model_run_id IS NOT NULL AND model_role = 'main_ai'))
CHECK ((draft_kind = 'reactive' AND proactive_decision_id IS NULL) OR
       (draft_kind = 'proactive' AND proactive_decision_id IS NOT NULL))
CHECK (state NOT IN ('ready','editing','approved','send_queued','send_unknown','sent')
       OR current_revision_no IS NOT NULL)
```

`copilot_draft_revisions` 保存 immutable sensitive text：

```text
id UUIDv7 PK
account_id
conversation_id
draft_id
revision_no
author_type model|admin_edit
content_text
content_sha256
created_at
redacted_at?
UNIQUE (draft_id, revision_no)
UNIQUE (id, account_id, draft_id)
```

`copilot_drafts.current_revision_no`使用deferrable composite FK。30分钟TTL终止可操作状态，terminal draft正文在30分钟内redact；contact purge/account wipe和Telegram证据delete reconciliation优先执行单向redaction。

`copilot_action_tokens` 只保存 token hash、admin ID、draft/revision、purpose、expires/used_at；`copilot_edit_sessions` 绑定 Bot chat/admin/reply message/draft 并强制短期过期。两表都不保存 callback 明文 token 或重复 draft正文。

### 7.11 `outbound_delivery_groups`

实现顺序说明：本节描述M4及后续应达到的最终wide-FK schema。`0003_m3_telegram_lifecycle`先建立不会丢失的outbound identity、ordered payload、stable `telegram_random_id`、attempt与恢复状态，并逐字保存nullable `model_run_id`；此时`model_runs`、turn、proactive decision和COPILOT draft表尚不存在，因此M3不能伪造这些FK或快照。M4 migration必须扩展M3表并补齐本节的composite/widest FK、authorization snapshot、splitter和终态约束。该阶段性边界只延后不存在目标表的referential constraint，不允许应用忽略已经存在的account/conversation/group/random-ID约束。

一个完整 normalized logical output 对应一个 delivery group。短文本 group 也存在，只包含一个 intent；长文本按 Context Contract 的 versioned deterministic splitter 生成多个 intent。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `turn_id` | UUIDv7 | nullable composite FK；proactive 使用 decision scope |
| `model_run_id` | UUIDv7 | NOT NULL composite FK |
| `model_role` | TEXT | 固定 `main_ai`；所有实际消息正文均由 Main AI run 生成 |
| `proactive_decision_id` | UUIDv7 | nullable composite FK |
| `copilot_draft_id` | UUIDv7 | nullable composite FK |
| `approved_draft_revision_id` | UUIDv7 | nullable composite FK |
| `source` | TEXT | `ai/proactive_ai/copilot_approved` |
| `generation_no` | INTEGER | NOT NULL |
| `state` | TEXT | `pending/sending/partial/reconciled/cancelled/partial_cancelled/failed_terminal/dead_letter` |
| `account_control_version_snapshot` | BIGINT | NOT NULL |
| `mode_version_snapshot` | BIGINT | NOT NULL |
| `content_revision_snapshot` | BIGINT | NOT NULL |
| `logical_content_sha256` | BYTEA | nullable 32-byte；purge/redaction 时清空 |
| `normalizer_version` | TEXT | normalized logical text version |
| `splitter_version` | TEXT | deterministic splitter/policy version |
| `chunk_count` | INTEGER | positive，受 versioned policy 上限 |
| `max_delivery_chunks_snapshot` | INTEGER | positive，取自 context policy version |
| `reconciled_chunk_count` | INTEGER | nonnegative，不超过 chunk_count |
| `reply_to_telegram_message_id` | BIGINT | nullable；只有首 chunk 使用 |
| `send_authorized_at` | TIMESTAMPTZ | nullable；首段完整门禁通过 |
| `first_side_effect_at` | TIMESTAMPTZ | nullable；任一 Telegram RPC 可能产生副作用 |
| `created_at` | TIMESTAMPTZ | DB default |
| `completed_at` | TIMESTAMPTZ | nullable |
| `redacted_at` | TIMESTAMPTZ | nullable；清除 content-derived hash |

```text
UNIQUE (turn_id, generation_no, source)
  WHERE turn_id IS NOT NULL
UNIQUE (proactive_decision_id)
  WHERE proactive_decision_id IS NOT NULL
UNIQUE (copilot_draft_id)
  WHERE copilot_draft_id IS NOT NULL
UNIQUE (id, account_id, conversation_id, model_run_id, model_role, source, generation_no)
CHECK (reconciled_chunk_count BETWEEN 0 AND chunk_count)
CHECK (chunk_count BETWEEN 1 AND max_delivery_chunks_snapshot)
CHECK ((source = 'ai' AND model_role = 'main_ai' AND turn_id IS NOT NULL
        AND proactive_decision_id IS NULL AND copilot_draft_id IS NULL) OR
       (source = 'proactive_ai' AND model_role = 'main_ai'
        AND proactive_decision_id IS NOT NULL AND copilot_draft_id IS NULL) OR
       (source = 'copilot_approved' AND model_role = 'main_ai'
        AND turn_id IS NOT NULL AND proactive_decision_id IS NULL
        AND copilot_draft_id IS NOT NULL AND approved_draft_revision_id IS NOT NULL))
```

`(model_run_id, account_id, conversation_id, model_role)` 使用 composite FK 指向生成最终消息正文的 Main AI run；reactive/COPILOT 还使用包含 turn 的 widest FK。Proactive Agent run由`proactive_decisions`引用，主动消息group通过`proactive_decision_id`关联该decision并单独引用后续Main AI generation run。Proactive decision 与 COPILOT draft/revision 的 composite FK 绑定同一 account/conversation。Group 只有全部 chunks reconciled 才能进入 `reconciled`；已有副作用后被真人接管、edit/delete 或 control gate阻止剩余 chunks 时进入 `partial_cancelled`。

### 7.12 `outbound_intents`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `turn_id` | UUIDv7 | nullable composite FK，与 group 一致 |
| `model_run_id` | UUIDv7 | NOT NULL composite FK |
| `model_role` | TEXT | 固定 `main_ai`，与 group 的最终文本 run 一致 |
| `source` | TEXT | `ai/proactive_ai/copilot_approved` |
| `delivery_group_id` | UUIDv7 | NOT NULL widest composite FK group |
| `chunk_ordinal` | INTEGER | 从 1 开始 |
| `chunk_count_snapshot` | INTEGER | 与 group chunk_count 一致 |
| `state` | TEXT | Message Lifecycle intent 状态 |
| `generation_no` | INTEGER | NOT NULL |
| `account_control_version_snapshot` | BIGINT | NOT NULL |
| `mode_version_snapshot` | BIGINT | NOT NULL |
| `content_revision_snapshot` | BIGINT | NOT NULL |
| `idempotency_key` | BYTEA | 32-byte stable key |
| `content_text` | TEXT | nullable sensitive retry payload；redaction 时清空 |
| `content_sha256` | BYTEA | nullable 32-byte hash；redaction 时清空 |
| `reply_to_telegram_message_id` | BIGINT | nullable；仅 ordinal 1 可设置 |
| `telegram_random_id` | BIGINT | NOT NULL，稳定复用 |
| `telegram_message_id` | BIGINT | nullable |
| `reconciled_message_id` | UUIDv7 | nullable composite FK messages |
| `attempt_count` | INTEGER | nonnegative |
| `created_at` | TIMESTAMPTZ | DB default |
| `send_started_at` | TIMESTAMPTZ | nullable |
| `sent_at` | TIMESTAMPTZ | nullable |
| `reconciled_at` | TIMESTAMPTZ | nullable |
| `next_attempt_at` | TIMESTAMPTZ | nullable |
| `last_error_code` | TEXT | nullable |
| `retention_class` | TEXT | payload retention policy snapshot |
| `expires_at` | TIMESTAMPTZ | nullable；到期清除 content/hash，不删除 intent identity |
| `redacted_at` | TIMESTAMPTZ | contact/account purge 时清除 content_text/hash |

```text
UNIQUE (account_id, idempotency_key)
UNIQUE (account_id, telegram_random_id)
UNIQUE (account_id, conversation_id, telegram_message_id)
  WHERE telegram_message_id IS NOT NULL
UNIQUE (delivery_group_id, chunk_ordinal)
CHECK (octet_length(idempotency_key) = 32)
CHECK (model_run_id IS NOT NULL)
CHECK (chunk_ordinal BETWEEN 1 AND chunk_count_snapshot)
CHECK (chunk_ordinal = 1 OR reply_to_telegram_message_id IS NULL)
```

Intent 使用 `(delivery_group_id, account_id, conversation_id, model_run_id, model_role, source, generation_no)` widest composite FK 指向 group，数据库保证所有 chunks 属于同一 logical output。`idempotency_key` 覆盖 group ID、ordinal 和 chunk hash。同一 intent 的所有发送重试复用 `telegram_random_id`。Content text 在 intent reconciled 后仍可短期保留用于审计一致性，但 contact purge/account wipe 必须擦除；默认长期正文事实是 reconciled `messages/message_revisions`。

### 7.13 `outbound_attempts`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `outbound_intent_id` | UUIDv7 | FK intents |
| `attempt_no` | INTEGER | 从 1 开始 |
| `state` | TEXT | `started/succeeded/flood_wait/unknown/failed` |
| `started_at` | TIMESTAMPTZ | NOT NULL |
| `completed_at` | TIMESTAMPTZ | nullable |
| `telegram_message_id` | BIGINT | nullable |
| `flood_wait_seconds` | INTEGER | nullable nonnegative |
| `error_code` | TEXT | nullable |

```text
UNIQUE (outbound_intent_id, attempt_no)
```

## 8. Durable job、watermark 与 outbox

### 8.1 `background_jobs`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | nullable，部署级 job 可空 |
| `queue_name` | TEXT | 受控 queue |
| `job_type` | TEXT | 受控 discriminator |
| `idempotency_key` | BYTEA | 32-byte |
| `state` | TEXT | `pending/leased/retry_wait/succeeded/failed/dead_letter/cancelled` |
| `priority` | SMALLINT | 有界范围 |
| `payload_schema_version` | SMALLINT | NOT NULL |
| `payload` | JSONB | 只放 ID、范围和策略快照，不放正文/secret |
| `attempt_count` | INTEGER | nonnegative |
| `max_attempts` | INTEGER | positive |
| `available_at` | TIMESTAMPTZ | 延迟执行时间 |
| `lease_owner` | UUID | nullable instance token |
| `lease_expires_at` | TIMESTAMPTZ | nullable |
| `last_error_code` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | DB default |
| `completed_at` | TIMESTAMPTZ | nullable |
| `expires_at` | TIMESTAMPTZ | terminal record retention |

```text
UNIQUE (queue_name, idempotency_key)
UNIQUE (id, account_id)
CHECK leased 状态与 lease 字段一致
```

worker 使用 `FOR UPDATE SKIP LOCKED` 有界领取，写入 owner token 和 expiry。只有匹配 owner token 的 worker 可以续租或提交 terminal 状态。

### 8.2 `transactional_outbox`

保证 PostgreSQL 事务提交后 Redis 通知最终可发布。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK/发布顺序 |
| `account_id` | UUIDv7 | nullable |
| `topic` | TEXT | 受控 topic |
| `aggregate_type` | TEXT | 受控类型 |
| `aggregate_id` | TEXT | canonical internal ID string |
| `aggregate_version` | BIGINT | 单调版本或 generation |
| `payload_schema_version` | SMALLINT | NOT NULL |
| `payload` | JSONB | 只包含消费者重新查询所需 ID |
| `created_at` | TIMESTAMPTZ | DB default |
| `published_at` | TIMESTAMPTZ | nullable |
| `publish_attempts` | INTEGER | nonnegative |
| `last_error_code` | TEXT | nullable |

```text
UNIQUE (topic, aggregate_type, aggregate_id, aggregate_version)
```

relay 发布成功后更新 `published_at`。消费者仍按自身业务幂等键处理；outbox 不是 exactly-once 消息总线。

### 8.3 Watermark 约定

每种补偿扫描使用领域专用 watermark 表，不把所有范围塞入 generic job JSONB。watermark 至少包含：

```text
account_id
conversation_id or domain scope
watermark_kind
last_event_id / last_message_sequence / last_completed_turn_id
updated_at
version
```

`UNIQUE (scope, watermark_kind)`，更新使用 version CAS。Memory 具体 watermark 见第 9 节。

## 9. Memory、summary 与 embedding

### 9.1 `memories`

保存稳定 memory identity 和当前活动版本指针，不直接保存可变正文。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `contact_id` | UUIDv7 | nullable composite FK |
| `conversation_id` | UUIDv7 | nullable composite FK |
| `memory_type` | TEXT | `identity/relationship/fact/preference/event/intention/style` |
| `semantic_key_hash` | BYTEA | validator 对 versioned typed semantic key 的 32-byte hash；用于有界查重，不代表文本相等 |
| `status` | TEXT | `active/superseded/invalidated/forgotten`；模型 candidate 只存在于 proposal |
| `current_version_no` | INTEGER | 从 1 开始 |
| `superseded_by_memory_id` | UUIDv7 | nullable self FK |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | 状态/指针更新 |
| `forgotten_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (id, account_id)
CHECK contact_id IS NOT NULL OR conversation_id IS NULL
CHECK octet_length(semantic_key_hash) = 32
CHECK status 与 forgotten/superseded 字段一致
```

部署级 identity/personality memory 可以只有 `account_id`；联系人记忆必须有 `contact_id`。同一语义是否 merge 由 Memory Pipeline 决定，不依赖数据库对文本做 unique。

### 9.2 `memory_versions`

版本创建后不可修改，唯一例外是 memory forget、Telegram evidence delete reconciliation、contact purge 或 account wipe 触发的单向 redaction。若证据被删除，先隔离受影响 version，再通过新 replacement/invalidation version 或 stable identity 状态变化修正当前事实。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `memory_id` | UUIDv7 | composite FK memories |
| `version_no` | INTEGER | 从 1 单调递增 |
| `operation` | TEXT | `create/update/merge/supersede/invalidate` |
| `payload_schema_version` | SMALLINT | typed JSON schema version |
| `payload` | JSONB | 按 memory_type 验证的结构化内容 |
| `rendered_text` | TEXT | 模型/人工可读 sensitive content |
| `importance` | NUMERIC | 0..1 |
| `confidence` | NUMERIC | 0..1 |
| `observed_at` | TIMESTAMPTZ | nullable；证据被陈述/观察时间 |
| `valid_from` | TIMESTAMPTZ | nullable |
| `valid_to` | TIMESTAMPTZ | nullable |
| `time_precision` | TEXT | `exact/day/week/month/relative/unknown` |
| `timezone` | TEXT | nullable IANA timezone snapshot |
| `model_run_id` | UUIDv7 | nullable composite FK |
| `model_role` | TEXT | run 存在时固定为 `memory_agent` |
| `prompt_version` | TEXT | nullable |
| `validator_policy_version` | TEXT | acceptance/normalization policy version |
| `acceptance_kind` | TEXT | `automatic/manual/reconciliation/migration` |
| `created_at` | TIMESTAMPTZ | DB default |
| `redacted_at` | TIMESTAMPTZ | nullable；forget/evidence delete/purge/wipe 的单向清除时间 |
| `redaction_reason` | TEXT | nullable 受控原因码 |

```text
UNIQUE (memory_id, version_no)
UNIQUE (id, account_id)
CHECK (importance BETWEEN 0 AND 1)
CHECK (confidence BETWEEN 0 AND 1)
CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
CHECK ((model_run_id IS NULL AND model_role IS NULL) OR
       (model_run_id IS NOT NULL AND model_role = 'memory_agent'))
```

`memories.current_version_no` 使用 deferrable composite FK 指向本表。active Context 只读 stable identity 当前 version，不能自行选择旧版本。

### 9.3 `memory_proposals`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `contact_id` | UUIDv7 | nullable composite FK |
| `conversation_id` | UUIDv7 | nullable composite FK |
| `memory_job_id` | UUIDv7 | FK memory_jobs |
| `model_run_id` | UUIDv7 | composite FK model_runs |
| `model_role` | TEXT | 固定为 `memory_agent` |
| `idempotency_key` | BYTEA | 32-byte stable key |
| `proposal_ordinal` | INTEGER | model output 内稳定 ordinal，nonnegative |
| `operation` | TEXT | `create/update/merge/supersede/invalidate` |
| `semantic_key_hash` | BYTEA | 应用重新生成的 typed semantic key hash |
| `payload_schema_version` | SMALLINT | NOT NULL |
| `proposed_payload` | JSONB | typed candidate |
| `proposed_text` | TEXT | sensitive candidate |
| `proposed_confidence` | NUMERIC | 0..1 |
| `proposed_importance` | NUMERIC | 0..1 |
| `proposed_valid_from` | TIMESTAMPTZ | nullable |
| `proposed_valid_to` | TIMESTAMPTZ | nullable |
| `visual_only` | BOOLEAN | default false |
| `state` | TEXT | `received/validating/accepted/rejected/candidate/error/invalidated/expired` |
| `validation_code` | TEXT | nullable |
| `validator_policy_version` | TEXT | NOT NULL |
| `accepted_memory_version_id` | UUIDv7 | nullable composite FK；accepted 时精确结果 |
| `decision_actor_type` | TEXT | nullable `service/admin/system` |
| `decision_actor_id` | TEXT | nullable 受控 actor ref，不复制用户名 |
| `decision_reason_code` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | DB default |
| `decided_at` | TIMESTAMPTZ | nullable |
| `retention_class` | TEXT | terminal candidate retention snapshot |
| `expires_at` | TIMESTAMPTZ | nullable；到期清除 proposed payload/text |

```text
UNIQUE (account_id, idempotency_key)
UNIQUE (model_run_id, proposal_ordinal)
CHECK (model_role = 'memory_agent')
CHECK octet_length(semantic_key_hash) = 32
CHECK proposed_confidence BETWEEN 0 AND 1
CHECK proposed_importance BETWEEN 0 AND 1
CHECK proposed_valid_to IS NULL OR proposed_valid_from IS NULL OR proposed_valid_to >= proposed_valid_from
```

proposal/version 的 `(model_run_id, account_id, model_role)` 都通过 composite FK 指向 model run。accepted proposal 在同一事务创建 memory/version/evidence 并记录 proposal result。proposal 不能直接更新 active memory row；terminal proposal 的候选正文按 retention policy 清除，正式事实只保留在 memory version。

低置信度、歧义或 image-only 结果保持在 `memory_proposals.state=candidate`，不先创建 `memories.status=candidate`。candidate 不能进入 Context 或正式 embedding；证据/target 变化后进入 `invalidated`，retention 到期进入 `expired`。

### 9.4 Evidence 与 relation

`memory_proposal_targets` 规范化多 target operation：

```text
proposal_id
account_id
target_memory_id
target_version_no_snapshot
target_role primary|merge_source|superseded|invalidated
created_at
PRIMARY KEY (proposal_id, target_memory_id, target_role)
```

`create` 不允许 target，`update/supersede/invalidate` 恰好一个 primary target，`merge` 至少两个 target。target 使用包含 `account_id` 的 composite FK；acceptance 时必须重新检查 current version snapshot。模型返回空 proposal 时只在 job/watermark 记录 deterministic no-change，不创建伪 proposal。

`memory_proposal_evidence` 保存模型声明和验证层接受的候选证据：

```text
proposal_id
account_id
message_revision_id
media_object_id?
evidence_role
quoted_span_start?
quoted_span_end?
source_content_sha256
source_normalization_version
trust_class
created_at
PRIMARY KEY (proposal_id, message_revision_id, evidence_role)
```

quote span 使用 canonical text 的 Unicode code point 半开区间。`media_object_id` 存在时必须通过 `message_media` 证明属于该 revision 且状态为 validated ready；image-only proposal 默认只能进入 candidate。

`memory_evidence` 绑定正式 `memory_version_id` 与以下来源之一：

```text
message_revision_id
summary_version_id
other_memory_version_id
media_object_id?  # 仅作为 message revision 的 supplemental image source
evidence_role
trust_class
source_content_sha256
```

CHECK 要求前三个主来源列恰好一个非空；`media_object_id` 只有 `message_revision_id` 存在时才允许，且必须属于该 revision。证据保存 source ID、hash、trust 和 evidence role，不复制原文。Telegram delete 导致 source revision redacted 时，Memory reconciliation 按 evidence index 找到受影响 versions，立即从 active Context 隔离。若仍有独立有效证据则从剩余证据生成 replacement version；随后对受影响旧 version 的 payload/rendered text 和 embedding 做单向 redaction。没有剩余证据时直接 invalidate 并 redaction。

当 `memory_evidence` 引用 summary 或其他 memory version 时，应用必须通过 `summary_version_sources`/其他 evidence 递归到至少一个 current、未 redacted message revision，并拒绝循环、跨 scope、断链或深度超过 8 的 evidence graph。

`memory_relations` 表达 memory version 之间的 `supports/contradicts/derived_from/merges/supersedes`，主键为 `(from_version_id, to_version_id, relation_type)`，禁止 self relation。

### 9.5 `memory_jobs`

是领域聚合记录，不替代 generic `background_jobs`。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `job_kind` | TEXT | `episode/rolling_summary/consolidation/reconciliation` |
| `state` | TEXT | `pending/leased/running/succeeded/retry_wait/dead_letter/cancelled` |
| `generation` | INTEGER | 同 conversation/kind 从 1 单调递增 |
| `job_version` | BIGINT | pending refresh/状态 CAS version |
| `range_start_event_id` | BIGINT | inclusive |
| `range_end_event_id` | BIGINT | inclusive，可刷新扩大 |
| `eligible_revision_count` | INTEGER | nonnegative policy snapshot input |
| `estimated_input_tokens` | INTEGER | nonnegative estimate |
| `completed_turn_watermark` | UUIDv7 | nullable |
| `idempotency_key` | BYTEA | 32-byte |
| `quiet_until` | TIMESTAMPTZ | 安静窗口 |
| `hard_due_at` | TIMESTAMPTZ | 硬阈值 |
| `pipeline_version` | TEXT | NOT NULL |
| `policy_version` | TEXT | 阈值/接受 policy version |
| `prompt_version` | TEXT | NOT NULL |
| `input_schema_version` | SMALLINT | NOT NULL |
| `output_schema_version` | SMALLINT | NOT NULL |
| `input_manifest_id` | UUIDv7 | nullable；seal 后 composite FK |
| `sealed_at` | TIMESTAMPTZ | nullable；running 范围不可再扩大 |
| `background_job_id` | UUIDv7 | nullable FK generic job |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | CAS |
| `completed_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (account_id, idempotency_key)
UNIQUE (conversation_id, job_kind, generation)
UNIQUE (conversation_id, job_kind) WHERE state = 'pending'
CHECK (range_end_event_id >= range_start_event_id)
CHECK eligible_revision_count >= 0
CHECK estimated_input_tokens >= 0
CHECK ((sealed_at IS NULL AND input_manifest_id IS NULL) OR
       (sealed_at IS NOT NULL AND input_manifest_id IS NOT NULL))
```

同一 conversation/kind 的 pending job 可以在事务中扩大范围，不创建重复 job；quiet deadline 可以后移，`hard_due_at` 不得后移。已经 sealed/running 的范围不可改写，新事件进入下一 generation。默认 policy 使用 45 秒 quiet、20 revision/约 6000 tokens/10 分钟硬触发和 5 分钟补偿扫描；每个 job 保存具体版本和值的快照。

### 9.6 Memory input manifest

`memory_input_manifests` 保存一次 Memory Agent 调用的 immutable typed membership：

```text
id UUIDv7 PK
account_id
conversation_id
memory_job_id
generation
manifest_kind episode|rolling_summary|consolidation|reconciliation
range_start_event_id / range_end_event_id
pipeline_version / policy_version / prompt_version
input_schema_version / output_schema_version
model_config_version_id / credential_version_id
timezone_snapshot?
input_token_estimate
image_count
manifest_sha256 BYTEA
created_at
UNIQUE (memory_job_id, generation)
UNIQUE (account_id, manifest_sha256, pipeline_version, prompt_version, output_schema_version)
```

`memory_input_manifest_items`：

```text
id BIGINT IDENTITY PK
manifest_id
ordinal
source_type message_revision|media_object|memory_version|summary_version
message_revision_id?
media_object_id?
memory_version_id?
summary_version_id?
inclusion_role episode|supporting|related_current|prior_summary|profile
trust_class
source_content_sha256
selection_reason_code
UNIQUE (manifest_id, ordinal)
CHECK typed source columns match source_type
```

manifest 不复制正文。provider 调用前按 source ID 重读未 redacted content 并复核 hash；edit/delete 后旧 manifest 可以保留 ID/provenance，但不能恢复内容或接受迟到输出。

`memory_jobs.input_manifest_id` 使用 deferrable FK 指回同一 account/conversation/job 的 manifest；manifest 到 job 的 FK 为直接 owner 关系，使 seal transaction 可以先插入 manifest/items，再更新 job pointer 并整体提交。

`model_runs` 增加 nullable `memory_input_manifest_id`；仅 `logical_role=memory_agent` 时允许，且 run/job/manifest 的 account/conversation/model config version 必须一致。

### 9.7 `memory_watermarks`

```text
account_id
conversation_id
watermark_kind episode|reconciliation
last_scanned_event_id
last_contiguous_decided_event_id
last_succeeded_job_id?
version
updated_at
PRIMARY KEY (conversation_id, watermark_kind)
CHECK last_contiguous_decided_event_id <= last_scanned_event_id
```

watermark 使用 version CAS。明确 ineligible/no-change 的 event 也可成为已裁决范围；存在更早 pending/retry/dead-letter hole 时不得推进 contiguous watermark。

### 9.8 Summary

`summaries` 保存稳定 summary identity：

```text
id, account_id, conversation_id, summary_kind,
period_key?, timezone_snapshot?, period_start_at?, period_end_at?,
status active|quarantined|invalidated,
current_version_no, created_at, updated_at
```

`summary_kind` 至少预留 `rolling/daily/weekly/consolidated`。`summary_versions` 保存：

```text
id, account_id, summary_id, version_no,
range_start_event_id, range_end_event_id,
period_start_at?, period_end_at?, timezone_snapshot?,
content_text, content_sha256,
model_run_id, model_role='memory_agent', prompt_version,
pipeline_version, output_schema_version, manifest_sha256,
invalidation_state active|quarantined|invalidated,
created_at, redacted_at
```

唯一键 `(summary_id, version_no)`。summary 版本不可原地改写。源消息删除后，受影响 summary 先退出 active Context，再从未删除范围创建 replacement/invalidation version；旧 summary content 和 embedding 随后单向 redaction。contact purge 时所有正文物理清除。

`summary_version_sources` 保存 ordered source membership：

```text
summary_version_id
account_id
ordinal
message_revision_id?
prior_summary_version_id?
inclusion_role
source_content_sha256
created_at
UNIQUE (summary_version_id, ordinal)
CHECK exactly one source column is non-null
```

source 使用包含 `account_id` 的 composite FK。rolling summary 可以引用前一个 rolling version + 新 revisions；weekly 可以引用 daily versions。迟到 event、edit/delete 通过 source index 找到受影响 version，并递归隔离下游 summary。只保存 range 而没有 source membership 不满足可审计/重建要求。

`summary_watermarks` 使用：

```text
account_id, conversation_id, summary_kind,
last_included_event_id, last_included_message_id?,
last_summary_version_id, version, updated_at
```

主键 `(conversation_id, summary_kind)`，更新使用 version CAS。

rolling summary 默认在新增 50 条 eligible revision 或估算约 12000 tokens 时 eligible。daily/weekly identity 保存 period key、有效 IANA timezone 和 UTC boundary snapshot；空 period 不创建 summary。summary version/source/current pointer/watermark 必须在同一事务提交。

### 9.9 Embedding space

`embedding_spaces` 将向量与模型、维度和重建代次绑定：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | nullable；V1 deployment role binding 可共享定义 |
| `model_profile_id` | UUIDv7 | embedding profile composite FK |
| `profile_kind` | TEXT | 固定为 `embedding`，用于可执行 composite FK/CHECK |
| `config_version_id` | UUIDv7 | embedding config version FK |
| `model_name_snapshot` | TEXT | 非秘密 |
| `dimensions` | INTEGER | positive |
| `distance_metric` | TEXT | `cosine/inner_product/l2` |
| `normalization` | TEXT | `none/l2` |
| `chunker_version` | TEXT | normalized text/chunk boundary version |
| `state` | TEXT | `building/active/retired/failed` |
| `generation` | INTEGER | 从 1 递增 |
| `created_at` | TIMESTAMPTZ | DB default |
| `activated_at` | TIMESTAMPTZ | nullable |
| `retired_at` | TIMESTAMPTZ | nullable |

`CHECK (profile_kind = 'embedding')`，并以 `(model_profile_id, profile_kind)` 指向 model profile；`(config_version_id, model_profile_id)` 再通过 composite FK 指向该 profile 的 embedding config。同一 embedding profile 同时至多一个 active space。更换模型或维度创建新 space 并重建，不能把不同空间的向量混合检索。

`embedding_records` 使用一个统一表，typed target foreign key 恰好一个非空：

```text
id UUIDv7 PK
account_id
embedding_space_id
memory_version_id?
summary_version_id?
message_revision_id?
chunk_index
source_sha256
dimensions
embedding VECTOR
created_at
invalidated_at?
```

```text
CHECK exactly one target FK is non-null
CHECK vector_dims(embedding) = dimensions
```

目标唯一性由三个 partial unique index 强制：

```text
UNIQUE (embedding_space_id, memory_version_id, chunk_index)
  WHERE memory_version_id IS NOT NULL
UNIQUE (embedding_space_id, summary_version_id, chunk_index)
  WHERE summary_version_id IS NOT NULL
UNIQUE (embedding_space_id, message_revision_id, chunk_index)
  WHERE message_revision_id IS NOT NULL
```

`embedding_spaces`提供`(id, dimensions)`unique key，record使用composite FK保证声明维度一致。Pgvector ANN index绑定固定dimension/active space；V1默认HNSW `m=16/ef_construction=64/ef_search=100`，参数变化需真实dataset证据，不建立跨space的无约束全局ANN index。

active query 必须显式绑定一个 `embedding_space_id`，不能跨 space 混合分数。更换模型/config/dimension/metric/chunker 时创建 `building` shadow space；只有 eligible target coverage、dimension、source hash、抽样检索和 final delta 均验证后，才在 activation transaction 中把新 space 设为 active、旧 space 设为 retired。构建失败时旧 active space 保持可用。

candidate、superseded/invalidated/forgotten/redacted version 和 raw image pixels 不创建 active embedding。source edit/delete/forget 时同一逻辑删除边界先设置 `invalidated_at`，查询立即排除，物理删除随后幂等完成。

### 9.10 Memory review extension

Memory 管理复用第 12.2 节 `control_commands` 的 Bot update 幂等 identity，并使用 typed extension `memory_review_actions`：

```text
control_command_id UUIDv7 PRIMARY KEY/FK
account_id
action accept|reject|forget
proposal_id?
memory_id?
expected_proposal_state?
expected_memory_version_no?
action_token_hash BYTEA
expires_at
used_at?
state awaiting_confirmation|applied|rejected|expired
reason_code?
CHECK accept/reject targets proposal and forget targets memory
```

token 绑定 allowlisted admin、Bot chat、action、target 和 expected version，只保存 hash、单次使用。command/action 不复制 memory/proposal/message 正文；UI 显示时从仍有效 source 临时渲染最小摘要。

## 10. Proactive 与关系投影

### 10.1 Policy 与联系人设置

`proactive_policy_versions`保存deployment/account默认策略的不可变版本，active binding只指向其中一版：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | nullable；NULL为deployment default |
| `version_no` | BIGINT | scope内递增 |
| `enabled` | BOOLEAN | account global开关 |
| `scheduler_scan_seconds` | INTEGER | 默认900，正数 |
| `activity_suppression_seconds` | INTEGER | 默认1800，正数 |
| `quiet_start_local` / `quiet_end_local` | TIME | 默认22:00/08:00 |
| `absolute_no_send_start_local` / `absolute_no_send_end_local` | TIME | 固定00:00/07:00；V1不可放宽 |
| `bypass_importance_threshold` | NUMERIC(4,3) | 默认0.900，范围0..1 |
| `contact_bypass_daily_limit` | SMALLINT | 默认1，V1为0或1 |
| `account_daily_limit` | SMALLINT | 默认10，非负 |
| `close_daily_limit` / `friend_daily_limit` / `acquaintance_daily_limit` | SMALLINT | 默认2/1/1 |
| `close_min_interval_seconds` / `friend_min_interval_seconds` / `acquaintance_min_interval_seconds` | INTEGER | 默认21600/43200/86400 |
| `close_reconnect_seconds` / `friend_reconnect_seconds` | INTEGER | 默认259200/604800 |
| `reason_policy_schema_version` | SMALLINT | NOT NULL |
| `reason_policy` | JSONB | 只允许受schema校验的reason窗口扩展，不放任意表达式 |
| `context_contract_version` | TEXT | 主动输入选择契约 |
| `created_by_admin_id` | UUIDv7 | FK administrator |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (account_id, version_no) NULLS NOT DISTINCT
CHECK (scheduler_scan_seconds > 0 AND activity_suppression_seconds > 0)
CHECK (bypass_importance_threshold BETWEEN 0 AND 1)
CHECK (contact_bypass_daily_limit BETWEEN 0 AND 1)
CHECK (所有 daily limit >= 0 AND 所有 interval > 0)
```

`proactive_policy_bindings(scope_type, account_id, active_version_id, version, updated_at)`保存`deployment/account`的current pointer；deployment scope要求`account_id IS NULL`，account scope要求非空，scope内唯一。Active version使用包含scope/account的deferrable composite FK，不能以“最大version_no”推断当前策略。

`contact_proactive_setting_versions`保存contact override历史：

```text
id UUIDv7 PK
account_id
contact_id
version_no
enabled
daily_limit_override?
minimum_interval_seconds_override?
reconnect_seconds_override?
quiet_start_local_override?
quiet_end_local_override?
relationship_level_override?
created_by_admin_id
created_at
UNIQUE (contact_id, version_no)
UNIQUE (id, account_id, contact_id)
```

`contact_proactive_settings(contact_id PK, account_id, active_version_id, version, updated_at)`保存current pointer，并以`(active_version_id, account_id, contact_id)` composite FK指向version row。设置激活递增`version`，使旧candidate/authorization可通过snapshot失效。

Contact override可以关闭、收紧或经管理员确认调整普通限制；数据库/业务校验不允许其放宽`00:00-07:00`绝对禁发、bypass reason集合或每日一次上限。`contacts.proactive_enabled`保留为快速硬开关；active setting binding/version用于完整策略。

`relationship_level=unknown`在预算与间隔上使用`acquaintance_*`列，在reconnect规则中保持禁用；不能因缺少关系分类取得更宽松限制。

### 10.2 `life_events`

避免与 `message_events` 混淆，业务生活事件使用 `life_events`：

```text
id UUIDv7 PK
account_id
contact_id
title
event_kind
start_at?
end_at?
timezone?
importance 0..1
status
source_memory_version_id?
created_at
updated_at
```

### 10.3 `intentions`

```text
id UUIDv7 PK
account_id
contact_id
owner self|contact
content_text
expected_at?
timezone?
status
importance 0..1
source_memory_version_id?
created_at
updated_at
```

`life_events` 和 `intentions` 是由已验证 memory 派生的可查询投影；修改投影不能反向静默改写 memory evidence。

### 10.4 Relationship state

`relationship_states` 保存每个 contact 当前投影：

```text
contact_id PK
account_id
version
relationship_level
last_contact_at?
last_incoming_at?
last_assistant_at?
last_human_at?
last_proactive_at?
interaction_frequency?
payload_schema_version
payload JSONB
updated_at
```

`relationship_state_versions` 保存不可变历史和 source watermark，唯一键 `(contact_id, version)`。payload 只放低频扩展信号，主要 proactive SQL 筛选字段必须提升为普通列。

### 10.5 `proactive_rule_occurrences`

每个可调度规则实例为immutable generation；源对象或policy变化创建新row并使旧rowinvalidated：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` / `contact_id` / `conversation_id` | UUIDv7 | widest composite FK |
| `occurrence_key` | BYTEA | 32-byte versioned HMAC |
| `generation` | INTEGER | 同key递增 |
| `reason_code` | TEXT | V1 allowlist五类 |
| `state` | TEXT | `scheduled/eligible/grouped/evaluated/suppressed/invalidated/expired` |
| `window_start_at` / `window_end_at` | TIMESTAMPTZ | 非空且start < end |
| `hard_deadline_at` | TIMESTAMPTZ | 不晚于window end |
| `timezone_name_snapshot` | TEXT | IANA zone |
| `local_date_snapshot` | DATE | 联系人当地日 |
| `importance` | NUMERIC(4,3) | 0..1 |
| `quiet_bypass_possible` | BOOLEAN | 只表示规则资格，不是授权 |
| `source_object_type` | TEXT | `life_event/intention/relationship/explicit_followup` |
| `source_object_id` / `source_version` | UUIDv7 / BIGINT | typed source identity |
| `policy_version_id` | UUIDv7 | FK immutable policy |
| `contact_setting_version_id` | UUIDv7 | nullable FK |
| `relationship_state_version` | BIGINT | snapshot |
| `created_at` / `terminal_at` | TIMESTAMPTZ | audit |

```text
UNIQUE (account_id, occurrence_key, generation)
UNIQUE (id, account_id, contact_id, conversation_id)
CHECK (window_start_at < window_end_at AND hard_deadline_at <= window_end_at)
CHECK (importance BETWEEN 0 AND 1)
CHECK (quiet_bypass_possible = false OR
       (reason_code IN ('event_upcoming', 'promise_due') AND importance >= 0.900))
```

`proactive_occurrence_evidence`使用与memory evidence相同的typed-FK模式，恰好引用一个memory version、life event、intention、message revision或受控rule code。证据失效通过join可找到尚未发送的occurrence/decision并排队reconcile。

### 10.6 Candidate 与 membership

`proactive_candidates`是同一联系人同一兼容窗口内的稳定聚合：

```text
id UUIDv7 PK
account_id
contact_id
conversation_id
candidate_key BYTEA
generation INTEGER
membership_hash BYTEA
state open|evaluating|send_selected|deferred_once|evaluated_none|failed_model|superseded|expired
window_start_at
window_end_at
policy_version_id
timezone_name_snapshot
mode_version_snapshot
content_revision_snapshot
activity_revision_snapshot
lease_owner_id?
lease_expires_at?
created_at
terminal_at?
UNIQUE (account_id, candidate_key, generation)
UNIQUE (id, account_id, conversation_id)
```

`proactive_candidate_occurrences(candidate_id, occurrence_id, ordinal)`保存有序成员，PK `(candidate_id, occurrence_id)`并unique `(candidate_id, ordinal)`；candidate、occurrence的widest composite FK保证同一account/contact/conversation。新增eligible occurrence产生新membership hash/generation，不修改已sealed candidate。

### 10.7 Budget bucket 与 reservation

`proactive_budget_buckets`：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK |
| `contact_id` | UUIDv7 | contact/bypass scope非空 |
| `scope` | TEXT | `account_daily/contact_daily/contact_bypass` |
| `local_date` | DATE | scope当地日 |
| `timezone_name_snapshot` | TEXT | IANA zone |
| `starts_at` / `ends_at` | TIMESTAMPTZ | 精确UTC边界 |
| `limit_count` | SMALLINT | snapshot，非负 |
| `held_count` / `committed_count` | SMALLINT | 非负且总和不超过limit |
| `version` | BIGINT | CAS |

```text
UNIQUE (account_id, scope, contact_id, local_date) NULLS NOT DISTINCT
CHECK ((scope = 'account_daily' AND contact_id IS NULL) OR
       (scope IN ('contact_daily','contact_bypass') AND contact_id IS NOT NULL))
CHECK (starts_at < ends_at)
CHECK (held_count >= 0 AND committed_count >= 0 AND
       held_count + committed_count <= limit_count)
```

Account bucket按账号timezone；contact/bypass bucket按联系人timezone。已创建bucket保存边界，timezone变化不重切历史日。

`proactive_budget_reservations`：

```text
id UUIDv7 PK
account_id
contact_id
conversation_id
decision_id
reservation_key BYTEA
state held|committed|released|expired|send_unknown
account_bucket_id
contact_bucket_id
bypass_bucket_id?
policy_version_id
authorization_generation
expires_at
held_at
terminal_at?
outbound_group_id?
copilot_draft_id?
reason_code?
UNIQUE (account_id, reservation_key)
UNIQUE (decision_id) WHERE state IN ('held','committed','send_unknown')
```

三类bucket按account/contact/bypass固定顺序加行锁，在一个事务中检查capacity、增加held并创建reservation。`committed/send_unknown`使held转committed；明确副作用前失败才release。RPC未知或partial按一次committed保守处理。

### 10.8 `proactive_decisions`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `contact_id` | UUIDv7 | composite FK contacts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `candidate_id` | UUIDv7 | unique composite FK sealed candidate generation |
| `idempotency_key` | BYTEA | 32-byte，candidate/config/prompt稳定键 |
| `state` | TEXT | `evaluating/send_selected/deferred_once/evaluated_none/authorizing/reserved/generating/send_ready/draft_ready/sending/sent/send_unknown/partial/skipped/failed` |
| `action` | TEXT | `send_now/defer_once/none`；模型成功时非空 |
| `decision_code` | TEXT | 受控原因，不存完整 chain-of-thought |
| `priority` | NUMERIC(4,3) | 0..1，只排序/审计，不授予权限 |
| `topic_brief` | TEXT | nullable，长度受限敏感derived data |
| `topic_hash` | BYTEA | canonical brief HMAC/hash |
| `defer_count` | SMALLINT | 0或1 |
| `defer_until` | TIMESTAMPTZ | 只在`defer_once`设置且位于window内 |
| `rule_snapshot_schema_version` | SMALLINT | NOT NULL |
| `rule_snapshot` | JSONB | 不含证据 ID 的规则数值与结果 |
| `policy_version_id` | UUIDv7 | immutable policy snapshot |
| `contact_setting_version_id` | UUIDv7 | nullable setting snapshot |
| `timezone_name_snapshot` | TEXT | IANA zone |
| `relationship_state_version` | BIGINT | snapshot |
| `account_control_version_snapshot` | BIGINT | global gate snapshot |
| `mode_version_snapshot` | BIGINT | snapshot |
| `content_revision_snapshot` | BIGINT | conversation content snapshot |
| `activity_revision_snapshot` | BIGINT | meaningful activity snapshot |
| `candidate_membership_hash` | BYTEA | sealed membership fingerprint |
| `model_run_id` | UUIDv7 | nullable composite FK |
| `model_role` | TEXT | run 存在时固定为 `proactive_agent` |
| `scheduled_for` | TIMESTAMPTZ | initial/deferred attempt time |
| `quiet_result_code` | TEXT | normal/bypass/suppressed code |
| `final_gate_result_code` | TEXT | nullable terminal gate code |
| `created_at` | TIMESTAMPTZ | DB default |
| `decided_at` | TIMESTAMPTZ | nullable |
| `terminal_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (account_id, idempotency_key)
UNIQUE (candidate_id)
UNIQUE (id, account_id, conversation_id)
CHECK (priority IS NULL OR priority BETWEEN 0 AND 1)
CHECK (defer_count BETWEEN 0 AND 1)
CHECK ((action = 'defer_once' AND defer_count = 1 AND defer_until IS NOT NULL) OR
       (action IN ('send_now','none') AND defer_until IS NULL) OR
       action IS NULL)
CHECK ((model_run_id IS NULL AND model_role IS NULL) OR
       (model_run_id IS NOT NULL AND model_role = 'proactive_agent'))
```

Decision的candidate/model/policy/settings均使用包含account/conversation的widest composite FK；`(model_run_id, account_id, model_role)`指向Proactive Agent run。最终发送仍通过带唯一`proactive_decision_id`的outbound delivery group及其intents，不允许Proactive worker直接产生Telegram副作用。Group引用生成正文的Main AI run；reservation、draft和group从业务侧引用decision，避免双向nullable FK产生不一致指针。

`proactive_decision_evidence` 保存可约束来源：

```text
decision_id
account_id
ordinal
occurrence_id
evidence_type
memory_version_id?
life_event_id?
intention_id?
message_revision_id?
rule_code?
PRIMARY KEY (decision_id, ordinal)
UNIQUE (decision_id, occurrence_id, evidence_type, ordinal)
CHECK occurrence_id 必须属于该 candidate membership
CHECK typed evidence FK 恰好一个非空，或 evidence_type = rule 且只有 rule_code
```

`proactive_decision_occurrences(decision_id, occurrence_id, ordinal)`保存模型选择的candidate成员，不能引用candidate之外的occurrence。证据ID不放入`rule_snapshot`，contact purge可以通过join index找到并清理所有派生candidate/decision。

## 11. 模型 endpoint、profile 与 credential

### 11.1 `model_endpoints`

endpoint 可被多个 profile 复用，但一旦被不可变 config version 引用，其安全关键字段不能原地修改；修改创建新 endpoint row。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `label` | TEXT | 管理员标签 |
| `base_url` | TEXT | 规范化 URL，经过 SSRF policy 验证 |
| `canonical_sha256` | BYTEA | 32-byte canonical URL hash |
| `network_policy_id` | UUIDv7 | server-managed allowlist policy ref |
| `network_policy_version` | BIGINT | 验证时的不可变policy版本 |
| `network_category` | TEXT | `public/private` |
| `created_by_admin_id` | BIGINT | 创建者；不代表可修改network policy |
| `created_at` | TIMESTAMPTZ | DB default |

`UNIQUE(canonical_sha256, network_policy_id, network_policy_version)`复用完全相同的已验证endpoint。endpoint row和capability snapshot均由数据库trigger禁止UPDATE/DELETE；变更URL或policy必须创建新row。认证header属于protocol-specific config option，不放在endpoint identity中。

### 11.2 `model_profiles`

V1 部署初始化四行：

```text
main_ai
memory_agent
proactive_agent
embedding
```

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `logical_role` | TEXT | `UNIQUE NOT NULL` |
| `profile_kind` | TEXT | `generation/embedding` |
| `state` | TEXT | `disabled/active/blocked` |
| `active_config_version_no` | INTEGER | nullable deferrable composite FK |
| `version` | BIGINT | role binding CAS version |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | 激活时更新 |

```text
UNIQUE (id, logical_role)
UNIQUE (id, profile_kind)
```

三个 generation profile 必须互相独立；更新一个 profile 不得更新其他行。Embedding 的 profile kind 和字段约束不同于 generation。

### 11.3 `model_config_drafts`

短生命周期 Control Bot 输入会话修改 draft；Web App 不写此表。

```text
id UUIDv7 PK
profile_id
created_by_admin_id BIGINT
expected_profile_version BIGINT
state editing|validated|cancelled|expired|activated|conflict
endpoint_id?
credential_id?
protocol?
model_name?
temperature?
max_output_tokens?
timeout_seconds?
enabled?
protocol_options_schema_version
protocol_options JSONB
capability_snapshot_id?
validation_error_code?
draft_version BIGINT
created_at
updated_at
expires_at
consumed_at?
```

同一 profile 可以有多个管理员历史 draft，但 partial unique index 限制同一 admin/profile 同时一个 `editing/validated` draft。draft 使用 `draft_version` optimistic lock；激活同时比较`expected_profile_version`。Web App从不写draft。

### 11.4 `model_config_versions`

validated draft 复制成不可变版本；不能把 draft row 直接改名为 active version。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `profile_id` | UUIDv7 | FK profiles |
| `profile_kind` | TEXT | `generation/embedding`，与 profile composite FK 一致 |
| `version_no` | INTEGER | role 内递增 |
| `source_draft_id` | UUIDv7 | nullable FK |
| `endpoint_id` | UUIDv7 | FK endpoints |
| `credential_id` | UUIDv7 | profile-owned stable credential FK |
| `protocol` | TEXT | generation 三选一；embedding 使用 `embedding` |
| `model_name` | TEXT | NOT NULL |
| `temperature` | NUMERIC | nullable |
| `max_output_tokens` | INTEGER | generation positive，embedding null |
| `timeout_seconds` | INTEGER | positive |
| `enabled` | BOOLEAN | NOT NULL |
| `protocol_options_schema_version` | SMALLINT | NOT NULL |
| `protocol_options` | JSONB | discriminated schema |
| `capability_snapshot_id` | UUIDv7 | NOT NULL FK到独立不可变snapshot |
| `config_sha256` | BYTEA | canonical payload SHA-256 |
| `created_by_admin_id` | BIGINT | 激活者 |
| `validated_at` | TIMESTAMPTZ | NOT NULL |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (profile_id, version_no)
UNIQUE (profile_id, id)
CHECK profile_kind 与 generation/embedding 字段组合一致
```

`model_profiles` 提供 `(id, profile_kind)` unique key，config version 使用 `(profile_id, profile_kind)` composite FK；因此字段组合 CHECK 不需要跨表读取。整个config version row由trigger禁止UPDATE/DELETE，active lifecycle只通过`model_profiles.active_config_version_no + version`的CAS指针表达。

`model_profiles(id, active_config_version_no)` 使用deferrable composite FK指向`model_config_versions(profile_id, version_no)`。config version只引用稳定credential identity，不锁死某次key轮换；获取active snapshot时解析credential当前active version，每个`model_run`开始时再固定实际使用的credential version，使轮换只影响新run而不破坏旧run恢复。

### 11.5 `model_capability_snapshots`

每次无私人数据probe写入一行不可变观察，至少包含endpoint、protocol、model、text/temperature/reasoning/image/stream/structured能力、Chat token字段、context/output上限、输入role、embedding dimensions、status和`observed_at/expires_at`。draft validation和activation事务都必须确认snapshot匹配endpoint/protocol/model、状态为`valid`且未过期，并重新执行domain admission；过期或能力漂移创建新snapshot，不更新旧row。

### 11.6 Credential

`model_credentials` 是 role-owned 稳定 identity：

```text
id UUIDv7 PK
profile_id UNIQUE
status missing|active|deleted
active_version_no?
latest_version_no INTEGER DEFAULT 0
created_at
updated_at
```

`latest_version_no`只增不减，删除只清空active pointer而不重用历史序号；delete后重新set创建`latest_version_no + 1`。

`model_credential_versions` 保存独立轮换版本：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `credential_id` | UUIDv7 | FK credentials |
| `profile_id` | UUIDv7 | 冗余 composite scope |
| `version_no` | INTEGER | 从 1 递增 |
| `ciphertext` | BYTEA | AES-GCM ciphertext与authentication tag |
| `nonce` | BYTEA | 随机96-bit nonce |
| `algorithm` | TEXT | V1固定`aes_256_gcm` |
| `key_version` | INTEGER | credential master key key ID/version |
| `aad_schema_version` | SMALLINT | AAD绑定deployment/profile/credential/version |
| `secret_fingerprint` | BYTEA | keyed fingerprint，只用于判断是否变化 |
| `created_at` | TIMESTAMPTZ | DB default |
| `destroyed_at` | TIMESTAMPTZ | nullable，destroyed 时 ciphertext、nonce、fingerprint 全部清空 |
| `destroy_reason` | TEXT | destroyed时必填 |

```text
UNIQUE (credential_id, version_no)
UNIQUE (credential_id, profile_id, version_no)
```

credential 不跨 profile 共享。即使管理员输入相同 key，也创建独立 ciphertext/version，轮换一个角色不会隐式改变另一个角色。

key-only Web App设置或替换API key时，在credential row lock和expected version CAS下创建新version并切换`active_version_no`；它不创建或修改非密钥config version。已经开始的model run继续使用启动时取得的旧credential version，受限SECURITY DEFINER函数允许app/worker按已固定版本读取任何尚未销毁的同profile版本。删除会禁用profile并单向清空所有版本的ciphertext/nonce/fingerprint；M4引入in-flight run后，删除编排必须先处理这些引用再执行销毁。

`model_credentials` 提供 `(id, profile_id)` unique key；`model_config_versions(profile_id, credential_id)` 使用 composite FK，保证 config 不能引用其他 role 的 credential。`model_credential_versions` 同样以 `(credential_id, profile_id)` 约束 owner。`model_credentials(id, active_version_no)` 使用 deferrable composite FK 指向 `model_credential_versions(credential_id, version_no)`。

### 11.7 Control与key-only短会话

持久化非密钥多步 Bot 配置会话的最小状态：

```text
id UUIDv7 PK
admin_telegram_user_id BIGINT
profile_id
draft_id
state active|consumed|cancelled|expired
expected_draft_version
pending_field?
session_nonce_hash BYTEA
created_at
expires_at
consumed_at?
```

同一管理员只允许一个active输入会话；开始新会话会取消旧会话和对应open draft。不保存API key或疑似key的拒绝输入。

`model_key_launch_sessions`独立记录token hash、admin、profile、`set/replace/delete`、deployment version、expected credential version、5分钟过期和一次性`consumed_at`；不保存initData、明文token或key。`model_key_rate_limits`只保存HMAC/hash principal、窗口、计数、block时间和CAS version。

### 11.8 Canonical generation 与 protocol options

不可变 config version 只保存可配置的 canonical 字段：

```text
endpoint / credential identity
protocol
model_name
temperature?
max_output_tokens?
timeout_seconds
enabled
protocol_options
validated capabilities
```

每次请求才产生的 canonical 字段不复制进 config：

```text
system_instructions
messages / content parts
response_schema?
stream
```

它们由 prompt version、context manifest、业务 purpose 和 adapter version 共同确定；`model_runs` 保存这些版本及 canonical input keyed fingerprint。需要保留的业务输出写入 outbound delivery group/intents、memory proposal 或 proactive decision，model run 不保存完整 raw body。

`protocol_options` 使用 `protocol` 作为 discriminator：

| Protocol | schema | 允许内容 |
|---|---|---|
| `openai_responses` | `ResponsesOptions@N` | 经过 allowlist 的 Responses 特有参数 |
| `openai_chat_completions` | `ChatCompletionsOptions@N` | token limit field compatibility 等受控参数 |
| `anthropic_messages` | `MessagesOptions@N` | API version、认证/header/path policy 等受控参数 |
| `embedding` | `EmbeddingOptions@N` | dimensions、encoding、batch policy 等受控参数 |

`openai_chat_completions` 只表示 Chat Completions，不支持旧式纯文本 `/completions`。任意请求 JSON、任意 header、完整 URL override 和 secret 都不能进入该 JSONB。切换 protocol 必须重新验证 schema 和 endpoint policy，并创建新 config version。

### 11.8 Prompt、Context 与 retrieval policy version

Model config 与 Context policy 独立版本化，避免修改 budget 时伪装成模型 endpoint 切换：

```text
context_policies(
  id UUIDv7 PK,
  logical_role TEXT,
  purpose TEXT,
  active_version_id UUIDv7?,
  version BIGINT,
  created_at,
  updated_at,
  UNIQUE (logical_role, purpose)
)

context_policy_versions(
  id UUIDv7 PK,
  policy_id UUIDv7,
  version_no INTEGER,
  status validated|active|retired,
  max_input_tokens INTEGER,
  safety_reserve_basis_points INTEGER,
  minimum_safety_reserve_tokens INTEGER,
  current_budget_basis_points INTEGER,
  recent_budget_basis_points INTEGER,
  profile_budget_basis_points INTEGER,
  structured_budget_basis_points INTEGER,
  semantic_budget_basis_points INTEGER,
  summary_budget_basis_points INTEGER,
  structured_limit INTEGER,
  semantic_limit INTEGER,
  ann_candidate_limit INTEGER,
  current_image_limit INTEGER,
  fallback_auto_image_tokens INTEGER,
  telegram_max_text_units INTEGER,
  max_delivery_chunks INTEGER,
  token_estimator_policy TEXT,
  created_at,
  activated_at?,
  UNIQUE (policy_id, version_no),
  UNIQUE (id, policy_id),
  CHECK six budget basis-point columns sum to 10000
)
```

V1 Main AI 默认分别为 24,000、500 basis points、1,024、20/30/15/15/10/10%、12、8、64、10、2,048、4,096、8。`FOREIGN KEY (active_version_id, id) REFERENCES context_policy_versions(id, policy_id)`使用deferrable composite FK，并由partial unique在activation transaction中保证每个policy只有一个active version。

检索公式和 prompt 也保留不可变 registry。检索策略使用稳定identity和显式active pointer，不能依赖“版本号最大”推断当前版本：

```text
retrieval_policies(
  id UUIDv7 PK,
  policy_name TEXT UNIQUE,
  active_version_id UUIDv7?,
  version BIGINT,
  created_at,
  updated_at
)

retrieval_policy_versions(
  id UUIDv7 PK,
  policy_id UUIDv7,
  version_no INTEGER,
  status validated|active|retired,
  structured weight columns,
  semantic weight columns,
  half_life_schema_version,
  half_life_policy JSONB,
  tie_break_version,
  source_default_schema_version,
  source_defaults JSONB,
  content_sha256,
  created_at,
  activated_at?,
  UNIQUE (policy_id, version_no),
  UNIQUE (id, policy_id)
)

prompt_versions(
  id UUIDv7 PK,
  account_id?,
  logical_role,
  purpose,
  version,
  source_kind packaged|admin,
  content_text,
  content_sha256,
  schema_version,
  status validated|active|retired,
  created_at,
  activated_at?,
  redacted_at?
)
```

```text
UNIQUE (account_id, logical_role, purpose, version)
  WHERE account_id IS NOT NULL
UNIQUE (logical_role, purpose, version)
  WHERE account_id IS NULL
UNIQUE (account_id, logical_role, purpose)
  WHERE account_id IS NOT NULL AND status = 'active'
UNIQUE (logical_role, purpose)
  WHERE account_id IS NULL AND status = 'active'
```

`FOREIGN KEY (active_version_id, id) REFERENCES retrieval_policy_versions(id, policy_id)`使用deferrable composite FK；partial unique保证每个policy至多一个`active`version。Weights、limits、tie-break和active binding使用普通列/约束；只有按独立schema验证的per-type half-life/source default放JSONB。Prompt正文可能含个人identity instruction，按敏感配置读取、导出、备份和account wipe policy处理，不写日志。一次run保存实际 prompt bundle/version hash、retrieval policy version ID和context policy version ID；配置切换不改变在途run。

## 12. Agent state、服务状态与 audit

### 12.1 Agent state

`agent_states` 保存 deployment/account 当前状态：

```text
id UUIDv7 PK
account_id?
state_key
value_schema_version
value JSONB
version BIGINT
updated_at
```

PostgreSQL 对 nullable column 的普通 unique 不会把多个 NULL 视为冲突，因此这里用两个 partial unique index：

```text
UNIQUE (account_id, state_key) WHERE account_id IS NOT NULL
UNIQUE (state_key) WHERE account_id IS NULL
```

global pause、maintenance mode、预算代次等安全关键值应提升为 typed column 或独立表；Conversation Orchestrator 使用第 7.1 节 typed table，不得把这些值重复放入任意 JSONB。`agent_state_history` 只保存其他低频 versioned change metadata。

### 12.2 Control command 与 operational block

`control_commands` 持久化 Control Bot 写操作的幂等 identity：

```text
id UUIDv7 PK
bot_identity_id
telegram_update_id BIGINT
admin_user_id BIGINT
command_kind
scope_type
account_id
conversation_id?
expected_version?
result_version?
state received|confirmed|applied|rejected|expired
idempotency_key BYTEA
result_code
created_at
completed_at?
UNIQUE (bot_identity_id, telegram_update_id)
UNIQUE (idempotency_key)
```

command row 不保存 contact/message/draft 正文或 callback token。重复 update 返回既有 result，不再次递增 control/mode version。

`orchestrator_blocks` 使用 typed scope：

```text
id UUIDv7 PK
account_id
model_profile_id?
conversation_id?
turn_id?
reason_code
state active|probing|cleared
version
first_seen_at
retry_after?
last_probe_at?
cleared_at?
CHECK scope columns match scope_type
```

同一 scope/reason 至多一个 active generation。block 不保存 provider raw body；clear 后旧 row 保留最小 operational audit。

### 12.3 Context preview

`/context`只查询manifest聚合view。完整`/context_preview`使用三张不保存正文的表：

```text
context_preview_requests(
  id UUIDv7 PK,
  control_command_id UUIDv7 UNIQUE,
  bot_identity_id UUIDv7,
  admin_user_id BIGINT,
  bot_chat_id BIGINT,
  account_id UUIDv7,
  conversation_id UUIDv7,
  context_manifest_id UUIDv7,
  manifest_sha256 BYTEA,
  source_revision_vector_sha256 BYTEA,
  state pending_confirmation|confirmed|delivering|delivered|send_unknown|
        delete_pending|deleted|delete_partial|expired|cancelled|failed,
  chunk_count INTEGER?,
  delivered_chunk_count INTEGER NOT NULL DEFAULT 0,
  token_expires_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ?,
  delivered_at TIMESTAMPTZ?,
  delete_after TIMESTAMPTZ?,
  completed_at TIMESTAMPTZ?,
  last_error_code TEXT?,
  created_at TIMESTAMPTZ,
  UNIQUE (id, admin_user_id, bot_chat_id),
  UNIQUE (id, bot_identity_id, bot_chat_id),
  CHECK delivered_chunk_count >= 0 AND
        (chunk_count IS NULL OR delivered_chunk_count <= chunk_count)
)

context_preview_tokens(
  id UUIDv7 PK,
  request_id UUIDv7 UNIQUE,
  admin_user_id BIGINT,
  bot_chat_id BIGINT,
  purpose context_preview_confirm,
  token_hash BYTEA UNIQUE,
  expires_at TIMESTAMPTZ,
  used_at TIMESTAMPTZ?,
  created_at TIMESTAMPTZ,
  FOREIGN KEY (request_id, admin_user_id, bot_chat_id)
    REFERENCES context_preview_requests(id, admin_user_id, bot_chat_id)
)

context_preview_deliveries(
  id BIGINT IDENTITY PK,
  request_id UUIDv7,
  bot_identity_id UUIDv7,
  bot_chat_id BIGINT,
  ordinal INTEGER,
  state pending|sending|sent|send_unknown|delete_pending|deleted|delete_failed,
  bot_message_id BIGINT?,
  sent_at TIMESTAMPTZ?,
  delete_after TIMESTAMPTZ?,
  deleted_at TIMESTAMPTZ?,
  last_error_code TEXT?,
  UNIQUE (request_id, ordinal),
  FOREIGN KEY (request_id, bot_identity_id, bot_chat_id)
    REFERENCES context_preview_requests(id, bot_identity_id, bot_chat_id),
  UNIQUE (bot_identity_id, bot_chat_id, bot_message_id)
    WHERE bot_message_id IS NOT NULL
)
```

Delivery表冗余`bot_identity_id/bot_chat_id`以建立上述unique和删除路由，并通过包含request scope的composite FK保持一致。Preview token保存hash而非callback明文，并通过request间接绑定exact manifest，同时直接绑定admin、Bot chat和默认5分钟deadline。

确认CAS消费token后，`control`再次验证manifest/source未redacted，在内存中重建并按plain-text chunk发送。每个已知Bot message ID默认10分钟后best-effort删除。Bot send在RPC后断连可能成为`send_unknown`；由于Bot API路径没有本项目可持久化的random ID对账，系统不自动重发unknown chunk，避免扩大敏感内容复制。没有message ID时无法可靠自动删除，必须告警并在Disclosure中保留该残余风险。

Preview正文不进入request/token/delivery、queue、audit或普通log；图片只显示reference/hash/MIME/尺寸/detail metadata，`control`不读取media二进制。

### 12.4 服务状态

`service_instances` 保存当前 heartbeat projection：

```text
instance_id UUID PK
service_name
started_at
last_heartbeat_at
readiness
status_code
schema_revision
last_successful_operation_at?
metadata JSONB (白名单、无路径/secret)
UNIQUE (service_name, instance_id)
```

`service_status_events` 使用 BIGINT IDENTITY append-only 记录状态变化，不按每次 heartbeat 写历史，避免无价值增长。

### 12.5 `audit_log`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `occurred_at` | TIMESTAMPTZ | DB default，不允许调用者覆盖 |
| `account_id` | UUIDv7 | nullable |
| `actor_type` | TEXT | `admin/human/service/system` |
| `actor_ref` | TEXT | nullable internal ref |
| `action` | TEXT | 受控 action code |
| `target_type` | TEXT | 受控 discriminator |
| `target_id` | TEXT | internal ID string |
| `result` | TEXT | `success/rejected/failed` |
| `reason_code` | TEXT | nullable |
| `request_id` | UUID | correlation |
| `before_sha256` | BYTEA | nullable canonical state hash |
| `after_sha256` | BYTEA | nullable canonical state hash |
| `metadata_schema_version` | SMALLINT | NOT NULL |
| `metadata` | JSONB | 无正文、Prompt、secret、路径 |

应用数据库角色只能 INSERT/SELECT，不能 UPDATE/DELETE。维护角色执行 retention/purge 必须记录独立 audit 事件。V1 不做逐行密码学 hash chain，因为没有外部可信锚定时无法防止拥有数据库管理权限的人重写整条链。

## 13. Retention、export 与删除

### 13.1 Retention class

`retention_policies` 由 Operations 管理，Data Model 固定其 identity：

```text
retention_class PK
description
ttl_seconds?
terminal_ttl_seconds?
policy_version
updated_at
```

Operations固定V1 privacy-balanced profile：

| 数据 | 默认语义 |
|---|---|
| canonical message/revision | 不自动过期，直到 Telegram delete/contact purge/account wipe |
| active memory/summary | 不按年龄简单删除，由 Memory 生命周期或显式 forget 管理 |
| validated media | original 30天、provider copy 24小时；过期后不得继续作为模型输入 |
| read/typing/service transient event | 7天 |
| debug raw capture | 默认关闭；显式启用最多1小时 |
| model/outbound attempts | terminal后30天，只保留错误码/usage/metadata，不保留raw body |
| memory candidate | active最多30天；expire后redact候选正文 |
| proactive occurrence/candidate/decision/reservation | typed provenance按audit需要保留；terminal topic/brief 30天后redact，只留ID/hash/code/version/预算/结果 |
| COPILOT draft/revision/edit session | active最多30分钟；terminal正文30分钟内redact |
| control command/action token | terminal session 30分钟；unused token按自身更短TTL |
| context preview request/token/delivery | DB metadata 30天，正文永不落表；token默认5分钟；已知Bot消息默认10分钟后尽力删除 |
| service/model status aggregate | 30天 |
| audit | 无正文metadata 365天 |
| encrypted export artifact | 24小时 |
| credential retired version | 无in-flight引用后最多7天销毁ciphertext，保留非秘密版本审计 |

支持 TTL 的表显式保存 `retention_class` 与 `expires_at`。`expires_at` 是具体 policy snapshot，后续修改 policy 不应无审计地改写既有记录。

### 13.2 三类删除操作

#### Memory forget

针对一个或多个 memory identity：

1. 将 memory 状态改为 `forgotten`，创建 forget/invalidation history。
2. 从 Context 可见查询立即排除。
3. invalidate/delete 相关 embedding records。
4. invalidate/redact 仅由该 memory 派生且无法独立重建的 life event、intention、relationship/proactive projection。
5. 默认清除该 memory 的全部 payload/rendered text 和 embedding；message evidence 关联与未被删除的 canonical message revision 默认保留，但不能从已 forget memory 恢复正文。
6. 写 audit，不把被删除正文复制到 audit。

`/forget` 不等于删除联系人聊天历史。

#### Contact purge

针对 `account_id + contact_id`：

1. 创建 durable erasure request，contact/conversation 标记 `deleting`，关闭自动发送。
2. 在同一逻辑边界让 messages、media、memories、summaries、embeddings、proactive occurrence/candidate/decision 和未提交 reservation 不再可见或可执行。
3. 擦除所有 message revisions、COPILOT draft revisions/edit sessions、outbound payload、memory/summary versions、debug capture、proactive topic/brief和关系派生正文；释放可证明未产生副作用的held reservation，unknown/partial只保留无正文保守计费审计。
4. 取消仍待确认/发送的 context preview request，消费或失效未使用 token；对已知 Bot message ID 创建不含正文的立即删除任务并记录结果。
5. 删除 `media-data` 物理对象并确认结果。
6. 删除或匿名化可删除的 operational rows；保留最小 tombstone、erasure ledger 和不含正文的法定/安全 audit。
7. 完成后 conversation/contact 标记 deleted，写 completion audit。

已经发送到 Telegram Bot chat 的 preview 属于外部副本。Contact purge 必须尽力删除已知 message ID，但不能把 Telegram 通知、客户端缓存、转发、截图、平台副本或 `send_unknown` 消息声明为已经擦除。

#### Account wipe

覆盖该账号全部联系人和业务数据，并额外：

- 停止 `app`、worker side effects 和配置激活；
- 保留部署级 model profile/credential 配置；account wipe 不等于 deployment credential destruction。若要退役整个部署，必须另建显式 credential destruction 操作；
- 删除 media、embedding、jobs、cache 和 outbox pending payload；
- 取消全部 context preview request/token，并尽力删除有已知 Bot message ID 的 preview；
- 在数据库 purge 完成后由 Operations 单独处理 Telethon Session volume 和备份；
- 保留不会恢复敏感正文的最小 erasure ledger。

广泛 `ON DELETE CASCADE` 不能代替这三套流程，因为物理媒体、embedding index、Redis、备份和审计都有不同处置要求。

### 13.3 `data_erasure_requests`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `scope_type` | TEXT | `memory/contact/account` |
| `memory_id` | UUIDv7 | nullable |
| `contact_id` | UUIDv7 | nullable |
| `state` | TEXT | `requested/quiescing/redacting/media_cleanup/derived_cleanup/completed/failed` |
| `requested_by` | TEXT | internal admin/system ref |
| `request_idempotency_key` | BYTEA | 32-byte |
| `policy_version` | BIGINT | deletion policy snapshot |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | CAS |
| `completed_at` | TIMESTAMPTZ | nullable |
| `last_error_code` | TEXT | nullable |

```text
UNIQUE (account_id, request_idempotency_key)
CHECK scope_type 与 target FK 恰好一致
```

每个步骤使用独立 idempotency key 和 progress row，崩溃后可继续，不允许因为部分失败重新暴露已逻辑删除内容。

### 13.4 `erasure_ledger`

小型 append-only ledger 防止恢复旧备份后已删除数据重新进入服务：

```text
id BIGINT IDENTITY PK
account_scope_hmac
scope_type
target_scope_hmac
request_id
policy_version
completed_at
```

HMAC 使用独立 erasure-ledger key，不能使用普通 SHA-256 直接散列可枚举的 Telegram ID，也不能复用 credential master key。ledger 不保存正文、username、display name 或可直接显示的 Telegram ID。备份恢复后先回放 ledger 和 deletion jobs，再开放模型读取/自动发送。ledger 自身和 HMAC key 必须分别进入加密备份与恢复演练。

### 13.5 Export

`data_export_requests` 保存账号/联系人导出的 durable 状态：

```text
id UUIDv7 PK
account_id
contact_id?
state
requested_by
format_version
created_at
expires_at
completed_at?
artifact_sha256?
last_error_code?
```

导出可包含 canonical messages、非 redacted revisions、active/history memories、summary、关系/主动性审计摘要和 media manifest。默认不包含：

- Telethon Session、API Hash、Bot token、API key 或 credential ciphertext；
- access hash、diagnostic raw capture 和内部网络 endpoint secret；
- 已 redacted/forgotten/purged 的正文；
- 模型完整 raw request/response。

V1导出产物使用维护者age public recipient加密到root-only`export-staging`，只通过既有受限SSH/SFTP由host operator按request ID/hash取回，24小时内删除；不扩展Web App、Caddy或Telegram Bot下载路径。

### 13.6 Backup 与 restore 边界

- PostgreSQL backup 必须整体加密，密钥与 backup 分开。
- PostgreSQL使用pgBackRest continuous WAL、每日differential、每周full和4个full set retention；目标RPO 15分钟。
- Telethon Session每日及升级前由app停止后的one-shot helper校验并使用独立restic repository加密备份，保留7天。
- `media-data`、Redis AOF、cache和Caddy private key默认不进入DR backup。
- 因敏感正文在数据库中为明文，未加密 SQL dump 不得落入普通磁盘、日志或 CI artifact。
- credential master key、diagnostic key、Telethon Session 和数据库 backup 分别管理；任一单独备份不能自动恢复全部权限。
- restore 后所有 model credential 解密、account ownership、schema revision、outbound unknown intent 和 erasure ledger 检查通过前，自动发送 fail closed。
- 每个completed erasure通过outbox导出无正文HMAC ledger到独立加密off-host snapshot；恢复旧点必须叠加最新ledger。
- backup retention不得成为绕过contact purge/account wipe的长期影子存储；到期和restore erasure replay按Operations执行并留存无正文证据。

## 14. 外键与删除策略

### 14.1 默认策略

- 核心事实和版本表：`ON DELETE RESTRICT`。
- 纯 join table，如 `turn_messages`、proposal evidence：父实体由显式 purge 删除时可以 `ON DELETE CASCADE`。
- current-version 指针使用 `DEFERRABLE INITIALLY DEFERRED`，允许同一事务创建 version 并更新 parent。
- `SET NULL` 只用于保留 non-sensitive historical attribution，例如已删除 worker instance ref；不能让 evidence 静默失去来源。
- composite FK 必须包含 `account_id`，防止跨账号引用。

### 14.2 One-way redaction

message revision、COPILOT draft revision、debug capture、model run content fingerprint、outbound payload、proposal payload、memory/summary version 和 credential version 的 redaction/destroy 只允许：

```text
content present -> content cleared + redacted/destroyed timestamp
```

数据库 trigger 或受限 stored procedure 阻止从 redacted 状态恢复内容。context manifest item 的 content hash、summary content hash 以及其他 content-derived SHA-256 必须在来源 redaction 时同步清除；embedding record 则 invalidated 后物理删除。应用普通写角色不能直接执行批量 UPDATE 绕过 erasure request。

## 15. 索引设计

### 15.1 身份与消息

必要 B-tree/unique index：

```text
telegram_peers(peer_type, telegram_peer_id) UNIQUE
account_peers(account_id, peer_id) UNIQUE
contacts(account_id, account_peer_id) UNIQUE
conversations(account_id, telegram_chat_id) UNIQUE
messages(account_id, conversation_id, telegram_message_id) UNIQUE
messages(conversation_id, telegram_created_at DESC, telegram_message_id DESC)
messages(conversation_id, grouped_id, telegram_message_id) WHERE grouped_id IS NOT NULL
message_revisions(message_id, revision_no) UNIQUE
message_events(account_id, fingerprint_version, update_fingerprint) UNIQUE
message_events(conversation_id, id)
message_media(message_revision_id, position) UNIQUE
```

Context 查询 active message 使用 partial/covering index 时包含 `deleted_at IS NULL`，但仍必须 join non-redacted current revision。

### 15.2 Turn、run 与 intent

```text
account_orchestrator_states(account_id) PRIMARY KEY
conversation_turns(conversation_id, collection_sequence) UNIQUE
conversation_turns(conversation_id, state, quiet_deadline_at)
model_runs(turn_id, logical_role, generation_no) UNIQUE
model_runs(state, started_at)
  WHERE state IN ('queued','running','cancel_requested','failed_retryable')
copilot_drafts(conversation_id)
  UNIQUE WHERE state IN ('requested','collecting','generating','ready','editing','approved','send_queued','send_unknown')
copilot_drafts(expires_at)
  WHERE state IN ('ready','editing')
copilot_draft_revisions(draft_id, revision_no) UNIQUE
context_manifests(turn_id, logical_role, builder_version, manifest_sha256) UNIQUE
  WHERE turn_id IS NOT NULL
context_manifests(background_job_id, logical_role, builder_version, manifest_sha256) UNIQUE
  WHERE background_job_id IS NOT NULL
context_manifest_items(manifest_id, ordinal) UNIQUE
context_manifest_item_reasons(manifest_item_id, reason_ordinal) PRIMARY KEY
context_manifest_omissions(manifest_id, layer, reason_code)
context_policies(logical_role, purpose) UNIQUE
context_policy_versions(policy_id, version_no) UNIQUE
context_policy_versions(policy_id) UNIQUE WHERE status = 'active'
retrieval_policies(policy_name) UNIQUE
retrieval_policy_versions(policy_id, version_no) UNIQUE
retrieval_policy_versions(policy_id) UNIQUE WHERE status = 'active'
prompt_versions(account_id, logical_role, purpose, version) UNIQUE
  WHERE account_id IS NOT NULL
prompt_versions(logical_role, purpose, version) UNIQUE
  WHERE account_id IS NULL
prompt_versions(account_id, logical_role, purpose) UNIQUE
  WHERE account_id IS NOT NULL AND status = 'active'
prompt_versions(logical_role, purpose) UNIQUE
  WHERE account_id IS NULL AND status = 'active'
outbound_delivery_groups(turn_id, generation_no, source) UNIQUE
  WHERE turn_id IS NOT NULL
outbound_delivery_groups(proactive_decision_id) UNIQUE
  WHERE proactive_decision_id IS NOT NULL
outbound_delivery_groups(copilot_draft_id) UNIQUE
  WHERE copilot_draft_id IS NOT NULL
outbound_delivery_groups(state, created_at)
  WHERE state IN ('pending','sending','partial')
outbound_intents(account_id, idempotency_key) UNIQUE
outbound_intents(account_id, telegram_random_id) UNIQUE
outbound_intents(delivery_group_id, chunk_ordinal) UNIQUE
outbound_intents(state, next_attempt_at)
  WHERE state IN ('pending','sending','sent_unconfirmed','retry_wait','unknown')
outbound_intents(conversation_id, telegram_message_id)
```

### 15.3 Job、outbox 与清理

```text
background_jobs(queue_name, state, priority DESC, available_at)
  WHERE state IN ('pending','retry_wait')
background_jobs(lease_expires_at) WHERE state = 'leased'
transactional_outbox(id) WHERE published_at IS NULL
debug_payload_captures(expires_at) WHERE redacted_at IS NULL
media_objects(expires_at) WHERE status = 'ready' AND expires_at IS NOT NULL
media_objects(delete_requested_at) WHERE status = 'delete_pending'
orchestrator_blocks(scope_type, account_id, reason_code)
  WHERE state IN ('active','probing')
control_commands(bot_identity_id, telegram_update_id) UNIQUE
copilot_action_tokens(expires_at) WHERE used_at IS NULL
copilot_edit_sessions(expires_at) WHERE completed_at IS NULL
context_preview_requests(state, token_expires_at)
  WHERE state = 'pending_confirmation'
context_preview_requests(state, delete_after)
  WHERE state IN ('delivered','delete_pending','delete_partial')
context_preview_tokens(expires_at) WHERE used_at IS NULL
context_preview_deliveries(delete_after)
  WHERE state IN ('sent','delete_pending','delete_failed')
```

### 15.4 Memory 与 proactive

```text
memories(account_id, contact_id, memory_type, status)
memories(account_id, contact_id, memory_type, semantic_key_hash)
memory_versions(memory_id, version_no) UNIQUE
memory_evidence(message_revision_id) WHERE message_revision_id IS NOT NULL
memory_evidence(media_object_id) WHERE media_object_id IS NOT NULL
memory_proposals(state, expires_at)
memory_proposal_targets(target_memory_id)
memory_proposal_evidence(message_revision_id)
memory_jobs(conversation_id, job_kind, state, quiet_until, hard_due_at)
  WHERE state IN ('pending','retry_wait')
memory_input_manifest_items(message_revision_id)
memory_input_manifest_items(memory_version_id)
memory_watermarks(conversation_id, watermark_kind) PRIMARY KEY
summary_version_sources(message_revision_id)
summary_version_sources(prior_summary_version_id)
summary_watermarks(conversation_id, summary_kind) PRIMARY KEY
memory_review_actions(expires_at) WHERE used_at IS NULL
life_events(contact_id, status, start_at)
intentions(contact_id, status, expected_at)
proactive_policy_bindings(scope_type, account_id) UNIQUE NULLS NOT DISTINCT
contact_proactive_settings(contact_id) PRIMARY KEY
proactive_rule_occurrences(account_id, occurrence_key, generation) UNIQUE
proactive_rule_occurrences(contact_id, state, window_start_at, window_end_at)
  WHERE state IN ('scheduled','eligible','grouped')
proactive_occurrence_evidence(memory_version_id)
proactive_occurrence_evidence(life_event_id)
proactive_occurrence_evidence(intention_id)
proactive_candidates(account_id, candidate_key, generation) UNIQUE
proactive_candidates(contact_id, state, window_end_at)
  WHERE state IN ('open','evaluating','send_selected','deferred_once')
proactive_candidate_occurrences(occurrence_id)
proactive_decisions(account_id, idempotency_key) UNIQUE
proactive_decisions(candidate_id) UNIQUE
proactive_decision_occurrences(occurrence_id)
proactive_budget_buckets(account_id, scope, contact_id, local_date)
  UNIQUE NULLS NOT DISTINCT
proactive_budget_reservations(account_id, reservation_key) UNIQUE
proactive_budget_reservations(decision_id)
  UNIQUE WHERE state IN ('held','committed','send_unknown')
proactive_budget_reservations(state, expires_at)
  WHERE state = 'held'
```

### 15.5 JSONB、全文和向量

- 不默认对所有 JSONB 建 GIN index；只有实现期稳定query、EXPLAIN和代表性规模证据证明需要时，才添加表达式或受限 GIN index。
- V1 不以数据库全文检索作为 Context 主契约；若增加，索引只覆盖 non-redacted current content。
- 每个 active embedding space/dimension 建独立 pgvector ANN index，切换 space 后并行构建并原子切换查询 binding。
- deleted/invalidated embedding 使用 partial index predicate 或在切换前物理清理，不进入检索结果。

### 15.6 分区准备

V1 不分区。`message_events`、`audit_log`、`model_run_attempts`、`outbound_attempts`、`service_status_events` 和 `transactional_outbox` 的主键均为单调 BIGINT，并带 `created_at/occurred_at`，未来可通过新分区表、双写/回填和 rename migration 转换。

不能在 V1 预先分区，因为 partition key 会限制全局 unique constraint，并显著增加小型单账号部署的 migration 和备份复杂度。

## 16. 事务边界与并发

### 16.1 通用原则

- 默认 isolation 使用 `READ COMMITTED`。
- correctness 依赖 row lock、unique constraint、version CAS 和 advisory/lease owner check，不依赖长事务提高 isolation。
- Telegram、模型 API、文件下载和 Redis publish 不能在数据库事务内执行。
- 事务只写 PostgreSQL；跨事务通知通过 outbox。
- 捕获 unique violation 后按业务键读取既有行并对账，不能无条件重试 INSERT。

### 16.2 Update ingest

同一短事务：

1. 插入 `message_events`，重复 fingerprint 则读取既有 event。
2. upsert message identity/tombstone/source projection。
3. create/redact revision 或 media metadata。
4. 对语义变化 `UPDATE conversations SET content_revision = content_revision + 1 ... RETURNING`。
5. refresh pending turn/memory job 和写 outbox。
6. commit。

重复 event 不得再次递增 revision 或刷新 hard deadline。

### 16.3 Turn seal 与 model run

在 conversation row lock 下：

1. 确认 account control version、effective mode、mode_version、content_revision、resume floor、coverage 和 active turn。
2. seal turn membership/revision snapshot。
3. 使用固定 prompt/builder/retrieval/token/capability/embedding snapshots 创建 context manifest/items/reasons/omissions。
4. 提交前重验 selected current revisions、active memory/summary、delete/redact 和 config pointer，创建 model run `queued`。
5. 更新 turn `generating`，写 outbox 后 commit。
6. 事务外发送 read/typing 和调用 model provider。

provider 返回后使用 model run state/generation CAS 提交 succeeded/discarded，不能依赖原进程仍持有内存状态。

### 16.4 Outbound intent 与 Telegram send

在 account control snapshot + conversation row lock/CAS 事务中执行所有发送门禁。完整 normalized output 按 versioned splitter 生成 1..N chunks，并原子创建唯一 outbound delivery group、全部 ordered intents、各自稳定 random ID 和 outbox，然后 commit。不能在发送第一段后再生成后续 intent。

事务外按 chunk ordinal 串行调用 Telegram。首段前执行完整 revision/mode/control gate；首段已有副作用后，新 incoming 进入下一 turn，transient/FloodWait 只恢复未确认 chunk。后续chunk不再要求conversation `content_revision`等于group初始snapshot，因为前序group outgoing和新incoming都是允许的变化；它改为直接重验selected source revisions未edit/delete/redact、没有真人接管且account/mode/maintenance门禁有效。强门禁可以把未发送部分终止为 group `partial_cancelled`，已发送部分不删除或从头重发。

如果 RPC 结果 unknown，不回滚或删除 intent；reconciler 继续使用同一 random ID。

### 16.5 Edit/delete

edit 创建新 revision、更新 message current pointer、递增 conversation revision，并使未发送 turn/run 失效，全部在一个事务完成。

delete 在 ingest 事务先设置 tombstone、redact current/all revisions 的模型可见内容、递增 revision，并把引用这些 revisions 的 memory/summary/embedding 标记为隔离或 invalidation pending，再创建 media/memory reconciliation jobs。物理文件和派生内容清理在事务外，成功后更新对应状态。逻辑不可见不等待文件系统。

### 16.6 Human outgoing

source 确认为 human 的事务同时：

- 更新 message source；
- 递增 conversation mode_version/content_revision；
- 在功能启用时创建/续期 temporary HUMAN deadline；
- 写 mode/history 原因并更新 response coverage；
- cancel active pre-send turn/run/delivery group/intent/COPILOT draft；
- refresh memory job 和 outbox。

如果 source 仍是 `system_pending`，不能提前作为 human 提交上述不可逆状态；reconciler 必须先解析。

### 16.7 COPILOT approval

`control` 先按 Bot update/action token 幂等写 approval command，不直接发送。`app` 在 account snapshot + conversation row lock 下：

1. 锁 draft，确认 `ready`、exact revision、admin、token、30 分钟 deadline。
2. 确认 effective COPILOT、account/mode/content version 和 turn evidence 未变化。
3. 将 action token 标记 used、draft 标记 approved。
4. 创建唯一 `source=copilot_approved` outbound delivery group、全部 ordered intents并写outbox。
5. commit 后由 app 执行 Telegram RPC。

任一步失败整体回滚；已 used token、旧 revision 或 invalidated draft 不能再次批准。RPC unknown 后只运行 group/intent reconciliation。

### 16.8 Memory proposal acceptance

在 account/contact scope lock 下：

1. 锁 proposal、人工 review command（若有）和 sorted target memories。
2. 确认 state、idempotency、input manifest、target version、evidence hash/span/trust/root 均有效且未 redacted。
3. 按 operation 创建/锁 stable memory identity，重新检查 typed semantic key 重复或冲突。
4. 插入 immutable version、proposal targets、evidence 和 relations。
5. 使用 current-version/status CAS 提交 create/update/merge/supersede/invalidate。
6. 标记 proposal/review command accepted/used，保存精确 result version。
7. 创建 embedding job/outbox，必要时推进 contiguous memory watermark。
8. commit。

任一步失败整体回滚；不能留下 active memory 没有 evidence、accepted proposal 没有 result，或 watermark 越过未裁决 hole。provider/Redis/Embedding API 均在事务外调用。

### 16.9 Summary version 与 watermark

在 summary identity/watermark lock 下：

1. 验证 input manifest、source revisions/prior summaries 仍 current 且 hash 匹配。
2. 插入 immutable summary version 和 ordered `summary_version_sources`。
3. 更新 current pointer/status。
4. 使用 version CAS 推进对应 contiguous watermark。
5. 创建 embedding/outbox 后 commit。

summary version、source membership、current pointer 和 watermark 不允许拆成多个事务。source edit/delete 先将依赖 current summary 隔离并使 embedding 不可见，再异步重建。

### 16.10 Embedding space activation

shadow space 在事务外按 `(space, typed target, chunk_index)` 幂等构建。完成 count/dimension/source hash/sample retrieval 和 final delta 校验后，在 deployment/profile activation lock 下单事务把新 space `building -> active`、旧 space `active -> retired`。任一验证失败保持旧 space active；查询不能同时绑定两个 space。

### 16.11 Model config activation

在 profile row lock 下：

1. 确认 draft validated、endpoint 仍满足 policy、role-owned stable credential 当前有 active version。
2. 从 draft 创建 immutable config version。
3. 将旧 active version 改为 retired，新 version 改为 active。
4. 更新 profile active pointer/version。
5. 写 audit/outbox，commit。

进行中的 model run 继续引用旧 config/credential version。API key 独立轮换不创建 config version；销毁 retired credential ciphertext 前必须确认没有需要恢复/重试的非终态 run。

### 16.12 Context/prompt/retrieval policy activation

Control Bot draft在事务外完成schema、权重和fixture validation。激活时按固定顺序锁 logical role/purpose 的 context policy、retrieval policy binding和prompt binding：

1. 验证六层budget basis points总和、limits、token estimator、retrieval weights/half-life/tie-break和prompt source/trust。
2. 创建immutable version或确认待激活version仍为`validated`。
3. 旧active转retired、新version转active，更新deferrable active pointer/version。
4. 写audit/outbox后commit。

已经创建的manifest/run继续引用旧version；新activation不改写旧manifest、不重建在途run，也不自动触发backlog回复。

### 16.13 Context preview confirmation、投递与删除

`control`在短事务中锁定token和request，并同时验证管理员allowlist、Bot identity/chat、purpose、未使用状态、5分钟deadline、manifest/hash/source revision vector和所有source仍未redacted。验证成功后以CAS消费token并把request改为`confirmed`；任一条件失败整体回滚并fail closed。

确认事务后，`control`只通过受限view/function按精确manifest source refs在内存中重建canonical文本，生成确定性plain-text chunks，并在发送前插入不含正文的delivery metadata。每段Bot RPC完成后短事务记录已知message ID；RPC后断连则标记`send_unknown`且不自动重发。所有已知段发送完成后设置request `delivered`和默认10分钟`delete_after`。

删除worker只领取已到期且具有已知message ID的delivery row，调用Bot delete后记录`deleted`或稳定错误码。删除job、outbox、log和audit都不得携带preview正文；没有message ID的unknown段只能告警并保留残余风险。Contact purge/account wipe可把删除deadline提前，但不能从已redacted source重新构建正文。

### 16.14 Job claim

worker 用短事务 `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n` 领取 job，设置随机 owner 和 expiry 后 commit。执行任务不持有行锁；完成时使用 `(job_id, lease_owner, state, version)` CAS。

### 16.15 Proactive materialization、reservation 与 final gate

Occurrence materialization用`(account_id, occurrence_key, generation)`插入冲突对账；candidate seal在一个事务中写candidate、有序membership和membership hash，sealed row不再改成员。Proactive Agent输出提交时锁candidate并校验lease、window、membership、policy和evidence后写唯一decision/selected occurrences。

Preliminary authorization按account bucket、contact bucket、optional bypass bucket、decision的固定顺序加行锁，在同一事务中重新检查capacity、把每个bucket `held_count + 1`、创建唯一reservation和outbox。任何失败整体回滚，不能调用Main AI。

AUTO final gate按account control、conversation、decision/reservation、delivery group顺序加锁。首项副作用前，事务重新验证所有version/evidence/window/activity，原子关联reservation并创建唯一delivery group及ordered intents。Reservation在RPC期间保持held；已确认发送、RPC unknown或partial时把held转committed并按一次计费。明确证明首项副作用前失败时才将所有bucket`held_count - 1`并release。

COPILOT创建draft时reservation保持held；Send/Edit批准再次运行final gate，Ignore/expiry在确认无副作用后release。Reservation reaper必须排除active draft/send lease/send-unknown，不得仅凭TTL释放可能已使用的额度。

### 16.16 Account 与 conversation control

account default/global pause/maintenance 只锁 `account_orchestrator_states`，递增 `control_version`、追加 history/outbox，不批量锁 conversations。旧 run/delivery group/intent 通过 snapshot gate 失效。

conversation override/contact pause/takeover/cancel 锁 conversation，单次动作只递增一次 `mode_version` 并追加 history。resume、takeover expiry和operational block clear同时推进automation resume floor，不创建backlog turn。重复同值command写`no_change`result但不递增版本。

## 17. Alembic migration

### 17.1 基线

- 所有 schema 变更通过 Alembic revision，不允许应用启动时 `create_all()` 修补生产库。
- `migrate` 一次性服务获取全局 PostgreSQL advisory lock 后执行。
- migration 成功前 app/control/worker 不 ready；运行时检查预期 schema revision。
- `pgvector` extension 的创建/版本要求在 migration 中显式检查，不假设镜像外手工完成。

### 17.2 Expand, migrate, contract

兼容性变更采用：

1. expand：添加 nullable column、新表、新 index，不删除旧读路径。
2. migrate：批量 backfill，记录 watermark，支持中断继续。
3. dual read/write：必要时跨一个部署版本验证一致性。
4. contract：确认所有运行实例升级且数据验证通过后再删除旧结构。

大表 `CREATE INDEX CONCURRENTLY` 使用 Alembic autocommit block，不能包在普通 transaction 中。增加 `NOT NULL` 先使用可验证 CHECK/backfill，再转约束，避免长时间锁表。

### 17.3 Migration 编写规则

- revision 名称说明领域和动作，保持单一 head。
- autogenerate 只作为草稿，必须人工检查 constraint、partial index、server default 和 downgrade。
- 每个 foreign key、CHECK、unique 和 index 使用稳定显式名称。
- backfill 不调用 Telegram、模型 provider、Redis 或文件系统。
- data migration 需要 row batch、速率限制、progress watermark 和重复执行安全。
- destructive migration 必须先证明 retention/export/erasure 要求满足，并在变更记录中明确不可逆边界。
- 不提供会恢复已 redacted content、credential ciphertext 或删除列数据的虚假 downgrade。
- downgrade 无法安全实现时显式失败，而不是伪造成功。

### 17.4 Migration 测试

每个 release 至少验证：

```text
empty database -> head
previous supported revision -> head
interrupted backfill -> resume -> head
head schema constraint/index inventory
representative data preservation
deleted/redacted data does not reappear
app/control/worker schema readiness mismatch fails closed
```

## 18. 数据库角色与权限

建议分离：

| Role | 权限 |
|---|---|
| `migrator` | schema DDL，仅 migrate 任务持有 |
| `app_runtime` | message/turn/run/intent/media metadata，以及受限 proactive final-gate/reservation commit DML |
| `control_runtime` | mode、model draft/version、credential ciphertext、context preview metadata DML、受限 exact-manifest reconstruction function、audit INSERT |
| `worker_runtime` | job/memory/summary/embedding/proactive occurrence/candidate/decision/reservation hold 所需 DML；无 outbound side effect 权限 |
| `backup` | 受限一致性备份权限 |
| `maintenance` | erasure/retention，强审计，不供常驻服务使用 |

runtime role 不拥有 schema，不可修改 audit 历史，不可读取其他服务不需要的 secret 列。credential ciphertext 可以通过受限 view/function 返回给授权 role，普通配置查询 view 永不返回 ciphertext、nonce 或 fingerprint。

`control_runtime`不能对canonical content表做任意scan；只有Control Bot应用先完成allowlist、exact request/token和manifest绑定校验后，才能调用只接受manifest ID的重建函数。`/context`使用的聚合view只返回计数、预算、reason、freshness和版本。重建函数不返回image二进制，且数据库权限不能替代应用层二次确认与审计。

## 19. 跨文档与实现边界

Conversation Orchestrator 的 account control、mode overlay、reply floor、temporary takeover 和 COPILOT draft 契约以 `docs/architecture/04-conversation-orchestrator.md` 为准；后续修改必须同时更新本文的字段、约束与 migration。

Memory Pipeline 的 trigger、input manifest、proposal validation、evidence trust、summary source、embedding rebuild、freshness 和人工 review 契约以 `docs/architecture/05-memory-pipeline.md` 为准；后续修改必须同时更新本文的字段、约束与 transaction。

Context Contract 的 manifest item layer、token/image budget、adapter mapping、capability snapshot和delivery group增量以 `docs/architecture/06-context-contract.md` 为准；不能把未记录来源的正文注入模型。

Proactive Pipeline 的occurrence、candidate membership、policy/settings version、budget bucket/reservation和decision契约以`docs/architecture/07-proactive-pipeline.md`为准；发送必须落到`proactive_decisions -> outbound_delivery_groups -> outbound_intents`，并由`app`执行最终门禁。

Operations以`docs/architecture/08-operations.md`固定retention数值、AES-256-GCM keyring、media 20 MiB/40 MP/16384/30秒/10 GiB、pgBackRest/restic、2/4/40资源门禁、migration/restore和credential rotation；Data Model修改必须保持这些row snapshot与erasure语义。

`docs/architecture/09-test-strategy.md`要求使用真实PostgreSQL/pgvector验证本文的约束、角色、race、migration和erasure恢复；SQLite或mock结果不能替代对应证据。

## 20. 验收条件

- [x] account、peer、account peer、contact、conversation 和 message identity 已定义。
- [x] V1 单账号与未来多账号 composite boundary 已定义。
- [x] message event/revision、media、reaction、turn、manifest/reason/omission、run、delivery group、intent、attempt 和 job 表已定义。
- [x] account control、mode overlay、reply floor、operational block、COPILOT draft/revision/action 和 approval intent 已定义。
- [x] Memory identity/version/proposal/target/evidence/relation、input manifest 和 contiguous watermark 已定义。
- [x] Summary version/source membership/watermark 与 embedding shadow-space activation 已定义。
- [x] Memory candidate review extension、action token 和 acceptance transaction 已定义。
- [x] life event、intention、relationship state、proactive policy/setting version、occurrence、candidate membership、budget bucket/reservation、decision、agent/service state 和 audit 已定义。
- [x] 四个独立模型 role、endpoint、draft、config version 和独立 credential version 已定义。
- [x] canonical generation 字段与 protocol-options discriminated JSONB 边界已定义。
- [x] prompt、Context budget和retrieval policy的immutable version与active binding已定义。
- [x] Context budget/version snapshot、跨层选择理由、omission和长输出delivery group已定义。
- [x] metadata-only `/context` 与二次确认 `/context_preview` 的request/token/delivery、TTL、权限、删除和unknown-send边界已定义。
- [x] 主键、业务唯一键、composite foreign key、CHECK 和必要索引已定义。
- [x] UTC 时间、状态类型、JSONB、敏感数据和账号隔离约定已定义。
- [x] ingest、control、turn、send、edit/delete、human outgoing、COPILOT approval、context preview、memory、config 和 job 事务边界已定义。
- [x] memory forget、contact purge、account wipe、export、backup 和 erasure ledger 已定义。
- [x] Alembic expand/migrate/contract 与 migration 验证规则已定义。
- [x] Data Model约束、migration、role与restore erasure均已映射到Test Strategy证据层级。
