# Worker 完成检查与隔离初验

日期：2026-09-29。范围：完成 Worker 实现、检查代码并做隔离初验；用户已确认本阶段成果，完整全链路验证按要求留待后续进行。

## 结论

Worker 必需消费者已全部接入生产 composition。影子向量空间、大积压、历史时区重切及主动联系的扫描、决策、预算回收和 AUTO/COPILOT 发布已完成。
queue inventory 不再因缺失 Worker 消费者而报告 NOT_READY；实际 READY 仍依赖账号配置、schema、恢复闸门、磁盘及服务状态。

Design 未修改，本轮始终核对冻结 SHA-256：
`b09a2eb2aaeba20d77f9ebe7519a0b376e410f00032947a3c1e3738d8b4f5a7e`。
初验阶段保留此前工作树变更，没有创建 Git commit、部署服务或启用真实账号主动联系。随后用户授权提交推送当前成果，见 [源码收尾记录](2026-09-29-source-closeout.md)。

## 完成内容

| 范围 | 实现及关键保障 |
|---|---|
| Embedding 重建 | 独立 building space、每批 100 个来源、持久化进度；在账号/空间锁下复核当前覆盖、chunk hash、维度和 ready 状态，再原子切换；回退同样补齐新增来源 |
| 记忆积压 | 未封存范围按 32 条及约 12000 字节拆分，后续范围持久化；当前新消息为必需输入，可选正式记忆保留完整当前证据 |
| 日/周摘要 | 大日递归分块、完整覆盖后归并；内部块不冒充对外周期；迟到/编辑/删除递归失效旧摘要和向量 |
| 历史时区 | 显式维护入口验证配置、作废旧周期并重新排队，保留来源和版本；切回原时区仍发布新版本 |
| Proactive | 分页扫描当前正式事实和 canonical 人类来源；去重、send/none/defer_once、双模型封存输入、租约续期、失败重试和预算回收 |
| 交付 | Worker 原子生成 AUTO intent 或 COPILOT 草稿；App 在审批/RPC 前复核当前事实、证据、活动、模式和预算；不确定发送保守计费 |
| 到期清理 | 取消尚未产生副作用的 AUTO 计划、未审批草稿和已审批待发计划，再释放 held 预算 |

运行入口、批次限制、维护 API 和回退操作见 [Worker 运行说明](../runbooks/worker-runtime.md)。

## 检查中修复的问题

- 为主动生成增加独立 `delivery_turn_id`；模型执行仍归属 proactive job，App 交付使用独立 turn，避免改变模型 owner 语义。
- 修正不同表的 model-run 外键目标；reactive grace authorization 继续绑定原 `turn_id`。
- Worker 发布使用 INSERT 与必要列级 UPDATE，移除整表 UPDATE outbound_intents 的过宽授权；保留凭据读取边界。
- 修复 collecting COPILOT 草稿首次绑定 model run 被 metadata owner 不可变检查阻断的问题；仅允许未擦除、未阻断的首次 collecting→generating 绑定。
- 保留旧版本 service-status 历史事件的 schema revision，避免降级时因历史记录拒绝迁移。
- 修正旧迁移测试中人为插入的孤立 model-run 夹具，补齐所需 turn，并验证旧记录升级后 delivery_turn_id 正确；没有放宽生产外键。
- 修复重试预算有效期、已审批草稿到期清理、影子空间切换锁顺序、图片无正文来源哈希及大积压连续水位等边界。

## 验证结果

| 检查 | 结果及边界 |
|---|---|
| Windows / CPython 3.14.7 默认套件 | **1775 passed、4 skipped、224 deselected**；combined coverage **85.03%**，line **87.44%**、branch **77.46%**；保留原 85% 门槛 |
| Ubuntu / CPython 3.14.7 / PostgreSQL 17 + pgvector / Redis | **221 passed、1 skipped**，219.71 秒；唯一跳过项需要 Docker socket，已独立补测 |
| 独立 fresh bootstrap | **1 passed**，14.92 秒；真实初始化脚本、角色、SCRAM/peer 边界、迁移账号升级、历史 model-run 两次往返、凭据及状态角色隔离 |
| POSIX 补测 | **4 passed**；补齐 Windows 跳过的文件锁、符号链接、权限与 Bash 语法检查 |
| 定向数据库回归 | **24 passed**（最终 53.24 秒）；Worker 发布/撤销/重试、旧 COPILOT 路径、权限与迁移历史；最后补充主动交付的降级保护断言 |
| Ruff / format / strict mypy / import boundaries | PASS；480 个格式检查文件，398 个 mypy 源码文件 |
| 构建和仓库门禁 | PASS；wheel/sdist 构建与制品检查、仓库链接/敏感内容扫描、Design hash 检查 |

数据库测试使用真实 SQL、迁移、最小 Worker/App 角色、加密和协议适配器，provider transport 与消息数据为合成输入。普通回归容器无 Docker socket；独立 bootstrap 容器仅为创建一次性测试数据库挂载 socket。
完整回归的两条 pytest warning 来自既有同步导出测试继承模块级 asyncio 标记，不影响断言；未屏蔽 warning。

证据保存在 `.artifacts/worker-complete-20260929/`：默认套件 JUnit/coverage、服务器 JUnit/log、bootstrap 结果、源码逐文件 SHA-256、构建日志和静态门禁。
完整数据库回归与后续定向复测分别保留输入哈希清单，文档收尾另记录最终工作树清单；这些记录的是初验时工作树证据；后续签名提交不将其升级为完整生产发布验收。
临时数据库与 Redis 容器执行后清理，未操作既有服务数据。

## 留待后续进行

1. 固定候选源码及发布镜像，完成 Compose 服务启动/停止、readiness、依赖故障、重启和恢复闸门验证。
2. 在用户确认的账号、凭据与发送边界下完成真实 Telegram/provider、AUTO/COPILOT 全链路。
3. 完成备份恢复、删除覆盖重放、RPO/RTO、TLS、告警、资源边界及 24 小时 soak。

本轮未执行上述完整全链路，M8 生产验收仍为 NOT RUN。单条超出输入预算的来源继续明确失败；历史时区重切和向量回退目前通过维护 API 执行，尚无管理 UI。参见 [M8 兼容性与验收边界](../compatibility/m8.md)。
