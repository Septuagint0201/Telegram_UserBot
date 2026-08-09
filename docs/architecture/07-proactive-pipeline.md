# Proactive Pipeline

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的主动候选产生、时间窗口、规则筛选、Proactive Agent 决策、Main AI 最终生成、预算、quiet hours、幂等、最终发送门禁和审计契约。

总体设计见`docs/Design.md`；运行进程见`docs/architecture/01-runtime-topology.md`；消息恢复见`docs/architecture/02-message-lifecycle.md`；持久化见`docs/architecture/03-data-model.md`；模式门禁见`docs/architecture/04-conversation-orchestrator.md`；Memory投影见`docs/architecture/05-memory-pipeline.md`；模型上下文见`docs/architecture/06-context-contract.md`；worker、retry、retention、metrics和DR见`docs/architecture/08-operations.md`。

当前状态：V1 架构基线。本文的数值默认值属于不可变 proactive policy version，可由 allowlisted 管理员通过 Control Bot 创建、验证和激活新版本；消息、模型输出和普通配置 JSON 不能修改它们。

## 2. 已确认决策

| 主题 | V1 决策 |
|---|---|
| 触发 | durable due job + 每 15 分钟补偿扫描；没有候选不调用模型 |
| 候选来源 | 只允许 `promise_due/event_upcoming/event_followup/relationship_reconnect/explicit_followup` 五类确定性规则 |
| 模型发现 | Proactive Agent 不能从任意 memory 自行创造候选或联系人 |
| quiet hours | 联系人当地时间默认 `22:00–08:00` |
| 深夜硬禁区 | `00:00–07:00` 绝不主动发送，配置不能放宽 |
| quiet bypass | 仅 importance `>= 0.90` 的明确 upcoming event 或 self promise，在窗口早于 08:00 失效且当前位于 `22:00–24:00` 或 `07:00–08:00` 时允许 |
| bypass 限额 | 每个联系人每个当地自然日最多一次，仍受其他预算和门禁限制 |
| close friend | 3 天未联系可产生 reconnect；最多 2 次/当地日；最小间隔 6 小时 |
| friend | 7 天未联系可产生 reconnect；最多 1 次/当地日；最小间隔 12 小时 |
| acquaintance/unknown | 不产生 relationship-only reconnect；明确 event/promise 最多 1 次/当地日，最小间隔 24 小时 |
| 全局预算 | account timezone 自然日最多 10 次实际/不确定主动发送 |
| 活跃抑制 | 最近 30 分钟有 meaningful chat activity 时不主动插话 |
| 候选聚合 | 同联系人同一评估窗口内的多个原因合并为一个 envelope，最多一条消息 |
| Agent 输出 | strict `send_now/defer_once/none`；defer 必须位于既有候选窗口且最多一次 |
| `none` | 同一候选窗口终态；只有 transport/provider 故障可重试同一 run |
| 最终生成 | Main AI 使用 text-only 最小 proactive context，不加载图片 |
| 模式 | AUTO 自动发送；COPILOT 创建待审批主动草稿；HUMAN、temporary HUMAN、PAUSED、maintenance、BLOCKED 不运行模型或发送 |
| 错过窗口 | expire/skip，不在恢复后补发 backlog |

## 3. 目标、范围与非目标

### 3.1 目标

本文保证：

1. 每次模型评估都源于至少一个仍有效、可追溯的规则 occurrence。
2. Scheduler 重复 tick、worker 重试、进程崩溃或并发执行不会重复评估或重复发送。
3. 模型不能绕过 quiet hours、预算、模式、联系人开关、活跃会话和 evidence validity。
4. 时间计算在 IANA timezone、DST、当地自然日和 UTC deadline 之间可重放。
5. Proactive Agent 只决定是否/何时/为何围绕候选主题联系；Main AI 只负责最终措辞。
6. AUTO、COPILOT、HUMAN、PAUSED 和恢复路径的行为唯一且可审计。
7. 任何 Telegram 副作用都通过 `app`、conversation lock、budget reservation 和 outbound delivery group。
8. 主动消息及人工批准的主动草稿保留 provenance，不反向自证联系人事实或真人风格。

### 3.2 非目标

本文不负责：

- 扫描全部联系人并让模型自由决定“找谁聊天”；
- 让模型创建 life event、intention、relationship 或 active memory；
- 允许 Proactive Agent 读取 pending memory candidate、quarantined summary 或任意数据库表；
- 在 V1 主动路径加载图片、语音、视频或任意 URL；
- 代表管理员发送营销、群发、陌生人获客或批量消息；
- 绕过 Telegram、provider、联系人、账号或部署安全限制；
- 保证主动联系一定符合联系人的期望；
- 用模型 priority 取代确定性预算和门禁；
- 在错过窗口、服务恢复或模式恢复后追补主动消息；
- 在本文重复定义Operations已经固定的worker、retry、alert、backup和retention参数。

## 4. 术语与事实源

### 4.1 Rule occurrence

`proactive_rule_occurrence` 是一个规则原因在精确证据版本和 UTC 有效窗口上的 durable identity，例如：

```text
Bob interview event version 3
reason = event_upcoming
eligible window = [2026-08-11T01:00Z, 2026-08-11T07:00Z)
policy version = 8
```

Occurrence 不是发送许可，也不是模型输出。它只说明“这个原因在这个窗口可以进入后续筛选”。

### 4.2 Candidate envelope

同一联系人在同一 evaluation window 内的全部 eligible occurrences 按稳定顺序合并成一个 `proactive_candidate`。Envelope 最多产生一个 Proactive Agent decision；不能让每个 reason 各发一条消息。

