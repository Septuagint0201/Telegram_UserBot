# Conversation Orchestrator

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的 Conversation Orchestrator：基础模式、覆盖门禁、运行阻塞、Control Bot 命令、turn 创建、真人接管、COPILOT 草稿、恢复语义、版本门禁、并发边界、故障恢复与审计契约。

总体设计见 `docs/Design.md`；消息事件、发送和 3 秒 supersede 状态机见 `docs/architecture/02-message-lifecycle.md`；持久化约束见 `docs/architecture/03-data-model.md`；Memory extraction 与来源信任见 `docs/architecture/05-memory-pipeline.md`。本文不重新定义 Telegram update 幂等、模型 adapter、Memory extraction 或 Proactive 候选算法。

当前状态：V1 架构基线。

## 2. 已确认决策

| 主题 | V1 决策 |
|---|---|
| 模式表示 | `AUTO/HUMAN/COPILOT` 是基础模式；pause、maintenance、temporary HUMAN 是覆盖层 |
| 默认与 override | account default base mode + nullable conversation override |
| 优先级 | maintenance > global pause > contact pause > temporary HUMAN > contact override > account default |
| 全局版本 | account 级 `control_version`；不批量改写所有 conversation |
| 恢复 AUTO | 默认不回复非自动期间积压消息；显式 `/reply_pending` 才补一次 |
| 未来能力 | “恢复时询问是否补回复”只保留为未来可选 policy，V1 不启用 |
| 故障 | 保留基础模式，另设 `BLOCKED(reason)` operational state；恢复不补发 |
| COPILOT 投递 | Control Bot 草稿卡片；发送、修改、忽略均需管理员显式动作 |
| COPILOT 来源 | 经批准发送的消息使用 `source=copilot_approved`，不伪装成纯真人输入 |
| 响应式草稿 | 只在管理员执行 `/draft <contact>` 时生成，不因 incoming 自动生成 |
| 草稿期限 | content/mode/control snapshot 变化或 30 分钟到期，以先发生者为准 |
| proactive | AUTO 自动发送；COPILOT 只产生待审批草稿；HUMAN/PAUSED 不生成或发送 |
| temporary HUMAN | 功能默认关闭；启用后为 10 分钟 inactivity window，真人 outgoing 续期 |

## 3. Orchestrator 的职责与边界

Orchestrator 是决定“现在允许系统做什么”的唯一业务层，不直接持有 Telethon Session，也不自行实现模型或 Telegram RPC。

它负责：

- 解析 account、contact、conversation 的基础模式和覆盖门禁；
- 根据 incoming、真人 outgoing、Control Bot command、timer 和 dependency state 决定状态转换；
- 创建、seal、supersede 或取消 conversation turn；
- 为 model run、COPILOT draft、proactive decision 和 outbound intent 生成不可变控制快照；
- 在每个副作用边界重新执行发送门禁；
- 维护“不自动补回复”的 eligibility floor 和已回复 coverage；
- 产生结构化 audit、reason code 和 outbox 通知。

它不负责：

- 解析未经归一化的 Telegram update；
- 直接发送 Telegram 消息、read acknowledgement 或 typing；
- 决定 Memory proposal 内容；
- 决定 Proactive candidate 是否有业务价值；
- 修改模型 endpoint、credential 或 protocol；
- 把 Redis lease 当作正确性事实源。

进程所有权保持不变：`app` 执行实时 conversation coordination 和最终 Telegram user-account side effect；`control` 验证管理员并写入控制命令；`worker` 可以准备 Memory、Proactive candidate 和模型工作，但不能绕过 Orchestrator 创建 Telegram 副作用。

## 4. 状态词汇

### 4.1 Base mode

基础模式表达管理员希望 conversation 在门禁开放时采用的长期行为：

| Base mode | incoming 保存 | 响应式生成 | 自动发送 | user-chat read/typing |
|---|---:|---:|---:|---:|
| `AUTO` | 是 | 自动 | 是 | 生成开始时是 |
| `HUMAN` | 是 | 否 | 否 | 否 |
| `COPILOT` | 是 | 仅 `/draft` | 否，需批准 | 否 |

`PAUSED` 不是覆盖原基础模式的第四个长期值。它是 account 或 conversation 级 pause overlay；清除 overlay 后恢复原 base mode。

### 4.2 Gate overlay

gate overlay 是确定性的安全覆盖：

```text
maintenance
global_pause
contact_pause
temporary_human
contact_policy
```

前三项禁止新的 conversation/proactive model run 和发送；temporary HUMAN 将有效模式临时降为 HUMAN；contact policy 的 `blocked/review/deleting` 可以进一步拒绝自动化。

pause 不停止 canonical message ingest、必要 source reconciliation、delete redaction 或 Memory job 刷新。若 PostgreSQL 不可用，系统无法安全摄取时应由 Runtime readiness fail closed，而不是假装处于可工作的 PAUSED。

### 4.3 Operational state

operational state 不表达管理员意图：

```text
READY
BLOCKED(reason_code, scope, retry_after?)
```

典型 block：

- 缺少 active Main AI config 或 credential；
- provider circuit open 或 endpoint policy 失败；
- 当前 turn 包含无法验证或模型不支持的图片；
- schema revision 不兼容；
- erasure、contact deleting 或恢复后安全检查尚未完成。

block 可以属于 account、model profile、conversation 或 turn。turn-local unsupported media 不应把整个 contact 永久改成 PAUSED；account/model 级 block 必须持久化 version 或由 durable service state 推导，不能只存在于某个 worker 内存。

### 4.4 Effective result

一次决策返回结构化结果，而不是只返回一个字符串：

