# Telegram Personal AI Digital Twin

一个计划运行在真实Telegram用户账号上的长期个人AI分身：支持AI自动回复、真人即时接管、长期记忆、可审计上下文、COPILOT审批和受严格规则约束的主动消息。

## 当前状态

Worker 实现已补齐，Design 保持冻结。2026-09-29 本地默认测试 **1775 passed、4 skipped，覆盖率 85.03%**。影子向量空间重建与回退、大积压分块、历史时区重切，以及主动扫描、决策、预算回收、AUTO/COPILOT 发布均已接入。

当前 head 为 `0036_worker_complete`；queue inventory 已无 Worker 必需消费者缺口。运行 READY 仍需通过数据库、账号、恢复闸门、磁盘和依赖检查。详见 [Worker 运行说明](docs/runbooks/worker-runtime.md) 与 [本轮完成审计](docs/audits/2026-09-29-worker-complete.md)。

代码检查和隔离初验已完成，用户已确认本阶段成果。本次提交保存当前源码和文档；完整 Compose、真实 Telegram/provider 与生产全链路验证按用户要求留待后续，不随本次推送启动。参见 [源码收尾记录](docs/audits/2026-09-29-source-closeout.md)。

### 历史里程碑与证据边界

V1架构设计与M0—M6已经完成，M7也已完成；M8 Production Compose与Operations已进入实现阶段，但尚未取得生产运行证据。M7 implementation source baseline 是`19bf0c7974b7ef2e1a3e3b8064a10d4d162353b6`，其 GitHub Actions run [#32229187875](https://github.com/Septuagint0201/Telegram_UserBot/actions/runs/32229187875) 保留为源码实现证据；final acceptance baseline 是签名提交`7af2f524fcc4fc30fc04aa40de88a7b1302eb526`、tree `a2eaab5195c393c3905cc92620af54b7d8c208ab`，GitHub Actions run [#32234678340](https://github.com/Septuagint0201/Telegram_UserBot/actions/runs/32234678340) 的 Preflight（422 passed、58 deselected）、PostgreSQL/Redis integration（479 passed、1 deselected）和 Chromium browser contract（1 passed）均为`PASS`，M7 acceptance 为12/12，migration manifest记录96 tables、0 unnamed constraints和5 migration paths `PASS`。final baseline相对implementation source只修改状态文档，不改变M7实现；CI仍会为每个被验证的提交生成并校验新的M7 acceptance manifest。当前源码 Alembic head 为`0036_worker_complete`；M8生产Compose、真实Telegram/provider、真实AUTO、backup/restore、production load和24小时soak仍为`NOT RUN`。默认入口仍不连接Telegram或provider，也不启用真实AUTO。

因此：

- 可以运行本地安全校验、fake-only Telegram/model contract、key-only ASGI测试和显式的disposable PostgreSQL/Redis/browser integration test；M8也提供显式的生产服务/运维入口，但真实Telegram/provider、生产部署和完整业务运行仍需按runbook取得前置条件与证据；
- 文档中的服务与业务命令已有对应实现入口，但当前仍是未完成生产验证的候选实现，不能把入口存在误述为已部署或已启用；
- Windows真实database/Redis、live Telegram/provider、Ubuntu production、backup/restore和24小时soak仍为`NOT RUN`；
- final baseline的独立原生Linux amd64非live复现为`PASS`；`COMPAT-LINUX-ARM64-001`记录Linux arm64 locked-install在项目测试前因lock hash覆盖不足而独立`FAIL`，后续arm64 tests为`NOT RUN`，该兼容性backlog不属于M7或M8的`linux/amd64`门禁；
- RPO 15分钟、整机RTO 2小时和2 vCPU/4 GiB/40 GiB资源profile是待实现与实测的目标；2026-09-04检查的目标VM名义为64 GiB，根文件系统约60.9 GiB、可用约32.9 GiB，既不能证明40 GiB受限磁盘边界，也不是fresh soak环境。

精确兼容组合与平台边界见[M1 Compatibility Set](docs/compatibility/m1.md)、[M2 Compatibility Set](docs/compatibility/m2.md)、[M3 Compatibility Set](docs/compatibility/m3.md)、[M4 Compatibility Set](docs/compatibility/m4.md)、[M5 Compatibility Set](docs/compatibility/m5.md)、[M6 Compatibility Set](docs/compatibility/m6.md)、[M7 Compatibility Set](docs/compatibility/m7.md)和[M8 Compatibility Set](docs/compatibility/m8.md)。M8已实现真实Telegram/provider的显式生产组合路径，但尚未使用真实凭据完成live验证或启用真实AUTO。

## 架构摘要

- Python模块化单体，生产目标为Ubuntu Server 26.04 amd64上的Docker Compose。
- `app`唯一持有Telethon Session并执行真人账号消息副作用。
- `control`独立运行Control Bot和只处理API key的Telegram Web App。
- `worker`异步执行Memory、Embedding、Proactive和补偿任务。
- PostgreSQL是canonical事实源；Redis/arq只负责dispatch、cache、通知和短期租约。
- Main AI、Memory Agent、Proactive Agent使用三个独立generation profile；Embedding独立配置。
- 支持Responses、Chat Completions和Messages adapter，不支持legacy text `/completions`。
- AUTO/HUMAN/COPILOT与pause/maintenance/BLOCKED门禁阻止过期或未经授权的发送。
- 主动消息只能从确定性candidate产生，并受时区、quiet hours、预算、活跃会话和最终send gate约束。

## 文档索引

### 总体与实施

- [总体设计](docs/Design.md)
- [V1 Implementation Plan](docs/Implementation-Plan.md)
- [V1 Development TODO](TODO.md)
- [Architecture Decision Records](docs/adr/README.md)
- [Security and Dual-Use Disclosure](DISCLOSURE)

### 详细架构

1. [Runtime Topology](docs/architecture/01-runtime-topology.md)
2. [Message Lifecycle](docs/architecture/02-message-lifecycle.md)
3. [Data Model](docs/architecture/03-data-model.md)
4. [Conversation Orchestrator](docs/architecture/04-conversation-orchestrator.md)
5. [Memory Pipeline](docs/architecture/05-memory-pipeline.md)
6. [Context Contract](docs/architecture/06-context-contract.md)
7. [Proactive Pipeline](docs/architecture/07-proactive-pipeline.md)
8. [Operations](docs/architecture/08-operations.md)
9. [Test Strategy](docs/architecture/09-test-strategy.md)

## 开发环境

生产支持面已经固定为：

```text
Ubuntu Server 26.04 LTS
linux/amd64
Docker Engine + Compose v2
2 vCPU / 4 GiB RAM / 40 GiB SSD
```

标准GIL CPython固定为3.14（当前patch为3.14.7），pip 25.3及全部runtime/dev/lock工具使用独立hash lock。跨平台IANA时区数据固定为`tzdata==2026.3`；M1固定PostgreSQL 17.10、pgvector 0.8.6、Redis server 8.2.8、redis-py 5.3.1和arq 0.28.0。Caddy与production image留在M8。Windows unit/property/contract不能替代Linux CI或Ubuntu生产证据。

Windows PowerShell的可重复开发安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/bootstrap.lock
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
```

Ubuntu/Linux把解释器路径替换为`.venv/bin/python`。不要跳过hash校验或把真实credential放入开发环境。

M0安全入口只验证配置并输出allowlist JSON日志：

```powershell
.\.venv\Scripts\telegram-userbot-check.exe
```

该安全入口的外部集成开关仍必须全部为false；它不会启动M1数据库/Redis adapter、Telethon、Bot polling或生产 scheduler。M8的生产组合入口另行读取经过校验的部署配置和挂载 secret，未在该安全入口中调用。Alembic和integration test只能对显式提供的disposable PostgreSQL/Redis运行。

## M8运行入口与运维边界

M8工作树已定义下列Compose服务清单：

```text
https-gateway
app
control
worker
ops-monitor
postgres
redis
migrate
session-backup
data-export
```

部署模板、operator边界和逐项runbook见[deploy/README.md](deploy/README.md)与[docs/runbooks/README.md](docs/runbooks/README.md)。它们已经定义Compose/Caddy、secret、health、backup/restore和升级操作的实现契约，但不等同于已验证的生产部署说明：在固定镜像、真实secret、隔离restore、Ubuntu运行时和soak证据完成前，完整业务服务与真实AUTO仍为`NOT RUN`。M8的逐项兼容性与当前 synthetic/production 边界见[M8 Compatibility Set](docs/compatibility/m8.md)；2026-09-04检查的目标VM根文件系统约60.9 GiB、可用约32.9 GiB，不能替代40 GiB受限磁盘边界或fresh soak证据。

本地默认入口仍保持安全：不会连接真实Telegram/provider或启用真实AUTO。M2的model-control、M3的Telethon gateway和M4的Conversation Orchestrator均保留为可注入边界；真实Session、provider key、Bot polling、scheduler和Compose业务服务只能按上述runbook及Test Strategy/Disclosure门禁启动。

## 测试与证据

测试架构使用pytest、pytest-asyncio、Hypothesis、Testcontainers和Docker Compose，默认只使用synthetic fixture与fake Telegram/provider。真实Telegram只允许专用授权测试账号和allowlisted测试peer；真实provider smoke不能发送私人数据。

M0在Windows/CPython 3.14.7的本地结果：56 tests `PASS`，line coverage 97.77%，branch coverage 90.32%；Ruff、strict mypy、compileall、import boundary、build artifact Disclosure和secret/artifact扫描均`PASS`。签名提交`5e6f2b3512436a5ba70c958a42901b920ffa6caa`对应的[GitLab Linux pipeline #2](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2747413992)及`m0-preflight`作业均为`PASS`；其acceptance manifest绑定相同commit/tree，并将M0-001—M0-012全部记录为`PASS`。

M1在Windows/CPython 3.14.7的本地结果：86 tests `PASS`、10个integration/recovery test因本机无Docker为`NOT RUN`，总coverage 98.08%；Ruff、strict mypy、import boundary、wheel/sdist Disclosure与secret/artifact扫描均`PASS`。签名提交`9c2dbf61c8b67e75182f47abbb419ae82773678a`对应的[GitLab Linux pipeline #9](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2747812423)为`PASS`，真实PostgreSQL 17.10/pgvector 0.8.6与Redis 8.2.8上的96个测试全部通过；acceptance manifest绑定相同commit/tree并将M1-001—M1-012全部记录为`PASS`。Windows真实服务、Ubuntu production、backup/restore和production load仍为`NOT RUN`。

M2在Windows/CPython 3.14.7本地有143个默认测试通过，line coverage 92.11%、branch coverage 80.89%；本机Docker为`NOT RUN`且Chromium binary为`BLOCKED`。签名提交`def4ff1f846307a7ea428de3c048616601cab7a4`对应的[GitLab Linux pipeline #2748486868](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2748486868)全部通过：157个测试零失败/跳过，line coverage 92.11%、branch coverage 81.61%，四条migration路径、DB role、Chromium 151.0.7922.34和M2 acceptance均为`PASS`。真实Telegram/provider、Ubuntu production、backup/restore和production load仍为`NOT RUN`。

M3在Windows/CPython 3.14.7本地有179个默认测试通过，line coverage 91.41%、branch coverage 81.13%；本机没有Docker daemon，因此PostgreSQL/Redis integration为`NOT RUN`。签名提交`41f4160a6d53bdd34e2654f08a90a4b61b6675e8`对应的[GitLab Linux pipeline #2751916211](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2751916211)全部通过：`m3-telegram-fake`在PostgreSQL 17.10/pgvector 0.8.6与Redis 8.2.8上执行202个测试，1个browser测试按策略deselect，line coverage 93.22%、branch coverage 84.65%；4条migration路径、12项content-free replay和M3-001—M3-010 acceptance均为`PASS`。真实Telegram ingest/send与Session owner运行时保持`NOT RUN`。

M4的补强已完成：race/acceptance从JUnit stable test ID派生，continuation逐段验证原source与human takeover，Control Bot只入队command/outbox并由app executor写accepted/no-op/rejected终态，非status command保留SQL `NULL`而status payload保持JSON object。Windows/CPython 3.14.7有228个default测试通过、31个非默认测试deselect，line coverage 89.53%、branch coverage 80.73%，Ruff、strict mypy、import boundary与compileall通过；本机无容器运行时，M4 PostgreSQL integration仍为`NOT RUN`。签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`对应的[GitLab Linux pipeline #2758187631](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758187631)九个作业全部通过：M4 service job执行258个测试、1个deselect，line coverage 92.52%、branch coverage 84.50%，migration、JUnit-derived race、M4 acceptance和artifact scan全部`PASS`。M4-001—M4-011因此关闭；真实Telegram/provider、真实AUTO与部署运行时保持`NOT RUN`。

M5签名提交`9e6aeaf3a50ff58826a6830492c766a7983da9b6`对应的[GitLab Linux pipeline #2758537825](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758537825)共11个作业全部`PASS`：service job执行288个测试并deselect 1个，total coverage 90.68%，同时通过migration、role、browser、acceptance和artifact门禁。真实Telegram/provider、部署、backup/restore与production load保持`NOT RUN`。

M6在Windows/CPython 3.14.7有289个default测试`PASS`、41个非默认测试deselect，line coverage 88.71%、branch coverage 80.05%；Ruff、strict mypy、import boundary、compileall、offline migration SQL、wheel/sdist Disclosure和secret/artifact scan均为`PASS`。M6签名证据基线`645fb8da5d5c35de6896825c5f29f22f08d0b168`已由[GitLab pipeline #2763001231](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2763001231)的13个作业和[GitHub Actions #31907584107](https://github.com/Septuagint0201/Telegram_UserBot/actions/runs/31907584107)验证：GitLab M6 service执行329个测试并deselect 1个，line coverage 91.98%、branch coverage 84.32%；GitHub的preflight、PostgreSQL/Redis integration和Chromium browser contract均为`PASS`。迁移manifest记录80张表、零匿名约束和四条migration路径`PASS`，M6-001—M6-012 acceptance全部`PASS`。该提交与重签前`9be7012edf3aabe1dd5db7a325b0f36efce27063`及[GitLab pipeline #2762159878](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2762159878)均仅为历史证据，不标识当前`main`。本机无Docker，Windows PostgreSQL/Redis integration仍为`NOT RUN`；真实Telegram/provider、Control Bot polling、真实AUTO、部署和真实backup/restore保持`NOT RUN`。

M7新增deterministic occurrence/candidate、15分钟补偿扫描、DST/quiet/absolute no-send、account/contact/bypass reservation、strict Proactive Agent decision、Main AI text-only proactive context、AUTO/COPILOT final gate和send-unknown保守结算。quiet默认`22:00–08:00`，absolute no-send固定`00:00–07:00`。`0022_m7_job_scope_and_deadline`、`0023_m7_proactive_snapshot`和`0024_runtime_fencing_provenance`属于M7阶段历史迁移；当前源码 schema head 为`0036_worker_complete`。implementation source baseline `19bf0c7974b7ef2e1a3e3b8064a10d4d162353b6`由run `32229187875`验证，final acceptance baseline `7af2f524fcc4fc30fc04aa40de88a7b1302eb526`（tree `a2eaab5195c393c3905cc92620af54b7d8c208ab`）由run `32234678340`在固定 PostgreSQL 17.10/pgvector 0.8.6、Redis 8.2.8 和 Chromium 151.0.7922.34 上验证。当前本机无Docker，Windows本地PostgreSQL/Redis service test为`NOT RUN`；M8生产Compose、真实Telegram/provider、Control Bot polling、真实AUTO、production deployment、backup/restore、production load与soak保持`NOT RUN`。

常用本地门禁：

```powershell
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe scripts/check_import_boundaries.py
.\.venv\Scripts\pytest.exe
```

证据状态严格区分：

- `PASS`：在精确commit/environment执行并满足；
- `FAIL`：已执行但不满足；
- `NOT RUN`：尚未执行；
- `BLOCKED`：缺少授权、资源或外部前提。

文档静态检查通过不能替代runtime、restore或soak证据。

## 安全与授权

该设计将持有可控制Telegram账号的Session、私聊内容、长期记忆和模型credential，也会代表同一身份自动或主动发送消息，具有明显的隐私与dual-use风险。

只能由Telegram账号所有者在获得必要授权并遵守Telegram、模型provider和适用隐私规则的前提下部署。不得用于窃取Session、隐蔽监控、spam、phishing、联系人收集、限制绕过或未授权访问。

公开行为、数据、网络、备份、保留、保障措施和残余风险见根[DISCLOSURE](DISCLOSURE)。不要在public issue、commit、fixture或CI artifact中提交Session、API key、Bot token、真实私聊、完整endpoint或其他敏感证据。

## 贡献边界

- 架构语义变更必须同步总体设计、受影响详细文档、ADR、Test Strategy和Disclosure。
- 新实现从Implementation Plan的有序milestone进入，不提前开放真实Telegram副作用。
- 自动创建的Git commit必须使用项目规定的GPG签名子密钥。
- 任何公开push、PR、package、image或release都需要针对精确制品重新执行dual-use public-release review。
