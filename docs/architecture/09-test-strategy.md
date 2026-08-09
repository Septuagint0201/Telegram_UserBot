# Test Strategy

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 的测试分层、工具、fixture、fake、真实外部服务边界、并发与崩溃注入、CI门禁、覆盖率、资源soak、恢复演练和证据契约。

总体设计见`docs/Design.md`；被验证的详细契约见`docs/architecture/01-runtime-topology.md`至`08-operations.md`。本文不重新定义业务行为；测试与架构冲突时，测试必须暴露冲突并由架构变更或ADR解决，不能静默把现有实现当成规范。

当前状态：V1实现前的测试架构基线。仓库尚无可运行实现，因此本文定义的runtime、integration、restore和soak结果均为`NOT RUN`，不能因文档静态检查通过而升级为`PASS`。

## 2. 已确认决策

| 主题 | V1决策 |
|---|---|
| Python测试栈 | `pytest`、`pytest-asyncio`、Hypothesis |
| 基础设施 | Testcontainers提供隔离PostgreSQL/pgvector与Redis；Docker Compose负责完整E2E |
| Telegram | fake/replay是自动门禁；真实Telegram只在专用测试账号和隔离主机人工运行 |
| 模型provider | 本地HTTP fake和固定wire fixture是自动门禁；真实endpoint仅做受保护的合成数据smoke |
| 测试数据 | 确定性合成数据；禁止生产数据库、真实私聊和生产Session进入测试 |
| 时间与并发 | 注入虚拟时钟、随机数和ID；使用显式barrier、state machine和命名crash point |
| CI分层 | 无secret MR preflight、受保护integration、nightly/manual、release/operations-sensitive四级 |
| Runner | 无secret任务使用普通runner；容器/恢复/soak使用受保护Ubuntu runner；真实Session只在隔离测试主机 |
| Coverage | 全局line不低于85%，branch不低于80%；安全关键不变量必须有正反例 |
| Flake | 门禁失败不能通过自动重跑变成PASS；quarantine必须有owner和期限 |
| Evidence | JUnit、coverage、acceptance manifest；普通证据30天，release/restore/soak证据365天 |

## 3. 目标、范围与非目标

### 3.1 目标

测试系统必须证明：

1. 纯领域规则在确定性输入下满足模式、记忆、上下文、预算和保留不变量。
2. 三种模型协议、Telegram adapter、Web App和持久化边界符合固定contract。
3. PostgreSQL约束、事务、CAS、锁、lease、outbox和erasure在真实数据库上成立。
4. 重复、乱序、并发、超时、取消、进程退出和结果未知不会产生未经授权的重复消息。
5. Redis、provider、Telegram、磁盘、证书、backup和进程故障有明确降级与恢复证据。
6. 生产基线在Ubuntu 24.04 amd64、2 vCPU/4 GiB RAM/40 GiB SSD上有资源与恢复证据。
7. 任何未执行的外部、平台或长时间测试都被明确标为`NOT RUN`或`BLOCKED`。

### 3.2 范围

本文覆盖：

- unit、property/state-machine、contract、integration、Compose E2E；
- migration、crash recovery、backup/restore、security、resource soak；
- fake Telegram、Bot、provider、embedding、clock、random和filesystem fault；
- 真实Telegram/provider的受控smoke边界；
- CI runner、secret、artifact、coverage、flake和evidence治理。

### 3.3 非目标

V1测试不：

- 使用真实联系人、生产私聊、生产数据库副本或个人日常Telegram账号；
- 让共享或fork pipeline取得Telegram Session、Bot token、provider key或backup credential；
- 以SQLite、内存字典或mock证明PostgreSQL并发/约束语义；
- 以测试重复运行后的偶然成功掩盖race；
- 将真实provider输出文本作为稳定golden；
- 声称穷尽Telegram、模型provider、Docker、Linux或网络的全部故障；
- 在宿主机root filesystem上制造真实磁盘耗尽；
- 把coverage百分比替代需求追踪、负向测试或恢复证据。

## 4. 风险驱动的测试层级

| 层级 | 主要对象 | 外部依赖 | 典型耗时 | 何时门禁 |
|---|---|---|---:|---|
| Unit | domain value/object/state transition/policy | 无 | 毫秒 | 每次提交 |
| Property/stateful | 顺序、幂等、预算、状态机 | 无或in-memory fake | 秒 | 每次提交/定期扩展 |
| Contract | Telegram/provider/Web App/serialization | 本地fake | 秒 | 每次提交 |
| Integration | PostgreSQL/pgvector/Redis/crypto/media | Testcontainers | 分钟 | 每次提交 |
| Compose E2E | 服务、网络、Caddy、signal、reconciliation | 完整Compose | 十分钟级 | nightly/manual与候选 |
| Migration/recovery | Alembic、crash、Redis loss | disposable stack | 十分钟级 | nightly/manual与相关变更 |
| Restore/DR | pgBackRest、restic、erasure overlay | 隔离Ubuntu/backup target | 小时级 | release/operations-sensitive |
| Live smoke | Telegram/provider真实协议 | 隔离授权资源 | 分钟级 | 人工受保护门禁 |
| Resource soak | 2/4/40生产profile | 专用Ubuntu主机 | 24小时 | 首次部署与资源敏感变更 |