### 4.3 Decision

`proactive_decision` 是 Proactive Agent 对精确 candidate、manifest、模型和版本快照的 strict structured output。允许 action：

```text
send_now
defer_once
none
```

Decision 仍不是 Telegram 发送许可。

### 4.4 Budget reservation

Budget reservation 是对 account daily、contact daily 和可选 quiet-bypass bucket 的原子容量占位。它在生成最终文本前阻止并发过量；只有实际或不确定 Telegram 副作用才最终计费。

### 4.5 Meaningful chat activity

以下任一 committed event 更新 conversation 的 proactive activity watermark：

- contact incoming canonical message，包括V1不下载正文的语音/视频等metadata-only media message；
- 已对账的human、AI、proactive或COPILOT outgoing；
- 改变可见内容的message edit/delete；
- active incoming collection、turn、draft或delivery产生的冲突工作状态。

reaction、read acknowledgement、typing、service message、重复update和无正文技术重试不属于meaningful chat activity。

### 4.6 Source of truth

| 事实 | Source of truth |
|---|---|
| event/intention/memory | PostgreSQL active current version与 evidence root |
| relationship/current activity | PostgreSQL typed projection + canonical events |
| proactive policy | immutable active policy version |
| timezone | contact override → account timezone → deployment timezone snapshot |
| candidate/decision/reservation | PostgreSQL durable rows |
| queue/timer notification | Redis，可丢失、可重建 |
| mode/control/final gate | PostgreSQL account/conversation state |
| Telegram send | outbound delivery group/intent + random ID reconciliation |

Redis、进程内 timer、模型文本、日志和 metrics 都不是发送许可事实源。

## 5. 组件职责与依赖方向

### 5.1 `worker`

`worker` 负责：

- Scheduler leader 发布每 15 分钟的 compensation tick；
- 处理 event/intention 变化产生的 durable due jobs；
- 运行确定性 occurrence materializer 和 candidate filter；
- 构造 Time Context manifest；
- 调用 Proactive Agent 并验证 strict output；
- 请求/持有 budget reservation；
- 在获得 preliminary authorization 后调用 Main AI 生成最终文本；
- 在 COPILOT 中创建 proactive draft 及 Control Bot notification job；
- 向 `app` 提交 AUTO final-send request；
- 处理 expire、defer 和无 Telegram 副作用的模型重试。

`worker` 不能持有 Telethon Session、直接调用 Telegram user-account send、绕过 conversation gate 或修改 active memory。

### 5.2 `app`

`app` 负责：

- preliminary authorization 的 conversation/control snapshot gate；
- AUTO 最终发送前的完整 transaction gate；
- 原子提交 reservation、delivery group、ordered intents 和 outbox；
- Telethon RPC、random ID reconciliation 和 budget charge/release；
- 因真人 outgoing、incoming、edit/delete、mode change 或 maintenance 使 stale decision 失效。

### 5.3 `control`

`control` 负责：

- allowlisted 管理员 proactive 开关和非密钥 policy draft/validate/activate；
- contact override 和状态摘要；
- COPILOT proactive draft card、Send/Edit/Ignore；
- `/proactive_status` 和 `/server_status` 的非正文状态展示。

`control` 不能直接发送 Telegram 真人账号消息。

### 5.4 Memory Pipeline

Memory Pipeline 只提交 accepted active memory、life event、intention 和 relationship projection。Proactive Pipeline 只读这些 current versions；不能要求 Memory Agent 为某次主动发送临时制造证据。

### 5.5 调用方向

```text
Memory projections / canonical events
             |
             v
rule occurrence materializer
             |
             v
candidate aggregator
             |
             v
Time Context Builder -> Proactive Agent
             |
             v
preliminary gate + budget reservation
             |
             v
Main AI final generation
             |
             +-- COPILOT -> draft -> admin approval
             |
             +-- AUTO -> app final gate -> delivery group/intents -> Telegram
```

## 6. 端到端生命周期

```text
projection/event change or 15-minute compensation tick
  -> materialize deterministic rule occurrences
  -> select currently eligible occurrences
  -> deterministic rule/filter gates
  -> aggregate one candidate envelope per contact/window
  -> persist candidate + ordered occurrence membership
  -> build immutable proactive decision manifest
  -> call Proactive Agent
  -> strict validate send_now/defer_once/none
  -> none: terminal for this window
  -> defer_once: durable due job within same window
  -> send_now or due defer:
       preliminary mode/activity/evidence/budget gate
       atomically hold reservation
       build text-only Main AI proactive context
       generate and normalize final message
       AUTO: app final gate + commit group/intents
       COPILOT: create approval draft, hold reservation
       HUMAN/PAUSED/etc: skip and release
  -> reconcile Telegram result
  -> commit or release budget reservation
  -> update relationship/proactive projections and audit
```

任一步发现候选窗口、evidence、policy、timezone、mode 或 conversation 已变化，旧工作终止为稳定 reason；不能把旧输出绑定到新窗口或重新解释为普通 reactive reply。

## 7. 触发、due job 与补偿扫描

### 7.1 Event-driven due job

以下 current projection 的创建、更新、invalidate 或 supersede 必须刷新其 proactive due jobs：

- `life_events`；
- `intentions`；
- contact proactive setting；
- active proactive policy version；
- verified timezone；
- relationship level/manual override。

Due job 使用 occurrence key 派生的稳定 idempotency key和`available_at=window_start_at`。Projection更新先使旧generation失效，再创建新generation；旧job运行时必须重读current pointer。

### 7.2 15 分钟 compensation tick

