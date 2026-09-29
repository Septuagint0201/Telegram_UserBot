# Memory Agent 运行链路

## 已接入范围

2026-09-29 接通 `memory_jobs → memory.generate → model_runs`，支持 episode、
rolling_summary、consolidation 和 reconciliation 四种任务的文本输入；同日第十一步 C
补齐当前事件范围内 photo/image_document 的图片输入。
Worker 默认注册执行器，每 60 秒从 PostgreSQL 补偿通知；Redis 仅负责唤醒。

episode 提交后，按 rolling watermark 之后的当前有效消息计算摘要触发条件：
50 条 eligible revision（含图片消息）或正文约 12000 tokens（UTF-8 字节数 / 4 的估计）。
摘要任务仍受静默窗口约束。
成功提交的消息、正式记忆与摘要，会在本账号已有 active/building、chunker `v1` 的空间中
生成确定 ID 的 pending 分块；没有配置空间时保留正式数据，后续由空间重建补齐。
消费者不自动激活 building 空间。

### 图片输入与视觉证据

- 只发送已验证、ready、未过期且没有删除意图的 provider copy；必须归属于当前 revision。
  图片数量、累计字节与图片 token 估计均受所封存 capability 限制；不截断图片列表。
- 清单封存图片 SHA-256、MIME、大小、附件 ID/位置及所属 revision；provider 文档不含 storage key。
  重试重读同一来源，并在提交结果前检查完整附件集合；新增、移除、换位和来源失效都阻止写回。
- Worker 从现有只读媒体卷延迟读取，不 mkdir/chmod、不取得会写文件的 quota lock。
  调用前后校验实际字节、大小、文件类型和摘要；沿用私有存储的路径与符号链接防护。
- 本轮见过图片的全部提案保守地进入候选审核，包括附带 caption 的批次；模型省略
  `visual_only` 也不能自动接受。纯图片的 canonical root 在后续任务中继续标记为视觉来源。
  图片解读不能自动被当成用户确认的事实；人工接受仍检查消息是否当前有效。
- 无 caption 消息的 ingest body hash 保持 NULL。Memory 输入使用明确空正文 envelope 的 SHA-256，
  图片内容摘要单独封存在 media item 中；仅未 redacted、确有图片附件的空正文适用该约定。
  摘要和人工审核使用同一检查。纯图片不生成空正文 embedding。
- 历史正式记忆保留 canonical roots，但只读取本轮事件范围内的图片。因此已处理图片超过
  provider copy 保留期，不会阻塞后续文字任务。尚未处理范围内的图片过期仍明确失败，不推进水位。

当前适配器版本为 `memory-runtime-v3`。旧 v1/v2 已封存任务不能跨版本改变请求重试；升级前应完成旧任务，
或保留其失败记录并显式重建任务。不会通过清空清单、重置计数来绕过重试预算。

## 部署前置条件

- 数据库 head 为 `0036_worker_complete`，重新执行更新后的 `m8_roles.sql`。
  保留 credential version 元数据与 message event 投影状态的列级读取；0035 另外增加
  account 默认时区、contact 时区、conversation 删除标记及 event observed_at 的读取权限。
  没有增加 ciphertext 的 SELECT 权限。
- 激活独立的 `memory_agent` generation profile，包含有效配置、凭据及 capability。
  配置需支持文本、system/user 角色和现有生成协议的能力校验。
  处理图片时还须支持图片；不支持则返回 `MEMORY_VISION_UNAVAILABLE`。
- 配置对应的不可变 `prompt_versions` 记录：role 为 `memory_agent`；默认 job 的
  `prompt-v1` 对应 `version_no=1`。`template_sha256` 必须是 template_body UTF-8 的 SHA-256。
  模型角色、prompt 或凭据缺失时任务会明确失败，不会临时选用 Main AI。
- Worker 使用部署的 `credential_master_keyring` 解密，通过受限 accessor 取密文。
  `erasure_hmac_key` 以独立用途前缀计算输入/输出指纹；未结束的重试需要保持该密钥稳定。

## 执行与恢复

1. 每个 Memory job 对应固定 ID 的 `memory.generate`。同一会话按创建顺序串行处理。
   已发通知后若新消息延长静默窗口，执行器不调用模型；补偿仅能重新唤醒尚未封存、
   尚未尝试模型的 pending 任务。已耗尽或已尝试任务不能借补偿重置预算。
2. 首次准备时从当前水位开始补齐任务范围，选择当前消息、正式记忆及其 canonical roots、
   前一 active rolling summary。清单存 ID、revision、hash、信任级别和版本，不复制原文。
   消息 hash 与真实 ingest 一致，覆盖正文类型、正文和 entities；模型输入明确区分发言来源与方向。