低层测试用于快速定位，高层测试用于证明组合与平台边界。高层通过不能取消低层失败；fake通过也不能升级为真实外部服务`PASS`。

## 5. 计划中的测试目录

实现阶段使用以下语义布局：

```text
tests/
  acceptance/
  unit/
    domain/
    application/
  property/
  contract/
    telegram/
    providers/
    webapp/
  integration/
    postgres/
    redis/
    media/
    pipelines/
  e2e/
    compose/
    migration/
    recovery/
  operations/
    backup_restore/
    security/
    soak/
  fakes/
  fixtures/
    telegram/
    providers/
    images/
    migrations/
  support/
```

生产代码不能反向依赖`tests`。可复用fake只实现application port，不进入生产镜像；测试辅助函数不得成为绕过真实adapter或数据库约束的第二套业务实现。

## 6. 工具链与依赖固定

基础工具：

- [pytest](https://docs.pytest.org/en/stable/)：fixture、marker、参数化与执行；
- `pytest-asyncio`：asyncio生命周期与任务泄漏检查；
- [Hypothesis](https://hypothesis.readthedocs.io/en/latest/)：生成式与`RuleBasedStateMachine`测试；
- coverage.py/pytest-cov：line与branch coverage；
- [Testcontainers for Python](https://testcontainers-python.readthedocs.io/)：PostgreSQL/pgvector、Redis及局部依赖；
- Docker Compose：完整服务、网络、volume与signal行为；
- 浏览器自动化harness：key-only Web App的Chromium E2E；实现时固定driver/browser版本；
- JUnit XML与自定义acceptance manifest：证据交换。

全部test dependency进入hash-locked dev dependency set，镜像固定digest。CI禁止运行未固定的远端安装脚本。确切CPython minor、PostgreSQL/pgvector、Redis、Caddy和browser版本在实现compatibility set确定后写入deployment/test manifest；未固定前不能声称对应平台已验证。

## 7. Pytest marker与选择契约

至少注册：

```text
unit
property
contract
integration
compose
migration
recovery
security
live_telegram
live_provider
restore
soak
slow
```

Unknown marker使collection失败。默认本地命令只运行无secret、非live、非restore/soak集合；CI job使用显式marker表达其证据范围，不能通过文件名猜测。

每个测试必须有稳定test ID。Architecture acceptance使用`TS-<AREA>-NNN`，例如`TS-MSG-001`；参数化case追加可读case ID，不依赖执行顺序或随机collection位置。

## 8. 环境矩阵

| 环境 | 允许范围 | 可作为生产证据 |
|---|---|---|
| Windows开发机 | unit/contract；可选Docker integration | 否 |
| 普通Linux CI runner | 无secret static/unit/property/contract | 仅对应层级 |
| 受保护Ubuntu runner | Testcontainers、Compose、migration、recovery | 是，需环境manifest |
| 隔离Telegram测试主机 | 真实Telegram/Web App smoke | 是，仅外部协议项 |
| 2/4/40 Ubuntu 24.04 amd64 | restore/resource soak/upgrade | 是，需完整证据 |

Production support只承认Ubuntu 24.04 amd64。Windows或其他Linux结果用于开发反馈，不能替代生产Compose、signal、filesystem、backup或resource证据。

## 9. 测试数据与隐私

### 9.1 Synthetic-only默认

Fixture使用虚构账号、peer、消息、记忆、时间、图片和endpoint。稳定ID从测试seed派生，手机号、username、域名和文本使用明确保留的示例空间。不得从生产日志、数据库、Session、context preview或provider body复制内容。

### 9.2 Regression fixture例外

线上故障只能转化为最小、重新构造的合成fixture。若维护者判断必须保留输入形状：

1. 先移除正文、标识符、时间关联、媒体和credential；
2. 由第二人或自动privacy scanner复核；
3. 记录来源类别和sanitization review，不记录原值；
4. fixture仍不得可逆映射到真实联系人；
5. 未通过时以测试生成器复现，不提交样本。

### 9.3 Sentinel secret

安全测试使用带稳定前后缀的无效sentinel，例如`TEST_SECRET_DO_NOT_LOG_<random>`。结束后扫描日志、metrics、queue、DB audit、artifact和错误输出；任何完整或可认证片段出现即失败。Scanner自身输出只报告位置和hash，不回显sentinel正文。

## 10. 确定性时间、随机与ID

Application ports必须允许测试注入：

```text
Clock
MonotonicClock
RandomSource
IdFactory
TelegramRandomIdFactory
SchedulerWakeup
```

Unit/property/integration不使用不受控`sleep()`等待业务deadline。Virtual clock可以原子推进到debounce、quiet window、lease、retry、TTL、DST和retention边界。并发测试使用barrier/latch明确停在“事务已写入、RPC前、RPC未知、commit前”等位置。

Cryptographic correctness测试使用真实库与固定测试key；生产nonce仍来自CSPRNG。测试只允许在专用adapter边界注入确定nonce来验证wire/AAD，不能让生产配置启用确定nonce。

## 11. Telegram gateway fake

Fake实现application Telegram user gateway port，支持脚本化：

- incoming/outgoing/new/edit/delete/read/reaction/service update；
- duplicate、late、out-of-order和pts gap；
- private peer、unsupported peer、album/grouped ID；
- photo/image document metadata与分块下载；
- send success、FloodWait、retryable failure、terminal failure、RPC unknown；
- random ID查重、server message ID分配和history reconciliation；
- typing/read acknowledgement记录；
- disconnect、reconnect和Session unauthorized。

Fake记录typed call，不保存真实Session或access hash。测试可以断言调用顺序、参数hash、是否发生副作用以及cancel是否只为best effort，不能只断言最终返回文本。

## 12. 可重放Telegram fixture

Fixture采用versioned JSON/二进制最小envelope，至少保存：

```text
fixture_schema_version
synthetic account/peer/message IDs
update kind and Telegram ordering fields
grouped/reply/forward references
media descriptor and fixture hash
expected normalized event/revision/source
```

Fixture不得直接序列化未知版本Telethon对象作为唯一事实；adapter contract将synthetic wire-like input转成canonical event。每个fixture可以重复、重排或在命名crash point重放，并验证业务key`account_id + chat_id + telegram_message_id`、revision与outbound source保持稳定。

最小集合覆盖：单消息、快速连续消息、album、caption edit、delete、human outgoing、system pending outgoing、duplicate update、late update、unsupported peer、图片成功/拒绝、FloodWait和send unknown。

## 13. Telegram send与对账contract

发送测试必须跨以下边界参数化：

```text
before intent commit
after intent commit before RPC
RPC accepted before local response
response received before DB commit
partial delivery group
process restart before reconciliation
```

不变量：

- 同一intent的`telegram_random_id`不改变；
- unknown先对账，不创建replacement；
- 已确认chunk不重发；
- mode/control/content revision变化使未开始副作用stale；
- HUMAN/PAUSED/maintenance/BLOCKED恢复不自动补backlog；
- source reconciliation不会把AI/COPILOT/proactive误标为human。

## 14. Provider HTTP fake与wire contract

本地fake server必须：

- 验证method、path、headers名称、body schema和请求顺序；
- 对Authorization只验证sentinel存在与作用域，不记录值；
- 支持完整响应、stream、slow first byte、stream gap、429/Retry-After、5xx、reset、malformed、length和refusal；
- 支持重复provider request ID和迟到response；
- 为embedding返回固定dimension、NaN/Inf、错dimension和版本变化case；
- 提供请求计数，证明无candidate、stale或门禁失败时零调用。

Fake响应不应模拟模型“聪明程度”；它只验证adapter与业务状态契约。

## 15. 三种生成协议fixtures

每个协议至少有text、text+image、structured output、stream/error fixture：

| Canonical能力 | Responses | Chat Completions | Messages |
|---|---|---|---|
| text input/output | `input`/output items | `messages`/choice | `messages`/content blocks |
| image | `input_image`、`detail=auto` | image URL part、`detail=auto` | image source、canonical auto等价映射 |
| output limit | `max_output_tokens`映射 | capability选择`max_completion_tokens`或`max_tokens` | `max_tokens` |
| strict output | capability snapshot要求的schema模式 | provider支持子集 | tool/JSON text受控子集 |

Fixture断言canonical request fingerprint、config/credential/capability/prompt/context snapshots。Provider SDK升级导致wire变化时contract先失败；只有经过review的新fixture和capability version才能接受变化。

Legacy text `/completions`必须有明确拒绝测试，不能因endpoint路径相似被误支持。

## 16. ModelProfile与配置生命周期测试

覆盖：

- `main_ai`、`memory_agent`、`proactive_agent`独立draft/validate/activate；
- embedding profile独立于三个generation role；
- endpoint/protocol/model/temperature/max token/timeout/options的canonical validation；
- Web App只能set/replace/delete API key，不能修改非secret字段；
- 旧active配置在新draft validation失败时保持不变；
- 原子激活与Redis invalidation丢失；
- in-flight run继续使用开始时snapshot；
- credential replace/delete、master key rotation和missing-key fail closed。

## 17. Key-only Web App与Control Bot安全测试

### 17.1 Server contract

构造测试Bot token签名的`initData`，覆盖tamper、过期、未来时间、错误Bot、非admin、role/action/deployment不匹配、launch replay和并发CAS。API测试必须证明：

- 五分钟边界前后行为精确；
- 一次性launch成功或失败消费语义符合contract；
- API key不回显、不入URL、log、audit、queue或error；
- endpoint/model/temperature等越权字段被拒绝；
- rate limit的PostgreSQL事实与Redis加速丢失后仍安全。

### 17.2 Browser E2E

固定Chromium harness通过真实Caddy/test TLS origin验证CSP、no-store、无第三方请求、无browser storage和成功/失败清理。测试不能宣称JavaScript内存或客户端缓存可可靠擦除，只验证应用可控行为。

### 17.3 Control Bot

Bot fake覆盖短期输入session、命令顺序、旧action token、管理员越权、`/models`状态、`/model_key <role>`入口，以及API key在普通Telegram消息中的明确拒绝。

## 18. Unit与property测试重点

必须作为纯规则或最小application service测试：

- effective mode与overlay优先级；
- `mode_version`、`account_control_version`与revision gate；
- debounce、3秒conditional supersede和10秒hard cap；
- memory OR触发、range merge、confidence/importance、conflict；
- context layer排序、预算、截断、去重和stable tie-break；
- proactive reason allowlist、quiet/DST/bypass、预算和reservation；
- retention deadline、redaction可见性和cleanup eligibility；
- splitter的grapheme、Telegram长度与ordered intent；
- URL canonicalization与IP分类；
- retry分类、backoff上限与deadline。

Hypothesis生成消息序列、时间推进、mode变化、edit/delete、worker claim、provider结果和crash。State machine每步断言：无未授权发送、唯一active版本、budget不为负、redacted数据不可检索、watermark不倒退、confirmed intent不重发。

## 19. PostgreSQL与pgvector integration

Testcontainers启动与生产compatibility set一致的PostgreSQL/pgvector。Schema只通过Alembic建立，不使用ORM`create_all()`替代migration。

每个并行test worker使用独立database或schema及独立advisory-lock namespace。普通测试可以transaction rollback；需要验证commit、crash、listener、lease或多连接可见性的case必须使用disposable committed state并在case后销毁整个namespace。

覆盖：

- composite FK、partial unique、CHECK、exclusion和not-null；
- immutable version、single active version、evidence root和one-way redaction；
- row lock、advisory lock、CAS、serializable/deadlock retry；
- outbox/job/lease/watermark/reaper；
- outbound group/intent/random ID与unknown reconciliation；
- vector dimension/space隔离、HNSW candidate与exact rerank；
- retention、purge、account wipe和erasure ledger outbox；
- 最小数据库角色不能读取或写入未授权表/view。

SQLite测试只能验证与数据库无关的serialization辅助逻辑，不能计入上述acceptance。

## 20. Redis与arq integration

覆盖AOF配置、`noeviction`、queue notification、cache TTL、heartbeat、short lease和invalidation。关键场景：

1. PostgreSQL事务提交但enqueue前崩溃；
2. enqueue重复；
3. Redis清空或AOF损坏；
4. worker执行后CAS前崩溃；
5. lease过期与旧owner迟到完成；
6. cache/invalidation丢失；
7. maxmemory拒绝写入。

恢复后pending PostgreSQL work必须重新发布且不重复业务副作用。测试不能把Redis快照恢复当作canonical correctness来源。

## 21. Message Lifecycle与Orchestrator integration

使用真实PostgreSQL/Redis、fake Telegram/provider、virtual clock和多个application worker task验证：

- incoming persist → debounce → turn → run → intent → send → confirm；
- 3秒内完成与超过3秒supersede两条路径；
- edit-before-send rebuild、edit-after-send只记录、delete tombstone；
- album排序、duplicate/late update、reaction/service metadata；
- AUTO read/typing生命周期；其他mode无自动read/typing；
- HUMAN outgoing、mode切换、provider返回和send同时发生；
- temporary HUMAN与COPILOT draft/approval/expiry；
- pause、maintenance、BLOCKED与`/reply_pending`；
- multi-process conversation/account lock竞争；
- 每个命名crash point的restart reconciliation。

Concurrency case不得只依赖概率。测试先用barrier构造精确交错，再用Hypothesis补充序列探索。

## 22. Memory Pipeline测试

覆盖：

- 45秒quiet window以及20 revisions、约6000 tokens、10分钟硬阈值的OR关系；
- 每5分钟补偿扫描、pending refresh、running range seal与后继job；
- source trust、image-only candidate、confidence边界和冲突；
- create/update/supersede/invalidate/merge事务；
- rolling 50 revisions/约12000 tokens、daily/weekly source membership；
- late/edit/delete触发的递归rebuild和watermark；
- candidate accept/reject、旧token/version/evidence拒绝、forget；
- embedding shadow space重建、dimension错误、切换与旧space清理；
- queue积压的fresh/degraded/stale，Main AI不等待且不读取candidate/quarantine；
- restore后erasure overlay不会复活memory、summary或embedding。

Memory/provider fake只返回schema对象。它不能绕过application validator直接写active memory。

## 23. Context Contract测试

覆盖固定输入下manifest byte/hash稳定、layer顺序、24,000默认上限动态收紧、soft quota借用、current turn超限拒绝、memory lag扩展、ANN exact rerank、跨层去重和stable tie-break。

Injection corpus包含恶意forward/reply label、历史AI指令、memory文本、图片OCR文本和伪造system语句；断言它们始终作为data part，不能改变trusted instruction或provider options。

长输出测试生成ASCII、CJK、emoji、组合字符、超长URL和边界换行，验证deterministic splitter、delivery group原子创建、chunk random ID稳定与partial恢复。

`/context`只返回metadata；`/context_preview`覆盖token过期/重放/越权、source删除、send unknown、已知消息删除失败和所有存储/日志无preview正文。

## 24. Proactive Pipeline测试

覆盖：

- 无规则candidate时provider call count为零；
- durable due job与15分钟compensation tick的OR触发；
- account/contact timezone与DST gap/fold；
- quiet `22:00-08:00`、absolute no-send `00:00-07:00`及受限shoulder bypass；
- relationship/account/contact/bypass预算、最小间隔和原子reservation；
- activity 30分钟、mode、draft、真人接管与冲突抑制；
- `send_now/defer_once/none`及schema拒绝；
- AUTO 10分钟与COPILOT 30分钟reservation TTL；
- evidence/mode/activity/version变化导致stale；
- conversation lock内final gate与send-unknown保守计费；
- downtime不补过期window或proactive backlog。

Proactive Agent fixture不得发明contact、reason或自由文本evidence；Main AI proactive fixture只消费已经选择的text-only manifest。

## 25. Media安全与生命周期测试

Image corpus包含JPEG/PNG/WebP、错误magic/MIME、截断、超20 MiB、超40 MP、超16384边长、EXIF orientation、极端压缩比、解码异常和symlink/path traversal尝试。

验证：

- streaming byte limit、10秒connect/30秒total和并发1；
- temp原子写、crash残留1小时、provider copy去metadata；
- original 30天/provider copy 24小时；
- 10 GiB quota与整机90/95%行为；
- delete/purge/wipe立即排除并幂等unlink；
- worker只读、control无挂载、Caddy无route、backup不含media；
- 模型无图片能力或媒体不可用时不谎称已经看图。

解码炸弹在受资源限制的isolated process/container运行。测试使用专用bounded volume，不影响runner宿主机其他目录。

## 26. Migration测试

每个支持的release至少验证：

```text
empty database -> head
previous supported revision -> head
interrupted backfill -> resume -> head
head schema + previous compatible app
contracted schema + previous app -> BLOCKED
```

验证global migration lock、多head拒绝、extension compatibility、batch cursor、concurrent index边界、失败后not-ready和无自动downgrade。Migration fixture保存schema revision与合成规模，不保存业务数据。

发生destructive migration时必须额外验证export、backup、erasure overlay和rollback decision；单纯`alembic downgrade`成功不构成恢复证据。

## 27. Compose E2E

完整Compose使用生产模板、测试secret和隔离project name，至少验证：

- Caddy只公开443，其他服务无host port；
- edge/backend/backup网络解析边界；
- 非root、read-only、drop capabilities、无Docker Socket；
- secret reader GID应读/不应读矩阵，不输出内容；
- migrate成功后服务ready，schema不兼容保持not ready；
- Telethon Session account lock单owner；
- health 10/3/3、heartbeat 10/30和`/server_status`；
- SIGTERM grace与SIGKILL后恢复；
- Redis/PostgreSQL/provider/Caddy重启降级；
- 日志、metrics和artifact不含sentinel或正文。

Compose project、network、volume和临时证书使用唯一test run ID。Cleanup只操作经过解析并验证属于该run的精确对象；失败时保留sanitized locator供人工清理，不能使用无界prune。

## 28. SSRF、TLS与网络安全测试

本地network lab提供loopback、RFC1918、link-local、metadata-like、CGNAT、IPv6 ULA、public test endpoint、private allowlist、redirect、proxy和DNS rebinding场景。

断言canonicalization、全部A/AAAA检查、connect前重验、TLS SNI/hostname、private CA、redirect拒绝和environment proxy禁用。测试endpoint不访问真实云metadata或第三方系统；metadata场景由本地受控地址模拟。

Caddy测试验证TLS-ALPN/DNS/imported certificate配置选择，不在失败时静默开放80。公网端口扫描只在专用测试主机执行。

## 29. Backup、restore与erasure测试

在隔离Ubuntu/volume执行：

1. 生成synthetic消息、memory、credential ciphertext和outbound状态。
2. 完成pgBackRest full/differential/WAL与Session restic backup。
3. 在backup之后执行message delete、forget、contact purge和account wipe，导出最新erasure ledger。
4. 销毁测试stack并恢复旧数据库/Session snapshot。
5. 在`BOOTSTRAP_MAINTENANCE=1`下应用ledger、migration和reconciliation。
6. 证明被删除正文、memory、summary、embedding和candidate不复现。
7. 证明maintenance清除前无provider/Telegram副作用。
8. 记录实际DB/Session/whole-host RPO/RTO，分别判定。

Backup repository使用测试专用bucket/prefix和credential。Restore job不能连接真实联系人；Telegram授权验证使用隔离测试账号或保持send gateway disabled。

目标RPO 15分钟、whole-host RTO 2小时只有实测达到时才`PASS`。Session失效导致人工重新登录时单独记录，不用数据库RTO掩盖。

## 30. 真实Telegram与Web App smoke

真实Telegram测试必须人工触发并满足：

- 专用、获授权的staging user account、Control Bot和测试peer；
- 加密隔离主机，Session不进入GitLab shared/protected runner artifact；
- allowlist保证只能与测试peer交互；
- 开始前确认mode、预算和无生产数据，结束后撤销临时token并清理测试消息；
- 不运行spam、批量联系、联系人发现或账号限制绕过；
- 记录Telegram client/library版本、测试ID和content-free结果。

至少验证登录恢复、update catch-up、human/system source reconciliation、random ID幂等、FloodWait脚本允许的安全case、Bot Web App `initData`和known message best-effort delete。无法安全构造的RPC unknown保持fake/integration证据并标`NOT RUN`，不能故意破坏真实网络制造未授权副作用。

## 31. 真实Provider smoke

每个已配置协议/adapter在首次启用、adapter升级或release candidate时，以受保护人工job调用真实endpoint：

- 固定无私人数据text和极小token上限；
- 使用专用测试图片，不含EXIF或真实内容；
- 验证text、structured output、image capability和usage字段；
- 记录endpoint/profile ID、protocol、model、capability version、HTTP result和latency，不记录完整URL、header或body；
- provider key只在job内存/secret file短暂可用，不进入artifact。

Live输出不作为golden，只有schema/capability/result category用于证据。Provider不可用使该项`BLOCKED`或`FAIL`，不能用fake结果替代。

## 32. GitLab CI pipeline层级

### 32.1 每次提交/merge request preflight

```text
format/static/docs checks
unit + property
provider/Telegram/Web App contract
coverage + secret/artifact scan
```

不得使用发布、Telegram、provider、backup或production credential。Fork和未受信任branch只运行该无secret集合。

### 32.2 受保护pre-merge/main integration

```text
PostgreSQL/pgvector + Redis Testcontainers
Alembic empty -> head
database roles/constraints/CAS/lease/outbox
pipeline integration with local fakes
coverage merge + secret/artifact scan
```

Container integration必须在合并前通过，但只在maintainer批准的受保护Ubuntu runner执行。Fork或未受信任branch先完成review，再由受保护job检出精确source commit；job不得执行source-controlled privileged hook或取得live/backup/publication credential。

### 32.3 Nightly或人工受保护

```text
full Compose E2E
multi-process race/fault matrix
migration from previous supported revision
Redis loss and crash recovery
media/security/network lab
sanitized artifact scan
```

失败创建可追踪issue/alert，不自动把前一次成功结果沿用到新commit。

### 32.4 Release/operations-sensitive

以下变化触发完整候选门禁：runtime/Compose、DB schema、message send、Session、crypto/credential、SSRF、backup/restore、media limit、worker concurrency、resource profile或release workflow。

门禁包括nightly全集、live smoke适用项、backup/restore、upgrade/rollback和2/4/40 soak。纯文档候选不伪造runtime结果，但必须通过文档一致性、链接、Disclosure、secret和签名检查。

## 33. Runner与secret隔离

普通runner只运行无secret任务。受保护Ubuntu runner必须：

- 只接受protected `main`/受批准manual job；
- lock到本项目，拒绝untagged与fork pipeline；
- 使用临时workspace、隔离Docker project和最小网络；
- job结束销毁测试container/volume/credential；
- runner注册/发布credential不传给测试container；
- artifact upload前执行正文、sentinel、private endpoint和secret scan。

真实Telegram Session只存在隔离测试主机，不注册为通用CI variable。Provider/backup测试credential按用途分离、最小权限、可轮换，不复用生产credential。

任何无法证明secret没有进入不可信job的pipeline为`BLOCKED`。

## 34. Coverage与安全关键门禁

合并门禁：

```text
global line coverage >= 85%
global branch coverage >= 80%
```

Coverage以production Python package为分母；generated migration boilerplate、类型stub或不可达platform shim的exclude必须逐项review。新增代码不能靠扩大exclude保持数字。

以下安全关键不变量无论coverage数字如何都要求正例、反例、边界和race/crash case：

- Web App/admin/action/role/replay认证；
- credential encryption/AAD/rotation/delete；
- model endpoint SSRF/TLS；
- mode/revision/final send gate与random ID；
- one-way redaction、forget/purge/wipe与restore ledger；
- proactive quiet/budget/activity/final gate；
- database ownership、CAS、unique和migration；
- secret/log/artifact不泄露。

若模块无法对不变量建立可观察测试点，应先改善ports/状态记录，而不是降低门禁。

## 35. Flaky、rerun与quarantine

- Gating test第一次失败即使诊断重跑成功，原pipeline仍为`FAIL`。
- 可以在独立非门禁job用相同seed/环境重跑以收集证据，结果不得覆盖原失败。
- Hypothesis example、random seed、virtual time和container/image IDs必须写manifest。
- Quarantine需要issue、owner、失败证据、影响、临时替代门禁和不超过14天expiry。
- send/auth/credential/erasure/SSRF/migration/restore等release-critical测试不得quarantine后发布。
- `xfail`只用于明确未支持能力且必须strict；不能用于偶发失败。
- 到期未修复的quarantine使相关release`BLOCKED`。

## 36. 性能与2/4/40 soak

### 36.1 Workload

使用公开可描述的synthetic workload，至少包含：

- steady private messages、burst/debounce、album/image；
- Main/Memory/Embedding/Proactive受控fake latency；
- queue积压与恢复；
- summary/embedding cleanup和backup窗口；
- database size/vector query的代表性规模。

Workload version、seed、rate、payload size分布和provider delay进入manifest。不能使用真实聊天分布。

### 36.2 24小时门禁

在Ubuntu 24.04 amd64、2 vCPU、4 GiB RAM、40 GiB SSD、一个worker容器/concurrency 2运行。记录CPU、RSS、OOM、restart、queue age、DB/Redis/media/disk增长、model/send latency、unknown intent和cleanup lag。

最低通过条件：

- 无OOM、restart storm、duplicate/unauthorized send；
- 无未解释dead-letter、unknown intent或watermark倒退；
- disk未越过设计门禁，增长可由retention解释；
- health/heartbeat/backup/cleanup在目标内；
- 测试结束可优雅停止并完整reconcile。

首次生产部署前必须完成。之后在worker concurrency、media、DB/index、queue、backup、Python/runtime或resource profile变化时重跑。未跑时该profile保持`NOT RUN`，不得引用旧commit的soak作为新候选证据。

## 37. Evidence与状态模型

### 37.1 状态

| 状态 | 含义 |
|---|---|
| `PASS` | 精确commit/environment上的要求已执行并满足 |
| `FAIL` | 已执行且至少一个要求不满足 |
| `NOT RUN` | 未执行；不暗示成功或失败 |
| `BLOCKED` | 已尝试或被要求，但缺少授权、资源、外部状态或安全前提 |

Skip必须映射成`NOT RUN`并给出原因；required test被意外deselect视为pipeline failure。

### 37.2 Acceptance manifest

每个job输出不含secret的JSON manifest，至少包含：

```text
schema_version
test_run_id
source_commit and dirty=false
branch/pipeline/job identity
image digests and dependency lock hash
OS/arch/Python/PostgreSQL/Redis/pgvector versions
resource profile
test selection/markers and counts
Hypothesis/random/workload seeds
fixture/policy/schema versions
PASS/FAIL/NOT RUN/BLOCKED by requirement ID
start/end/duration
artifact names and SHA-256
sanitization/secret scan result
```

Signature/provenance由CI平台或后续release workflow提供时，必须绑定同一commit和artifact hash；不在自定义JSON中伪造“官方签名”字段。

### 37.3 Artifact retention

- 普通JUnit、coverage、sanitized failure artifact：30天；
- release、live smoke、migration、restore、upgrade、security和24小时soak证据：365天；
- raw diagnostic默认不生成；若Operations显式授权，最多1小时并独立加密；
- artifact到期删除不修改数据库中的content-free test run/audit摘要。

Artifact不得包含消息/记忆正文、Session、API key、Bot token、initData、launch token、Authorization、完整endpoint、private IP、backup config或browser storage dump。

## 38. Requirements traceability

维护`tests/acceptance/manifest.yaml`或等价机器可读source，记录：

```text
requirement_id
source_document and section anchor
risk class
test IDs
required environment/marker
expected evidence
current status
```

每个架构acceptance item至少映射一个自动test ID或一个明确人工procedure ID。一个测试可以覆盖多个要求，但不能只有“相关测试目录”而没有稳定ID。

新增或修改架构规则时，同一change必须更新traceability；测试删除使requirement无覆盖时CI失败。实现前没有test file的要求保持`NOT RUN`，但traceability设计本身可以静态验证。

## 39. 跨文档关键验收矩阵

| Area | 关键自动证据 | 必要高层证据 |
|---|---|---|
| Runtime Topology | ownership/network/secret/health contract | Compose、signal、single Session owner |
| Message Lifecycle | replay/state/property/DB concurrency | crash/send unknown/reconciliation E2E |
| Data Model | constraints/index/role/migration | previous→head、restore erasure |
| Conversation Orchestrator | state table/property/race | multi-process mode/send race |
| Memory Pipeline | trigger/evidence/summary/embedding | backlog/rebuild/restore |
| Context Contract | budget/trust/wire/splitter | real tokenizer/provider smoke适用项 |
| Proactive Pipeline | time/budget/state/final gate | DST/concurrency/downtime E2E |
| Operations | static/Compose/security/recovery | live、restore、upgrade、2/4/40 soak |

详细case仍以各文档的自动化验收章节为source。本文负责环境、证据和门禁，不降低任何既有case。

## 40. 实现顺序

1. 建立pytest config、marker、coverage和acceptance manifest schema。
2. 建立Clock/Random/ID、Telegram/provider fake和synthetic fixture factory。
3. 建立PostgreSQL/pgvector/Redis Testcontainers与Alembic fresh migration。
4. 先实现send/auth/erasure/SSRF等安全关键contract与integration门禁。
5. 随业务milestone逐项填充unit/property/pipeline测试和traceability。
6. 建立Compose E2E、crash point和migration recovery。
7. 建立protected runner、artifact scan、backup/restore和live smoke runbook。
8. 首次生产候选完成2/4/40 24小时soak并归档证据。

不得先实现自动发送再把幂等、mode gate和crash recovery测试推迟到发布前。

## 41. 尚未固定的实现参数

以下不是未决业务行为，但必须在对应实现milestone开始前固定并进入manifest：

| 参数 | 决策期限 | 所需证据 |
|---|---|---|
| CPython与dependency exact versions | 项目脚手架合并前 | lock/install/unit/Compose |
| PostgreSQL/pgvector/Redis/Caddy digests | 首个integration环境前 | compatibility/migration/restore |
| Browser automation driver/version | Web App实现前 | Chromium auth/storage/CSP E2E |
| Previous supported migration revision | 第二个schema release前 | previous→head/rollback gate |
| 专用Telegram测试账号/peer | 首次live smoke前 | owner authorization/allowlist |
| Provider smoke endpoints/models | 对应adapter启用前 | capability probe/policy review |
| 代表性synthetic workload规模 | 首次性能基线前 | generator version/resource fit |
| Protected runner与backup target | 首次Compose/restore门禁前 | isolation/credential/artifact scan |

任何参数缺失只阻塞依赖它的证据，不允许用默认个人账号、生产credential或共享数据库临时替代。

## 42. 安全、审计与Disclosure边界

- 公开仓库只保存synthetic fixture、fake和测试schema，不保存真实Session/对话/provider body。
- 测试工具不得加入任意联系人发现、批量消息、限制绕过或未经授权的外部探测。
- Live测试只验证本项目声明的合法个人账号使用路径，并限制到allowlisted测试资源。
- CI、runner、artifact、backup和live endpoint的新增行为必须同步根`DISCLOSURE`。
- 测试失败日志遵守Operations脱敏；为了调试测试不能默认开启raw capture。
- 对安全或dual-use能力的公开push仍需独立public-release review；测试通过不自动授权发布。

## 43. 完成检查表

- [x] Unit/property/contract/integration/Compose/live/restore/soak边界已定义。
- [x] pytest、pytest-asyncio、Hypothesis、Testcontainers与Compose职责已定义。
- [x] Telegram fake、replay fixture、send unknown和真实账号边界已定义。
- [x] 三种generation协议、embedding、Web App和Control Bot contract已定义。
- [x] Synthetic-only数据、sentinel scan和regression fixture隐私边界已定义。
- [x] Virtual clock、barrier、state machine、crash point和flake规则已定义。
- [x] PostgreSQL/pgvector、Redis/arq、migration和erasure恢复已定义。
- [x] Message、Orchestrator、Memory、Context和Proactive验收范围已定义。
- [x] Media、SSRF/TLS、Compose、backup/restore和2/4/40 soak已定义。
- [x] CI分层门禁、runner/secret隔离、coverage和artifact retention已定义。
- [x] Acceptance ID、traceability、evidence manifest和四态结果已定义。
- [x] 尚未固定的实现参数、期限和所需证据已列出。
