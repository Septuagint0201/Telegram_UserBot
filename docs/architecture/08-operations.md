# Operations

## 1. 文档状态

本文档定义 Telegram Personal AI Digital Twin V1 在单台 Ubuntu Server 上使用 Docker Compose 的生产部署、配置、Secret、HTTPS、队列、迁移、媒体、retention、日志、health、备份恢复、资源、升级和灾难恢复契约。

总体设计见`docs/Design.md`；进程、网络、volume和Session所有权见`docs/architecture/01-runtime-topology.md`；消息恢复见`docs/architecture/02-message-lifecycle.md`；数据、retention和erasure模型见`docs/architecture/03-data-model.md`；模式及maintenance/BLOCKED门禁见`docs/architecture/04-conversation-orchestrator.md`；Memory、Context和Proactive后台任务分别见`05-memory-pipeline.md`、`06-context-contract.md`和`07-proactive-pipeline.md`。

当前状态：V1实现前的Operations基线。所有版本、digest、证书、域名、凭据、备份目标和实测容量值必须在部署时写入deployment manifest；本文给出的资源和时限是默认上限或验收目标，不是已经运行验证的结果。

## 2. 已确认决策

| 主题 | V1决策 |
|---|---|
| 生产拓扑 | 单台Ubuntu Server，Docker Compose为唯一一等生产部署方式 |
| 资源基线 | 2 vCPU、4 GiB RAM、40 GiB SSD |
| CPU架构 | `linux/amd64`为V1验收基线；其他架构必须独立验证全部镜像与恢复流程 |
| HTTPS gateway | Caddy；只有TCP 443公网入站，使用自动HTTPS与TLS-ALPN-01或显式验证的证书方案 |
| 任务队列 | arq + Redis；PostgreSQL job/outbox/watermark始终是durable事实源 |
| Secret | root控制、secret-specific reader GID的host files通过Compose secrets挂载；不把secret放入`.env`或镜像 |
| 模型凭据加密 | AES-256-GCM envelope encryption，版本化master key keyring |
| 数据库备份 | pgBackRest，持续WAL、每日differential、每周full、off-host S3-compatible加密仓库 |
| DR目标 | PostgreSQL RPO不超过15分钟，整机RTO目标2小时；必须以演练实测确认 |
| Session备份 | 每日及升级前一致性快照，独立加密，保留7天 |
| 图片 | 20 MiB、40 MP、单边16384 px、30秒；original 30天、provider copy 24小时、总配额10 GiB |
| Retention | 隐私平衡profile；canonical message/active memory不按年龄自动删除 |
| 可观测性 | JSON stdout + Docker轮换 + internal Prometheus metrics；完整监控栈不在2/4/40基线内 |
| 更新 | 人工、固定revision/digest、先备份后迁移；禁止Watchtower式自动容器更新 |
| Ubuntu补丁 | 自动安装安全更新，不自动重启；reboot进入维护窗口 |

## 3. 目标、范围与非目标

### 3.1 目标

本文保证部署设计能够：

1. 在全新、满足前置条件的Ubuntu主机上按可重复runbook启动。
2. 只公开key-only Web App所需的Caddy 443入口。
3. 让Session、Bot token、API key、数据库凭据和master key遵守最小挂载范围。
4. 在Redis丢失、worker重启、模型失败和容器重建后从PostgreSQL恢复工作。
5. 在SIGTERM、迁移、升级和恢复过程中阻止stale或重复Telegram副作用。
6. 对40 GiB磁盘和4 GiB内存设置有界使用、清理与fail-closed门禁。
7. 让PostgreSQL和Session备份具备独立加密、恢复验证与erasure重放。
8. 让日志、metrics、告警和`/server_status`不复制secret或私聊正文。

### 3.2 非目标

V1不提供：

- Kubernetes、Docker Swarm、多主机HA或自动failover；
- Ubuntu原生systemd Python服务与Compose的双生产维护；
- Control Bot访问Docker Socket、宿主机shell或任意管理API；
- PostgreSQL、Redis、health、metrics或媒体的公网入口；
- 自动数据库downgrade、自动容器更新或无人审批的恢复后自动发送；
- 本机默认运行Prometheus、Grafana、Loki和Alertmanager完整栈；
- `media-data`远端备份；
- 对未演练的RPO/RTO、吞吐或资源余量作保证；
- 在Operations文档中保存真实域名、IP、Telegram ID、bucket、access key或任何credential。

## 4. 支持矩阵与主机前置条件

### 4.1 生产支持面

V1生产验收目标：

```text
Ubuntu Server 24.04 LTS
linux/amd64
Docker Engine + Compose v2 plugin
2 vCPU
4 GiB RAM
40 GiB SSD
public DNS name
public TCP 443 forwarded directly to Caddy
UTC-synchronized system clock
off-host S3-compatible backup target
```

Ubuntu 22.04 LTS或`linux/arm64`可以在实现后作为新支持矩阵加入，但在完整Compose、图片解码、pgvector、pgBackRest、Session restore和resource soak返回证据前不得声称等价支持。

### 4.2 主机要求

- 专用主机或专用VM，不与不可信工作负载共享Docker daemon。
- 使用SSH key；关闭密码登录和root远程登录。
- `ufw`/等价防火墙只允许受限管理来源的22和公网443；不开放80、5432、6379或内部health端口。
- NTP/systemd-timesyncd处于synchronized；时钟漂移超过30秒时Web App credential写入和time-sensitive proactive work fail closed。
- 数据盘使用LUKS2或云平台等价的at-rest volume encryption，并记录到deployment manifest。
- 禁止未加密disk swap；4 GiB基线优先使用最多1 GiB zram，不把secret page写入普通swap partition。
- Docker data root、Compose项目目录和`/etc/telegram-userbot`不得位于网络共享文件系统。

### 4.3 目录与权限

建议宿主机布局：

```text
/opt/telegram-userbot/             reviewed deployment checkout
/etc/telegram-userbot/config/      root:root 0750, non-secret config
/etc/telegram-userbot/secrets/     root:root 0700；source file为root:<secret-reader-gid> 0440
/var/lib/telegram-userbot/         root:root 0750, local ops state only
/var/log/telegram-userbot-ops/     root:adm 0750, sanitized host runbook logs
```

部署操作者使用独立`telegram-userbot-deploy`账号。加入`docker`组等价于root能力，只授予受信任维护者；应用容器不能挂载Docker Socket。Repository checkout不得包含`.session`、`.env` secret、backup credential或实际Caddy certificate private key。

## 5. Deployment manifest 与配置分层

### 5.1 Deployment manifest

每个部署维护不含secret的`deployment-manifest.yaml`，至少记录：

```text
deployment_id
source_commit
compose_project_name
image names and immutable digests
Ubuntu release and architecture
Docker/Compose versions
public base URL and timezone
PostgreSQL/pgvector schema compatibility
active backup/retention policy versions
resource profile name = v1_2cpu_4g_40g
last migration/backup/restore-drill IDs and outcomes
```

Manifest可以进入私有运维备份，但公开仓库只保存schema/example，不能保存真实主机、bucket、账号或内部endpoint。

### 5.2 配置层级

从低到高：

1. 代码中的安全默认值和schema。
2. 版本控制的Compose/Caddy非secret模板。
3. deployment manifest和root-only非secret host config。
4. PostgreSQL中的immutable active policy/model config versions。
5. `/run/secrets/*`只读secret文件。
6. maintenance/restore期间显式的host hard gate，例如`BOOTSTRAP_MAINTENANCE=1`。