3. 清单同时封存配置 UUID、凭据版本 UUID、prompt/capability hash、输入输出 schema。
   model run 与每次 attempt 持久化。重试重新读取同一清单，不切换到活动配置或新凭据。
   已封存 capability 可以过期，但配置和凭据仍须存在且可用，profile/credential 必须保持 active。
4. 数据库事务结束后才调用 provider。返回后重新验证 Worker owner/token/有效期、Memory job
   owner/job_version/有效期，以及每个来源的当前状态、scope 删除意图和完整输入指纹。
5. 严格解析完整 JSON，调用现有 evidence/confidence 校验。高置信度提案先记录 validating，
   再通过现有 acceptance 事务写正式记忆；candidate 仍待人工确认。目标记忆必须在输入清单中，
   使用其封存版本执行更新。未知证据或目标使整次结果失败。
6. 提案、正式数据、embedding 分块、摘要/记忆水位、run/attempt 和 domain job 的成功状态
   在同一事务内提交。模型错误、未完成输出、来源变化、租约丢失均不能推进水位。
   未投影事件、未确定发言来源不能被跳过。新出现的范围内 canonical 来源会使旧结果失效。
   图片尚未就绪返回可重试的 `MEMORY_IMAGE_PENDING`；过期/删除返回 `MEMORY_IMAGE_UNAVAILABLE`；
   图片超限返回 `MEMORY_IMAGE_LIMIT_EXCEEDED`，实际文件校验失败使用 `MODEL_IMAGE_SNAPSHOT_*`。
7. Worker 原有租约恢复处理崩溃。下次领取用新 domain fence 接管，将原 started attempt
   标为 unknown；调用失败只持久化稳定错误码，不记录原始模型输出或异常正文。

## 日/周摘要

- Worker 在调度主锁下每小时扫描；每次最多 100 个会话，下一 tick 继续下一页。
  完整轮次提交后才开始一小时等待。进程重启可重扫，持久化任务 ID 防止重复执行与预算重置。
- 时区依次采用 contact、account、deployment 的有效 IANA 值。按消息的 Telegram 创建时间
  切日，编辑仍归属原来的日期；日已结束且会话至少静默 15 分钟才入队。未投影事件、
  来源 pending、删除请求或同会话未完成 Memory job 都阻止启动。
- 日摘要使用该日当前有效文本、caption 与图片空正文标记；历史图片不重新加载像素，
  无文字图片只能表示存在一张未解读图片，不能推断内容。周摘要使用已完成的当前日摘要；
  每个非空日必须完整，空日也在模型返回后重查，迟到消息不能被旧周任务遗漏。
- 周期任务使用 `consolidation + output_schema_version=2`；既有提案 consolidation 保持 schema 3。
  `background_jobs.payload` 保存周期/时区/UTC 边界与来源摘要，不保存正文；manifest 封存
  timezone，输入 HMAC 覆盖边界。summary parent/version 同时保存这些快照和来源关系。
- 周期输出必须有 summary_text。周期覆盖独立于 rolling watermark，允许重建旧日期，
  不要求连续的全局 event ID，也不推进滚动摘要水位。发布、run/attempt、任务完成与
  embedding 分块仍在同一事务内提交。
- 扫描发现日来源变化时，先使旧日版本、递归依赖的周版本及其 embedding 失效，再排新版本。
  模型返回时重查完整来源集合、每日覆盖与双层租约；变化返回 `MEMORY_PERIOD_CHANGED`。
  同一输入的失败任务不会每小时自动获得新预算；来源实际变化才能产生新任务。
- 已安排周期的边界固定；已发布摘要本身也保存权威边界，不依赖任务 payload 永久保留。
  修改时区不会改写旧周期。新时区与旧边界重叠、或周内日边界不兼容时
  fail closed（`MEMORY_PERIOD_TIMEZONE_CONFLICT`），不会静默重切或重复计算；可通过 `MemoryPeriodRepository.repartition_history` 显式重切历史，保留旧版本 provenance。
- 大日数据按有界分块归并；episode/rolling/reconciliation 积压按连续范围拆分。
  正式历史记忆使用独立参考预算，不能挤掉新消息。单条过大来源仍明确失败。
  影子 embedding 重建、验收、激活和回退复核已接入补偿任务。

完整 Worker、维护入口与最新限制见 [Worker 运行说明](worker-runtime.md)。

## 验证边界

文本阶段证据目录为 `.artifacts/memory-runtime-20260929/`，图片阶段为
`.artifacts/memory-images-20260929/`，周期阶段为 `.artifacts/memory-periods-20260929/`。验证使用服务器一次性 PostgreSQL 17/pgvector、
真实迁移/角色授权/SQL/凭据加密/协议适配器，provider transport 返回合成结果。
没有调用真实 provider、发送 Telegram 消息或发布部署。最终数量与源码校验见
[接手审计](../audits/2026-09-20-takeover.md)。Design 保持冻结。