Scheduler leader 每 15 分钟发布一个轻量 compensation job。它只执行 SQL/rule 扫描：

1. 找到 window 已打开但 occurrence/job 缺失的 event/intention。
2. 找到满足 reconnect threshold 且当天尚未产生 relationship occurrence 的联系人。
3. 重新发布 lease 过期或 notification 丢失的 pending work。
4. expire 已越过 window end 的 occurrence/candidate/reservation。
5. 对账 budget reservation 与 delivery/draft terminal state。

Tick 不无条件调用 Proactive Agent，也不按“每个联系人一次模型请求”工作。没有新 eligible occurrence 时只提交 scan watermark/metrics。

### 7.3 Scan cursor

Compensation 使用持久化 cursor：

```text
policy_version
scan_window_start_at
scan_window_end_at
partition/account cursor
completed_at
```

同一 scan range 可重复运行；occurrence unique key 负责去重。Cursor 只能在该范围所有分区完成后推进，不能因一个 worker 崩溃越过 hole。

### 7.4 Scheduler outage

恢复后只物化“当前仍在有效窗口”的 occurrence。已经结束的窗口标记 `missed_during_outage`，不补发、不调用模型。

## 8. 时间、timezone 与 DST

### 8.1 有效时区

按固定优先级选择：

1. validated contact timezone override；
2. account IANA timezone；
3. deployment default IANA timezone。

候选创建时保存：

```text
timezone_name
tzdata_version
local_date
UTC offset at evaluation
window local boundaries
window UTC boundaries
timezone_source
```

消息正文、模型输出、未经验证的 memory string不能直接成为 timezone配置。

### 8.2 DST 安全规则

- Quiet interval 使用当地 wall-clock rule重新计算当天 UTC 边界。
- Ambiguous quiet-start 取较早 instant，ambiguous quiet-end 取较晚 instant，使 quiet 时段更长。
- Nonexistent quiet boundary向安全方向扩展：start取前一有效instant，end取后一有效instant。
- Candidate local time 落入不存在时刻时向后移动到首个有效 instant；若越过 window end 则 expire。
- Candidate local time 有两个 instant 时使用较晚一个，避免意外提前发送。
- 已持久化 occurrence 保存 UTC window，不因 tzdata更新静默重写；未开始的新generation使用新 timezone snapshot。

### 8.3 时区变化

Timezone在 candidate/decision 后变化：

- 尚未生成 Telegram 副作用：旧 occurrence/candidate/reservation失效，按新时区重新物化仍有效窗口；
- 已发送或 unknown：保留原 snapshot和预算local date，不重新计费；
- 不把旧 quiet bypass资格转移到新 local date。

## 9. 允许的规则原因

### 9.1 通用 eligibility

所有 occurrence 必须满足：

- V1 非 Bot 用户的一对一 private conversation；
- contact未 blocked/review/deleting，`proactive_enabled=true`；
- account global proactive enabled；
- evidence是current、active、未redacted且具有有效root；
- reason在active policy allowlist；
- start/end可确定为有界UTC window；
- importance为有限`[0,1]`；
- occurrence idempotency key可从typed facts确定性计算。

缺少时间或证据时不让模型补猜。

### 9.2 `promise_due`

来源是 accepted active `intention`，默认只允许：

```text
owner = self
status = active
expected_at is known
importance >= 0.60
```

V1默认窗口：

```text
[expected_at - 15 minutes, expected_at + 2 hours)
```

Contact作出的承诺不能自动被解释为“催促对方”；只有 projection带显式管理员/validated mutual-followup标记时才转为`explicit_followup`。

### 9.3 `event_upcoming`

来源是 accepted active `life_event`，默认要求 start time 或已验证 all-day local date。Timed event 默认窗口：

```text
[start_at - 24 hours, start_at - 2 hours)
```

All-day event 使用event当地日：

```text
[09:00, 12:00) local time on event date
```

Event-kind policy可创建更窄的versioned窗口，但不能把已经结束的event重新标为upcoming。

### 9.4 `event_followup`

只在 event policy明确允许follow-up时创建：

```text
with end_at:    [end_at + 1 hour, end_at + 24 hours)
without end_at: [start_at + 2 hours, start_at + 24 hours)
```

生日等all-day event默认不创建同日follow-up，除非event kind policy显式启用。

### 9.5 `relationship_reconnect`

来源是current relationship state和canonical activity watermark，不引用模型自由文本：

```text
close:        last meaningful contact >= 3 days
friend:       last meaningful contact >= 7 days
acquaintance/unknown: disabled
```

每个联系人每个当地自然日最多物化一个reconnect occurrence，默认窗口为`[08:00,22:00)`。当日Agent返回`none`后不再评估；下一当地日仍超过threshold时可以形成新窗口。

### 9.6 `explicit_followup`

只允许具有明确 expected time/window 和 accepted evidence root 的 follow-up projection。默认窗口：

```text
[expected_at, expected_at + 6 hours)
```

“以后聊”“有空再说”等无界自然语言不自动形成 occurrence；需要 Memory validator 生成有界 typed intention 或管理员确认。

### 9.7 禁止的隐式原因

以下内容不能单独产生 candidate：

- 向量检索相似度高；
- 模型觉得“可以关心一下”；
- 联系人在线、头像变化或 read status；
- 未确认 memory candidate；
- AI/proactive自己此前说过某个联系人事实；
- 日志、provider输出或外部URL；
- 关系等级以外的任意画像评分；
- 仅因为到达Scheduler tick。

## 10. Deterministic rule filter

### 10.1 模型前硬门禁

