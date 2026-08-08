# Data Model

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的 PostgreSQL 逻辑数据模型、实体身份、版本化方式、业务唯一键、外键、检查约束、索引、事务边界、保留与删除语义，以及 Alembic migration 规则。

总体设计见 `docs/Design.md`，运行组件所有权见 `docs/architecture/01-runtime-topology.md`，消息状态机见 `docs/architecture/02-message-lifecycle.md`。后续 Conversation Orchestrator、Memory Pipeline、Context Contract 和 Proactive Pipeline 可以增加字段和受约束的扩展类型，但不得绕过本文定义的账号边界、版本快照、证据链、删除语义和发送幂等约束。

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
  |                        |          +--< model_runs --< model_run_attempts
  |                        |          +--< outbound_intents --< outbound_attempts
  |                        |
  |                        +--< memories --< memory_versions --< memory_evidence
  |                        |                    |
  |                        |                    +--< embedding_records
  |                        +--< summaries --< summary_versions
  |                        +--< memory_jobs
  |
  +--< audit_log
  +--< background_jobs
  +--< data_erasure_requests

model_endpoints --< model_config_versions >-- model_profiles
                                             |
                                             +-- model_credentials --< model_credential_versions

message_events -> message projection / conversation revision
transactional_outbox -> Redis notification relay
life_events / intentions / relationship_states -> proactive_decisions -> outbound_intents
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
| `status` | TEXT | `bootstrap_required/active/paused/disabled/deleting` |
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
| `mode` | TEXT | 当前 `AUTO/HUMAN/COPILOT/PAUSED` |
| `mode_version` | BIGINT | 从 1 开始单调递增 |
| `content_revision` | BIGINT | 从 0 开始，内容语义变化时单调递增 |
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
```

mode 和两个 version 位于同一行，是发送前 row lock/CAS 的事实源。缓存中的 conversation 状态不能覆盖数据库值。

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
| `source` | TEXT | `telegram_user/ai/proactive_ai/human/system_pending/system` |
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
| `retention_class` | TEXT | Operations 定义数值 |
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

### 7.1 `conversation_mode_history`

`conversations.mode/mode_version` 保存当前事实，本表保存不可变变更历史。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `mode_version` | BIGINT | 与更新后的 conversation 一致 |
| `previous_mode` | TEXT | nullable，首次初始化为空 |
| `new_mode` | TEXT | NOT NULL |
| `reason` | TEXT | `control/human_outgoing/global_pause/maintenance/system` |
| `actor_type` | TEXT | `admin/human/system` |
| `actor_ref` | TEXT | nullable internal ref，不存显示名 |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (conversation_id, mode_version)
```

同一事务必须同时更新 `conversations` 并插入 history；不允许只写 history。

### 7.2 `conversation_turns`

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
| `mode_snapshot` | TEXT | seal 时模式 |
| `mode_version_snapshot` | BIGINT | seal 时版本 |
| `content_revision_snapshot` | BIGINT | seal 时 conversation revision |
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

### 7.3 `turn_messages`

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

### 7.4 `context_manifests`

