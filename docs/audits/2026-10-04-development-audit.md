# Telegram_UserBot 开发副本审计与时效维护

审计日期：2026-10-04（Asia/Tokyo）。本轮完成源码、锁定依赖和本地默认验证范围内的一轮审计与修复；生产基础镜像安全更新、真实数据库/恢复及真实业务验证仍有明确未完成项。

## 基线与范围

- 开发副本：`E:\AMLY\works\Python\Telegram_UserBot`；原备份：`E:\AMLY\doc\repository-backup\Telegram_UserBot`。原备份未由本审计改动。
- Git 基线：`234cfb2178ea8a8a461e1f0227c2c1863f334f0a`；本轮只留未提交变更，没有 commit、push 或 deploy。
- 541 个 tracked 文件，其中 436 个 Python 文件；`src` 下 194 个文件（193 个 Python 文件和 `py.typed`）。清单含文件 SHA-256、行数、AST 状态和适用的覆盖率，保存于 `.artifacts/audit-20261004/tracked-inventory.json`。
- CPython 3.14.7，Windows，标准 GIL；在目标副本中新建 `.venv`，先用原 hash locks 建立基线，再更新和验证新 locks。
- Alembic 图共 36 个 revision、单一 head `0036_worker_complete`；源码 metadata 为 106 张表。没有运行数据库升级或修改迁移。
- `docs/Design.md` SHA-256 保持 `b09a2eb2aaeba20d77f9ebe7519a0b376e410f00032947a3c1e3738d8b4f5a7e`。

## 模块覆盖

所有 tracked 文件进入清单、仓库敏感信息/路径及 Markdown 链接检查；所有 tracked Python 文件完成 AST 解析，质量检查和默认测试按仓库入口执行。下表列人工审阅的重点及对应测试范围；“覆盖”不表示每个未执行分支都已动态验证。

| 范围 | 基线文件数 | 审查与验证重点 |
|---|---:|---|
| `src/.../processes` | 23 | app/control/worker 启停、有限关闭、恢复与 READY 门禁、worker lease/fencing、outbox、消费者清单、memory/embedding/proactive 管线；默认进程/恢复模拟回归 |
| `src/.../adapters` | 83 | Telegram send-unknown 与 random_id、最终发送授权、数据库事务/发布、控制命令、WebApp HMAC/输入、provider SSRF/HTTP、media 存储和擦除；单元与 fake contract |
| `src/.../domain` | 42 | AUTO/HUMAN/COPILOT 状态、上下文与记忆、主动发送预算、绝对静默/DST、redaction；默认领域与 property 回归 |
| `src/.../platform` | 30 | Secret no-follow/权限/限额、AES-GCM binding、部署配置、健康/磁盘、日志、网络端点、退出期限；平台及 operations 合约 |
| `src/.../application` 与包入口 | 16 | Ports、模块导入边界、接口契约与安全启动 |
| `alembic` | 38 | 单头图、0036 delivery_turn 外键/权限、已发布主动输出的降级保护、事务与擦除 schema 约束；图和源码静态审阅，实际数据库迁移未运行 |
| `deploy` | 54 | Compose 身份/隔离/secret/资源、Caddy 路由、恢复闸门、erasure ledger 复制、backup/export、systemd、基础镜像锁与 SBOM 状态；默认 operations synthetic/static 回归 |
| `tests` | 186 | 全量默认测试、增加输入/协议/时区回归；另行实际运行 loopback Chromium 合约 |
| `scripts` / `schemas` | 11 | 仓库敏感信息和链接扫描、导入边界、覆盖率、制品 Disclosure、验收证据格式 |
| `requirements` / `docs` / 根配置与 CI | 58 | 全部 4 个 hash lock（77 个不同锁定包）、官方更新与安全公告、可重复安装、固定版本/文档一致性；未改历史验收记录 |

## 已修复问题

