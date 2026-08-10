# Telegram Personal AI Digital Twin

一个计划运行在真实Telegram用户账号上的长期个人AI分身：支持AI自动回复、真人即时接管、长期记忆、可审计上下文、COPILOT审批和受严格规则约束的主动消息。

## 当前状态

V1架构设计已经完成，M0工程脚手架已形成candidate并在Windows完成本地门禁。仓库现在包含Python package、hash lock、纯本地配置校验入口、synthetic测试和无secret GitLab CI，但仍没有Telegram/provider、PostgreSQL/Redis adapter、migration、Dockerfile、Compose部署或公开发布包。

因此：

- 只能运行M0本地安全校验和测试，不能连接Telegram/provider、启动业务服务或部署本项目；
- 文档中的最终服务与业务命令仍是实现契约，不是已经存在的入口；
- Linux CI、database/Redis integration、live Telegram/provider、backup/restore和24小时soak仍为`NOT RUN`；
- RPO 15分钟、整机RTO 2小时和2 vCPU/4 GiB/40 GiB资源profile是待实现与实测的目标。

下一步先让M0 candidate通过GitLab Linux CI并生成绑定签名commit的acceptance manifest，再关闭M0并进入[V1 Implementation Plan](docs/Implementation-Plan.md)的M1。

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

M0固定标准GIL CPython 3.14（当前patch为3.14.7）、pip 25.3和`pyproject.toml`中的构建/检查工具；runtime/dev/lock工具使用独立hash lock。跨平台IANA时区数据固定为`tzdata==2026.3`。PostgreSQL/pgvector与Redis的精确版本留在M1，Caddy与production image留在M8。Windows unit/property/contract不能替代Linux CI或Ubuntu生产证据。

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

M0外部集成开关必须全部为false；该入口没有网络、数据库、Redis或Telegram连接实现。

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

这些业务入口目前尚未实现。未来运行命令必须随实际Compose文件、migration和runbook一起加入README，并经过Test Strategy与Disclosure审查。

## 测试与证据

测试架构使用pytest、pytest-asyncio、Hypothesis、Testcontainers和Docker Compose，默认只使用synthetic fixture与fake Telegram/provider。真实Telegram只允许专用授权测试账号和allowlisted测试peer；真实provider smoke不能发送私人数据。

M0 candidate在Windows/CPython 3.14.7的本地结果：56 tests `PASS`，line coverage 97.77%，branch coverage 90.32%；Ruff、strict mypy、compileall、import boundary、build artifact Disclosure和secret/artifact扫描均`PASS`。GitLab Linux CI在candidate推送前仍为`NOT RUN`。

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