Context Contract 后续定义装配算法，本表先固定可复现快照的 identity。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `turn_id` | UUIDv7 | composite FK turns |
| `builder_version` | TEXT | Context Builder 版本 |
| `prompt_version` | TEXT | system/developer prompt 版本 |
| `input_token_estimate` | INTEGER | nullable nonnegative |
| `image_count` | INTEGER | nonnegative |
| `manifest_sha256` | BYTEA | 对 ordered item manifest 的 hash |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (turn_id, builder_version, manifest_sha256)
```

### 7.5 `context_manifest_items`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | BIGINT IDENTITY | PK |
| `manifest_id` | UUIDv7 | FK context_manifests |
| `ordinal` | INTEGER | 总装配顺序 |
| `layer` | TEXT | `identity/personality/relationship/memory/summary/recent/current` |
| `source_type` | TEXT | typed discriminator |
| `message_revision_id` | UUIDv7 | nullable FK |
| `memory_version_id` | UUIDv7 | nullable FK，延后添加 |
| `summary_version_id` | UUIDv7 | nullable FK，延后添加 |
| `media_object_id` | UUIDv7 | nullable FK validated provider copy |
| `trust_level` | TEXT | `system/trusted_derived/untrusted_user` |
| `token_estimate` | INTEGER | nullable nonnegative |
| `selection_reason_code` | TEXT | 不复制正文 |
| `content_sha256` | BYTEA | 选中内容或序列化 content part hash |

```text
UNIQUE (manifest_id, ordinal)
CHECK typed source foreign key 恰好符合 source_type
```

正文由 version FK 重建，manifest 本身不复制 message/memory text。若 source 后续被 Telegram delete/contact purge 擦除，manifest 仍能解释选择过哪个 ID，但不能恢复已删除正文。

### 7.6 `model_runs`

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
| `config_version_id` | UUIDv7 | 不可变 config snapshot FK |
| `credential_version_id` | UUIDv7 | 同 role credential snapshot FK，不含 secret |
| `context_manifest_id` | UUIDv7 | nullable FK |
| `input_fingerprint` | BYTEA | canonical request 的 keyed fingerprint；purge 时清除 |
| `output_fingerprint` | BYTEA | nullable normalized output keyed fingerprint；purge 时清除 |
| `adapter_version` | TEXT | protocol adapter 实现版本 |
| `request_schema_version` | SMALLINT | canonical request schema |
| `output_schema_version` | SMALLINT | normalized result schema |
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
```

`model_profiles` 提供 `(id, logical_role)` unique key，run 使用 composite FK 保证 role；config version 和 credential version 也分别通过 `(id, profile_id)` composite FK 绑定同一个 `model_profile_id`，数据库会拒绝把其他 role 的配置或 key 用于本次运行。

业务输出由目标表反向引用 `model_run_id`：Main AI 由 outbound intent 引用，Memory Agent 由 proposal 引用，Proactive Agent 由 decision 引用。避免 `model_runs` 使用不可约束的 polymorphic output ID。

### 7.7 `model_run_attempts`

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

### 7.8 `turn_grace_authorizations`

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

### 7.9 `outbound_intents`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `turn_id` | UUIDv7 | nullable composite FK，proactive 也必须有 logical turn/decision ref |
| `model_run_id` | UUIDv7 | NOT NULL composite FK |
| `model_role` | TEXT | 与 source 一致的 `main_ai/proactive_agent` snapshot |
| `proactive_decision_id` | UUIDv7 | nullable FK，延后添加 |
| `source` | TEXT | `ai/proactive_ai` |
| `state` | TEXT | Message Lifecycle intent 状态 |
| `generation_no` | INTEGER | NOT NULL |
| `mode_version_snapshot` | BIGINT | NOT NULL |
| `content_revision_snapshot` | BIGINT | NOT NULL |
| `idempotency_key` | BYTEA | 32-byte stable key |
| `content_text` | TEXT | nullable sensitive retry payload；redaction 时清空 |
| `content_sha256` | BYTEA | nullable 32-byte hash；redaction 时清空 |
| `reply_to_telegram_message_id` | BIGINT | nullable |
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
UNIQUE (turn_id, generation_no, source)
UNIQUE (proactive_decision_id)
  WHERE proactive_decision_id IS NOT NULL
CHECK (octet_length(idempotency_key) = 32)
CHECK (model_run_id IS NOT NULL)
CHECK ((source = 'ai' AND model_role = 'main_ai' AND turn_id IS NOT NULL
        AND proactive_decision_id IS NULL) OR
       (source = 'proactive_ai' AND model_role = 'proactive_agent'
        AND proactive_decision_id IS NOT NULL))
