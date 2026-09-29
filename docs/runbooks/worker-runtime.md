# Worker 实现与本轮验证边界

更新：2026-09-29。当前 schema head：`0036_worker_complete`。
本轮完成 Worker 实现与隔离初验；用户已确认本阶段成果；完整 Compose、真实 Telegram/provider 和全链路验收按要求留待后续执行。

## 运行职责

- Worker 现有后台任务、记忆、图片、摘要、embedding、删除及补偿任务继续使用 PostgreSQL 持久化任务和 Redis 唤醒。
- 主动联系使用 `proactive_jobs` 独立租约；调度主锁每分钟驱动补偿、到期领取和预算回收。
  扫描每页 25 个会话，完整轮次按策略等待（默认 15 分钟）。重启可重扫，已处理 occurrence 不重复决策。
- 已接入 `proactive_jobs`、`proactive_scan_cursors`、`proactive_budget_reservations:held`。
  queue inventory 不再存在 Worker 必需消费者缺口。READY 仍取决于 schema、账号、恢复闸门、磁盘、Redis 和消费者状态。
- Worker 生成 AUTO 发送计划或 COPILOT 草稿；Telegram RPC 由 App 原有发送入口执行。
  主动联系默认关闭，本轮未修改真实账号策略，也未发送 Telegram 消息。

## 主动联系链路

1. 从当前正式 event/intention/relationship 记忆读取规则事实；每条证据必须回到当前有效的 canonical 用户消息。
   不完整日期、失效事实、AI 自己生成的消息和未确认的视觉推断不能授权主动联系。
2. 规则层决定窗口、重要度、quiet hours、最小间隔及活动抑制；对 occurrence 去重，再入队候选。
3. Proactive Agent 只能选择 `send_now`、`defer_once`、`none`；延后只有一次。
   选中后才预留预算并由 Main AI 生成一条短消息。两次请求分别封存配置、凭据版本、prompt、来源和输入 HMAC。
4. HTTP 在数据库事务外执行，独立续租。返回后重查租约、事实、证据、模式、控制版本、联系人设置及活动版本。
   迟到输出不能覆盖新状态；异常仅记录稳定错误码，不记录原始异常或模型正文。
5. 失败重试复用已接受的决策和原预算有效期。发布发送目标与完成模型执行记录在同一事务中提交。
6. AUTO 的 App 发送前、COPILOT 的审批和发送前均再次复核。首次 RPC 授权即保守计入预算；不确定结果不能释放额度或重复计费。
   到期且没有开始副作用的发送计划、已审批待发计划和草稿被撤销，再释放 held 预算。

## 记忆积压与周期

- episode、rolling/reconciliation 的未封存输入按最多 32 条及约 12000 字节工作预算拆分；后续范围持久化，水位只在完整处理后推进。
- 新消息始终属于必需输入。正式记忆是可选参考：最多 16 项、独立 8000 字节预算，每项最多 8 条完整且当前有效的原始证据；不会截断正文或证据集合。
- 大日数据使用 event ID 的 16 进制区间递归分块，子摘要完成并覆盖全部当前来源后才归并日摘要，再生成周摘要。
  分块不作为对外日/周周期；修改或删除来源使依赖摘要和 embedding 递归失效。
- 不能容纳的单条来源仍明确失败，不能截断后虚报完整覆盖。
- 修改联系人/账号时区后，维护入口 `MemoryPeriodRepository.repartition_history` 可显式重切指定会话历史。
  它验证新配置、使旧周期失效并取消旧任务，保留 provenance；后续扫描重建。切回旧时区会发布新版本。
- Memory provider contract 升至 `memory-runtime-v4`，声明主动规则所需日期/归属字段；不兼容的旧封存请求不能混用新 contract 重试。

## Embedding 影子空间

- 激活新的 embedding 配置后，补偿任务创建独立 building space；每批最多补齐 100 个缺失来源。
- embedding records 是持久化检查点。重启、迟到来源、配置切换不会把旧向量混入新空间。
- 在账号与空间锁下检查当前来源覆盖、每个 chunk hash、维度和 ready 状态；全部通过后原子退役旧空间并激活新空间。
  失败保留原 active space。写入向量时仍验证有限数值和归一化。
- 回退时先恢复所需模型配置，再调用维护入口 `EmbeddingRebuildRepository.request_rollback`，指定账号及 retired space。
  回退空间重新进入 building，补齐新增来源并复核覆盖后才激活。

## 迁移与回退

`0036` 为 model run 增加 `delivery_turn_id`，主动模型执行继续归属于 proactive job，发送执行有独立 turn。
这保留模型 owner 的唯一性，并让现有 App/COPILOT 发送约束覆盖主动结果。

未发布主动目标时允许降级；已发布主动输出时拒绝降级，避免丢失执行归属。迁移调整 Worker 发布权限和 App 的事实状态读取权限，仍禁止 Worker 直接读取凭据密文。
升级与发布操作须在后续完整验证中按维护流程执行；本轮没有升级现有服务。

## 本轮证据

证据目录：`.artifacts/worker-complete-20260929/`。包含默认测试/覆盖率、隔离 PostgreSQL/Redis JUnit、源码逐文件 SHA-256、构建检查和 Design hash。
最终结果见 [本轮审计](../audits/2026-09-29-worker-complete.md)。