Candidate聚合前按以下顺序筛选：

```text
account active/global proactive enabled
supported peer/contact proactive enabled
contact not blocked/review/deleting
occurrence current and window open
effective mode allows proactive evaluation
no global/contact pause or maintenance/block
quiet-hours or valid bypass possibility
contact/global budget capacity appears available
minimum interval appears satisfied
no meaningful activity in previous 30 minutes
no unresolved incoming/reactive turn/send/draft
evidence roots current and memory freshness acceptable
```

这是precheck，不是最终发送门禁。Capacity、mode、activity和evidence必须在reservation和最终发送时重新验证。

### 10.2 Memory freshness

- `fresh`：正常读取selected active memory/evidence。
- queue backlog导致`degraded/stale`：仍可使用current canonical event/intention和未失效active memory，但manifest标记freshness。
- edit/delete/forget reconciliation导致stale：所有受影响derived event/intention/memory先排除；不能用recent text补造同一candidate。
- evidence root断裂、summary quarantined或projection invalid：对应occurrence立即invalidated。

### 10.3 Priority只用于排序

规则层使用versioned priority：

```text
promise_due
event_upcoming
event_followup
explicit_followup
relationship_reconnect
```

同priority按window end升序、importance降序、occurrence ID升序。Priority不能绕过budget、quiet、mode或activity gate。

### 10.4 Filter结果

每个被跳过的occurrence保存stable reason，不调用模型：

```text
disabled
unsupported_peer
mode_suppressed
quiet_hours
budget_exhausted
minimum_interval
conversation_active
conflicting_work
evidence_invalid
window_not_open
window_expired
policy_changed
```

不保存完整私聊、event title、intention text或memory正文到普通audit。

## 11. Candidate envelope

### 11.1 聚合范围

同一`account_id + contact_id`在本次evaluation instant可共同评估的eligible occurrence合并为一个envelope。Candidate window：

```text
window_start_at = max(now floor, minimum member start)
window_end_at   = min(member end for selected compatible set)
```

窗口无交集的occurrence不能强行合并；按priority选择最早结束的compatible set，其余留给自己的window，但受active candidate和预算限制。

### 11.2 Stable membership

Membership按第10.3节排序seal，保存每个occurrence version/hash。Candidate key：

```text
HMAC(
  account_id + contact_id + conversation_id
  + ordered occurrence id/version list
  + window_start_at + window_end_at
  + proactive_policy_version_id
  + relationship/settings/timezone snapshots
)
```

HMAC仅用于幂等，不替代unique constraint或正文存储；contact purge时清除content-derived key并保留最小tombstone。

### 11.3 一次只产出一条

Proactive Agent可以选择一个或多个相容reason支持同一topic，但最终只能：

- 生成一个`send_now`；
- 生成一个`defer_once`；或
- 对整个envelope返回`none`。

不能让一个envelope生成多条文本、多个draft或多个delivery group。

### 11.4 新 occurrence 到达

- Candidate尚未claim：在同一短事务按稳定规则rebuild membership/generation。
- Agent运行中：不改写sealed candidate；新occurrence形成后续candidate。
- 旧decision为`none`：已经评估的occurrence在原window内保持terminal，新occurrence不能让它们复活。
- 已有reservation/draft/group：新occurrence等待独立窗口，不修改已授权topic。

## 12. Time Context Builder

### 12.1 输入层

Proactive Agent使用text-only、provider-independent canonical context：

```text
trusted proactive decision instructions
current UTC/local time + timezone/DST snapshot
effective mode and non-secret policy summary
relationship level and contact/activity intervals
ordered candidate reasons
minimal typed evidence summaries
relevant accepted active memories
last proactive outcome metadata
budget/quiet/activity facts
```

不加入图片、API key、Telegram Session、Bot token、任意URL内容或全量聊天历史。

### 12.2 Reason data

每个reason block至少包含：

```text
occurrence short ID
reason_code
window start/end
importance
typed evidence reference/version
bounded display summary
source trust
current validity
```

Event title、intention content和memory excerpt仍是data，不是instruction。Forward、historical AI和proactive文本不能提升权限。

### 12.3 Relevant memory

只允许：

- accepted active current memory；
- evidence root current且未redacted；
- 与candidate topic/contact scope相关；
- structured最多8项，semantic最多4项；
- 单一active embedding space；
- 不读取candidate、forgotten、superseded、invalidated或quarantined内容。

### 12.4 Recent conversation摘要

Proactive Agent不读取完整recent transcript。它只接收：

```text
last meaningful activity timestamp/direction/source
last initiator
unresolved work flags
last proactive timestamp/result
bounded recent topic summary if current and non-quarantined
```

是否适合发消息由deterministic 30分钟activity gate决定，不能让模型覆盖。

### 12.5 Manifest

Builder写`purpose=proactive_decision`的context manifest，记录：

```text
candidate/occurrence IDs and versions
policy/settings/relationship/timezone snapshots
budget bucket snapshots
selected memory/summary source IDs
freshness and embedding space
token estimates and omissions
prompt/builder/retrieval/adapter/capability versions
```

相同sealed candidate和版本必须产生相同ordered manifest hash。

## 13. Proactive Agent contract

### 13.1 Strict output

```json
{
  "schema_version": 1,
  "action": "send_now",
  "decision_code": "timely_support",
  "selected_occurrence_ids": ["occ-short-id"],
  "topic": "job_interview_support",
  "priority": 0.82,
  "defer_until": null
}
```

或：