```

`(model_run_id, account_id, conversation_id, model_role)` 使用 composite FK 指向 model run；AI turn 还以包含 `turn_id` 的最宽 FK 保证 run 确属该 turn，不能把 Memory Agent 的输出或其他会话 run 错绑到发送。proactive intent 的 decision FK 同样包含 account/conversation scope。同一 intent 的所有发送重试复用 `telegram_random_id`。content_text 在 intent reconciled 后仍可短期保留用于审计一致性，但 contact purge/account wipe 必须擦除；默认长期正文事实是 reconciled `messages/message_revisions`。

### 7.10 `outbound_attempts`

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
| `status` | TEXT | `candidate/active/superseded/invalidated/forgotten` |
| `current_version_no` | INTEGER | 从 1 开始 |
| `superseded_by_memory_id` | UUIDv7 | nullable self FK |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | 状态/指针更新 |
| `forgotten_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (id, account_id)
CHECK contact_id IS NOT NULL OR conversation_id IS NULL
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
| `valid_from` | TIMESTAMPTZ | nullable |
| `valid_to` | TIMESTAMPTZ | nullable |
| `model_run_id` | UUIDv7 | nullable composite FK |
| `model_role` | TEXT | run 存在时固定为 `memory_agent` |
| `prompt_version` | TEXT | nullable |
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
| `operation` | TEXT | `none/create/update/merge/supersede/invalidate` |
| `target_memory_id` | UUIDv7 | nullable composite FK |
| `payload_schema_version` | SMALLINT | NOT NULL |
| `proposed_payload` | JSONB | typed candidate |
| `proposed_text` | TEXT | sensitive candidate |
| `state` | TEXT | `received/validating/accepted/rejected/candidate/error` |
| `validation_code` | TEXT | nullable |
| `created_at` | TIMESTAMPTZ | DB default |
| `decided_at` | TIMESTAMPTZ | nullable |
| `retention_class` | TEXT | terminal candidate retention snapshot |
| `expires_at` | TIMESTAMPTZ | nullable；到期清除 proposed payload/text |

```text
UNIQUE (account_id, idempotency_key)
CHECK (model_role = 'memory_agent')
```

proposal/version 的 `(model_run_id, account_id, model_role)` 都通过 composite FK 指向 model run。accepted proposal 在同一事务创建 memory/version/evidence 并记录 proposal result。proposal 不能直接更新 active memory row；terminal proposal 的候选正文按 retention policy 清除，正式事实只保留在 memory version。

### 9.4 Evidence 与 relation

`memory_proposal_evidence` 保存模型声明和验证层接受的候选证据：

```text
proposal_id
message_revision_id
evidence_role
quoted_span_start?
quoted_span_end?
created_at
PRIMARY KEY (proposal_id, message_revision_id, evidence_role)
```

`memory_evidence` 绑定正式 `memory_version_id` 与以下来源之一：

```text
message_revision_id
summary_version_id
other_memory_version_id
```

CHECK 要求恰好一个来源列非空。证据保存 source revision ID 和 evidence role，不复制原文。Telegram delete 导致 source revision redacted 时，Memory reconciliation 按 evidence index 找到受影响 versions，立即从 active Context 隔离。若仍有独立有效证据则从剩余证据生成 replacement version；随后对受影响旧 version 的 payload/rendered text 和 embedding 做单向 redaction。没有剩余证据时直接 invalidate 并 redaction。

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
| `range_start_event_id` | BIGINT | inclusive |
| `range_end_event_id` | BIGINT | inclusive，可刷新扩大 |
| `completed_turn_watermark` | UUIDv7 | nullable |
| `idempotency_key` | BYTEA | 32-byte |
| `quiet_until` | TIMESTAMPTZ | 安静窗口 |
| `hard_due_at` | TIMESTAMPTZ | 硬阈值 |
| `background_job_id` | UUIDv7 | nullable FK generic job |
| `created_at` | TIMESTAMPTZ | DB default |
| `updated_at` | TIMESTAMPTZ | CAS |
| `completed_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (account_id, idempotency_key)
CHECK (range_end_event_id >= range_start_event_id)
```

同一 conversation/kind 的 pending job 可以在事务中扩大范围，不创建重复 job；已经 running 的范围不可改写，新事件进入下一 job。

### 9.6 Summary

`summaries` 保存稳定 summary identity：

```text
id, account_id, conversation_id, summary_kind,
status, current_version_no, created_at, updated_at
```

`summary_kind` 至少预留 `rolling/daily/weekly/consolidated`。`summary_versions` 保存：