高层不能改变低层定义的能力边界：数据库配置不能公开新端口、增加volume挂载、修改master key路径、开放Docker Socket或放宽SSRF root policy。

### 5.3 Environment变量

环境变量只传递非secret定位与开关，例如：

```text
APP_ENV=production
DEPLOYMENT_ID
TZ
DATABASE_HOST
REDIS_HOST
PUBLIC_BASE_URL
CONFIG_FILE
SECRET_FILE_PATHS
BOOTSTRAP_MAINTENANCE
```

API key、Bot token、Telegram API hash、数据库密码、Redis密码、S3 secret、repo cipher passphrase和master key禁止进入environment、Compose interpolation output、`docker inspect`或shell history。

### 5.4 启动校验

任何常驻服务在ready前验证：

- production config没有placeholder/default credential；
- 全部secret文件为regular file、非symlink、owner/mode符合policy；
- deployment ID、database scope、schema revision和active policy一致；
- 系统时间、timezone和公共URL合法；
- 不允许的debug/raw capture默认关闭；
- resource/retention/media limit不是无界值；
- restore hard gate没有被意外清除。

失败时输出稳定错误code，不输出secret path内容、配置正文或credential fingerprint之外的值。

## 6. 镜像、构建与供应链

### 6.1 镜像集合

| Image | 用途 |
|---|---|
| application image | `app/control/worker/migrate`共享代码，使用不同entrypoint |
| Caddy image | `https-gateway` |
| PostgreSQL image | PostgreSQL + pgvector + pgBackRest |
| Redis image | queue/cache/AOF |
| session-backup image | one-shot SQLite校验与restic备份，不常驻 |
| data-export image | one-shot受限数据库导出与age公钥加密，不常驻 |

所有镜像固定完整digest。浮动`latest`、启动时`pip install`、从互联网下载脚本以及容器内自更新均禁止。

### 6.2 Application image

- 使用固定CPython minor和Debian slim基线；实现阶段通过依赖测试选择并锁定精确版本/digest。
- 使用lockfile与hash校验安装依赖，runtime image不包含compiler、package manager cache、Git metadata或测试credential。
- 创建固定非root UID/GID；`app/control/worker/migrate`默认非root运行。
- root filesystem只读；仅显式volume、`tmpfs /tmp`和必要runtime目录可写。
- 设置`no-new-privileges:true`、drop all capabilities、默认seccomp、有限`pids_limit`。
- build context通过`.dockerignore`排除`.git`、`.handoff`、`.env*`、Session、backup、media、logs和本机cache。

### 6.3 PostgreSQL与扩展

PostgreSQL major、pgvector和pgBackRest版本在实现时作为一个经过迁移/恢复测试的compatibility set固定，不从浮动apt repository在容器启动时安装。更新任一成员都视为数据库升级，必须完成第26节流程。

## 7. Compose拓扑

### 7.1 常驻与一次性服务

| Service | 类型 | Restart | 对外端口 |
|---|---|---|---|
| `https-gateway` | 常驻 | `unless-stopped` | `443/tcp` |
| `app` | 常驻 | `unless-stopped` | 无 |
| `control` | 常驻 | `unless-stopped` | 无 |
| `worker` | 常驻 | `unless-stopped` | 无 |
| `postgres` | 常驻 | `unless-stopped` | 无 |
| `redis` | 常驻 | `unless-stopped` | 无 |
| `migrate` | one-shot | `no` | 无 |
| `session-backup` | `ops` profile one-shot | `no` | 无 |
| `data-export` | `ops` profile one-shot | `no` | 无 |

`migrate`成功是业务服务ready的必要条件。Compose `depends_on`只能优化启动顺序，服务仍必须自行重试依赖并验证schema/ownership。

### 7.2 Networks

```text
edge
backend
backup-egress
```

| Service | edge | backend | backup-egress |
|---|---:|---:|---:|
| `https-gateway` | 是 | 否 | 否 |
| `control` | 是 | 是 | 否 |
| `app` | 否 | 是 | 否 |
| `worker` | 否 | 是 | 否 |
| `postgres` | 否 | 是 | 否 |
| `redis` | 否 | 是 | 否 |
| `migrate` | 否 | 是 | 否 |
| `session-backup` | 否 | 否 | 是 |
| `data-export` | 否 | 是 | 否 |

Gateway只能解析`control`的Web App端口。`postgres`和`redis`只bind容器网络，不发布host port。Docker network本身不是完整egress firewall；endpoint SSRF和host firewall仍按第14节执行。

### 7.3 Volumes

| Volume | 挂载边界 | 备份 |
|---|---|---|
| `postgres-data` | postgres读写 | pgBackRest |
| `pgbackrest-spool` | postgres读写 | 不单独备份 |
| `redis-data` | redis读写 | 不作DR事实备份 |
| `telethon-session` | app读写；受控backup/restore窗口例外 | restic加密备份 |
| `media-data` | app读写、worker只读 | 默认不备份 |
| `caddy-data` | gateway读写 | 不备份，可重新签发 |
| root-only `export-staging` bind | data-export写；host operator读 | 24小时内清理，不进入普通backup |

Session例外必须满足：`app`已停止并释放account lock；one-shot helper只读挂载Session volume；helper退出后立即解除挂载；任何常驻非app服务仍不得挂载该volume。

### 7.4 Compose secret source