```json
{
  "schema_version": 1,
  "action": "defer_once",
  "decision_code": "better_later_in_window",
  "selected_occurrence_ids": ["occ-short-id"],
  "topic": "job_interview_support",
  "priority": 0.78,
  "defer_until": "2026-08-11T07:30:00+08:00"
}
```

或：

```json
{
  "schema_version": 1,
  "action": "none",
  "decision_code": "not_natural_now",
  "selected_occurrence_ids": [],
  "topic": null,
  "priority": 0.0,
  "defer_until": null
}
```

### 13.2 字段约束

- `action`只允许三值。
- `decision_code`来自versioned allowlist，不接受自由chain-of-thought。
- selected IDs必须属于candidate sealed membership，顺序和数量有上限。
- `topic`是长度受限的主题code/brief，不是最终消息、命令或数据库query。
- `priority`必须为有限`[0,1]`，只供排序/审计，不能提升权限。
- `defer_until`只有`defer_once`可设置，必须是带offset时间并映射到同一候选UTC窗口。
- extra field、Markdown fence、解释文本、tool call和非有限数值全部拒绝。

### 13.3 应用层验证

模型返回后重新验证：

1. candidate仍为相同generation和membership hash；
2. occurrence/evidence仍current且窗口未结束；
3. selected reason相容且至少一个；
4. action与字段组合合法；
5. defer尚未使用、时间在window内且不违反absolute deep quiet；
6. mode/control/policy变化没有使candidate失效；
7. 输出不包含secret、任意endpoint/header/tool或最终发送命令。

验证失败是model output failure，不把原始output发送给联系人或管理员。

### 13.4 模型职责边界

Proactive Agent可以判断：

- 候选主题现在是否自然；
- 多个有效reason中哪个最适合作为单一topic；
- 在既有window内现在发送还是延期一次；
- 返回可解释的稳定decision code和priority。

它不能：

- 选择candidate之外的联系人/evidence；
- 创建新的event、promise、memory或relationship fact；
- 修改quiet hours、预算、mode或policy；
- 编写最终Telegram正文；
- 请求tool、URL、文件、数据库或Telegram API；
- 安排window之外或任意未来的发送；
- 直接声明quiet bypass已获准。

## 14. `send_now`、`defer_once` 与 `none`

### 14.1 `send_now`

Decision提交后进入preliminary authorization；通过才可以hold budget并启动Main AI。模型action本身不消耗quota。

### 14.2 `defer_once`

`defer_until`必须：

```text
now < defer_until < candidate.window_end_at
defer_count = 0
not inside absolute 00:00-07:00 deep quiet
```

提交时设置`defer_count=1`并创建stable due job。到期后不再次调用Proactive Agent；系统重跑全部deterministic/preliminary gates，再决定是否进入Main AI。任何gate失败都skip，不自动寻找第二个时间。

如果defer target因timezone/policy变更或DST重算不再合法，旧decision终止；新candidate只能来自重新物化的新occurrence generation。

### 14.3 `none`

`none`使candidate和本次membership occurrence在该window内进入terminal evaluated-none。之后的15分钟tick不能重复调用模型。只有新增未评估occurrence或新的当地日relationship window才能形成新candidate。

### 14.4 Provider失败

Transport、timeout和明确retryable provider错误可以在同一logical run下有限重试，必须复用candidate/manifest/config/prompt/version snapshot。Schema、tool call、length、malformed和`none`不是可重试理由。达到上限后candidate `failed_model`，不自动发送固定替代文本。

## 15. Preliminary authorization 与 budget hold

### 15.1 目的

在支付Main AI成本和产生draft前先获得短期、可恢复的授权快照，并为并发budget占位。它不是最终send gate。

### 15.2 检查

按固定锁顺序验证：

```text
account control/global proactive policy
contact/settings/relationship snapshot
conversation mode/content/activity
candidate/decision/window/evidence
account daily bucket
contact daily bucket
optional bypass bucket
```

必须满足：

- AUTO或COPILOT仍允许proactive；
- HUMAN、temporary HUMAN、PAUSED、maintenance、BLOCKED均不存在；
- 30分钟activity窗口和conflicting work门禁通过；
- quiet rule或exact bypass eligibility通过；
- global/contact/bypass容量和minimum interval通过；
- candidate、decision、evidence、policy和timezone snapshots仍current。

### 15.3 Reservation

同一事务：

1. 锁定/创建account当地日bucket。
2. 锁定/创建contact当地日bucket。
3. quiet bypass时锁定/创建contact bypass bucket。
4. 重新计算`committed + held < limit`。
5. 创建唯一reservation并将对应bucket `held_count + 1`。
6. 保存authorization版本、deadline和outbox。
7. commit。

失败时不调用Main AI。AUTO reservation上限10分钟，COPILOT上限30分钟，且均取candidate/draft deadline与Operations上限的较小值；每分钟reaper只能释放可证明没有副作用的held reservation。

### 15.4 Minimum interval

Minimum interval从最近一次以下时间的较晚者计算：

```text
last reconciled proactive side effect
last send_unknown proactive RPC start
last partial proactive group first side effect
```

普通reactive AI或human outgoing由30分钟activity gate处理，不重置relationship-specific proactive interval以外的历史事实，但仍阻止当前插话。

## 16. Main AI 最终生成

### 16.1 输入边界

Proactive Agent 的decision不是可发送正文。通过preliminary authorization并取得reservation后，worker为`main_ai`创建独立run，使用`purpose=proactive_final`的text-only Context Builder：

```text
trusted proactive framing
selected reason/topic and typed evidence summary
identity/personality policy
current relationship/time snapshot
accepted active structured memory
small semantic memory set
bounded recent committed text history
last reconciled proactive outcome
```