```text
id, account_id, summary_id, version_no,
range_start_event_id, range_end_event_id,
content_text, content_sha256,
model_run_id, model_role='memory_agent', prompt_version,
created_at, redacted_at
```

唯一键 `(summary_id, version_no)`。summary 版本不可原地改写。源消息删除后，受影响 summary 先退出 active Context，再从未删除范围创建 replacement/invalidation version；旧 summary content 和 embedding 随后单向 redaction。contact purge 时所有正文物理清除。

`summary_watermarks` 使用：

```text
account_id, conversation_id, summary_kind,
last_included_event_id, last_included_message_id?,
last_summary_version_id, version, updated_at
```

主键 `(conversation_id, summary_kind)`，更新使用 version CAS。

### 9.7 Embedding space

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

`embedding_spaces` 提供 `(id, dimensions)` unique key，record 使用 composite FK 保证声明维度一致。pgvector ANN index 必须绑定固定 dimension/active space；具体 HNSW/IVFFlat 选择和查询参数留给 Context Contract/Operations，不建立跨 space 的无约束全局 ANN index。

## 10. Proactive 与关系投影

### 10.1 `life_events`

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

### 10.2 `intentions`

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

### 10.3 Relationship state

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

### 10.4 `proactive_decisions`

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `account_id` | UUIDv7 | FK accounts |
| `contact_id` | UUIDv7 | composite FK contacts |
| `conversation_id` | UUIDv7 | composite FK conversations |
| `idempotency_key` | BYTEA | 32-byte，候选/预算窗口稳定键 |
| `state` | TEXT | `candidate/rejected/approved/generating/send_ready/sent/skipped/failed` |
| `decision_code` | TEXT | 受控原因，不存完整 chain-of-thought |
| `rule_snapshot_schema_version` | SMALLINT | NOT NULL |
| `rule_snapshot` | JSONB | 不含证据 ID 的规则数值与结果 |
| `relationship_state_version` | BIGINT | snapshot |
| `mode_version_snapshot` | BIGINT | snapshot |
| `budget_version` | BIGINT | snapshot |
| `model_run_id` | UUIDv7 | nullable composite FK |
| `model_role` | TEXT | run 存在时固定为 `proactive_agent` |
| `scheduled_for` | TIMESTAMPTZ | nullable |
| `created_at` | TIMESTAMPTZ | DB default |
| `decided_at` | TIMESTAMPTZ | nullable |

```text
UNIQUE (account_id, idempotency_key)
UNIQUE (id, account_id, conversation_id)
CHECK ((model_run_id IS NULL AND model_role IS NULL) OR
       (model_run_id IS NOT NULL AND model_role = 'proactive_agent'))
```

decision 的 `(model_run_id, account_id, model_role)` 使用 composite FK 指向 model run。最终发送仍通过带唯一 `proactive_decision_id` 的 outbound intent，不允许 Proactive worker 直接产生 Telegram 副作用。只在 outbound 一侧保存关系，避免双向 nullable FK 产生不一致指针。

`proactive_decision_evidence` 保存可约束来源：

```text
decision_id
account_id
ordinal
evidence_type
memory_version_id?
life_event_id?
intention_id?
message_revision_id?
rule_code?
PRIMARY KEY (decision_id, ordinal)
CHECK typed evidence FK 恰好一个非空，或 evidence_type = rule 且只有 rule_code
```

证据 ID 不放入 `rule_snapshot`，contact purge 可以通过 join index 找到并清理所有派生 decision。

## 11. 模型 endpoint、profile 与 credential

### 11.1 `model_endpoints`

endpoint 可被多个 profile 复用，但一旦被不可变 config version 引用，其安全关键字段不能原地修改；修改创建新 endpoint row。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `label` | TEXT | 管理员标签 |
| `base_url` | TEXT | 规范化 URL，经过 SSRF policy 验证 |
| `auth_scheme` | TEXT | `bearer/x_api_key/custom_supported` |
| `network_policy_id` | UUIDv7 | server-managed allowlist policy ref |
| `created_at` | TIMESTAMPTZ | DB default |
| `retired_at` | TIMESTAMPTZ | nullable |