```text
base_mode
base_source = account_default | conversation_override
effective_mode = AUTO | HUMAN | COPILOT | PAUSED
pause_reason?
operational_state = READY | BLOCKED
block_reason?
account_control_version
mode_version
content_revision
automation_resume_floor_event_id
last_response_covered_event_id
```

日志、`/status` 和 audit 使用同一 resolver，不允许各模块分别猜测 effective mode。

## 5. 模式解析优先级

### 5.1 解析算法

在一个一致的数据库 snapshot 中按以下顺序解析：

```text
1. account/contact 是否 active 且允许 automation
2. maintenance 是否 active
3. global pause 是否 active
4. contact pause 是否 active
5. temporary HUMAN 是否尚未过期
6. conversation override 是否存在
7. account default base mode
8. 当前动作所需依赖是否 READY
```

伪代码：

```text
if account/contact disallows action:
    deny(policy_reason)
elif maintenance_active:
    effective = PAUSED(maintenance)
elif global_paused:
    effective = PAUSED(global_pause)
elif contact_paused:
    effective = PAUSED(contact_pause)
elif temporary_human_until > now:
    effective = HUMAN(temporary_takeover)
else:
    effective = conversation_override ?? account_default

if requested_action requires unavailable dependency:
    operational = BLOCKED(reason)
else:
    operational = READY
```

`BLOCKED` 不覆盖展示的 base/effective mode；例如状态可以是 `base=AUTO, effective=AUTO, operational=BLOCKED(model_unavailable)`。这样依赖恢复不会隐式执行模式迁移。

### 5.2 Snapshot 一致性

resolver 的输入至少包括：

```text
account orchestrator row
conversation row
contact automation policy
active temporary takeover
applicable operational blocks
current turn/run/intent state
```

创建 model run 或 intent 时必须在 transaction/row lock 内读取，不能把几次无锁查询拼成一个门禁结果。Redis cache 只能缩短正常路径；cache miss、过期或矛盾时以 PostgreSQL 为准。

## 6. 持久化控制状态

### 6.1 Account orchestrator state

每个 account 一行 typed state：

```text
account_id PK
default_base_mode AUTO|HUMAN|COPILOT
global_paused boolean
maintenance_state inactive|draining|active
temporary_takeover_enabled boolean default false
temporary_takeover_seconds integer default 600
resume_pending_policy ignore
control_version bigint >= 1
updated_at
updated_by
```

V1 的 `resume_pending_policy` 只允许 `ignore`。未来可通过 migration 增加 `ask`，但不能提前实现隐藏提示、自动 Bot 询问或等待人工确认的半成品状态。

以下实际变化递增 `control_version`：

- account default base mode 改变；
- global pause 开启或关闭；
- maintenance state 改变；
- temporary takeover policy 改变；
- account/model-profile 级 operational block 的安全版本变化。

重复提交相同值是 idempotent no-op：记录 `result=no_change` audit，但不递增版本。若管理员只想取消当前 generation，使用显式 `/cancel <contact>`。

### 6.2 Conversation control state

conversation 保存：

```text
base_mode_override AUTO|HUMAN|COPILOT|null
contact_paused boolean
temporary_human_until timestamptz|null
mode_version bigint >= 1
content_revision bigint >= 0
automation_resume_floor_event_id bigint|null
last_response_covered_event_id bigint|null
```

以下变化递增 `mode_version`：

- 设置、修改或清除 base mode override；
- contact pause/resume；
- temporary takeover start、renew 或 expiry；
- 已确认真人 outgoing；
- 显式 cancel/reset；
- contact policy 变化导致旧生成必须失效。

一次业务动作只递增一次。例如 human outgoing 同时刷新 temporary takeover 时，在同一事务将 `mode_version + 1`，不能连续加两次。

### 6.3 History 与 command identity

account 和 conversation 控制 history 均为 append-only metadata。每个 Control Bot command 生成 `command_id` 和稳定 idempotency key；callback、重试或 Bot update 重放必须命中同一 command result。

history 至少记录：

```text
scope
previous requested state
new requested state
previous/new version
effective result reason
actor admin/system/human
command_id or source message event
occurred_at
result changed|no_change|rejected
```

不把联系人消息正文、草稿正文、Prompt 或 API key 放入 control history。

## 7. Control Bot 命令契约

### 7.1 基础模式命令

```text
/ai [contact]
/human [contact]
/copilot [contact]
/mode_inherit <contact>
```

不带 contact 的前三个命令修改 account default，只影响没有 conversation override 的 effective base mode；它们不批量清除 override。带 contact 时设置该 conversation override。`/mode_inherit` 清除 override，使 conversation 重新继承 account default。

任何命令使一组 conversation 从非 AUTO 变为 AUTO 时，都同时把适用的 account/conversation resume floor 推进到命令事务看到的最新 committed event watermark。unscoped `/ai` 使用 account-level lazy floor，不执行全表更新；因此模式切换本身不会回复旧 backlog。

命令成功响应必须显示：

```text
scope
base mode and source
effective mode
active overlays/block
control_version/mode_version
whether active work was cancelled
pending messages were not auto-replied
```

### 7.2 Pause 与 resume

```text
/pause
/resume
/pause <contact>
/resume <contact>
```

无 contact 操作 global pause；带 contact 操作 contact pause overlay。resume 只清除对应 overlay，不修改 account default 或 conversation override。

进入 pause：

1. 在控制 row lock 下更新 overlay/version/history。
2. 使所有尚未进入不可判定 Telegram RPC 的相关 run/intent 失效。
3. 发布 invalidation outbox。
4. 已经 `sending/unknown` 的 intent 进入 reconciliation，不声明取消成功。