V1不包含current reactive turn、图片、历史media、未确认outgoing、candidate/quarantined memory、被删除正文或Proactive Agent原始自由文本。默认上限为8,000 input tokens、8条structured memory、4条semantic memory和12条recent messages；这些值进入policy/context version，可在后续验证后调整。

Manifest必须列出每个part的canonical ID、revision/version、source actor、trust class、token estimate和选择理由。最终消息只可围绕selected occurrences；Main AI不能更换联系人、扩展到未经证据支持的事件或要求执行tool。

### 16.2 输出契约

输出必须是适合一条主动对话的非空纯文本。禁止tool/function call、reply target、附件、按钮、内部reason、JSON控制字段和“系统决定主动联系”等实现泄露。长度超限、截断、空白、malformed或provider moderation拒绝都不能发送部分结果。

最终delivery group引用生成正文的`main_ai` run；`proactive_decision`单独引用前序`proactive_agent` run。两者不得混用。

## 17. Conversation mode 与主动行为

| effective state | Proactive/ Main AI | 最终行为 |
|---|---|---|
| `AUTO` | 允许在全部gate后运行 | final gate通过后自动发送 |
| `COPILOT` | 允许在全部gate后运行 | 只创建`draft_kind=proactive`草稿，等待管理员审批 |
| `HUMAN` | 不运行 | candidate按当前窗口skip，不补发 |
| temporary HUMAN | 不运行 | 与HUMAN相同 |
| `PAUSED` | 不运行 | 不补跑错过窗口 |
| maintenance/BLOCKED | 不运行 | fail closed，不形成正文或backlog |

COPILOT草稿继续占用reservation，但不算committed发送；管理员Send/Edit成功进入第一项Telegram副作用时才commit quota。Ignore、expiry或在副作用前失效释放reservation。批准后的source为`copilot_approved`，同时保留`proactive_decision_id`和是否编辑的provenance。

同一conversation已存在active reactive/proactive draft时，不创建第二个草稿队列。candidate以`skipped_conflicting_draft`结束；恢复后不补跑。

## 18. Quiet hours 与受限例外

### 18.1 默认窗口

联系人当地时间默认quiet hours为`22:00-08:00`。联系人可以关闭proactive或配置更严格的quiet窗口；放宽默认窗口属于管理员明确修改的敏感配置，必须版本化和审计。时间判断使用第8节解析出的IANA timezone与当地日。

普通candidate在quiet hours内不调用任何模型；如果其window跨过`08:00`，创建/更新同一occurrence的due job到下一个合法时点。window在此前结束则`skipped_quiet_hours`，不补发。

### 18.2 例外资格

只有同时满足以下条件才可绕过普通quiet hours：

```text
reason in {event_upcoming, promise_due}
importance >= 0.90
evidence is explicit and current
latest useful time < next local 08:00
local time in [22:00, 24:00) or [07:00, 08:00)
contact bypass bucket has capacity
all other gates pass
```

`relationship_reconnect`、`event_followup`、`explicit_followup`和一般关系维护永不绕过quiet hours。模型priority不能取得或扩大绕过资格。

### 18.3 绝对禁发区与每日限制

`00:00-07:00`是absolute no-send window；任何reason、importance、管理员普通policy或模型decision都不能绕过。此边界在DST fold/gap中按第8节的fail-closed规则解释。

每个联系人每个联系人当地日最多一次quiet bypass。绕过发送同时占用account daily、contact daily和contact bypass三个bucket；任何一个不足都skip。记录`quiet_bypass_used=true`及资格rule codes，不保存chain-of-thought。

## 19. 预算、关系等级与计费

### 19.1 默认限制

| relationship level | contact daily limit | minimum interval | reconnect threshold |
|---|---:|---:|---:|
| `close` | 2 | 6小时 | 3天 |
| `friend` | 1 | 12小时 | 7天 |
| `acquaintance` | 1 | 24小时 | 默认不启用reconnect |
| `unknown` | 1 | 24小时 | 默认不启用reconnect |

Deployment默认account global daily limit为10。`proactive_enabled=false`是立即硬开关；contact override可以收紧或在管理员明确操作下调整限制，但不能绕过absolute no-send、mode、account control、activity、证据或幂等gate。

Contact daily与bypass bucket按联系人当地日计数；account global daily bucket按账号timezone当地日计数。各bucket保存timezone snapshot和精确UTC边界，timezone变化不重切已存在bucket。

### 19.2 Reservation状态

```text
held -> committed
held -> released
held -> expired
held -> send_unknown
```

- Telegram第一项可观察副作用发生后commit；多段消息只计一次。
- 明确在副作用前失败、candidate stale、草稿Ignore/expiry时release。
- RPC结果未知或已发生部分发送时按一次已用额度保守计费，不自动重试整条消息。
- `expired`只能由reaper在确认没有active draft、send lease或未知副作用后释放；否则进入人工/reconciliation路径。

所有bucket更新使用数据库行锁/CAS，不能先查询余量再异步扣减。

## 20. 活跃会话与冲突工作

在preliminary和final gate都检查最近30分钟meaningful chat activity：

- contact incoming；
- 已对账的human/AI/COPILOT/proactive outgoing；
- active incoming debounce/turn/model run；
- active COPILOT draft、delivery group或send reconciliation；
- human takeover、edit/delete引起的未完成失效工作。

reaction和service metadata本身不刷新30分钟窗口。无正文的技术重试也不算meaningful activity。