base URL unique 不是强制，因为相同 URL 可以有不同 auth/network policy；使用 canonical hash 发现重复但不自动合并。

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
| `active_config_version_id` | UUIDv7 | nullable deferrable FK |
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
admin_user_id BIGINT
control_session_id UUIDv7
state editing|validating|validated|cancelled|expired|activated
endpoint_id?
protocol
model_name
temperature?
max_output_tokens?
timeout_seconds
enabled
protocol_options_schema_version
protocol_options JSONB
draft_version BIGINT
created_at
updated_at
expires_at
```

同一 profile 可以有多个管理员历史 draft，但 partial unique index 限制同一 admin/profile 同时一个 `editing/validating` draft。draft 使用 `draft_version` optimistic lock。

### 11.4 `model_config_versions`

validated draft 复制成不可变版本；不能把 draft row 直接改名为 active version。

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `profile_id` | UUIDv7 | FK profiles |
| `profile_kind` | TEXT | `generation/embedding`，与 profile composite FK 一致 |
| `version_no` | INTEGER | role 内递增 |
| `status` | TEXT | `validated/active/retired` |
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
| `capabilities_schema_version` | SMALLINT | NOT NULL |
| `capabilities` | JSONB | 验证后的能力 snapshot |
| `validated_at` | TIMESTAMPTZ | NOT NULL |
| `activated_at` | TIMESTAMPTZ | nullable |
| `retired_at` | TIMESTAMPTZ | nullable |
| `created_at` | TIMESTAMPTZ | DB default |

```text
UNIQUE (profile_id, version_no)
UNIQUE (profile_id, id)
UNIQUE (profile_id) WHERE status = 'active'
CHECK profile_kind 与 generation/embedding 字段组合一致
```

`model_profiles` 提供 `(id, profile_kind)` unique key，config version 使用 `(profile_id, profile_kind)` composite FK；因此字段组合 CHECK 不需要跨表读取，PostgreSQL 可以直接执行。config payload 字段在创建后不可修改，只有 `status/activated_at/retired_at` lifecycle envelope 可按受控状态机更新。

`model_profiles(id, active_config_version_id)` 使用 deferrable composite FK 指向 `model_config_versions(profile_id, id)`，并与 partial unique active row 在同一 activation transaction 保持一致。config version 引用稳定 credential identity，不锁死某次 key 轮换；每个 `model_run` 在开始时另行记录实际使用的 `credential_version_id`。

### 11.5 Credential

`model_credentials` 是 role-owned 稳定 identity：

```text
id UUIDv7 PK
profile_id UNIQUE
status configured|missing|disabled|deleting
active_version_no?
created_at
updated_at
```

`model_credential_versions` 保存独立轮换版本：

| 字段 | 类型 | 约束与语义 |
|---|---|---|
| `id` | UUIDv7 | PK |
| `credential_id` | UUIDv7 | FK credentials |
| `profile_id` | UUIDv7 | 冗余 composite scope |
| `version_no` | INTEGER | 从 1 递增 |
| `ciphertext` | BYTEA | 应用层密文 |
| `nonce` | BYTEA | 加密参数 |
| `key_version` | INTEGER | credential master key version |
| `secret_fingerprint` | BYTEA | keyed fingerprint，只用于判断是否变化 |
| `status` | TEXT | `active/retired/destroyed` |
| `created_at` | TIMESTAMPTZ | DB default |
| `retired_at` | TIMESTAMPTZ | nullable |
| `destroyed_at` | TIMESTAMPTZ | nullable，destroyed 时 ciphertext、nonce、fingerprint 全部清空 |

```text
UNIQUE (credential_id, version_no)
UNIQUE (id, profile_id)
UNIQUE (profile_id) WHERE status = 'active'
```

credential 不跨 profile 共享。即使管理员输入相同 key，也创建独立 ciphertext/version，轮换一个角色不会隐式改变另一个角色。

key-only Web App 设置或替换 API key 时，在 credential row lock 下创建新 version、切换 `active_version_no` 和 active status，并发布 config invalidation。它不创建新的非密钥 config version。已经开始的 model run 继续记录并使用启动时取得的旧 credential version；旧 ciphertext 只有在没有引用它的非终态 run、重试或恢复窗口后才能进入 destroyed 状态。

`model_credentials` 提供 `(id, profile_id)` unique key；`model_config_versions(profile_id, credential_id)` 使用 composite FK，保证 config 不能引用其他 role 的 credential。`model_credential_versions` 同样以 `(credential_id, profile_id)` 约束 owner。`model_credentials(id, active_version_no)` 使用 deferrable composite FK 指向 `model_credential_versions(credential_id, version_no)`。

### 11.6 `control_input_sessions`

持久化非密钥多步 Bot 配置会话的最小状态：

```text
id UUIDv7 PK
admin_user_id BIGINT
session_kind
target_profile_id?
state active|confirmed|cancelled|expired
step
draft_id?
created_at
expires_at
completed_at?
```

不保存 API key 或可能是 key 的拒绝输入。key-only Web App 的短期服务端 session 可以使用独立表，记录 admin、role、nonce hash、expires 和 used_at，不保存提交明文。

### 11.7 Canonical generation 与 protocol options

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

它们由 prompt version、context manifest、业务 purpose 和 adapter version 共同确定；`model_runs` 保存这些版本及 canonical input keyed fingerprint。需要保留的业务输出写入 outbound intent、memory proposal 或 proactive decision，model run 不保存完整 raw body。

`protocol_options` 使用 `protocol` 作为 discriminator：

| Protocol | schema | 允许内容 |
|---|---|---|
| `openai_responses` | `ResponsesOptions@N` | 经过 allowlist 的 Responses 特有参数 |
| `openai_chat_completions` | `ChatCompletionsOptions@N` | token limit field compatibility 等受控参数 |
| `anthropic_messages` | `MessagesOptions@N` | API version、认证/header/path policy 等受控参数 |
| `embedding` | `EmbeddingOptions@N` | dimensions、encoding、batch policy 等受控参数 |

`openai_chat_completions` 只表示 Chat Completions，不支持旧式纯文本 `/completions`。任意请求 JSON、任意 header、完整 URL override 和 secret 都不能进入该 JSONB。切换 protocol 必须重新验证 schema 和 endpoint policy，并创建新 config version。

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

global pause、maintenance mode、预算代次等安全关键值应提升为 typed column 或独立表；不得只依赖任意 JSONB。`agent_state_history` 保存 versioned change metadata。

### 12.2 服务状态

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

### 12.3 `audit_log`

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

具体天数在 Operations 文档确定。V1 语义基线：

| 数据 | 默认语义 |
|---|---|
| canonical message/revision | 不自动过期，直到 Telegram delete/contact purge/account wipe |
| active memory/summary | 不按年龄简单删除，由 Memory 生命周期或显式 forget 管理 |
| validated media | 有界 retention，由 Operations 设置；过期后不得继续作为模型输入 |
| read/typing/service transient event | 短期 retention |
| debug raw capture | 强制短期 TTL，默认功能关闭 |
| model/outbound attempts | terminal 后有限 retention，保留错误码和 usage，不保留 raw body |
| audit | 有界长期 retention，不能复制 sensitive content |
| credential retired version | 受控轮换后销毁 ciphertext，保留非秘密版本审计 |

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
2. 在同一逻辑边界让 messages、media、memories、summaries、embeddings、proactive candidates 不再可见。
3. 擦除所有 message revisions、outbound payload、memory/summary versions、debug capture 和关系派生正文。
4. 删除 `media-data` 物理对象并确认结果。
5. 删除或匿名化可删除的 operational rows；保留最小 tombstone、erasure ledger 和不含正文的法定/安全 audit。
6. 完成后 conversation/contact 标记 deleted，写 completion audit。

#### Account wipe

覆盖该账号全部联系人和业务数据，并额外：

- 停止 `app`、worker side effects 和配置激活；
- 保留部署级 model profile/credential 配置；account wipe 不等于 deployment credential destruction。若要退役整个部署，必须另建显式 credential destruction 操作；
- 删除 media、embedding、jobs、cache 和 outbox pending payload；
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

导出产物必须加密、短期保存并由一次性授权下载；具体交付流程由 Operations 定义。

### 13.6 Backup 与 restore 边界

- PostgreSQL backup 必须整体加密，密钥与 backup 分开。
- 因敏感正文在数据库中为明文，未加密 SQL dump 不得落入普通磁盘、日志或 CI artifact。
- credential master key、diagnostic key、Telethon Session 和数据库 backup 分别管理；任一单独备份不能自动恢复全部权限。
- restore 后所有 model credential 解密、account ownership、schema revision、outbound unknown intent 和 erasure ledger 检查通过前，自动发送 fail closed。
- backup retention 不得成为绕过 contact purge/account wipe 的长期影子存储；Operations 必须定义过期和 restore 后重放 erasure 的流程。

## 14. 外键与删除策略

### 14.1 默认策略

- 核心事实和版本表：`ON DELETE RESTRICT`。
- 纯 join table，如 `turn_messages`、proposal evidence：父实体由显式 purge 删除时可以 `ON DELETE CASCADE`。
- current-version 指针使用 `DEFERRABLE INITIALLY DEFERRED`，允许同一事务创建 version 并更新 parent。
- `SET NULL` 只用于保留 non-sensitive historical attribution，例如已删除 worker instance ref；不能让 evidence 静默失去来源。
- composite FK 必须包含 `account_id`，防止跨账号引用。

### 14.2 One-way redaction

message revision、debug capture、model run content fingerprint、outbound payload、proposal payload、memory/summary version 和 credential version 的 redaction/destroy 只允许：

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
conversation_turns(conversation_id, collection_sequence) UNIQUE
conversation_turns(conversation_id, state, quiet_deadline_at)
model_runs(turn_id, logical_role, generation_no) UNIQUE
model_runs(state, started_at)
  WHERE state IN ('queued','running','cancel_requested','failed_retryable')
outbound_intents(account_id, idempotency_key) UNIQUE
outbound_intents(account_id, telegram_random_id) UNIQUE
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
```