Compose secrets引用`/etc/telegram-userbot/secrets`中的root-controlled files并在容器内挂载到`/run/secrets`。本地file source可能以bind mount实现，不能假设Compose long syntax一定会重写`uid/gid/mode`；具体行为以[Docker Compose secrets官方文档](https://docs.docker.com/compose/how-tos/use-secrets/)和部署版本的验收结果为准。

每个secret分配固定、记录在deployment manifest中的reader GID；宿主机source为`root:<secret-reader-gid> 0440`，只有需要它的非root容器进程获得对应supplementary group并挂载该secret。共享master keyring可以使用仅授予`app/control/worker`的共享reader GID，per-service数据库密码使用不同GID。Fresh-install验收必须在每个容器内证明“应读者可读、其他服务未挂载/不可读”，但不得输出内容；不能以`0444`或让应用以root运行来规避权限错误。

## 8. 2 vCPU / 4 GiB / 40 GiB资源profile

### 8.1 Memory limits

| Service | memory limit | 说明 |
|---|---:|---|
| `postgres` | 768 MiB | `shared_buffers`初始192 MiB，实际以DB测试校准 |
| `redis` | 256 MiB | Redis数据目标上限192 MiB，`noeviction` |
| `app` | 640 MiB | 包含单图片完整解码峰值 |
| `worker` | 768 MiB | worker concurrency 2，图片读取并发1 |
| `control` | 256 MiB | Bot/Web App轻量路径 |
| `https-gateway` | 128 MiB | Caddy与证书状态 |
| `migrate` | 256 MiB | maintenance窗口one-shot |
| `session-backup` | 256 MiB | app停止后的one-shot |
| `data-export` | 256 MiB | maintenance/低负载one-shot |

常驻limit合计约2.75 GiB，剩余空间供Ubuntu、Docker daemon、page cache和短时抖动。Migrate、backup、restore和本机完整监控栈不得与高负载业务任务同时运行。

### 8.2 CPU与并发

- 全局只有2 vCPU，不用硬性reservation假装物理隔离；通过container CPU limit、nice/ionice和应用semaphore抑制后台任务。
- `app` conversation generation每个conversation串行；全账号Main AI同时provider calls默认最多2。
- `worker` job concurrency默认2；Memory/summary/embedding CPU-heavy本地阶段合计最多1。
- 图片下载/完整解码并发1。
- PostgreSQL migration、backup和ANN index build进入maintenance或低负载窗口。
- pgBackRest `process-max`默认1；性能证据支持后才能提高。

### 8.3 Redis

```text
appendonly yes
appendfsync everysec
maxmemory 192mb
maxmemory-policy noeviction
```

Redis达到限制时新queue/cache写入失败并告警，不驱逐durable-work提示造成静默遗漏。PostgreSQL outbox/scan负责恢复；AOF不是灾难恢复事实备份。

### 8.4 Disk budget与门禁

`media-data`逻辑hard quota为10 GiB；应用同时检查数据库累计大小和filesystem free space。整机阈值：

| 使用率 | 行为 |
|---:|---|
| 70% | warning，报告增长速率与最大类别 |
| 80% | 运行到期清理、限制debug、暂停非必要ANN rebuild |
| 90% | 停止新媒体下载和proactive/低优先级后台工作；保留文本ingest与对账 |
| 95%或剩余<1 GiB | account operational BLOCKED；停止新模型/发送，完成可安全事务后断开Telethon并告警 |

不能通过删除未过期canonical message、active memory、audit或未知发送记录来临时腾空间。Docker image/build cache清理由host runbook按精确对象执行，不允许应用访问Docker Socket。

## 9. Host与容器安全加固

### 9.1 Ubuntu

- `unattended-upgrades`只自动安装安全更新；自动reboot关闭。
- 需要reboot时进入maintenance，先备份并完成第25节优雅停止。
- SSH只允许key，建议限制管理CIDR并启用登录失败速率限制。
- 不安装无关Web面板、数据库管理器或公网容器管理UI。
- Docker daemon socket仅维护者可访问，不通过TCP公开。
- 每月审计开放端口、用户、sudo/docker组、timer、mount和过期secret。

### 9.2 Containers

除上游数据库/网关镜像必须的受控例外外：

```text
user: non-root UID/GID
read_only: true
cap_drop: [ALL]
security_opt: [no-new-privileges:true]
pids_limit: finite
tmpfs: /tmp with size/noexec/nosuid
```

需要写入的Session、media、database、Redis、Caddy data和spool都使用精确volume。不能使用`privileged`、host PID/network、设备映射或Docker Socket。

## 10. Caddy与HTTPS

### 10.1 Public boundary

Public base URL固定为一个专用HTTPS origin。Caddy只把以下路径反向代理给`control`：

```text
/webapp/*
/api/v1/model-keys/*
```

其他路径返回404；不代理health、metrics、Bot handler、数据库、Redis、app或worker。Request body默认最大16 KiB，超限在gateway或control进入应用前拒绝。

### 10.2 Certificate

Caddy使用[Automatic HTTPS](https://caddyserver.com/docs/automatic-https)。稳态防火墙只开放443，因此部署必须验证TLS-ALPN-01可以完成签发/续期；不能满足时需要维护者显式选择DNS challenge或受控证书导入，不能静默开放新的公网端口。

证书续期失败在到期前30天warning、14天critical。Caddy data volume包含TLS private material，只由gateway挂载；不进入日志、Git或普通备份。

### 10.3 HTTP headers与日志

至少设置：

```text
Strict-Transport-Security after first successful issuance/renewal
Content-Security-Policy with exact Telegram/self origins
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Cache-Control: no-store for credential API
```

Credential route不启用Caddy response cache。Access log不记录query/body/Authorization/Cookie/initData/launch token；只保留时间、method、route template、status、latency、truncated request ID和经过privacy review的client network category。

## 11. Key-only Telegram Web App

### 11.1 Launch authorization

`/model_key <role>`创建256-bit随机launch token，只保存HMAC/hash并绑定：

```text
allowlisted administrator ID
Control Bot identity/chat
logical role
action set = set|replace|delete
created_at/expires_at
used_at
```

默认5分钟过期且只能消费一次。Token不得放在普通URL query；使用Telegram签名覆盖的start parameter或URL fragment传递，并在客户端兼容测试中证明Caddy/access log看不到明文token。

### 11.2 `initData` validation

每个credential POST都按Telegram [Mini App data validation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)在server端使用Control Bot token验证原始`initData`。额外要求：

- `auth_date`不早于当前时间5分钟，未来偏差最多30秒；
- signed user ID与launch token的allowlisted admin完全一致；
- Bot identity、role、action和deployment一致；
- launch token未使用、未过期且使用CAS消费；
- server clock synchronized；
- 不信任客户端传入的username、role、configured状态或endpoint。

签名比较使用constant-time函数。JSON解析设置深度、长度和重复字段策略；验证前不执行数据库credential写入或模型请求。

### 11.3 Browser boundary

- 同origin、无CORS wildcard、无credential cookie；POST携带签名initData和one-time launch proof。
- 不把API key写入URL、DOM持久属性、localStorage、sessionStorage、IndexedDB、service worker、analytics或crash reporter。
- 输入框使用password type和autocomplete禁用提示，但披露浏览器/键盘/系统仍可能缓存输入。
- 页面关闭、成功或失败后尽快覆盖JS变量和表单；不能声称JavaScript内存可可靠擦除。
- 成功response固定为空的204，不返回credential状态、version、原key、ciphertext、nonce或可用于认证的片段；状态和版本只通过Control Bot查看。
- 不加载第三方analytics、广告、字体、CDN脚本或任意外部图片。

### 11.4 Rate limit与审计

M2先以PostgreSQL对每admin执行15分钟最多5次credential请求、对每admin/role执行每分钟最多2次写请求；M8在Caddy可信代理边界建立后再叠加脱敏client network维度。Rate limit可在Redis加速，但PostgreSQL状态、token CAS和credential version仍是事实源。审计只记录admin ID、role、action、result code、credential version和request ID，不记录key、initData、token、IP全文或request body。

## 12. Secret inventory与挂载矩阵

| Secret | app | control | worker | migrate | postgres | gateway | session-backup | data-export |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Telegram API ID/hash | 只读 | 否 | 否 | 否 | 否 | 否 | 否 | 否 |
| Control Bot token | 否 | 只读 | 否 | 否 | 否 | 否 | 否 | 否 |
| credential master keyring | 只读 | 只读 | 只读 | 否 | 否 | 否 | 否 | 否 |
| PostgreSQL role password | app role | control role | worker role | migrator role | server | 否 | 否 | export role |
| Redis password | 只读 | 只读 | 只读 | 否 | server | 否 | 否 | 否 |
| pgBackRest repo credential/cipher | 否 | 否 | 否 | 否 | 只读 | 否 | 否 | 否 |
| restic Session repo credential | 否 | 否 | 否 | 否 | 否 | 否 | 只读 | 否 |
| TLS private material | 否 | 否 | 否 | 否 | 否 | Caddy data | 否 | 否 |

`migrate`只获得migrator数据库凭据。`data-export`只获得可读取allowlisted export view的数据库角色；age recipient是非秘密配置，private decryption key永不进入服务器。两者均不获得Telegram、Bot、master key、model key、Session或backup secret。不同用途不能共用一个password/key。独立外部告警credential只由host monitor或独立monitoring profile持有，不挂载到上述业务服务。

## 13. API key加密、轮换与恢复

### 13.1 Envelope format

模型API key使用AES-256-GCM。每个credential version保存：

```text
key_id
random 96-bit nonce
ciphertext + authentication tag
AAD schema/version fingerprint
created_at/retired_at
non-secret status fingerprint
```

AAD至少绑定deployment ID、logical role、credential identity和version，防止跨role/部署替换密文。Nonce由CSPRNG每次新建，绝不在同一key下复用。数据库只保存ciphertext；32-byte master keys只存在root-only keyring和独立离线恢复介质。

### 13.2 Rotation

1. 在host keyring加入新key ID并标记encrypt-active，旧key为decrypt-only。
2. 原子重启/滚动业务进程，使三者加载相同keyring generation。
3. `control`按row lock/CAS逐条重新加密active credential，保留新version和audit。
4. 验证所有active/in-flight credential references可解密且模型config不漂移。
5. 从在线keyring移除旧key前，确认没有active/in-flight引用。
6. 旧key离线恢复副本保留到所有可能引用它的加密数据库backup过期；之后执行双人确认销毁。

轮换不通过Bot消息传key，不原地改ciphertext且不修改模型非secret config version。

### 13.3 API key set/replace/delete

- Set/replace在一个事务中创建credential version、验证expected profile/role、切换active pointer并写audit/outbox；旧version保留为非active、可按已启动run的固定引用读取，直到受控销毁。
- Delete先阻止新run，等待或终止安全的in-flight references，再销毁active ciphertext并使模型profile`credential_missing`。
- Provider验证失败不删除旧active key；新key保持unvalidated或立即redact，按管理员选择重试。
- API key永不通过读取API返回。Control Bot只显示configured、last validation result和version。

### 13.4 Master key丢失

没有online key时，模型调用和credential写入fail closed，消息ingest、human outgoing识别、删除和状态查询继续。恢复顺序：

1. 进入maintenance并停止新模型/发送。
2. 从独立离线介质恢复匹配key IDs，校验权限和deployment binding。
3. 对每个credential执行只在内存中的authenticated decrypt probe，不调用provider。
4. 运行受控模型validation后再清除maintenance。

若无法恢复，管理员只能通过key-only Web App重新输入各role key；系统不得伪造旧credential已恢复。

## 14. 模型Endpoint URL与SSRF防护

### 14.1 两类endpoint

```text
public HTTPS endpoint
explicitly allowlisted private HTTP/HTTPS endpoint
```

默认只允许解析到公网unicast地址的HTTPS。Private endpoint必须由维护者在root-only deployment network policy中声明精确scheme、normalized hostname、port和可接受CIDR；Control Bot只能选择已存在policy ID，不能创建或放宽allowlist。

### 14.2 Canonicalization与拒绝

在credential或任何请求发送前：

- URL必须是绝对`http/https`，拒绝userinfo、fragment、空host、异常port和非规范编码。
- Hostname执行IDNA/case/trailing-dot normalization，保存canonical tuple。
- Public HTTPS解析得到的全部A/AAAA都必须是允许的公网地址。
- 默认拒绝loopback、RFC1918、link-local、multicast、unspecified、CGNAT、IPv6 ULA、云metadata和Docker内部地址。
- Private allowlist要求每个resolved address都落入显式CIDR，且scheme/host/port完全匹配。
- 禁用从environment继承HTTP(S) proxy，除非root policy显式配置受信任proxy。
- 禁用自动redirect；3xx作为validation/request错误。后续若支持redirect，必须对每一跳重新执行完整policy且限制次数。
- DNS结果只短期缓存；连接前和重试前重新验证。实现必须防止DNS rebinding或在连接transport中绑定已验证地址，同时保留正确TLS SNI/hostname verification。

`localhost`、Unix socket、`file:`、`ftp:`、`data:`、任意callback URL和消息内URL永不成为model endpoint。

### 14.3 TLS与请求

- 验证系统信任链与hostname；`verify=false`不能通过Bot配置。
- Private CA只能由root policy挂载精确CA bundle，不能由模型配置上传。
- Adapter只使用受控认证header，不允许provider options注入任意header、method或path traversal。
- Endpoint validation先通过network policy，再使用对应role credential进行最小probe；probe response不记录body。
- 每次active config保存network policy、resolved category和TLS capability snapshot。

### 14.4 Egress审计

日志/metrics只记录endpoint ID、scheme、public/private category、port、policy version、result code和latency；不记录完整URL query、DNS完整响应、Authorization或provider body。检测到policy drift时新请求fail closed，旧in-flight request按snapshot与deadline结束或取消。

## 15. PostgreSQL、pgvector与Alembic migration

### 15.1 Startup migration

标准启动：

```text
postgres/redis healthy
  -> migrate one-shot acquires global advisory lock
  -> alembic upgrade head
  -> schema/extension inventory check
  -> app/control/worker allowed to become ready
  -> gateway routes credential traffic
```

常驻服务的expected revision必须与image内revision完全一致。Schema过旧、过新、多head、pgvector缺失或extension version不兼容均not ready；应用启动时不执行`create_all()`或隐式DDL。

### 15.2 Migration gate

每次生产migration要求：

1. reviewed/signed source revision和immutable image digest已记录；
2. 最近1小时内成功的pre-upgrade pgBackRest backup和Session backup；
3. WAL archive lag低于15分钟且off-host repository可读；
4. account进入maintenance，停止新模型/发送并完成unknown reconciliation；
5. migration在单独one-shot container运行，只有migrator credential；
6. command、duration、from/to revision和result写sanitized ops audit；
7. 失败时业务服务保持not ready，不自动downgrade或反复重启migration。

### 15.3 Expand/migrate/contract

- Expand先增加兼容列/表/索引。
- Backfill使用小batch、durable cursor、可中断重入，不持有长事务。
- `CREATE INDEX CONCURRENTLY`按Alembic autocommit边界执行。
- 至少跨一个已部署application version完成dual read/write验证后才contract。
- Destructive migration确认retention、export、erasure和backup恢复边界，并在release note显式标记不可逆。
- Database downgrade默认不支持；需要回退数据时使用受控restore并接受RPO内数据损失决策。

### 15.4 pgvector maintenance

ANN index按embedding space/dimension独立建立。V1默认使用pgvector HNSW，初始`m=16`、`ef_construction=64`、query `ef_search=100`，ANN candidate仍按Context Contract执行exact distance rerank和stable tie-break。Build/reindex进入maintenance或低负载窗口，worker concurrency降为1，并监控临时磁盘；可用空间不足`estimated build bytes * 2 + 2 GiB`时拒绝开始。新space验证完成前旧space保持active；任何参数调优需要真实dataset recall/latency/memory证据。

## 16. arq、Redis与任务恢复

### 16.1 角色边界

arq负责async dispatch、defer和worker执行；使用方式以[arq官方文档](https://arq-docs.helpmanual.io/)为准。它不是业务事实源：

```text
domain transaction
  -> durable job/watermark/outbox in PostgreSQL
  -> outbox publisher enqueues arq notification
  -> worker claims PostgreSQL job with lease
  -> idempotent execution
  -> CAS terminal/retry state
```

Notification丢失、重复或arq result过期不改变job事实。`job_id`使用稳定HMAC用于减少重复，但PostgreSQL unique/CAS才是最终正确性防线。

### 16.2 Redis payload

Queue payload只包含：

```text
job type
durable job ID
account/conversation scope IDs
generation/version
correlation ID
```

禁止消息正文、memory正文、prompt、API key、initData、Session、endpoint credential、context preview正文和raw provider body。arq result默认不保留或只保留不含正文的短期状态最多60秒。

### 16.3 Lease与执行参数

- PostgreSQL job lease默认60秒，每20秒续租；task class可提高lease但保持同一renew ratio。
- Worker concurrency 2；CPU-heavy/image local stage全局semaphore 1。
- Infra job最多5次execution，full-jitter backoff基线`5s/30s/120s/600s`，超过5次或24小时进入dead letter。
- Proactive candidate window、COPILOT expiry、erasure priority和业务deadline优先于通用retry次数。
- Stable schema/evidence/policy错误立即terminal，不用队列重试掩盖。
- Worker SIGTERM停止claim，最多90秒完成；未完成job释放/等待lease过期。

### 16.4 Model attempt defaults

| Role | 默认attempt total deadline | 合法配置范围 | 同logical run最多attempt |
|---|---:|---:|---:|
| `main_ai` | 90秒 | 5–300秒 | 3 |
| `memory_agent` | 180秒 | 5–300秒 | 3 |
| `proactive_agent` | 60秒 | 5–300秒 | 3 |
| `embedding` | 60秒 | 5–300秒 | 3 |

只对DNS/connection reset、429、明确retryable 5xx和provider暂时不可用重试；honor `Retry-After`但单次自动等待最多30秒且不得越过turn/candidate deadline。Backoff使用full jitter，默认1秒和5秒。确定性4xx、auth、schema、tool、content/refusal、length和malformed为terminal。每次retry前重验generation/mode/evidence。

### 16.5 Telegram FloodWait

自动等待上限15分钟。未开始RPC的intent在到期后重跑门禁；已开始或结果未知的intent先按random ID对账。FloodWait大于15分钟进入dead letter/operational alert，不创建replacement group、不更换random ID，也不在恢复后盲发。

### 16.6 Redis loss

Redis不可用时：

- app仍可在PostgreSQL可用时持久化incoming，但自动generation/send进入degraded或按领域门禁暂停；
- control只执行可直接事务提交且不依赖异步副作用的安全操作；
- worker停止claim；
- Redis恢复或清空后，outbox、pending jobs、watermarks和compensation scans重新发布；
- cache miss只影响性能，不能读取stale cache替代数据库。

### 16.7 Reservation与短期授权TTL

- AUTO proactive budget reservation：最多10分钟，且不得越过candidate window/deadline。
- COPILOT proactive reservation：最多30分钟，且不得越过draft/candidate expiry。
- Web App launch/initData：5分钟；Control Bot action token使用各领域更短TTL。
- Reservation reaper每分钟扫描；只有证明没有active draft/send lease/unknown副作用时才release。

## 17. `media-data`限制、清理与故障

### 17.1 Admission limits

| 限制 | V1值 |
|---|---:|
| Telegram下载字节 | 20 MiB hard limit |
| 完整解码像素 | 40,000,000 pixels |
| 任一边 | 16,384 px |
| connect timeout | 10秒 |
| total download/validation timeout | 30秒 |
| 同时下载/解码 | 1 |
| volume logical quota | 10 GiB |

只接受Message Lifecycle定义的JPEG、PNG和WebP。Streaming download在超过声明或实际byte limit时立即停止并删除temp；完整decode在隔离worker/thread和内存预算内执行。EXIF orientation应用后再检查最终dimensions，并生成metadata-free provider copy。

### 17.2 Storage lifecycle

```text
temp partial             -> request结束立即删除，crash残留最多1小时
validated original       -> 30天
metadata-free provider copy -> 24小时
rejected/quarantined bytes -> 不保留；只留reason metadata 7天
```

Telegram delete、contact purge和account wipe不等待TTL，立即使对象不可见并排队物理清理。任何过期/删除对象不得从旧manifest、cache或provider file reference恢复为模型输入。

### 17.3 Cleanup

Worker每小时扫描到期对象，使用数据库claim、object key allowlist和幂等unlink：

1. CAS标记`delete_pending`，Context查询立即排除。
2. 验证resolved path仍位于`media-data`根目录且不是symlink。
3. 删除文件并fsync parent directory。
4. 标记`deleted`或稳定错误；重复不存在视为成功。
5. 删除失败重试，24小时仍失败进入critical alert。

清理不能跟随message提供的路径、glob或未解析环境变量。

### 17.4 Quota behavior

达到10 GiB或整机90%阈值后停止新图片下载。Message仍以metadata/caption持久化；需要图片的Main AI turn标记`media_unavailable_quota`并fail closed，不静默改为“看过图片”的文本答复。Media默认不进入远端backup，失去文件后历史消息只保留manifest/metadata。

## 18. Retention policy

### 18.1 V1 privacy-balanced profile

| Data class | Retention |
|---|---|
| canonical message/revision | 不自动过期；delete/purge/wipe优先 |
| active memory/summary | 不按年龄自动删除；memory lifecycle/forget优先 |
| validated original image | 30天 |
| provider image copy | 24小时 |
| read/typing/service transient metadata | 7天 |
| rejected media metadata | 7天 |
| model/outbound attempts | terminal后30天，只留metadata/usage/error code |
| active memory candidate | 最多30天，之后expire/redact候选正文 |
| terminal memory review/COPILOT draft正文 | terminal后30分钟内redact |
| proactive topic/brief | terminal后30天redact，保留hash/code |
| control input/action session | terminal后30分钟；unused token按自身更短TTL |
| context preview DB metadata | 30天；正文永不落库 |
| known Bot context preview message | 默认10分钟后best-effort delete |
| raw diagnostic capture | 默认关闭；显式启用最多1小时 |
| service status/model attempt aggregate | 30天 |
| non-content audit | 365天 |
| encrypted export artifact | 24小时 |
| retired credential ciphertext | 无in-flight引用后最多7天，再redact |
| pgBackRest backup sets | 约4周，按4个weekly full sets管理 |
| encrypted Session backup | 7天 |

Retention数值进入versioned policy和每row snapshot。缩短policy可以排队提前清理；延长policy不复活已redacted/expired内容。

### 18.2 Cleanup schedule

- media/temp：每小时；
- token/session/terminal draft：每10分钟；
- operational rows与candidate：每日03:30 deployment local time；
- audit与backup expiry：每日，先验证新备份/ledger状态；
- contact/account erasure：高优先级，不等待普通schedule。

每批限制rows、bytes和事务时间，保存cursor。Cleanup lag超过目标两倍warning，超过24小时critical。Disk pressure可以加速已到期数据清理，不能提前删除未到期敏感事实作为一般容量策略。

### 18.3 Diagnostic capture

只有allowlisted管理员在Control Bot二次确认、指定scope/reason并设置不超过1小时TTL后才能启用。使用独立diagnostic key加密；不捕获API key、Session、Bot token、master key、完整Authorization或context preview。到期执行crypto erase和物理清理；普通debug log不能成为绕过capture policy的替代路径。

### 18.4 Data export

V1不扩展key-only Web App或Control Bot文件下载。Allowlisted管理员可以创建durable export request；host timer在低负载窗口启动one-shot`data-export`，使用受限DB role重建未redacted数据并以维护者预配置的[age](https://age-encryption.org/) public recipient加密到root-only`export-staging`。Private decryption key永不进入服务器。

Artifact只通过既有受限SSH/SFTP管理通道由host operator取回，使用request ID + SHA-256核对；不通过Telegram Bot、Caddy或公共URL传输。成功/失败后24小时内幂等删除artifact，DB只留hash、状态和时间。默认只包含media manifest，不包含图片bytes、Session、model/API/backup credentials或raw provider payload。

## 19. 日志、metrics与告警

### 19.1 Structured logs

所有服务输出一行JSON到stdout/stderr，至少包含：

```text
timestamp UTC
level
service/instance
event_code
correlation/request/job/run/group IDs
state transition/retry ordinal
duration/result code
```

禁止正文、Prompt、memory、contact display name、username、手机号、完整Telegram ID、完整endpoint URL、initData、launch token、credential fingerprint可认证片段、secret path内容和stack local values。Exception经过stable classifier；production默认不输出完整traceback给Control Bot。

Docker使用`local`日志driver或等价轮换，每container默认`10 MiB x 5 files`。Host ops脚本日志只记录command ID、revision、duration/result，不记录environment或`docker inspect`secret mount内容，保留30天。

### 19.2 Metrics

Internal `/metrics`只在backend/monitoring网络暴露，至少包括：

- ingest/turn/model/job/delivery result与latency histogram；
- queue depth/oldest age/dead-letter；
- DB pool、Redis、heartbeat、scheduler lag；
- provider role/protocol/result category，不含endpoint/模型自由文本label；
- media bytes/quota/reject/cleanup；
- disk/memory/CPU/container restart；
- backup age/WAL archive lag/restore drill age；
- certificate expiry、clock sync、credential validation age；
- unknown/partial sends、erasure/retention lag。

Telegram/contact/conversation/message/memory/run具体ID不作为metric label。Metrics不包含正文或secret。

### 19.3 Monitoring deployment

2/4/40基线不常驻本机Prometheus/Grafana/Loki。支持：

1. 外部Prometheus通过受限VPN/SSH tunnel或host collector拉取；或
2. 另行扩容后启用独立`monitoring` Compose profile。

启用本机完整监控前必须重新测量memory/disk并修改resource profile；不能挤占PostgreSQL、app或worker保障空间。

### 19.4 Alerts

Critical事件可由Control Bot发送不含正文的管理员告警，但还必须配置一个独立于`control`/Telegram的外部uptime或host告警通道。至少告警：

```text
app/control/worker down or restart loop
PostgreSQL/Redis unavailable
account lock/session unauthorized
unknown/partial send reconciliation lag
dead-letter/queue lag
disk >= 80/90/95%
backup age/WAL lag/restore drill overdue
certificate expiry/clock unsynchronized
credential master key/config invalid
erasure/retention cleanup failure
```

Alert去重和cooldown默认5分钟；critical恢复也发送一条resolved事件。告警路由失败不能阻塞业务事务。

## 20. Liveness、readiness与`/server_status`

### 20.1 Healthcheck参数

常驻服务内部healthcheck默认：

```text
interval 10s
timeout 3s
retries 3
start_period 30s
```

Application image使用内置Python healthcheck命令，不为此增加curl和shell依赖。PostgreSQL使用`pg_isready`加schema外部readiness；Redis使用authenticated`PING`。Health endpoint不返回异常、config、path或依赖body。

### 20.2 Semantics

- Liveness只证明event loop/health server未死锁，不因单个外部provider失败变false。
- Readiness证明核心职责当前安全：schema、DB/Redis、ownership、Session/Bot loop、required config和maintenance gate。
- Provider endpoint失败使对应role degraded，不把整个进程liveness置false。
- Disk 95%、restore bootstrap gate、Session unauthorized或account lock丢失使app not ready并禁止发送。
- Healthcheck失败可触发container restart，但restart不是业务恢复正确性的替代。

### 20.3 Heartbeat与status

Heartbeat保持10秒刷新、30秒过期。`/server_status`聚合heartbeat、直接依赖probe和PostgreSQL重要状态，输出`healthy/degraded/down/unknown`及稳定reason。它增加Operations字段：

```text
disk band/media quota
backup age/WAL lag/last restore drill
certificate days remaining
clock sync
retention/erasure lag
resource profile
deployment source commit/image digest abbreviations
```

不得返回真实主机路径、bucket、IP、secret、联系人数据或完整内部endpoint。

## 21. PostgreSQL backup

### 21.1 Repository与schedule

pgBackRest使用独立off-host S3-compatible bucket/prefix、least-privilege credential和repo encryption；具体能力/配置以[pgBackRest官方文档](https://pgbackrest.org/user-guide.html)为准。

```text
WAL archive: continuous, archive-async, monitored
full backup: Sunday 02:00 deployment local time
differential backup: other days 02:00
retention: 4 full backup sets and dependent WAL
process-max: 1 on the 2/4/40 profile
```

Repository encryption passphrase、S3 access secret和credential master key互不复用。S3 policy限制到指定bucket/prefix和backup所需动作；不授予其他bucket或账号权限。

### 21.2 Backup gates

- 每次backup先运行stanza/check、确认archive可用和DB不在migration。
- Backup ID、type、start/stop、LSN、size、repo、result和source revision进入不含secret的ops audit。
- 只有新backup完成并通过`info/check`后才expire旧set。
- WAL archive lag超过5分钟warning，超过15分钟critical，表示RPO目标未满足。
- 每日backup超过36小时未成功critical；不自动删除现有backup或执行upgrade。
- Backup失败不直接删除业务数据，也不能无限重试压垮2 vCPU主机。

### 21.3 Verification

每月在独立临时主机/volume执行完整restore drill：

1. 恢复最近full/diff/WAL到目标时间。
2. 运行PostgreSQL启动、schema/extension inventory、约束抽样和row counts。
3. 应用最新独立erasure ledger overlay。
4. 以`BOOTSTRAP_MAINTENANCE=1`启动应用只读验证，不连接真实发送路径。
5. 记录实际RPO/RTO、artifact IDs和失败。
6. 销毁临时明文volume并保留无正文证据。

没有成功演练时RPO/RTO是目标而不是PASS声明。

## 22. Telethon Session与必要配置备份

### 22.1 Session schedule

每日04:30及每次upgrade/migration前执行：

1. 设置app maintenance/not ready，停止新模型与send。
2. 优雅停止`app`，等待account lock释放和Session SQLite关闭。
3. 启动one-shot `session-backup`，只读挂载`telethon-session`。
4. 对SQLite执行`integrity_check`并确认预期表；失败不备份损坏快照。
5. 使用restic流式加密写入独立off-host repository，保留7天。
6. Helper退出并卸载volume，立即启动app并验证Session authorization/account lock。
7. 记录backup snapshot ID、hash、duration/result，不记录Session内容。

Restic使用以[官方文档](https://restic.readthedocs.io/en/stable/)为准的authenticated encrypted repository；repository password和S3 credential只挂载one-shot helper，与pgBackRest、master key分开。

日常备份短暂停机期间Telegram更新依靠重新连接catch-up，但必须通过Telegram fake/真实授权测试证明不丢canonical update。Backup失败仍应尝试重启app并critical alert；pre-upgrade Session backup失败阻止升级。

### 22.2 Config与key recovery

- 非secret Compose/Caddy/config由signed Git revision恢复。
- Deployment manifest和sanitized ops state每日进入独立加密配置备份。
- Credential master keyring、erasure HMAC key和Session repository recovery secret由维护者分别保存在离线密码管理/恢复介质，不进入自动数据库backup。
- Caddy certificate可重新签发，不备份private key。
- Redis、cache、pgBackRest spool和`media-data`不作为DR备份源。

### 22.3 Independent erasure ledger

每个completed erasure通过durable outbox把无正文HMAC ledger增量写入独立加密off-host对象，并定期生成累计snapshot。Restore必须取得晚于目标database backup的最新ledger snapshot；导出lag超过15分钟critical。Ledger export失败不重新暴露已在live DB删除的数据，但阻止旧点恢复后开放模型/自动发送。

## 23. Restore与灾难恢复

### 23.1 Restore原则

- 所有restore先在`BOOTSTRAP_MAINTENANCE=1`下启动。
- 恢复来源必须绑定deployment/source revision、backup IDs、hash和key IDs。
- Redis从空实例重建，不用旧AOF覆盖PostgreSQL事实。
- 未完成erasure replay、unknown-send reconciliation、credential/session验证前禁止任何自动发送。
- Restore不是自动failover；需要allowlisted维护者审批和记录。

### 23.2 全机恢复顺序

1. 准备满足第4节的全新Ubuntu主机，安装已批准Docker/Compose。
2. Checkout并验证目标signed source commit与image digests。
3. 配置防火墙、加密volume、domain和root-controlled service-readable secrets，保持443未路由业务。
4. 使用pgBackRest恢复PostgreSQL到最新可用一致点或明确PITR目标。
5. 应用最新独立erasure ledger，完成所有redaction/cleanup jobs。
6. 恢复匹配的Session snapshot；若不用快照则走显式重新登录。
7. 恢复master/erasure keys或计划通过Web App重新输入model keys。
8. 启动空Redis、运行schema/pgvector compatibility检查和必要forward migration。
9. 启动control/worker/app但保持bootstrap maintenance；验证Bot、Session、account lock和credential decrypt。
10. 对账outbox、system_pending、unknown/partial group/intents、expired drafts/takeover和budget reservations。
11. 运行只读context/memory/proactive integrity抽样和backup restore acceptance。
12. 维护者通过Control Bot/host双重确认后清除bootstrap gate，再开放Caddy credential API和AUTO。

### 23.3 RPO/RTO

PostgreSQL目标RPO 15分钟、整机目标RTO 2小时。Session每日backup意味着Session文件RPO最多24小时，但Telegram authorization通常可从较旧有效Session恢复；若失效必须重新登录，RTO可能超过2小时。每次演练分别报告DB、Session、erasure和整机实际结果，不能合并为虚假PASS。

### 23.4 Point-in-time restore风险

恢复到过去会丢失该时间之后的canonical incoming/human outgoing/配置。执行PITR前必须记录maintainer decision、目标时间和影响；最新erasure ledger仍必须覆盖过去快照，防止已删除数据复现。不能把PITR作为普通application rollback。

## 24. 优雅停止与强制终止

### 24.1 Compose grace

| Service | stop grace |
|---|---:|
| `app` | 90秒 |
| `worker` | 90秒 |
| `control` | 30秒 |
| `https-gateway` | 30秒 |
| `redis` | 60秒 |
| `postgres` | 120秒 |

### 24.2 Application sequence

`app`：not ready → stop new model/group → best-effort cancel → finish/mark unknown RPC → stop update intake → disconnect Telethon/close Session → release account lock。

`worker`：stop claim/scheduler → allow leased jobs within grace → CAS complete或让lease expire → final heartbeat。

`control`：stop newWeb App/Bot sessions → finish committed transaction → invalidate uncommitted launch/input sessions → stop polling/server。

PostgreSQL先拒绝新application work，再fast shutdown/checkpoint；不能作为常规操作使用immediate shutdown。强制kill后启动恢复必须执行Message Lifecycle reconciliation、job lease回收和Session integrity检查。

### 24.3 Prohibited operations

未经明确数据销毁授权禁止：

```text
docker compose down -v
docker volume prune
删除/重建 postgres-data 或 telethon-session
用空Session覆盖原volume
清空unknown intents/outbox/erasure ledger
```

普通redeploy使用`stop/up/recreate`且保留named volumes。

## 25. 故障与降级矩阵

| 故障 | 自动行为 | 恢复 |
|---|---|---|
| PostgreSQL不可用 | 所有持久化/模型/send fail closed，app停止消费并断开 | DB恢复后schema/ownership/reconciliation |
| Redis不可用/丢失 | queue/cache暂停，PostgreSQL继续保存可安全事实 | 空Redis + outbox/job/watermark重发 |
| app崩溃 | 不创建新发送，unknown RPC保留 | account lock + Session + intent reconciliation |
| control崩溃 | 无新管理/key写入；既有app模式继续 | Bot/Web App重启，旧session/token按TTL/CAS |
| worker崩溃 | Memory/Proactive/cleanup延迟，Main AI用已提交memory | lease过期重领与补偿扫描 |
| Caddy/证书失败 | Web App不可用，Bot和app内部路径不受影响 | 证书/route修复，不开放通用HTTP |
| model endpoint失败 | 对应role有限重试后terminal/degraded，不发错误文本 | 配置验证或provider恢复 |
| master key不可用 | 模型和credential写入blocked，ingest/删除可继续 | 离线key恢复或重新输入keys |
| Session unauthorized/corrupt | app not ready、不自动登录/发送 | 受控restore或人工登录 |
| disk 90% | 停媒体/proactive/低优先worker | 到期清理、扩容、host cache维护 |
| disk 95% | operational BLOCKED并安全断开 | 腾出已授权空间/扩容后完整校验 |
| backup/WAL lag | critical告警，禁止upgrade和旧backup expiry | repo/network/credential修复并验证新backup |
| clock drift >30s | Web App key写入和time-sensitive work blocked | NTP恢复、重新计算未开始窗口 |
| host reboot | Compose重启但业务ready受schema/lock/session gate约束 | startup recovery后自动或人工clear gate |

依赖恢复不自动回复HUMAN/PAUSED/maintenance期间backlog，也不补发过期proactive window。

## 26. Upgrade、rollback与补丁

### 26.1 Manual upgrade runbook

1. 确认目标source commit已reviewed且GPG signature有效；固定全部image digest。
2. 对公开发布候选重新执行dual-use/disclosure/secret检查；本地部署不等于公开许可。
3. 检查disk、queue、unknown sends、backup/WAL、clock和certificate。
4. 完成最近1小时内DB + Session pre-upgrade backup。
5. 进入maintenance，等待active RPC/job到安全点。
6. 拉取exact images，先运行`compose config`与migration dry/static checks。
7. 启动postgres/redis，运行one-shot migrate。
8. 启动app/control/worker，保持AUTO blocked；验证health、schema、Session、Bot、model config和reconciliation。
9. 启动Caddy并执行key-only route/TLS smoke。
10. 维护者清除maintenance，观察至少30分钟后记录completion。

禁止Watchtower、浮动tag和容器内自升级。Dependency或OS安全更新也必须进入reviewed revision/maintenance流程。

### 26.2 Application rollback

只有migration仍向后兼容时才可切回前一image digest；旧应用必须在新schema上通过compatibility check。若contract migration已删除旧字段，则application rollback BLOCKED。

### 26.3 Database rollback

Alembic downgrade不作为默认恢复手段。需要回退数据库时在maintenance下从pre-upgrade pgBackRest backup/PITR恢复，明确接受backup点之后数据损失，并应用最新erasure ledger。该动作需要maintainer decision，不能自动执行。

### 26.4 Ubuntu security updates

安全更新自动安装但不自动reboot。Kernel/Docker更新要求maintenance、backup和post-reboot smoke。Docker major/Compose plugin变化先在staging/restore host验证，不在生产主机直接试验。

## 27. Ubuntu原生运行边界

原生进程只用于开发或故障排查：

- 使用project `.venv`和同一lockfile，不提供独立production dependency set。
- 真实Session进程启动前必须停止Compose `app`并取得同一account lock；优先使用隔离测试账号/Session copy。
- PostgreSQL/Redis仍通过Compose或受限loopback/tunnel使用，不把端口开放公网。
- 原生运行不绕过migration、secret file、SSRF、mode、intent或audit契约。
- 不为原生模式维护生产systemd auto-restart、backup或HA承诺。
- 排障结束后清理临时credential、debug capture和port forward，并重新执行Compose readiness。

## 28. 标准运维runbook

### 28.1 Fresh install

```text
verify host prerequisites/firewall/encryption/time
checkout and verify signed source
create root-controlled config/secrets with validated reader GIDs from templates
validate docker compose config without printing secrets
start postgres + redis
run migrate one-shot
start app + control + worker
verify internal health and reconciliation
start Caddy and verify TLS/key-only routes
configure/verify off-host DB and Session backups
run acceptance smoke
enable AUTO only by explicit administrator action
```

### 28.2 Daily review

```text
/server_status summary
container restart/health
queue/dead-letter/unknown sends
disk/media growth
WAL archive and latest backup
certificate/clock
retention/erasure lag
security update/reboot pending
```

### 28.3 Maintenance commands

Runbook可以使用不含secret的确定命令，例如：

```text
docker compose config --quiet
docker compose ps
docker compose run --rm migrate upgrade head
docker compose exec -T postgres pgbackrest --stanza=app check
```

不能在文档、shell history或issue中粘贴`docker compose config`完整输出、environment、Session、backup config或HTTP Authorization。任何删除/restore命令在实现runbook中必须先解析并显示精确目标，再要求确认。

## 29. 安全、隐私与Disclosure边界

生产Disclosure必须准确说明：

- 只有Caddy 443 key-only Web App公网入口；Bot、Telegram user account和model/S3/ACME为出站网络。
- PostgreSQL保存可查询私聊/记忆明文，依赖加密磁盘、权限与加密backup保护。
- API key通过Telegram Web App输入并由AES-256-GCM application encryption保护，但`app/control/worker`属于可解密trusted boundary。
- Session是可控制Telegram账号的高敏感credential，每日off-host加密备份增加恢复能力也增加副本风险。
- `media-data`不备份且按30天/24小时过期；canonical文本和active memory不按年龄自动过期。
- Raw diagnostic默认关闭，日志/metrics不含正文；V1不发送产品telemetry或analytics。
- 正常外部连接限Telegram、配置验证后的模型endpoint、S3-compatible backup、ACME和显式告警目标。
- Backup restore可能复现旧数据，因此最新erasure ledger必须在模型读取/发送前覆盖恢复点。
- RPO/RTO、资源上限和控制都是设计目标，只有测试返回后才能声称实施有效。

Operations变更不能弱化根`DISCLOSURE`。未来新增外部监控、cloud KMS、media backup、port、telemetry或remote admin API都属于material disclosure change。

## 30. 自动化与人工验收矩阵

后续Test Strategy至少覆盖：

| 场景 | 必须结果 |
|---|---|
| 全新Ubuntu 24.04 amd64主机 | 只按文档可启动到maintenance-ready |
| 公网端口扫描 | 只有443和受限SSH；5432/6379/health/metrics不可达 |
| Compose/image inspection | 无secret env、无Session/image、无Docker Socket、digest固定 |
| Secret权限/symlink/placeholder错误 | 对应服务not ready且不泄露值 |
| Caddy签发/续期失败 | Web App失败并告警，不开放HTTP fallback |
| initData篡改/过期/未来/replay/非admin | credential不写入、token不错误消费 |
| API key set/replace/delete | key不回显、不入log；版本/rotation/CAS正确 |
| SSRF loopback/private/metadata/IPv6/DNS rebind/redirect | 默认拒绝；只有root allowlist精确通过 |
| Redis清空/AOF损坏 | 从PostgreSQL恢复pending jobs，不重复副作用 |
| 最多5次job execution后dead letter | 4段稳定backoff后terminal，payload不含正文 |
| FloodWait <=/>15分钟 | 前者重验门禁；后者dead letter且不换random ID |
| 20 MiB/40 MP/16384/timeout/炸弹 | reject并清理temp，不传模型 |
| media 10 GiB/整机90/95% | 按阈值降级/BLOCKED，不删canonical事实 |
| retention虚拟时钟 | 每类精确TTL、redaction、不会复活 |
| data export | age public recipient、受限view、hash/24小时清理，Caddy/Bot无下载路径 |
| SIGTERM与SIGKILL每个crash point | Session/lease/unknown sends幂等恢复 |
| pgBackRest full/diff/WAL | 可恢复且实测RPO <=15分钟目标 |
| Session backup/restore | 一致、加密、app独占例外正确、账号可验证 |
| 恢复旧DB + 最新erasure ledger | 删除正文不复现、自动发送保持blocked |
| pre-upgrade backup或migration失败 | upgrade停止、旧服务/数据不被伪装成功 |
| compatible/incompatible app rollback | 前者可执行，后者明确BLOCKED |
| 2 vCPU/4 GiB/40 GiB 24小时soak | 无OOM/restart storm，queue/latency/disk在阈值内 |
| 日志/metrics/alert/backup artifact扫描 | 无secret、正文、initData、Session或private endpoint泄露 |
| independent alert channel失效 | Control Bot告警不掩盖外部通道状态，反之亦然 |

Runtime、restore和soak未实际运行前标记`NOT RUN`，文档静态检查不能替代这些证据。

## 31. Test Strategy与实现边界

`docs/architecture/09-test-strategy.md`已把第30节转换为unit/property/contract/Testcontainers/Compose/live/restore/soak层级，并固定synthetic-only数据、隔离Telegram账号、受保护Ubuntu runner、85/80 coverage和四态证据格式。

实现阶段必须补充：

- exact image versions/digests、lockfile与SBOM策略；
- Compose/Caddy/config/secret templates；
- Alembic migration和fresh install脚本；
- pgBackRest/restic配置与systemd timer；
- backup/restore/upgrade/erasure runbook；
- monitoring rules和external alert channel；
- 2/4/40环境实测resource tuning。

任何实测后修改默认值都要更新本文、deployment policy version、Disclosure和回归测试，不能只改Compose变量。

## 32. 完成检查表

- [x] Caddy 443、edge/backend/backup网络、Compose services和volume边界已定义。
- [x] Ubuntu/amd64生产支持面与原生开发/排障边界已定义。
- [x] Config layering、Compose secrets、启动校验和最小挂载矩阵已定义。
- [x] Key-only Web App initData、one-time launch、rate limit和无回显已定义。
- [x] AES-256-GCM credential keyring、轮换、删除和恢复已定义。
- [x] Public/private model endpoint、DNS/redirect/TLS/SSRF规则已定义。
- [x] Alembic/pgvector migration、expand-contract和rollback边界已定义。
- [x] arq/Redis/PostgreSQL durable job、lease、retry、deadline和dead-letter已定义。
- [x] pgBackRest/WAL和Session/restic backup、RPO/RTO、restore/erasure replay已定义。
- [x] 20 MiB/40 MP/16384 px/30秒、10 GiB和media retention已定义。
- [x] 全部short-term retention、cleanup schedule和diagnostic TTL已定义。
- [x] Data export的age public-recipient、root-only staging、SSH/SFTP-only取回和24小时清理已定义。
- [x] JSON logs、metrics、alerts、health、heartbeat和server status已定义。
- [x] 2 vCPU/4 GiB/40 GiB resource/disk thresholds和degraded modes已定义。
- [x] SIGTERM、upgrade、rollback、security update和DR runbook已定义。
- [x] 可自动化/人工验证的Operations acceptance matrix已定义。
- [x] Compose、security、backup/restore、live smoke与2/4/40 soak均已映射到Test Strategy证据层级。