出现任何新conversation content/activity revision时，未产生副作用的proactive generation/draft授权变stale：best-effort cancel，丢弃late result，释放reservation并终止，不把该主动主题并入reactive turn。第一项主动发送副作用之后到达的incoming属于下一turn，不能撤销已发生的消息，只能阻止未发送的剩余chunks并进入partial reconciliation。

## 21. 幂等键

幂等键均由deployment secret下的HMAC-SHA-256生成，输入使用长度前缀canonical encoding和版本号，禁止拼接含正文的可猜字符串：

| 对象 | 稳定输入 |
|---|---|
| rule occurrence | account/contact/reason/source object/source version/window generation |
| candidate | account/contact/sorted occurrence keys/membership hash/policy version |
| Proactive Agent decision | candidate key + candidate generation + proactive profile/config/prompt version |
| one-time defer job | decision key + defer ordinal `1` + scheduled UTC |
| budget reservation | decision key + authorization generation |
| proactive final run | decision key + context manifest fingerprint + main profile/config/prompt version |
| COPILOT draft | decision key + draft generation |
| outbound delivery group | decision key + final output fingerprint + mode generation |

重试复用logical identity；输入、成员、配置或授权版本变化必须创建新generation，不得覆盖旧记录。Database unique constraint是最终防线，应用锁不是唯一正确性来源。

## 22. Final send gate

### 22.1 唯一副作用所有者

`app`是最终gate和Telethon side effect的唯一所有者。worker只能提交带decision/reservation/run/version references的send或draft request。

### 22.2 锁与检查顺序

`app`按以下固定顺序取得锁，避免与reactive、HUMAN takeover和Control command互锁：

```text
account control
conversation
proactive decision/reservation
outbound delivery group
```

在锁内重新读取而非信任worker payload：

1. account READY，global proactive enabled；
2. effective mode与请求路径一致；
3. contact enabled，timezone/policy/relationship snapshot current；
4. conversation `mode_version`、`content_revision`和activity revision未变；
5. quiet/absolute no-send/bypass重新计算仍通过；
6. occurrence/evidence仍active，window/deadline未过；
7. reservation仍held且所有bucket匹配；
8. 没有active turn/draft/group或human takeover；
9. Main AI run complete、schema合法、manifest与decision相符；
10. delivery group idempotency key尚无副作用。

任何变化在首个副作用前都`skipped_stale_final_gate`并释放reservation；不自动重跑Proactive Agent或Main AI。

### 22.3 原子创建与发送

AUTO在同一事务内提交final authorization、把reservation关联到唯一delivery group，并原子创建全部ordered intents/random IDs；事务提交后才调用Telegram。第一项RPC开始后进入Message Lifecycle的`send_started/send_unknown/partial`规则。

COPILOT在同一gate创建唯一草稿；管理员批准时再次执行等价final send gate，不能把旧草稿approval当作永久发送授权。

## 23. 状态机

### 23.1 Occurrence / candidate

```text
scheduled -> eligible -> grouped -> evaluated
    |           |          |-> expired
    |           |-> suppressed
    |-> invalidated

candidate:
open -> evaluating -> send_selected | deferred_once | evaluated_none
  |        |              |              |-> expired
  |        |-> failed_model
  |-> superseded/expired
```

### 23.2 Decision / delivery

```text
send_selected
  -> authorizing
  -> reserved
  -> generating
  -> send_ready | draft_ready
  -> sending/approved
  -> sent | send_unknown | partial | skipped | failed

deferred_once -> due -> authorizing | skipped
```

只有明确列出的transition可执行；terminal state不复活。每次transition写from/to、actor、reason code、expected version和时间。

## 24. Data Model 增量

Data Model必须补充以下受约束实体，字段与索引详见`03-data-model.md`：

- immutable `proactive_policy_versions`与contact override version；
- `proactive_rule_occurrences`及typed evidence；
- `proactive_candidates`与有序membership；
- expanded `proactive_decisions`与selected occurrence membership；
- account/contact/bypass `proactive_budget_buckets`；
- `proactive_budget_reservations`；
- stable due jobs、state transitions和outbox requests。

自由文本topic/brief属于敏感derived data，有限长度、有限保留且不进入普通日志。规则值、ID、hash和reason code可用于长期审计，但contact purge/account wipe仍必须沿evidence graph清除或不可逆redact。

## 25. 事务、租约与并发

- Scheduler singleton lease只减少重复扫描，不承担正确性。
- due job claim使用`FOR UPDATE SKIP LOCKED`与短lease；lease过期可重领。
- occurrence materialization、candidate membership和decision generation各有unique constraint。
- 同一contact/conversation同时最多一个active proactive authorization；partial unique约束兜底。
- budget bucket锁顺序固定为account、contact、bypass；多个candidate竞争时只有一个能hold最后额度。
- final gate与reactive发送共用conversation lock和outbound group uniqueness。
- 所有跨进程请求使用transactional outbox/inbox，投递语义为at-least-once，消费必须幂等。

## 26. 重试、崩溃恢复与对账

| 失败点 | 恢复行为 |
|---|---|
| occurrence/candidate事务前 | 下次due/tick重新物化同一key |
| Proactive Agent调用中 | 同logical run有限重试；过窗即expire |
| reservation提交后、Main AI前 | lease恢复后复用reservation或安全release |
| Main AI返回前崩溃 | 复用run/manifest；late result仍过final gate |
| draft创建后 | Control Bot恢复active card；expiry释放reservation |
| group提交、RPC前 | app按group恢复发送 |
| RPC未知/部分成功 | Message Lifecycle对账；保守计一次，不重发已确认ordinal |
| policy/timezone/mode改变 | 未产生副作用的旧generation stale/skip |