### 15.4 Memory 与 proactive

```text
memories(account_id, contact_id, memory_type, status)
memory_versions(memory_id, version_no) UNIQUE
memory_evidence(message_revision_id) WHERE message_revision_id IS NOT NULL
memory_jobs(conversation_id, state, quiet_until)
summary_watermarks(conversation_id, summary_kind) PRIMARY KEY
life_events(contact_id, status, start_at)
intentions(contact_id, status, expected_at)
proactive_decisions(account_id, idempotency_key) UNIQUE
```

### 15.5 JSONB、全文和向量

- 不默认对所有 JSONB 建 GIN index；只有后续文档证明存在稳定查询才添加表达式或受限 GIN index。
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

1. 确认 mode、mode_version、content_revision 和 active turn。
2. seal turn membership/revision snapshot。
3. 创建 context manifest 和 model run `queued`。
4. 更新 turn `generating`，写 outbox 后 commit。
5. 事务外发送 read/typing 和调用 model provider。

provider 返回后使用 model run state/generation CAS 提交 succeeded/discarded，不能依赖原进程仍持有内存状态。

### 16.4 Outbound intent 与 Telegram send

在 conversation row lock/CAS 事务中执行所有发送门禁，创建唯一 outbound intent 并 commit。事务外调用 Telegram。RPC 返回或 listener update 在新事务按 random ID/message ID 对账。