| 编号/级别 | 确认的问题 | 修复与证据 |
|---|---|---|
| TUDT-01 / P1 功能 | WebApp 使用 bot-token HMAC 时错误剔除 `signature` 字段，携带该字段且由 Telegram 正确签名的 initData 会被拒绝。剔除 `signature` 只适用于另一条第三方 Ed25519 校验流程。 | `src/telegram_userbot/adapters/webapp/auth.py:79` 保留该字段参与 HMAC；测试确认合法字段通过、篡改或添加未签名字段被拒绝。依据 [Telegram 官方 Mini Apps 验签规范](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)。这是兼容性/完整性修复，不宣称旧代码存在身份认证绕过。 |
| TUDT-02 / P2 防御 | WebApp 先 `request.body()` 全量缓冲，再检查 16KiB 和 Content-Length；直连/异常 ASGI 请求可令应用继续读取超限内容。Caddy 原有限额降低了标准生产路径的暴露。 | `src/telegram_userbot/adapters/webapp/app.py:212` 逐块检查声明长度及上限，超限即拒绝、不读取后续块；2 个流式回归先失败后通过。保持统一、无敏感内容的 400 回复。 |
| TUDT-03 / P2 健壮性 | 64 字符非 ASCII hash 导致 `compare_digest` TypeError；非法 Unicode 和极大已签名 auth_date 不能稳定归类为认证拒绝。 | `auth.py:56` 与 `auth.py:82` 约束编码/十六进制 hash，`auth.py:103` 处理时间戳溢出；JSON 递归异常也统一归类。Unicode/时间戳回归先失败后通过；深 JSON 用例作为额外拒绝路径覆盖。 |
| TUDT-04 / P2 正确性 | provider URL 的显式 `:0` / `:000` 经 `parsed.port or 443` 被默默改写成默认 HTTPS 端口，违反精确端点契约。 | `src/telegram_userbot/platform/network/endpoint_policy.py:170` 区分缺省端口与 0，后者稳定拒绝；2 个 URL corpus 回归先失败后通过。不夸大为任意 SSRF 绕过。 |
| TUDT-05 / P1 依赖维护 | pip 25.3、PyJWT 2.13.0、urllib3 2.7.0 的当前 PyPI 版本漏洞元数据命中公告。 | 更新到下表修复版本，保留 SHA-256 校验；重新解析锁、安装、`pip check`、全套与 browser 回归。`Dockerfile:11` 构建阶段同样先安装更新的 bootstrap lock，避免只修开发环境。 |
| TUDT-06 / P2 时区维护 | 锁定的时区已落后 IANA 当前变更；Linux 默认优先系统 zoneinfo，单独更新 wheel 不能保证 Docker 使用它。 | 更新 tzdata 2026.5，`Dockerfile:29` 设置 `PYTHONTZPATH=""`；新增 `tests/unit/domain/test_timezone_release.py:11` 验证 Manitoba 在 2026-10-31 后维持 UTC-05。未重写历史数据库的时区/摘要记录。 |

另同步 README 的当前审计入口和 pip/tzdata 值，补上已有的 `erasure-ledger-export` 服务清单；M8 compatibility 文档明确区分历史与本轮证据。

## 依赖时效维护

对 `bootstrap.lock`、`runtime.lock`、`dev.lock`、`lock-tools.lock` 的全部 77 个不同锁定包逐一请求官方 PyPI 版本 JSON。初次查询有 2 个网络错误，最终重试全部成功；最终结果为 **77 成功、0 查询失败、0 命中 PyPI vulnerabilities 元数据**。原始和最终快照分别为 `.artifacts/audit-20261004/dependencies-baseline.json`、`dependencies-final.json`。PyPI 元数据结果不代表基础镜像、操作系统包或未知漏洞的全面安全结论。