退出 pause 默认设置相关 conversation 的 automation resume floor 为当时最新 event watermark；因此 pause 期间 incoming 不会突然触发旧消息回复。global resume 不做无界全表同步更新；floor 在 conversation 下次被访问时以 account resume watermark 惰性物化，resolver 必须能比较 account floor 和 conversation floor。

### 7.3 Conversation work 命令

```text
/draft <contact>
/reply_pending <contact>
/cancel <contact>
/status [contact]
```

- `/draft` 只在 effective COPILOT、operational READY 且没有 active draft 时创建响应式草稿 request。
- `/reply_pending` 只在 effective AUTO 且所有发送门禁开放时，从最近未覆盖片段显式创建一个 AUTO turn。
- `/cancel` 递增 conversation mode version，取消 pre-send work，但不改变 base mode。
- `/status` 是只读命令，不能延长 takeover、改变 floor 或取得长期 conversation lease。

任何命令都不能通过 contact display name 作为最终身份键。Control Bot 可以让管理员搜索/选择联系人，但 callback payload 使用短期 opaque token，服务端解析为 account/contact/conversation ID。

### 7.4 授权与确认

只有 allowlist 管理员可以执行。所有写命令：

- 绑定 Bot chat、admin Telegram user ID 和 command nonce；
- 对高影响 global/maintenance 操作显示确认；
- callback token 单次使用且短期过期；
- 在 transaction 中再次验证 actor、scope 和 expected version；
- 不把 contact private content 放入 callback data。

## 8. 事件到动作总表

| 事件 | AUTO | HUMAN | COPILOT | PAUSED/BLOCKED |
|---|---|---|---|---|
| eligible incoming | debounce 并创建 turn | 只保存/Memory | 只保存，等待 `/draft` | 只保存；必要流程继续 |
| incoming edit/delete | invalidate pre-send work | 保存/reconcile | invalidate active draft | 保存/reconcile |
| confirmed human outgoing | invalidate run，刷新 takeover | 保存/Memory | invalidate draft | 保存/Memory |
| `/draft` | reject wrong mode | reject wrong mode | 创建一个 draft request | reject with reason |
| `/reply_pending` | 显式创建一次 turn | reject | reject | reject |
| proactive candidate | 可继续自动发送路径 | skip | 生成待审批 draft | skip |
| mode/control version change | cancel/discard stale work | 更新 effective state | invalidate draft | 保持 fail closed |
| dependency unavailable | 当前动作 BLOCKED | 不影响人工聊天 | draft request BLOCKED | 保持 blocked |

所有模式都继续摄取受支持的 canonical message，并按 Memory Pipeline 的独立策略刷新任务。模式控制不等于删除、forget 或停止数据一致性处理。

## 9. Incoming 与 reply coverage

### 9.1 Eligible incoming

Orchestrator 只对 Message Lifecycle 标记为可触发且满足以下条件的 incoming 考虑响应：

```text
supported one-to-one private non-Bot contact
canonical projection committed
not deleted/tombstoned
source resolved
text/caption or validated supported image
event newer than applicable automation resume floor
not already covered by a completed/approved response
```

reaction、service message、无 caption 的 metadata-only media 和被 policy 忽略的 peer 不成为 response coverage 起点。

### 9.2 两个 watermark

`automation_resume_floor_event_id` 表达“在此 event 及以前不得因为恢复自动化而自行补回复”。`last_response_covered_event_id` 表达“系统或真人已明确回应到哪个输入范围”。二者含义不同，不能合并成一个 `last_seen` 字段。

floor 在以下时机推进到当前 committed event watermark：

- 从 HUMAN/COPILOT/contact PAUSED 回到 AUTO；
- global pause 或 maintenance 解除；
- temporary HUMAN 到期；
- account/conversation operational block 恢复；
- contact 从不允许 automation 变为 allowed。

推进 floor 不把消息标为 replied，也不删除它们。Memory、summary 和 `/draft` 仍可以使用允许的 canonical history。

coverage 在以下时机推进：

- AUTO outbound intent 成功 reconciled；
- `copilot_approved` outbound intent 成功 reconciled；
- confirmed human outgoing 被明确关联为对当前 pending segment 的回应；
- 管理员显式执行“忽略 pending”操作；V1 不提供默认命令，可由未来 UX 增加。

### 9.3 Explicit `/reply_pending`

该命令是 3A 默认规则的唯一 V1 例外。它不修改 floor 历史，而是创建一个带 `trigger_kind=manual_pending_reply` 的 turn authorization，选择：

1. 最近一次已确认 outgoing/coverage 之后；
2. 当前 command snapshot 之前；
3. 尚未删除且 source resolved；
4. 不属于已 completed response coverage；
5. 按 canonical ordering 连续的最新 incoming segment。

命令执行前显示 segment 的消息数量和时间范围，不显示完整私密正文作为 callback data。若范围为空、存在 unresolved outgoing、当前 effective mode 不是 AUTO 或 gate 已关闭，则拒绝且不隐式切换模式。

未来 `resume_pending_policy=ask` 可以在恢复时创建一个短期 Control Bot proposal，让管理员选择 `/reply_pending` 或忽略；它不会改变 V1 的默认 ignore，也不能在未确认时自动生成。

## 10. AUTO turn 契约

### 10.1 Collection

AUTO eligible incoming 复用 Message Lifecycle 的 sliding 3 秒 debounce 和 10 秒 hard cap。Orchestrator 创建/刷新 collecting turn 时保存：