如果 RPC 结果 unknown，不回滚或删除 intent；reconciler 继续使用同一 random ID。

### 16.5 Edit/delete

edit 创建新 revision、更新 message current pointer、递增 conversation revision，并使未发送 turn/run 失效，全部在一个事务完成。

delete 在 ingest 事务先设置 tombstone、redact current/all revisions 的模型可见内容、递增 revision，并把引用这些 revisions 的 memory/summary/embedding 标记为隔离或 invalidation pending，再创建 media/memory reconciliation jobs。物理文件和派生内容清理在事务外，成功后更新对应状态。逻辑不可见不等待文件系统。

### 16.6 Human outgoing

source 确认为 human 的事务同时：

- 更新 message source；
- 递增 conversation mode_version/content_revision；
- 写 mode/history 原因；
- cancel active pre-send turn/run/intent；
- refresh memory job 和 outbox。

如果 source 仍是 `system_pending`，不能提前作为 human 提交上述不可逆状态；reconciler 必须先解析。

### 16.7 Memory proposal acceptance

在 account/contact scope lock 下：

1. 锁 proposal 并确认 state、idempotency、evidence 均有效且未 redacted。
2. 创建或锁 stable memory identity。
3. 插入 immutable version 和 evidence/relation。
4. 更新 current pointer/status。
5. 标记 proposal accepted，创建 embedding job/outbox。
6. commit。

