# V1 Implementation Plan

## 1. 状态与使用方式

本文把已完成的总体设计、九篇详细架构和ADR转换为首轮实现工作包。M0—M5已经关闭；M4补强由签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`及GitLab pipeline [#2758187631](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758187631)验证。M5的Windows static/unit/property/contract与synthetic image门禁已经通过，精确签名提交的GitLab disposable-service/migration/acceptance证据将在本阶段推送后补齐。当前进入M6 Memory、Summary与Embedding Pipeline。Telegram/provider live、部署和production load仍为`NOT RUN`。

本文定义milestone的范围、边界和退出目标；根目录的[开发执行清单](../TODO.md)提供稳定issue ID、逐项依赖、验证要求和实时完成状态。

实现必须按依赖顺序推进。每个milestone只有在代码、migration、测试、文档和证据同时满足时才完成；不能先开放真实Telegram自动发送，再补幂等、mode gate或恢复测试。

## 2. 全局交付规则

每个milestone必须：

1. 只实现该milestone明确范围，保留未来接口但不提前实现未授权行为。
2. 更新受影响的架构、ADR、Disclosure、acceptance traceability和deployment manifest schema。
3. 使用synthetic fixture；不提交Session、API key、Bot token、真实私聊或生产配置。
4. 运行适用的unit/property/contract/integration门禁，并将未执行项标`NOT RUN`或`BLOCKED`。
5. 创建GPG签名commit；公开push前单独执行dual-use release review。
6. 保持`main`可构建、可迁移且默认不能连接真实账号或自动发送。

## 3. 目标代码布局

首个脚手架应建立：

```text
src/telegram_userbot/
  processes/
  domain/
    messaging/
    conversation/
    memory/
    context/
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
alembic/
tests/
deploy/
```

依赖方向保持`processes -> adapters + application -> domain`。Domain不导入Telethon、Bot framework、SQLAlchemy、Redis client或provider SDK。

## 4. Milestone总览

| Milestone | 目标 | 允许的外部副作用 | 状态 |
|---|---|---|---|
| M0 | 工程脚手架与测试基础 | 无 | COMPLETE — WINDOWS/GITLAB LINUX PASS |
| M1 | PostgreSQL/Redis与核心持久化 | 仅测试容器 | COMPLETE — WINDOWS/GITLAB LINUX PASS |
| M2 | 模型配置、adapter与key-only控制面 | 仅local fake | COMPLETE — WINDOWS/GITLAB LINUX PASS |
| M3 | Telegram ingest/outbound intent基础 | fake Telegram；隔离smoke可选 | COMPLETE — WINDOWS/GITLAB LINUX PASS |
| M4 | Conversation Orchestrator与Main AI | fake provider/Telegram | COMPLETE — WINDOWS/GITLAB LINUX PASS |
| M5 | Media与Context Contract | fake provider；测试图片 | COMPLETE |
| M6 | Memory/Summary/Embedding Pipeline | fake provider/embedding | IN PROGRESS |
| M7 | Proactive Pipeline与COPILOT主动草稿 | fake provider/Telegram | NOT STARTED |
| M8 | Production Compose、backup与运维加固 | 测试backup/alert targets | NOT STARTED |
| M9 | Release candidate验证 | 受保护live smoke与授权测试peer | NOT STARTED |

## 5. M0 — 工程脚手架与安全默认值

### 5.1 目标

建立可安装、可测试、可静态检查的Python项目和架构边界，不连接Telegram、provider或公网服务。

### 5.2 代码任务

- 固定CPython minor并建立`pyproject.toml`、hash-locked runtime/dev dependency。
- 创建`src/telegram_userbot`包与第3节目录，配置typing、format、lint和import boundary检查。
- 定义不含framework的核心ID、UTC timestamp、version、result/error和redaction类型。
- 定义Clock、MonotonicClock、RandomSource、IdFactory、UnitOfWork、Queue、TelegramGateway、ModelGateway ports。
- 建立structured logging API，默认拒绝正文/secret字段。
- 建立配置schema与production placeholder/default-secret拒绝；尚不接受真实credential。
- 建立pytest、pytest-asyncio、Hypothesis、coverage、marker和acceptance manifest schema。
- 建立synthetic fixture factory、sentinel scanner和四态结果模型。
- 建立最小CI：docs/static/unit/property/contract collection、coverage和secret scan。
- 创建架构依赖测试，阻止domain导入adapter/framework。

### 5.3 测试任务

- Clock/timezone/DST基础value object property tests。
- ID/version/hash稳定性与serialization round-trip。
- 日志字段allowlist与sentinel不泄露。
- Unknown pytest marker、required marker被deselect、dirty source manifest失败。
- Acceptance manifest JSON schema与`PASS/FAIL/NOT RUN/BLOCKED`语义。

### 5.4 完成门禁

- Fresh checkout可在支持的开发环境建立`.venv`并以lockfile安装。
- Unit/property/contract collection通过；coverage门禁开始生效。
- Production entrypoint不存在真实连接路径，默认运行不会发送网络请求。
- CI artifact扫描无secret/正文；M0 evidence manifest绑定签名commit。

### 5.5 完成证据

- 标准GIL CPython 3.14，Windows本地patch为3.14.7；pip 25.3、Hatchling与全部工具由hash lock固定。
- 全新隔离`.venv`按bootstrap/dev lock安装、editable package安装、`pip check`和import均`PASS`。
- 56个unit/property/contract tests为`PASS`；line 97.77%，branch 90.32%。
- Ruff format/lint、strict mypy、compileall、AST import boundary、wheel/sdist Disclosure和secret/artifact scan均`PASS`。
- 签名提交`5e6f2b3512436a5ba70c958a42901b920ffa6caa`的GitLab Linux pipeline #2与`m0-preflight`作业均为`PASS`，使用CPython 3.14.7。
- GitLab acceptance manifest绑定相同commit和tree，M0-001—M0-012全部为`PASS`，且记录`external_service_access=false`。
- Telegram、provider、PostgreSQL、Redis、应用container和production evidence保持`NOT RUN`。

## 6. M1 — Data Model、migration与durable work

### 6.1 目标

建立PostgreSQL/pgvector、Redis/arq和Alembic基础，使canonical状态、outbox、job、lease和erasure可在真实数据库验证；仍不连接Telegram。

### 6.2 代码任务

- 固定PostgreSQL/pgvector/Redis compatibility set与Testcontainers image digest。
- 实现Data Model第一批migration：account/peer/conversation、message event/revision、mode/control、job/outbox、audit/erasure基础表。
- 实现SQLAlchemy mapping/repository、UnitOfWork和数据库角色边界。
- 实现outbox publisher、PostgreSQL job lease/CAS、Redis notification和loss recovery。
- 实现account/session advisory lock abstraction，不读取Session文件。
- 实现one-way redaction primitives与erasure ledger outbox。
- 建立empty→head、interrupted backfill skeleton与schema compatibility check。

### 6.3 测试任务

- Composite FK、partial unique、CHECK、immutable version、CAS和advisory lock。
- PostgreSQL transaction/deadlock/serializable retry；多连接可见性。
- Redis丢失、重复enqueue、lease过期和旧owner迟到完成。
- Restricted DB roles与export view deny-by-default。
- Delete/redaction后query不可见且旧revision不复现。

### 6.4 完成门禁

- Schema只由Alembic建立；SQLite/mock不计入数据库证据。
- Redis清空后pending work从PostgreSQL重建，无重复domain completion。
- Migration、constraint和role acceptance全部PASS。
- 尚无Telegram、provider或credential写入能力。

### 6.5 完成证据

- Windows/CPython 3.14.7 上86个unit/property/contract测试为`PASS`，10个需要Docker的integration/recovery测试为`NOT RUN`；Ruff、strict mypy、import boundary、coverage、build、Disclosure与secret/artifact扫描均为`PASS`。
- 签名提交`9c2dbf61c8b67e75182f47abbb419ae82773678a`对应的GitLab Linux pipeline [#9](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2747812423)为`PASS`；`m0-preflight`和`m1-postgres-redis`分别在CPython 3.14.7运行成功。
- Linux作业收集的96个测试全部`PASS`，覆盖digest-pinned PostgreSQL 17.10/pgvector 0.8.6、Redis 8.2.8、migration、角色、约束、CAS/lease/fencing、outbox恢复、one-way redaction和synthetic EXPLAIN。
- M1 acceptance manifest绑定commit `9c2dbf61c8b67e75182f47abbb419ae82773678a`与tree `ef98f76a5bf223d41d86f86f9b7be5b3514f8227`，M1-001—M1-012全部为`PASS`。
- Migration manifest记录revision `0001_m1_durable_state`、23张表、零匿名约束、empty→head、head→base→head和中断恢复为`PASS`；首个baseline的previous supported→head为`NOT_APPLICABLE_BASELINE`。
- Windows真实PostgreSQL/Redis、Ubuntu production、backup/restore和production load仍为`NOT RUN`，没有用Linux disposable integration或1000行synthetic EXPLAIN替代这些证据。

## 7. M2 — Model配置、protocol adapter与key-only控制面

### 7.1 目标

实现三个generation profile、独立embedding profile、canonical adapter、credential envelope和Control Bot/Web App配置边界，外部请求仅发往local fake。

当前状态：Windows static/unit/contract与GitLab disposable PostgreSQL/Redis migration、受限DB role、Linux Chromium browser和M2 acceptance均已通过，本milestone已经关闭。

### 7.2 代码任务

- 实现model endpoint/profile/draft/config/credential/capability/prompt version表与事务。
- 实现Responses、Chat Completions、Messages和Embedding canonical mapping；明确拒绝legacy `/completions`。
- 实现AES-256-GCM envelope、AAD、keyring generation、rotation与delete。
- 实现endpoint canonicalization、public/private allowlist、DNS/IP/TLS/redirect/proxy策略。
- 实现Control Bot非secret配置command/session和`/models`查询。
- 实现Caddy后的key-only Web App server、`initData`和one-time launch token。
- 实现API key set/replace/delete、无读取API和不回显响应。
- 实现provider HTTP fake、wire fixture和Web App browser harness。

### 7.3 测试任务

- 三profile独立draft/validate/activate与in-flight snapshot。
- 三协议text/image/structured/stream/error wire contract。
- Auth tamper/expiry/future/replay/admin/role/action/deployment CAS。
- Credential nonce/AAD/rotation/delete/missing-key、日志与artifact sentinel scan。
- SSRF loopback/private/metadata/IPv6/rebind/redirect/proxy/private CA。

### 7.4 完成门禁

- Web App不能修改endpoint/model/parameter，Bot消息不能接收API key。
- Provider fake之外的网络默认blocked；真实provider smoke仍`NOT RUN`。
- Auth、credential、SSRF和wire contract为release-critical，不得quarantine。

### 7.5 完成证据

- 签名提交`def4ff1f846307a7ea428de3c048616601cab7a4`对应的GitLab Linux pipeline [#2748486868](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2748486868)全部`PASS`。
- Linux/CPython 3.14.7在digest-pinned PostgreSQL 17.10/pgvector 0.8.6和Redis 8.2.8上运行157个测试，零失败、零跳过；line coverage 92.11%，branch coverage 81.61%。
- Migration manifest绑定Alembic head `0002_m2_model_control`并记录34张表、零匿名约束与四条migration路径`PASS`；Chromium 151.0.7922.34 browser manifest记录零外部请求、零存储、无credential echo。
- M2 acceptance manifest绑定tree `afca1b77506911d82f2424be8bf5b686ce4cf4ce`，M2-001—M2-011全部`PASS`；真实Telegram/provider和Ubuntu production仍为`NOT RUN`。

## 8. M3 — Telegram event ingest、source与outbound intent

### 8.1 目标

实现Telethon adapter边界、canonical event/revision、图片metadata、outbound group/intent/random ID与reconciliation；默认使用fake Telegram。

### 8.2 代码任务

- 实现Telegram user gateway port与Telethon adapter normalization。
- 实现private peer范围、incoming/outgoing/edit/delete/reaction/service/album摄取。
- 实现message business key、update fingerprint、revision/tombstone和source projection。
- 实现outbound delivery group、ordered intent、stable random ID与send attempt。
- 实现human/system pending/AI来源匹配和startup reconciliation。
- 实现read/typing port，但尚不由Orchestrator自动触发。
- 实现Telegram fake、replay fixture与命名send crash point。

### 8.3 测试任务

- Duplicate/late/out-of-order/update gap、album顺序、edit/delete。
- Human outgoing与system pending source reconciliation。
- Send success/FloodWait/RPC unknown、partial group和restart。
- 同intent random ID稳定、confirmed chunk不重发。
- Unsupported peer不持久化正文或触发模型。

### 8.4 完成门禁

- 默认entrypoint不加载真实Session；fake全矩阵PASS。
- 若执行真实Telegram smoke，只允许隔离测试账号/peer并单独记录。
- 仍没有AUTO生成或主动消息能力。

### 8.5 完成状态

- 已固定Telethon 1.44.0并实现注入client的gateway；默认入口不创建client、不读取Session、不访问Telegram。
- 已实现private 1:1事件规范化、fingerprint去重、revision/tombstone、album/media metadata、outbound group/intent/attempt、稳定random ID、来源核对、read high-watermark和typing lease。
- deterministic fake覆盖success、FloodWait、transient、permanent、unknown-before/after-accept与同random ID去重；disposable PostgreSQL测试覆盖重复/乱序/delete-first、partial、crash-after-send、role和M4 staged FK边界。
- Windows Ruff、strict mypy与179个默认测试为`PASS`，line coverage 91.41%、branch coverage 81.13%；本机没有Docker daemon，PostgreSQL/Redis integration为`NOT RUN`。
- 签名提交`41f4160a6d53bdd34e2654f08a90a4b61b6675e8`对应的GitLab pipeline [#2751916211](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2751916211)全部通过；Linux服务集成执行202个测试，4条migration路径、12项replay与M3 acceptance全部`PASS`。真实Telegram仍为`NOT RUN`。

## 9. M4 — Conversation Orchestrator与Main AI

当前状态：COMPLETE。domain、PostgreSQL migration/repository、fake runtime、durable Control Bot backend、race/integration测试和content-free evidence writer已经实现。race/acceptance从JUnit stable test ID派生，command/outbox→app executor权限边界已完成；签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`的GitLab pipeline [#2758187631](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758187631)九个作业全部`PASS`，M4 service job为258 passed、1 deselected，line/branch coverage 92.52%/84.50%。

### 9.1 目标

实现AUTO/HUMAN/COPILOT、overlay、turn/debounce、Main AI run、final gate和COPILOT reactive draft。

### 9.2 代码任务

- 实现effective mode解析、account/control/mode/content version与operational block。
- 实现sliding 3秒debounce、10秒hard cap和conversation lock。
- 实现3秒conditional supersede、best-effort cancel与late result discard。
- 实现AUTO read/typing、Main AI logical run/attempt和response normalization。
- 实现final gate、group/intent原子创建与长文本splitter。
- 实现HUMAN outgoing takeover、temporary HUMAN、pause/maintenance/BLOCKED。
- 实现manual `/draft`、edit/approval/ignore/expiry和`copilot_approved`。
- 实现`/reply_pending`显式范围，不自动补backlog。

### 9.3 测试任务

- Mode priority/state machine、version CAS和全race矩阵。
- 3秒内/外supersede、edit-before/after-send、delete。
- Human/model/mode/send并发与每个crash point。
- COPILOT token/revision/approval/send unknown。
- Splitter grapheme、partial delivery与random ID。

### 9.4 完成门禁

- Fake环境下无未经授权或重复发送。
- HUMAN/PAUSED/maintenance/BLOCKED恢复不补backlog。
- Send/mode/idempotency关键测试不能quarantine。
- 真实账号AUTO保持disabled，除非M9批准。

## 10. M5 — Media与Context Contract

### 10.1 目标

实现安全图片摄取、context manifest、budget/retrieval/trust、三协议图片与`/context`管理面。

### 10.2 代码任务

- 实现20 MiB/40 MP/16384/timeout/并发1媒体pipeline和private volume。
- 实现original/provider copy、EXIF处理、hash、TTL与幂等cleanup。
- 实现context layer、24,000默认预算动态收紧、token estimator和omission。
- 实现structured/vector/recent selection、exact rerank、去重与stable tie-break。
- 实现instruction/data content part和prompt injection隔离。
- 实现manifest snapshot/hash与三协议image mapping/detail auto。
- 实现metadata-only`/context`与二次确认`/context_preview`。

### 10.3 测试任务

- 图片magic/MIME/炸弹/边长/timeout/symlink/quota/TTL。
- Budget、selection、memory lag、manifest重建与injection corpus。
- Responses/Chat/Messages图片wire fixtures。
- Preview replay/越权/source redaction/send unknown/delete failure/log scan。

### 10.4 完成门禁

- Media无公共route、control无二进制访问、backup不含media。
- 被删source不进入context；模型无能力时fail closed。
- Context同输入/版本可重建，全部item有source/trust。

当前状态：COMPLETE。实现覆盖20 MiB/40 MP/16384 px图片校验、private 0700/0600 media storage、10 GiB quota与TTL cleanup、动态context预算、稳定selection、instruction/data隔离、content-free manifest、Responses/Chat Completions/Messages图片映射，以及metadata-only `/context`和按钮回调二次确认的`/context_preview`。Windows本地static/unit/property/contract与synthetic image证据为`PASS`；本机无Docker，PostgreSQL/Redis migration/role integration为`NOT RUN`，精确签名提交的GitLab证据待推送后记录。

## 11. M6 — Memory、Summary与Embedding

### 11.1 目标

实现异步Memory proposal/validation、summary membership、embedding shadow space、candidate review与freshness降级。

### 11.2 代码任务

- 实现45秒quiet/三硬阈值OR/5分钟补偿与pending range merge。
- 实现Memory input manifest、strict output、proposal validator和事务操作。
- 实现source trust、confidence/importance、conflict与candidate。
- 实现rolling/daily/weekly summary source membership与watermark。
- 实现embedding chunk/space、shadow rebuild和atomic switch。
- 实现candidate Control Bot review、accept/reject和forget。
- 实现edit/delete/forget/purge递归rebuild与one-way redaction。
- 实现fresh/degraded/stale和Main AI recent-window扩展。

### 11.3 测试任务

- Trigger/range/replay/lease/crash/dead-letter。
- Human/AI/proactive/COPILOT evidence与image-only candidate。
- Summary late event/timezone/edit/delete rebuild。
- Embedding dimension/space/isolation/switch。
- Restore erasure不复活derived data。

### 11.4 完成门禁

- Memory provider失败不阻塞Main AI或canonical ingest。
- Candidate/quarantine不进入context。
- AI/proactive不能自证联系人事实或human style。
- Forget/purge/restore erasure全部PASS。

## 12. M7 — Proactive Pipeline

### 12.1 目标

实现deterministic candidate、Time Context、Proactive Agent、预算reservation、Main AI proactive生成、AUTO final send与COPILOT approval draft。

### 12.2 代码任务

- 实现due job与15分钟compensation scan、occurrence和candidate membership。
- 实现reason allowlist、event/intention/relationship evidence和Time Context。
- 实现timezone/DST、quiet、absolute no-send和受限bypass。
- 实现account/contact/bypass budget、minimum interval与reservation。
- 实现Proactive Agent strict decision与`send_now/defer_once/none`。
- 实现Main AI text-only proactive context与mode mapping。
- 实现activity/draft/takeover suppression、final gate和unknown计费。
- 实现Control Bot proactive policy/settings version。

### 12.3 测试任务

- 无candidate零模型调用、due/compensation幂等。
- DST gap/fold、quiet肩部与绝对禁发。
- 并发budget/reservation/reaper/send unknown。
- Mode/activity/evidence stale与downtime无backlog。
- AUTO/COPILOT/HUMAN/PAUSED/BLOCKED行为。

### 12.4 完成门禁

- 模型不能发明candidate、reason或evidence。
- AUTO只有最终门禁后创建intent；COPILOT必须批准。
- Proactive默认使用保守预算，真实账号启用仍留到M9。

## 13. M8 — Production Compose与Operations

### 13.1 目标

实现Ubuntu 24.04 amd64 Compose部署、Caddy、secret、migration、backup、health、alert、retention、export和runbook。

### 13.2 代码/配置任务

- 固定application/PostgreSQL/Redis/Caddy/backup/export image digest和SBOM策略。
- 实现Compose networks、volumes、resource limits、healthcheck与stop grace。
- 实现root-controlled secret source与reader GID验收。
- 实现Caddy 443 key-only route、certificate流程与security headers。
- 实现Alembic one-shot、startup readiness与expand/migrate/contract runbook。
- 实现pgBackRest/WAL、Session restic、erasure ledger和restore gate。
- 实现retention cleanup、media quota、disk 70/80/90/95门禁。
- 实现JSON log、internal metrics、Control Bot与独立alert channel。
- 实现age data-export one-shot与SSH/SFTP-only retrieval边界。
- 实现upgrade/rollback/security patch/fresh install runbook。

### 13.3 测试任务

- Compose port/network/secret/read-only/capability/Docker Socket。
- SIGTERM/SIGKILL、dependency loss、restart与reconciliation。
- SSRF/TLS/Caddy、disk/quota/media、artifact secret scan。
- Empty/previous migration、compatible/incompatible rollback。
- pgBackRest/restic/erasure restore与actual RPO/RTO。

### 13.4 完成门禁

- 公网只有443和受限SSH；所有runtime secret最小挂载。
- Restore在maintenance下完成erasure和unknown-send对账。
- RPO/RTO只有实测达成后标PASS。
- 首次release前所有Operations critical测试无quarantine。

## 14. M9 — Release Candidate与首个部署门禁

### 14.1 目标

在精确候选commit/digest上完成公开发布与首个生产部署所需证据，不新增业务功能。

### 14.2 验证任务

- 执行全部commit、nightly/manual和operations-sensitive测试。
- 在隔离账号/peer执行真实Telegram/Web App smoke。
- 对三个generation协议与embedding实际启用项执行synthetic provider smoke。
- 执行backup/restore、upgrade/rollback和旧DB+最新erasure overlay。
- 在2 vCPU/4 GiB/40 GiB Ubuntu 24.04 amd64运行24小时soak。
- 扫描source、image、artifact、logs、metrics和backup metadata的secret/正文。
- 完成dependency/license/SBOM、image digest、commit signature和provenance核对。
- 更新README运行命令、部署runbook、Disclosure和实际限制。
- 对精确公开artifact和GitLab路径执行最终dual-use review。

### 14.3 完成门禁

- Required acceptance全部为PASS；任何required `NOT RUN`、`FAIL`或`BLOCKED`阻止对应发布声明。
- Source commit、image digest、test manifest、Disclosure和deployment manifest一致。
- 真实Telegram只允许管理员显式从安全mode启用AUTO/Proactive。
- 推送/发布后从公共consumer路径验证commit、签名、Disclosure和artifact digest。

## 15. 跨Milestone不可延后项

以下工作必须随首次相关代码一起交付：

| 首次出现 | 同步必须完成 |
|---|---|
| Sensitive字段 | 日志/metrics/artifact脱敏与sentinel测试 |
| 新表/列/约束 | Alembic、fresh migration、role与retention |
| 新domain状态 | 状态机、version/CAS与crash recovery |
| 外部请求 | fake/contract、timeout/retry、SSRF与Disclosure |
| Telegram副作用 | durable intent、random ID、final gate与reconciliation |
| 新Memory/Context source | provenance、trust、delete/forget传播 |
| 新后台任务 | durable job、idempotency、lease、dead-letter与metrics |
| 新public route | auth、Caddy、rate limit、browser E2E与release review |

## 16. 实现阶段的首个Issue列表

M0可以直接拆成以下有序issue：

1. `M0-001` 固定CPython与创建`pyproject.toml`/lockfile。
2. `M0-002` 创建package layout与import boundary rule。
3. `M0-003` 实现core IDs、UTC/version/result types。
4. `M0-004` 定义Clock/Random/ID/UoW/Queue/Telegram/Model ports。
5. `M0-005` 建立safe structured logging与sentinel scanner。
6. `M0-006` 建立typed config与production startup validation skeleton。
7. `M0-007` 配置pytest/asyncio/Hypothesis/coverage/markers。
8. `M0-008` 建立synthetic fixture factories和state-machine harness。
9. `M0-009` 定义acceptance manifest schema与traceability validator。
10. `M0-010` 建立无secret CI pipeline与artifact retention。
11. `M0-011` 建立docs/link/Disclosure/secret/signature release checks。
12. `M0-012` 汇总M0 evidence，更新README和M1启动条件。

Issue必须引用受影响的架构section、ADR和acceptance IDs，并写明明确不在本issue实现的行为。

## 17. 当前结论

M0—M5已经关闭，当前进入M6 Memory、Summary与Embedding Pipeline。真实Telegram/provider smoke继续保持`NOT RUN`，应用容器、真实AUTO和生产部署仍不存在。