```text
account_id / conversation_id
trigger_kind incoming|replacement|manual_pending_reply
ordered message revision membership
account_control_version_snapshot
mode_version_snapshot
content_revision_snapshot
base/effective mode snapshot
automation floor and coverage snapshot
debounce/hard-cap policy snapshot
```

新 incoming 只能扩大 collecting turn 的输入范围，不能改写已经 sealed turn 的 membership。

### 10.2 Ready 到 generating

进入 generating 前，在 conversation lock 下检查：

```text
effective mode = AUTO
operational state = READY
account control version unchanged
conversation mode/content versions unchanged
turn still active and ready
all included media ready and supported
no confirmed human outgoing after collection snapshot
no active conflicting COPILOT draft/send
```

通过后创建 context manifest 和 Main AI model run。只有此时才对本 turn incoming 发 read acknowledgement，并在 provider 请求前启动 user-chat typing。

### 10.3 Output 与发送

模型输出提交为 `output_ready` 后，创建 outbound intent 前再次运行完整 resolver 和版本检查。intent 至少快照：

```text
account_control_version
mode_version
content_revision or grace authorization
turn/generation
model config/credential version
response coverage end event
```

intent commit 后、Telegram RPC 前执行轻量门禁；mode、control、human outgoing、edit/delete、lease loss 或 policy change 均无宽限并取消未发送 intent。

### 10.4 Incoming during generation

只对新的 eligible incoming 使用已确认的 3 秒规则：完整且验证通过的模型结果在 run 的 `t0 + 3s` 前完成时，可以为旧范围创建 grace authorization；否则旧 run superseded、尽力取消并生成 replacement turn。

以下变化永不获得 3 秒宽限：

- account/control version 变化；
- conversation mode version 变化；
- confirmed human outgoing；
- edit/delete；
- contact policy、pause、maintenance 或 erasure gate；
- COPILOT approval/cancel。

## 11. HUMAN 模式与真人 outgoing

### 11.1 HUMAN incoming

HUMAN 下 incoming：

- 幂等持久化并刷新 Memory job；
- 不创建 response turn 或 Main AI run；
- 不自动 read，不发送 user-chat typing；
- 不因消息数量、时间或情绪自动切换到 AUTO；
- 不自动发送 proactive message。

Control Bot `/status` 可以显示 unanswered count 和最近时间，但不得复制整段聊天正文到普通日志。

### 11.2 Confirmed human outgoing

只有 Message Lifecycle source reconciler 最终确认 `source=human` 后，Orchestrator 才执行接管事务：

1. 锁 conversation 并读取 active work。
2. `mode_version + 1`、`content_revision + 1`。
3. 使 pre-send turn/run/intent 和 COPILOT draft 失效。
4. 若 temporary takeover 启用，将 `temporary_human_until = now + configured seconds`；否则不改变 base mode。
5. 更新 response coverage；无法可靠判断 coverage 时保守记录 human event，但不越过未知范围。
6. 写 history/audit/outbox 并 commit。

`system_pending` outgoing 不得提前触发。若它后来对账为 AI/proactive/copilot approved，不能保留曾经误触发的 HUMAN 状态变化。

### 11.3 发送竞态

真人 outgoing 与 AI result 同时到达时，只有一个 transaction 能先锁定 conversation：

- human transaction 先提交：AI 的 mode/content snapshot 失效，不得创建 intent；
- intent 已安全 commit、但 RPC 尚未开始：human transaction 将 intent cancelled；
- Telegram RPC 已开始或结果 unknown：不假定取消成功，等待 random ID/message ID reconciliation；
- AI 已 confirmed sent 后才出现 human outgoing：两条消息都保留，后续 Memory 可观察该事实，不删除已发送 AI 消息。

## 12. Temporary HUMAN

### 12.1 默认与触发

功能默认 `enabled=false`。启用后，confirmed human outgoing 创建或续期：

```text
temporary_human_until = database_now + 600 seconds
```

期限使用 account policy snapshot，不能由消息内容或模型决定。每次 confirmed human outgoing 在同一 mode-version transaction 中续期。

### 12.2 有效期行为

window 内 effective mode 为 HUMAN，但 base mode 和 override 不变。incoming 只持久化/进入 Memory；没有 response turn、read、typing 或 proactive send。

Control Bot 显示 base mode 和 takeover 剩余时间。管理员仍可：

- `/ai <contact>` 修改 base override，但 takeover 到期前仍是 HUMAN；
- `/human <contact>` 将 base override 永久设为 HUMAN；
- `/pause <contact>` 施加更高优先级 pause；
- `/cancel <contact>` 取消 active work，但不提前结束 takeover；
- 显式 `/takeover_end <contact>` 提前结束可选 takeover。

### 12.3 Expiry

expiry 由持久化 deadline + worker compensation scan 驱动，不依赖单个内存 timer。领取 expiry job 后在 conversation lock 下：

1. 确认 deadline 仍是目标值且没有更晚续期。
2. 清除 overlay，`mode_version + 1`。
3. 将 automation resume floor 推进到当前 event watermark。
4. 写 history/outbox。
5. 不创建 backlog turn，不发送“恢复了”消息。

重复 expiry 是 no-op。进程停机跨过 deadline 时，恢复扫描执行相同 CAS；在 expiry 事务提交前 resolver 仍按 HUMAN fail closed。

## 13. COPILOT 响应式草稿

### 13.1 Manual-only trigger

COPILOT incoming 本身不创建草稿、model run 或 Control Bot 通知。管理员观察到消息后执行：

```text
/draft <contact>
```

Orchestrator 验证 effective COPILOT、operational READY、没有 active draft/发送、存在未覆盖 eligible segment，然后创建一个 `trigger_kind=copilot` turn。该 turn 复用 3 秒 quiet window，以避免命令刚好落在 album 或连续消息中间；hard cap 仍是 10 秒。