任一步失败整体回滚；不能留下 active memory 没有 evidence。

### 16.8 Model config activation

在 profile row lock 下：

1. 确认 draft validated、endpoint 仍满足 policy、role-owned stable credential 当前有 active version。
2. 从 draft 创建 immutable config version。
3. 将旧 active version 改为 retired，新 version 改为 active。
4. 更新 profile active pointer/version。
5. 写 audit/outbox，commit。

进行中的 model run 继续引用旧 config/credential version。API key 独立轮换不创建 config version；销毁 retired credential ciphertext 前必须确认没有需要恢复/重试的非终态 run。

### 16.9 Job claim

worker 用短事务 `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n` 领取 job，设置随机 owner 和 expiry 后 commit。执行任务不持有行锁；完成时使用 `(job_id, lease_owner, state, version)` CAS。

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
| `app_runtime` | message/turn/run/intent/media metadata 所需 DML |
| `control_runtime` | mode、model draft/version、credential ciphertext、audit INSERT |
| `worker_runtime` | job/memory/summary/embedding/proactive 所需 DML |
| `backup` | 受限一致性备份权限 |
| `maintenance` | erasure/retention，强审计，不供常驻服务使用 |

runtime role 不拥有 schema，不可修改 audit 历史，不可读取其他服务不需要的 secret 列。credential ciphertext 可以通过受限 view/function 返回给授权 role，普通配置查询 view 永不返回 ciphertext、nonce 或 fingerprint。

## 19. 后续文档边界

Conversation Orchestrator 可以扩展 mode state、temporary takeover 和 COPILOT draft 表，但必须使用 `mode_version`、turn snapshot 和 outbound gate。

Memory Pipeline 可以扩展 memory typed schemas、proposal validation 和 consolidation 状态，但必须使用 immutable version、evidence 和 watermark。

Context Contract 可以扩展 manifest item layer、token/image budget 和 adapter mapping，但不能把未记录来源的正文注入模型。

Proactive Pipeline 可以扩展 candidate/budget/quiet-hour tables，但发送必须落到 `proactive_decisions -> outbound_intents`。

Operations 负责确定 retention 数值、磁盘/备份加密、media 配额、数据库参数、backup/restore runbook 和 credential key rotation。

Test Strategy 负责把本文的约束、race、migration 和 erasure 恢复声明实现为自动化测试。

## 20. 验收条件

- [x] account、peer、account peer、contact、conversation 和 message identity 已定义。
- [x] V1 单账号与未来多账号 composite boundary 已定义。
- [x] message event/revision、media、reaction、turn、manifest、run、intent、attempt 和 job 表已定义。
- [x] Memory identity/version/proposal/evidence/relation、summary/watermark 和 embedding space 已定义。
- [x] life event、intention、relationship state、proactive decision、agent/service state 和 audit 已定义。
- [x] 四个独立模型 role、endpoint、draft、config version 和独立 credential version 已定义。
- [x] canonical generation 字段与 protocol-options discriminated JSONB 边界已定义。
- [x] 主键、业务唯一键、composite foreign key、CHECK 和必要索引已定义。
- [x] UTC 时间、状态类型、JSONB、敏感数据和账号隔离约定已定义。
- [x] ingest、turn、send、edit/delete、human outgoing、memory、config 和 job 事务边界已定义。
- [x] memory forget、contact purge、account wipe、export、backup 和 erasure ledger 已定义。
- [x] Alembic expand/migrate/contract 与 migration 验证规则已定义。
