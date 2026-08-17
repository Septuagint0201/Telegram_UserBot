# Telegram Personal AI Digital Twin

一个计划运行在真实Telegram用户账号上的长期个人AI分身：支持AI自动回复、真人即时接管、长期记忆、可审计上下文、COPILOT审批和受严格规则约束的主动消息。

## 当前状态

V1架构设计与M0—M6已经完成，M7 Proactive Pipeline实现也已完成；当前 HEAD 的 M7 Linux PostgreSQL/Redis service acceptance 仍为`NOT RUN`。此前绑定旧 HEAD 的 GitHub Actions 运行曾因迁移约束重复创建而失败，不能作为当前 HEAD 的通过证据。M6保留原有签名 GitLab/GitHub 证据；M7 Windows static/unit 门禁已通过。默认入口仍不连接Telegram或provider，也不启用真实AUTO。

因此：

- 可以运行本地安全校验、fake-only Telegram/model contract、key-only ASGI测试和显式的disposable PostgreSQL/Redis/browser integration test，但不能连接真实Telegram/provider、启动完整业务服务或部署本项目；
- 文档中的最终服务与业务命令仍是实现契约，不是已经存在的入口；
- Windows真实database/Redis、live Telegram/provider、Ubuntu production、backup/restore和24小时soak仍为`NOT RUN`；
- RPO 15分钟、整机RTO 2小时和2 vCPU/4 GiB/40 GiB资源profile是待实现与实测的目标。

精确兼容组合与平台边界见[M1 Compatibility Set](docs/compatibility/m1.md)、[M2 Compatibility Set](docs/compatibility/m2.md)、[M3 Compatibility Set](docs/compatibility/m3.md)、[M4 Compatibility Set](docs/compatibility/m4.md)、[M5 Compatibility Set](docs/compatibility/m5.md)、[M6 Compatibility Set](docs/compatibility/m6.md)和[M7 Compatibility Set](docs/compatibility/m7.md)。真实Telegram/provider仍未接入。

## 架构摘要

- Python模块化单体，生产目标为Ubuntu Server 24.04 amd64上的Docker Compose。
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
Ubuntu Server 24.04 LTS
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

该安全入口的外部集成开关仍必须全部为false；它不会启动M1数据库/Redis adapter，也没有Telegram连接实现。Alembic和integration test只能对显式提供的disposable PostgreSQL/Redis运行。

## 计划中的运行入口

最终Compose服务预计包含：

```text
https-gateway
app
control
worker
postgres
redis
migrate
session-backup
data-export
```

M2已实现可嵌入`control`进程的model-control组件，M3已实现由`app`注入已连接client后才能使用的Telethon gateway，M4提供可注入的Conversation Orchestrator/runtime与Control Bot命令边界。默认入口既不创建Telegram client、读取Session或解密provider key，也不启动Bot polling、scheduler、Caddy或Compose wiring，因此仍不存在可部署的完整业务入口和真实AUTO发送路径。未来运行命令必须随实际Compose文件和runbook一起加入README，并经过Test Strategy与Disclosure审查。

## 测试与证据

测试架构使用pytest、pytest-asyncio、Hypothesis、Testcontainers和Docker Compose，默认只使用synthetic fixture与fake Telegram/provider。真实Telegram只允许专用授权测试账号和allowlisted测试peer；真实provider smoke不能发送私人数据。