命令只触发一次。生成期间出现新 incoming、edit/delete、human outgoing 或版本变化时，当前 draft invalidated/discarded，不自动重生成；管理员需要再次执行 `/draft`。

### 13.2 Draft state machine

```text
requested
  -> collecting
  -> generating
  -> ready
  -> editing -> ready
  -> approved -> send_queued -> sent

requested/collecting/generating/ready/editing
  -> ignored | expired | invalidated | failed
```

同一 conversation 至多一个 active reactive/proactive COPILOT draft。terminal row 不复用；重新 `/draft` 创建新 identity 和 idempotency key。

draft 快照至少包括：

```text
account_control_version
mode_version
content_revision
turn/message revisions
model run/config/prompt version
current draft revision
requested_by admin
expires_at = ready_at + 30 minutes
```

### 13.3 Control Bot draft card

ready 后 Control Bot 发送管理员私聊卡片：

```text
contact label
input time range and message count
draft text
expires at
[Send] [Edit] [Ignore]
```

callback data 只含短期 opaque action token，不含 contact ID、正文或状态 JSON。card 被 Telegram 转发或截图不是授权；服务端必须重新验证 callback sender 是 allowlist admin、token 未使用、draft active、版本匹配且未过期。

### 13.4 Edit

`Edit` 创建绑定 draft/admin 的短期 ForceReply session。管理员提交修改后：

1. 验证消息来自绑定的 Control Bot chat/admin/reply target。
2. 校验长度、允许字符和 Telegram send limits。
3. 创建不可变 draft revision，`author=admin_edit`。
4. 返回预览卡片，再次要求 Send/Edit/Ignore。

修改文本是联系人敏感内容，不进入普通 control log；Bot 消息删除不是数据清除手段。terminal draft revision 按 retention policy 清理，contact purge/account wipe 立即 redaction。

### 13.5 Ignore 与 expiry

Ignore 使用 state/version CAS 写 terminal `ignored`，不改变 mode、coverage 或 floor，也不向联系人产生副作用。

ready 后 30 分钟到期，或以下任一 snapshot 变化时进入 `expired/invalidated`：

- account control version；
- conversation mode/content version；
- 输入 message revision/delete；
- effective mode 不再是 COPILOT；
- contact policy、erasure 或 operational gate；
- 另一个 confirmed outgoing 已覆盖该 segment。

失效 card callback 返回稳定原因，不允许“仍然发送”。

## 14. COPILOT approval 与发送

### 14.1 Approval transaction

管理员点击 Send 后，`control` 只提交 approval command；它不持有 Telethon Session。`app` 消费 durable command，在 conversation lock 下检查：

```text
draft state = ready
draft revision = approved revision
not expired
effective mode = COPILOT
operational state = READY
account_control_version matches
mode_version matches
content_revision matches
turn/messages still valid
admin approval valid and unused
no existing outbound intent for draft
```

通过后同一事务：

- 将 draft 标记 `approved`；
- 创建唯一 outbound intent；
- 设置 `source=copilot_approved`、`model_role=main_ai`；
- 保存 `copilot_draft_id`、approved revision 和 approval actor reference；
- 写 audit/outbox。

`control` 不能直接调用 app 内部未鉴权 HTTP send endpoint；跨进程通过 PostgreSQL durable command/outbox，最终副作用仍由 app 执行。

### 14.2 Provenance

最终 canonical outgoing：

```text
role = assistant
source = copilot_approved
```

并可查询：

```text
original model draft revision
approved draft revision
was_edited
approved_by admin ref
approval time
model run/config/prompt
Telegram random_id/message_id
```

它进入 conversation history 和 Memory，但 style-learning 默认必须区分：

- `human`：真人在普通 Telegram 客户端独立发送；
- `copilot_approved + was_edited=true`：真人编辑后的 AI 草稿；
- `copilot_approved + was_edited=false`：真人批准但文本由 AI 生成。

不能因为有人批准就把未修改 AI 文本当作真人写作风格的同等证据。

### 14.3 Send/reconciliation

intent commit 后复用 Message Lifecycle 的 RPC 前门禁、stable `telegram_random_id`、unknown-send reconciliation 和 FloodWait 规则。approval 不能绕过 global/contact pause、maintenance、erasure 或 version change。

发送成功后 draft `sent`、intent reconciled、coverage 推进。RPC unknown 时 draft 保持 `send_queued` 或 `send_unknown` 投影，按钮全部禁用；在 reconciliation 完成前禁止创建第二个 intent或再次批准。

## 15. Proactive 与模式

### 15.1 AUTO

AUTO 中 Proactive candidate 依次通过候选规则、预算、quiet hours、contact policy、conversation activity 和最终 Orchestrator gate。Proactive Agent 决定是否/主题，Main AI 生成文本，最后创建 `source=proactive_ai` intent。

run/decision/intent 同时快照 account control、mode、content、budget 和 policy version。任一变化都没有 3 秒宽限。

### 15.2 COPILOT

6B 的 manual-only 规则只针对 incoming 的响应式草稿。已由 Proactive Pipeline 规则层筛出的主动候选在 effective COPILOT 下可以自动运行 Proactive Agent 和 Main AI，但最终产物进入 COPILOT draft `ready`，绝不创建 outbound intent，等待管理员 Send/Edit/Ignore。

proactive draft：

- 保存 `proactive_decision_id`；
- 使用与响应式 draft 相同的 30 分钟和 version gates；
- card 明确标记“主动消息建议”和候选原因 code；
- 不展示 chain-of-thought；
- 批准发送后 source 仍为 `copilot_approved`，同时保留 proactive provenance。