| 组件 | 原版本 | 本轮版本 | 原因与官方依据 |
|---|---|---|---|
| pip | 25.3 | 26.2.1 | 包安装/归档与索引 URL 安全修复；[官方 changelog](https://pip.pypa.io/en/stable/news/) |
| PyJWT | 2.13.0 | 2.15.1 | 上游多个 JWK/JWT 安全边界修复及兼容修正；通过 redis 的传递依赖进入 runtime；[官方 changelog](https://pyjwt.readthedocs.io/en/stable/changelog.html) |
| urllib3 | 2.7.0 | 2.8.0 | HTTPS proxy TLS、无限 chunk-size 行、Deflate 循环的安全修复；本仓库属于 dev 工具链依赖；[官方 release](https://github.com/urllib3/urllib3/releases/tag/2.8.0) |
| pip-tools | 7.6.0 | 7.6.1 | 配合维护后的 installer 重新生成 locks；[PyPI](https://pypi.org/project/pip-tools/7.6.1/) |
| Starlette | 1.6.0 | 1.7.0 | HTTP/Host 等修正；现有 AnyIO 4.14.2 符合新 AnyIO 4 要求；[官方 release](https://github.com/Kludex/starlette/releases/tag/1.7.0) |
| Telethon | 1.44.0 | 1.45.0 | Telegram API layer 229 及 reconnect 行为修正；[官方 changelog](https://docs.telethon.dev/en/stable/misc/changelog.html#new-layer-v1-45) |
| tzdata | 2026.3 | 2026.5 | IANA 2026e，含 Manitoba 永久 UTC-05 变更；[IANA](https://www.iana.org/time-zones)、[PyPI release](https://pypi.org/project/tzdata/2026.5/) |

`pyproject.toml`、`runtime.in`、相关 policy test 与 runtime/dev locks 同步更新。继续保留 Redis 5.3.1/arq 0.28.0、SQLAlchemy 2.0.51 等原兼容组合，避免无证据地跨主版本升级。另有 33 个锁定包存在更新版本，最终清单记录其版本；没有把“并非最新版”误报为已知漏洞。

## 本轮确切验证结果

| 检查 | 本轮结果 |
|---|---|
| 原副本默认基线 | **1775 passed / 4 skipped / 224 deselected**，85.03%，28.62 秒 |
| 新回归缺陷复现 | WebApp 文件 **7 failed / 19 passed**；端点 corpus **2 failed / 5 passed / 14 deselected**，均保留 red 日志 |
| 修复后相关定向套件 | **53 passed**（后续 lint 调整端点代码后其领域文件再跑 **21 passed**） |
| 最终默认全量 | **1787 passed / 4 skipped / 224 deselected**，21.15 秒；综合覆盖率 **85.07%**，line **87.47%** / branch **77.52%** |
| 实际 Chromium WebApp 合约 | **1 passed**，Chromium **151.0.7922.34**，仅 loopback；0 外部请求、0 存储残留、未回显合成 key |
| Ruff format / lint | **PASS**，format 报告 482 files already formatted |
| strict mypy | **PASS**，399 source files |
| import boundaries | **PASS** |
| tracked Python AST / compileall | **PASS** |
| hash-locked bootstrap/dev 安装、editable 安装后 pip check | **PASS**，No broken requirements found |
| wheel/sdist 构建与制品 Disclosure 检查 | **PASS**；制品只作为本地构建验证，不作为发布物 |
| repository/link/sensitive-artifact 与 git diff --check | **PASS** |
| 安全 bootstrap smoke | **PASS**，`network_enabled=false` |
| Alembic 图和 Design hash | **PASS**，36 revisions / 单头 0036，Design 未改 |

4 个 skip 分别是 POSIX directory-fd/no-follow 清理、Bash 语法执行、POSIX flock crash/release、Unix permission 检查；本 Windows 运行不能代替这些 Linux 证据。224 个默认 deselected 保留 integration/recovery/soak/live/requires_secret 策略；单独的 browser 测试已显式运行并通过。

测试日志、JUnit、dependency snapshots、inventory、browser manifest 及质量检查记录保存在副本 `.artifacts/audit-20261004/`；`.venv`、浏览器缓存和证据均被 Git 忽略。README 中旧 2026-09-29 的测试数字保留为历史记录，没有冒充本轮新结果。

## 尚未完成的维护与执行边界

1. **P1：CPython/数据库基础镜像安全维护尚未完成。** 官方已发布 [CPython 3.14.8（2026-09-30 security release）](https://www.python.org/downloads/release/python-3148/) 和 [PostgreSQL 17.11（2026-08-13）](https://www.postgresql.org/docs/release/17.11/)。源码基础镜像仍固定 3.14.7 / 17.10。应在隔离环境核验新 digest、重建镜像、重跑 PostgreSQL/pgvector/角色/迁移和恢复回归，再更新全部引用与兼容文档。本机未发现 Docker 可执行程序，且本轮不连接生产/历史 VM；没有以修改版本字符串代替验收，也未声称现有旧基线达到最新安全状态。
2. **NOT RUN：** Docker/Compose 完整构建与启动、实际 PostgreSQL/Redis 集成、数据库 upgrade/downgrade、Ubuntu 原生 POSIX 四项验证、whole-image SBOM/license/OS 漏洞扫描。
3. **NOT RUN：** 真实 Telegram/provider、账号/Session、Bot polling、真实 AUTO、public TLS、off-host backup/restore、擦除重放恢复闸门、RPO/RTO、40GiB 受限资源与 24 小时 soak。本轮仅读取源码、使用 synthetic fixtures，没有读取或请求真实凭据。
4. 本轮依赖更新后的生产候选仍需上述隔离验证。迁移、Design、业务发送门禁和 live 授权条件均未被放宽。

## 最终锁定依赖更新差异清单

以下为官方 PyPI 元数据确认有较新版本、但本轮未升级的包；保留原兼容集，具体版本与无公告查询结果均可在最终 JSON 中核对。

| 包 | 本轮保留版本 | 查询时最新版本 |
|---|---|---|
| alembic | 1.19.1 | 1.20.0 |
| anyio | 4.14.2 | 4.15.1 |
| ast-serialize | 0.8.0 | 0.12.1 |
| build | 1.5.0 | 1.6.1 |
| charset-normalizer | 3.4.9 | 3.5.2 |
| click | 8.4.2 | 8.5.0 |
| coverage | 7.15.4 | 7.16.2 |
| cryptography | 50.0.0 | 50.0.2 |
| greenlet | 3.5.5 | 3.5.6 |
| grimp | 3.15 | 3.17 |
| hatchling | 1.31.0 | 1.32.4 |
| hiredis | 3.4.1 | 3.4.2 |
| hypothesis | 6.165.2 | 6.168.3 |
| idna | 3.18 | 3.20 |
| import-linter | 2.13 | 2.15 |
| librt | 0.15.0 | 0.16.0 |
| mako | 1.4.1 | 1.4.3 |
| markupsafe | 3.0.3 | 3.0.4 |
| mypy | 2.3.0 | 2.4.0 |
| playwright | 1.62.0 | 1.63.0 |
| psycopg | 3.3.4 | 3.3.6 |
| psycopg-binary | 3.3.4 | 3.3.6 |
| pyee | 13.0.1 | 14.0.0 |
| pygments | 2.20.0 | 2.21.0 |
| pyproject-hooks | 1.2.0 | 1.3.3 |
| python-dotenv | 1.2.2 | 1.2.4 |
| redis | 5.3.1 | 8.1.0 |
| ruff | 0.16.2 | 0.16.10 |
| sqlalchemy | 2.0.51 | 2.1.3 |
| trove-classifiers | 2026.6.1.19 | 2026.9.21.13 |
| uvicorn | 0.52.1 | 0.54.0 |
| wheel | 0.47.0 | 0.48.0 |
| wrapt | 2.3.0 | 2.5.0 |