M0在Windows/CPython 3.14.7的本地结果：56 tests `PASS`，line coverage 97.77%，branch coverage 90.32%；Ruff、strict mypy、compileall、import boundary、build artifact Disclosure和secret/artifact扫描均`PASS`。签名提交`5e6f2b3512436a5ba70c958a42901b920ffa6caa`对应的[GitLab Linux pipeline #2](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2747413992)及`m0-preflight`作业均为`PASS`；其acceptance manifest绑定相同commit/tree，并将M0-001—M0-012全部记录为`PASS`。

M1在Windows/CPython 3.14.7的本地结果：86 tests `PASS`、10个integration/recovery test因本机无Docker为`NOT RUN`，总coverage 98.08%；Ruff、strict mypy、import boundary、wheel/sdist Disclosure与secret/artifact扫描均`PASS`。签名提交`9c2dbf61c8b67e75182f47abbb419ae82773678a`对应的[GitLab Linux pipeline #9](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2747812423)为`PASS`，真实PostgreSQL 17.10/pgvector 0.8.6与Redis 8.2.8上的96个测试全部通过；acceptance manifest绑定相同commit/tree并将M1-001—M1-012全部记录为`PASS`。Windows真实服务、Ubuntu production、backup/restore和production load仍为`NOT RUN`。

M2在Windows/CPython 3.14.7本地有143个默认测试通过，line coverage 92.11%、branch coverage 80.89%；本机Docker为`NOT RUN`且Chromium binary为`BLOCKED`。签名提交`def4ff1f846307a7ea428de3c048616601cab7a4`对应的[GitLab Linux pipeline #2748486868](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2748486868)全部通过：157个测试零失败/跳过，line coverage 92.11%、branch coverage 81.61%，四条migration路径、DB role、Chromium 151.0.7922.34和M2 acceptance均为`PASS`。真实Telegram/provider、Ubuntu production、backup/restore和production load仍为`NOT RUN`。

M3在Windows/CPython 3.14.7本地有179个默认测试通过，line coverage 91.41%、branch coverage 81.13%；本机没有Docker daemon，因此PostgreSQL/Redis integration为`NOT RUN`。签名提交`41f4160a6d53bdd34e2654f08a90a4b61b6675e8`对应的[GitLab Linux pipeline #2751916211](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2751916211)全部通过：`m3-telegram-fake`在PostgreSQL 17.10/pgvector 0.8.6与Redis 8.2.8上执行202个测试，1个browser测试按策略deselect，line coverage 93.22%、branch coverage 84.65%；4条migration路径、12项content-free replay和M3-001—M3-010 acceptance均为`PASS`。真实Telegram ingest/send与Session owner运行时保持`NOT RUN`。

M4的补强已完成：race/acceptance从JUnit stable test ID派生，continuation逐段验证原source与human takeover，Control Bot只入队command/outbox并由app executor写accepted/no-op/rejected终态，非status command保留SQL `NULL`而status payload保持JSON object。Windows/CPython 3.14.7有228个default测试通过、31个非默认测试deselect，line coverage 89.53%、branch coverage 80.73%，Ruff、strict mypy、import boundary与compileall通过；本机无容器运行时，M4 PostgreSQL integration仍为`NOT RUN`。签名提交`2b1ba2974d44bbd323d329f0421012dbe651638f`对应的[GitLab Linux pipeline #2758187631](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758187631)九个作业全部通过：M4 service job执行258个测试、1个deselect，line coverage 92.52%、branch coverage 84.50%，migration、JUnit-derived race、M4 acceptance和artifact scan全部`PASS`。M4-001—M4-011因此关闭；真实Telegram/provider、真实AUTO与部署运行时保持`NOT RUN`。

M5签名提交`9e6aeaf3a50ff58826a6830492c766a7983da9b6`对应的[GitLab Linux pipeline #2758537825](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2758537825)共11个作业全部`PASS`：service job执行288个测试并deselect 1个，total coverage 90.68%，同时通过migration、role、browser、acceptance和artifact门禁。真实Telegram/provider、部署、backup/restore与production load保持`NOT RUN`。

M6在Windows/CPython 3.14.7有289个default测试`PASS`、41个非默认测试deselect，line coverage 88.71%、branch coverage 80.05%；Ruff、strict mypy、import boundary、compileall、offline migration SQL、wheel/sdist Disclosure和secret/artifact scan均为`PASS`。M6签名证据基线`645fb8da5d5c35de6896825c5f29f22f08d0b168`已由[GitLab pipeline #2763001231](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2763001231)的13个作业和[GitHub Actions #31907584107](https://github.com/Septuagint0201/Telegram_UserBot/actions/runs/31907584107)验证：GitLab M6 service执行329个测试并deselect 1个，line coverage 91.98%、branch coverage 84.32%；GitHub的preflight、PostgreSQL/Redis integration和Chromium browser contract均为`PASS`。迁移manifest记录80张表、零匿名约束和四条migration路径`PASS`，M6-001—M6-012 acceptance全部`PASS`。该提交与重签前`9be7012edf3aabe1dd5db7a325b0f36efce27063`及[GitLab pipeline #2762159878](https://gitlab.com/Septuagintks/telegram_userbot/-/pipelines/2762159878)均仅为历史证据，不标识当前`main`。本机无Docker，Windows PostgreSQL/Redis integration仍为`NOT RUN`；真实Telegram/provider、Control Bot polling、真实AUTO、部署和真实backup/restore保持`NOT RUN`。

M7新增deterministic occurrence/candidate、15分钟补偿扫描、DST/quiet/absolute no-send、account/contact/bypass reservation、strict Proactive Agent decision、Main AI text-only proactive context、AUTO/COPILOT final gate和send-unknown保守结算。quiet默认`22:00–08:00`，absolute no-send固定`00:00–07:00`。Windows/CPython 3.14.7 的 M7 unit tests、Ruff、strict mypy、import boundary、build、Disclosure和artifact static checks为`PASS`。`0022_m7_job_scope_and_deadline`和`0023_m7_proactive_snapshot`为前序迁移，当前 head 为`0024_runtime_fencing_provenance`：补齐 legacy nullable outbound provenance、发送 lease/fencing 和 model-run manifest metadata 的迁移闭环；GitHub Actions service acceptance for current HEAD 为`NOT RUN`，因此不把历史运行结果绑定到当前提交。当前本机无Docker，Windows本地PostgreSQL/Redis service test为`NOT RUN`；真实Telegram/provider、Control Bot polling、真实AUTO、部署、backup/restore、production load与soak保持`NOT RUN`。

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