若 conversation 已有 active draft，新的 proactive candidate 标记 `deferred_active_draft` 或 `skipped`，不能替换响应式草稿，也不能形成多草稿队列。

### 15.3 HUMAN、PAUSED 与 BLOCKED

HUMAN、temporary HUMAN、contact/global PAUSED 或 maintenance 下，不生成 proactive model run 或 draft。BLOCKED 时保留 candidate 的非正文 reason/idempotency metadata，按 Proactive Pipeline 的过期规则 skip；恢复后不补跑已经错过的主动消息窗口。

## 16. Pause、maintenance 与 BLOCKED

### 16.1 Pause semantics

pause 的目标是立即关闭新的模型聊天副作用，同时保留可恢复基础模式。它阻止：

- 新 AUTO/COPILOT/proactive model run；
- 新 read acknowledgement 和 typing；
- 新 outbound intent；
- 尚未开始的 Telegram RPC。

它不阻止：

- canonical ingest、edit/delete reconciliation；
- source reconciliation 和 unknown-send recovery；
- erasure、retention 和安全清理；
- Memory job refresh；是否执行非实时 Memory model run由 Operations maintenance scope控制。

### 16.2 Maintenance

maintenance 至少支持：

```text
inactive
draining
active
```

`draining` 停止领取新 conversation side effect，让已安全开始的 provider call尽快提交或取消；RPC unknown 必须对账。`active` 禁止所有新 conversation/proactive生成和发送。进入、升级或退出均递增 account control version。

完整基础设施 shutdown 由 Runtime Topology 处理；数据库已不可用时无法通过 maintenance row 提供正确性，因此服务 readiness 和启动顺序仍必须 fail closed。

### 16.3 Operational block lifecycle

durable block 保存：

```text
scope type/id
reason_code
state active|probing|cleared
version
first_seen_at
retry_after
last_probe_at
cleared_at
```

block 创建时取消受影响 pre-send work。恢复探测成功后 CAS clear，并将 resume floor 推进到当前 watermark；不创建 backlog turn。若 provider 短暂恢复后再次失败，创建新 generation/version，不复用已 clear block。

BLOCKED reason 必须是稳定 code，不把 provider body、endpoint secret 或联系人正文放入状态和 `/server_status`。

## 17. Version、lock 与发送门禁

### 17.1 Version ownership

| Version | 所有者 | 何时变化 | 主要消费者 |
|---|---|---|---|
| `account_control_version` | account orchestrator row | account default、global pause、maintenance、global block/policy | 所有 conversation run/intent/draft/decision |
| `mode_version` | conversation row | override、contact pause、takeover、human outgoing、cancel | 当前 conversation work |
| `content_revision` | conversation row | message create/edit/delete、confirmed outgoing | turn/context/run/draft |
| `turn generation_no` | active turn | replacement/regeneration | model run/intent |
| `draft revision_no` | COPILOT draft | model output或管理员 edit | approval/intent |
| `proactive budget_version` | Proactive policy | quota/window mutation | proactive decision/intent |

所有 version 都是单调整数，不使用 wall-clock 作为 CAS。时间只用于 deadline；相同时间戳不能表示相同状态。

### 17.2 Snapshot contract

每个会产生模型调用或 Telegram 副作用的记录必须保存适用 snapshot。最小关系：

```text
turn
  -> account_control_version, mode_version, content_revision
model_run
  -> turn snapshots + config/credential/prompt/context versions
copilot_draft
  -> turn snapshots + draft revision + expires_at
proactive_decision
  -> control/mode/content/policy/budget snapshots
outbound_intent
  -> all final applicable snapshots + source authorization
```

只在日志中打印版本而不落库，不能满足崩溃恢复或 stale-result prevention。

### 17.3 Lock order

避免 deadlock 的固定顺序：

```text
account orchestrator row
  -> contact policy row (only when changing it)
  -> conversation row
  -> active turn/draft/decision
  -> outbound intent
```

普通 conversation transaction 已知 account snapshot 时不长期持有 account row lock；它读取版本，并在写 conversation/intent 前用条件查询确认版本仍匹配。global control transaction只更新 account row，不遍历锁定 conversations。

### 17.4 Final gate

创建 intent 和 RPC 前检查的共同核心：

```text
account active and control_version matches
no maintenance/global/contact pause
contact policy allows source/action
effective mode permits source
operational dependencies ready
conversation mode_version matches
content_revision matches or exact incoming grace authorization
turn/draft/decision active and version matches
no confirmed human outgoing invalidation
no duplicate idempotency key/random_id
lease/CAS ownership valid
```

source-specific rules：

- `ai`：effective AUTO，active AUTO turn；
- `proactive_ai`：effective AUTO，active approved proactive decision；
- `copilot_approved`：effective COPILOT，active approval + exact draft revision；
- `human`：来自 Telegram outgoing source reconciliation，不由 Orchestrator创建 intent。

## 18. 状态转换与不变量

### 18.1 Base mode transition

| 当前 | 命令 | 新 base | active work |
|---|---|---|---|
| inherited/override 任意 | scoped `/ai` | override AUTO | stale pre-send work取消；不补 backlog |
| inherited/override 任意 | scoped `/human` | override HUMAN | stale pre-send work取消 |
| inherited/override 任意 | scoped `/copilot` | override COPILOT | stale AUTO work取消；不自动 draft |
| override 任意 | `/mode_inherit` | account default | 按新 effective mode取消冲突 work；不补 backlog |
| 任意 | unscoped base命令 | account default改变 | account version使所有 stale work失效 |