补偿扫描只寻找仍在有效window内的eligible occurrence、到期defer和悬挂reservation。服务恢复不发送已错过的“问候积压”。无法证明没有副作用的reservation不自动释放为可重用额度。

## 27. 安全、隐私与审计

每次candidate/decision至少记录：

```text
account/contact/conversation IDs
reason codes and occurrence IDs
typed evidence IDs and versions
policy/timezone/mode/content/activity snapshots
candidate membership hash
Proactive and Main AI run IDs/config/prompt versions
decision action/topic hash/priority/defer time
quiet/bypass/budget results and reservation
final gate result/outbound group or draft
terminal outcome and timestamps
```

不记录chain-of-thought、API key、Authorization header、完整provider raw body或普通日志中的消息/记忆正文。管理员可通过Control Bot查看metadata summary和受限证据摘要；正文预览沿Context Contract的一次性确认边界。

Proactive输入属于私密联系人行为、关系、事件、承诺和记忆的派生处理。deployment disclosure必须明确AUTO可在同一Telegram身份下主动发送、COPILOT需批准、默认频率/quiet限制、有限quiet bypass以及模型/时区/证据错误仍可能导致不合时宜消息。

## 28. Control Bot 管理面

建议命令面：

```text
/proactive status [contact]
/proactive on|off <contact>
/proactive limits <contact>
/proactive limits_set <contact>
/proactive quiet <contact>
/proactive quiet_set <contact>
/proactive account_limits
/proactive account_limits_set
/proactive decisions [contact]
/proactive decision <id>
```

修改命令使用allowlisted admin、短期多步session、明确字段、范围预览和最终确认；每次写入新policy/settings version并使尚未发送的旧authorization失效。Bot不展示API key，也不允许通过自由文本JSON注入任意规则。`/proactive off`可以立即取消未产生副作用的run/draft/reservation。

## 29. Metrics 与告警

至少暴露低基数指标：

- occurrence/candidate数量及按reason的terminal result；
- no-candidate ticks与实际模型调用比率；
- decision action、schema failure、defer和expiry；
- quiet/activity/mode/budget/final-gate suppression；
- held/committed/send-unknown reservation与reaper age；
- proactive draft approval/edit/ignore/expiry；
- send success/unknown/partial和reconciliation lag；
- due/compensation scan lag、lease contention和dead letter。

contact、conversation、消息正文、topic和memory ID不作为metrics label。异常bypass率、send_unknown、悬挂reservation和错过scheduler窗口需要告警。

## 30. 自动化验收矩阵

后续Test Strategy至少实现：

| 场景 | 必须结果 |
|---|---|
| 无规则candidate的15分钟tick | 零模型调用、零decision、零发送 |
| due job与补偿扫描并发 | 同一occurrence/candidate仅一次评估 |
| worker crash/lease重领 | 重用idempotency key，不重复正文或发送 |
| DST gap/fold/timezone改变 | UTC窗口确定、旧generation失效、无双发 |
| `22:00-08:00`普通candidate | 不调用模型，合法则延期到08:00 |
| 0.90显式event于23:00且08:00前失效 | 可占用一次bypass并继续 |
| 相同candidate于03:00 | absolute no-send，不能绕过 |
| reconnect于23:00 | 永不绕过quiet hours |
| 两worker竞争最后daily quota | 仅一个reservation成功 |
| send unknown后重试candidate | 已计额度且不重发完整group |
| 30分钟内有incoming/human outgoing | candidate被抑制，零模型调用 |
| HUMAN/PAUSED/BLOCKED | 零Proactive/Main模型调用、零backlog |
| COPILOT | 仅一个草稿；批准时重跑final gate |
| decision后新incoming/mode change | late generation丢弃、reservation释放 |
| `none`后的重复tick | 同一window不再调用模型 |
| `defer_once`到期 | 不再次调用Proactive Agent，只重跑gate一次 |
| evidence edit/delete/forget | final gate拒绝旧decision，派生记录失效 |
| 图片仅存在于近期上下文 | proactive requests仍为text-only |
| group首段后新incoming | 不重复已发段，剩余段进入partial规则 |

模型fixture必须验证strict schema和完整canonical request；PostgreSQL integration test必须验证unique、partial unique、row lock、CAS、outbox、reservation和崩溃恢复，不能只用mock代替。

## 31. 后续文档边界

- Operations固定worker concurrency 2、job lease 60秒/20秒renew、最多5 executions、Proactive Agent 60秒/3 attempts、terminal topic/brief 30天、disk/backup/restore/metrics/alerts，见`docs/architecture/08-operations.md`。
- Test Strategy实现第30节的时钟、provider、Telegram、并发和数据库测试。
- 实现阶段可以调优阈值，但必须通过新policy version和回归数据，不得静默改变本文语义。

## 32. 完成检查表

- [x] Hybrid due job与15分钟补偿扫描已定义。
- [x] 明确reason allowlist、窗口、证据和无candidate零模型调用已定义。
- [x] Time Context、strict decision schema和Main AI text-only context已定义。
- [x] 默认quiet、有限高重要性例外和`00:00-07:00`绝对禁发已定义。
- [x] 关系级、全局、最小间隔、reservation与未知副作用计费已定义。
- [x] 30分钟activity、mode、草稿和真人接管抑制已定义。
- [x] occurrence/candidate/decision/draft/group幂等与at-least-once恢复已定义。
- [x] preliminary authorization与`app`最终send gate已定义。
- [x] 审计、隐私、Control Bot、metrics和自动化验收矩阵已定义。