同值命令不改变 version；不同值 transition 原子记录 previous/new state。

### 18.2 Overlay transition

| Overlay event | Version | 结果 |
|---|---|---|
| global pause/resume | account control +1 | 所有新 side effect gate关闭/恢复 |
| maintenance change | account control +1 | drain/stop/reopen |
| contact pause/resume | conversation mode +1 | 仅该 conversation |
| temp takeover start/renew/expire | conversation mode +1 | effective HUMAN/恢复 base |
| operational block open/clear | scope version | cancel/stay blocked/恢复且推进 floor |

resume、expiry 和 block clear 都不创建 turn。

### 18.3 Core invariants

系统始终满足：

1. 一个 conversation 的 base mode 来源唯一：override 或 account default。
2. pause/maintenance 不覆写 base mode。
3. 任何 stale account/mode/draft revision 都不能创建 Telegram副作用。
4. confirmed human outgoing 对 pre-send AI/COPILOT work 无条件失效。
5. 3 秒 grace 只覆盖列明的新 incoming event delta。
6. COPILOT incoming 不自动生成响应式草稿。
7. 一个 conversation 至多一个 active COPILOT draft。
8. approval 一次性且只对应一个 immutable draft revision。
9. 恢复自动化不自动回复旧 backlog。
10. Redis 丢失不改变 requested/effective mode 的 durable truth。

## 19. 崩溃与恢复

### 19.1 Control command crash

Control Bot 先持久化 command，再回复成功。若 commit 后 Bot acknowledgement 失败，Telegram update 重放或管理员重试按 idempotency key 返回既有结果，不重复递增版本。

若 command 只写了一半，数据库 transaction 回滚；不能出现 account version 已变但 history/outbox 缺失。

### 19.2 Coordinator/model crash

- collecting/ready turn：lease expiry 后按 durable deadline 重新领取；
- running model call：attempt 状态和 lease 判定是否重试，旧 provider result 必须通过 version CAS；
- output_ready：重新运行 final gate，再创建或找到既有 intent；
- cancelled/superseded：late result 保存最小 usage/error 后 discarded；
- typing：依赖 Telegram TTL 自然消失，不重放；
- read acknowledgement：按 high-watermark有限对账，不因失败重复生成。

### 19.3 Draft crash

- generating draft：恢复逻辑与 model run 一致；
- ready 但 Bot card 未成功投递：durable notification job 重试，使用同一 draft/card generation；
- edit session 丢失：session 过期，draft 回到 ready 或保持原 revision；不采纳半提交文本；
- approved 但 intent 未创建：app 依据 durable approval command CAS 创建一次；
- intent 已创建：任何重试只读取既有 intent；
- send unknown：禁用 card，先 reconciliation。

### 19.4 Timer/scan recovery

temporary takeover、draft expiry、block retry 和 debounce deadline 都由持久化时间 + 补偿扫描保证。Redis delayed notification 可以丢失，但 PostgreSQL scan 必须重新发现 overdue row。

系统使用 database time 比较 deadline，避免不同容器 wall clock 漂移导致提前恢复或过期。

### 19.5 Restore safety

数据库恢复后，在以下检查完成前 maintenance gate 保持 active：

```text
schema revision
account/control state
erasure ledger replay
credential availability
unknown outbound reconciliation
expired takeover/draft scan
stale lease cleanup
```

恢复不得因为旧 backup 中的 AUTO 状态直接重放 backlog 或旧 approval。

## 20. 数据模型增量契约

本阶段在 Data Model 基线上增加或调整：

### 20.1 `account_orchestrator_states`

保存 account default、global pause、maintenance、temporary takeover policy、resume policy、control version 和更新者。`account_id` 唯一；CHECK 保证 V1 resume policy 仅 `ignore`。

### 20.2 Conversation 字段

将原 `conversations.mode` 细化为：

```text
base_mode_override nullable AUTO|HUMAN|COPILOT
contact_paused boolean
temporary_human_until nullable
mode_version
automation_resume_floor_event_id nullable
last_response_covered_event_id nullable
```

account resume watermark 可以保存在 account orchestrator row；conversation resolver 使用两者较大值，按需惰性物化。

### 20.3 Control history/commands

```text
account_control_history
conversation_mode_history
control_commands
```

`control_commands` 以 `(bot_id, telegram_update_id)` 和 server idempotency key 唯一，保存 command kind、scope IDs、expected/result version、state 和无正文 result code。

### 20.4 Operational blocks

`orchestrator_blocks` 使用 typed scope foreign key、reason、state、version、retry deadline 和 lifecycle timestamps。account/profile/conversation/turn scope 恰好一个非空；active block 建 partial index。

### 20.5 COPILOT

```text
copilot_drafts
copilot_draft_revisions
copilot_action_tokens
copilot_edit_sessions
```

draft 和 revisions 是敏感内容表，受 contact purge、account wipe 和 retention控制。action token只保存 hash、purpose、admin、draft/revision、expires/used_at。

`outbound_intents` 增加 nullable `copilot_draft_id` 和 `approved_draft_revision_id`，并以 partial unique保证一个 draft 最多一个 intent；source CHECK 扩展 `copilot_approved`，绑定 `model_role=main_ai`。

### 20.6 Snapshot 字段

turn、model run、proactive decision、COPILOT draft 和 outbound intent 增加 `account_control_version_snapshot`。不能只通过 conversation 反查当前 account version，因为历史 run 必须保留启动时事实。

所有新增 composite FK 继续包含 account/conversation scope；具体 DDL 名称、索引和 migration 规则以更新后的 Data Model 文档为准。

## 21. Audit、隐私与可观察性

### 21.1 Audit events

至少审计：

```text
account default changed
global/contact pause changed
maintenance changed
conversation override set/cleared
temporary takeover start/renew/expire/end
operational block open/clear
turn allowed/denied/cancelled
manual pending reply authorized
copilot draft requested/ready/edited/ignored/expired/invalidated
copilot approval accepted/rejected
outbound final gate accepted/rejected
```

每条记录包含 actor、scope、版本、reason code、request/correlation ID 和结果。拒绝也需要 audit，但高频重复 gate rejection 应聚合 metric，避免 audit flood。

### 21.2 Privacy

- audit、status 和 metric 不保存 message/draft正文；
- Control Bot card 只发到 allowlist管理员私聊；
- contact label最小化，callback data不含正文；
- draft revision、edit session和Bot notification text按Operations retention清理；
- Telegram delete、contact purge、account wipe使相关 draft/action/session立即不可见并redaction；
- Prompt、provider raw body、API key、Session、access hash不进入Orchestrator状态。

### 21.3 Metrics

建议指标：

```text
orchestrator_decisions_total{effective_mode,action,result,reason}
mode_transitions_total{scope,from,to,reason}
stale_results_discarded_total{reason}
pending_reply_commands_total{result}
temporary_takeovers_active
copilot_drafts_total{kind,state}
copilot_draft_age_seconds
copilot_approval_latency_seconds
operational_blocks_active{scope,reason}
final_gate_rejections_total{source,reason}
```

label 不能使用 account ID、contact ID、username、model output 或任意高基数 message ID。

### 21.4 Status presentation

`/status <contact>` 显示：

```text
account default and version
conversation override or inherited
effective mode and overlay reason
temporary takeover expiry
operational block code/retry time
active turn/draft summary
unanswered count/time range
automation resume floor
```

不显示正文、Prompt、credential、内部路径或 provider error body。

## 22. 自动化验收场景

### 22.1 模式与版本

- account default 改变只更新一行，不批量改写 conversation；旧 run 因 control version 失效。
- conversation override 优先于 account default；clear 后继承最新 default。
- global/contact pause 和 maintenance清除后恢复原 base mode。
- 重复同值命令不递增版本；`/cancel`会递增并失效 active work。
- Redis清空后resolver结果不变。

### 22.2 Backlog

- HUMAN/COPILOT/PAUSED/temporary HUMAN/BLOCKED期间 incoming 在恢复后不自动回复。
- `/reply_pending`只在AUTO创建一次稳定范围turn，重放command不重复。
- future `ask` policy在V1无法被配置或触发。

### 22.3 真人竞态

- human outgoing早于intent commit时AI不得发送。
- human outgoing晚于RPC start时进入reconciliation而非宣称取消。
- `system_pending`最终为AI时不触发temporary HUMAN。
- temporary takeover续期、expiry和停机补偿使用CAS且不补backlog。

### 22.4 COPILOT

- incoming不自动创建响应式draft；只有`/draft`触发。
- 新incoming/edit/delete/mode change使active draft失效且不自动重生成。
- card token被其他用户、过期、重放或对应旧revision时拒绝。
- Edit创建新immutable revision；Send只批准精确revision。
- approved intent/Telegram send在重试和崩溃后至多一次。
- canonical source为`copilot_approved`，style learning区分未编辑/已编辑/纯human。
- COPILOT proactive candidate只形成draft，HUMAN/PAUSED不运行。

### 22.5 故障与恢复

- provider/config/credential block不改base mode，clear后不补旧消息。
- draft card投递失败可重试但不重复生成draft。
- DB restore后旧approval、过期takeover和unknown intent在自动发送前完成reconcile。
- global control、conversation change与model result竞态只有一个合法CAS结果。

## 23. 后续文档边界

Memory Pipeline 可以读取所有模式下的 committed conversation history，但不得把 Memory 任务成功当成回复授权。其来源信任必须保留 `human`、已编辑/未编辑 `copilot_approved`、`ai` 和 `proactive_ai` 的差异，完整契约见 `docs/architecture/05-memory-pipeline.md`。

Context Contract 定义 AUTO、COPILOT reactive、COPILOT proactive 和 manual pending reply 的具体 context layers/token/image budget；必须消费本文的turn/draft snapshot。

Proactive Pipeline 定义候选、预算、quiet hours和decision状态；必须遵守本文的mode mapping和最终gate。

Operations 确定draft/control history retention、timer scan interval、maintenance runbook和block probe policy。

Test Strategy 将第22节实现为fake Telegram/provider、并发事务、时钟和崩溃恢复测试。

## 24. 验收条件

- [x] AUTO、HUMAN、COPILOT、PAUSED overlay和BLOCKED operational state语义已定义。
- [x] account default、conversation override、pause、maintenance和temporary HUMAN优先级已定义。
- [x] account control version、mode/content version和final gate已定义。
- [x] Control Bot基础模式、pause/resume、draft、reply pending、cancel和status命令已定义。
- [x] 恢复AUTO默认不补backlog，future ask policy边界已定义。
- [x] AUTO turn输入、snapshot、生成和发送契约已定义。
- [x] confirmed human outgoing、竞态和temporary takeover已定义。
- [x] COPILOT manual draft、edit、approval、expiry、provenance和send流程已定义。
- [x] proactive在AUTO/COPILOT/HUMAN/PAUSED下的行为已定义。
- [x] pause、maintenance、operational block和故障恢复已定义。
- [x] 新增数据模型、事务、幂等、审计、隐私和指标要求已定义。
- [x] 自动化验收场景与后续文档边界已定义。
