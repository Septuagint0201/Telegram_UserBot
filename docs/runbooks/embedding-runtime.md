# Embedding 待处理任务

## 当前范围

2026-09-29 已接通 `embedding_records:pending` 的 Worker 消费者。
它处理已有的待算分块，支持当前消息文本/caption、正式记忆版本和有效摘要版本。
Memory Agent 文本流程现已自动产生这些分块，详见 [Memory runtime](memory-runtime.md)。
全量空间重建及覆盖率验收调度仍待接入；这一步不代表整个记忆功能已经完成。

## 执行与重试

1. Worker 每 60 秒的补偿扫描为 pending 记录创建确定 ID 的 `embedding.compute` 任务。
   已删除或已变更的来源直接 invalidated，不阻塞同一轮删除补偿。
2. PostgreSQL 负责任务租约和重试预算；outbox/Redis 只传递唤醒通知。重复通知不重置预算。
3. 首次执行将空间、配置版本、凭证版本号、能力摘要、来源分块 hash 和 conversation 绑定写入任务快照。
   payload 不含原文、API key 或向量；conversation 绑定使 contact/account 删除覆盖该任务元数据。
4. 事务提交后才调用 provider；Worker 继续续租。重试使用空间指定的配置与已经固定的凭证版本，
   不跟随活动配置或凭证的轮换。凭证通过现有受限 accessor 读取，未扩大数据库授权。
5. 返回后重新验证租约 owner/token/到期时间、空间、快照、来源版本/hash、删除请求与凭证可用性，
   校验向量数量、维数和有限数值，再按空间的 `none`/`l2` 策略写入 ready。
6. 暂时失败保留 pending；终止错误标为 failed，来源失效标为 invalidated。
   最后一次执行崩溃后，租约恢复和补偿扫描也会使记录 failed，不会无限重开任务。

当前只识别仓库已有的 `v1` 分块器（1500 字符、重叠 100，hash 为分块 UTF-8 字节的 SHA-256）。
未知分块版本拒绝执行。building 空间接收向量；补偿扫描中的重建组件完成当前来源覆盖、chunk hash、维度和 ready 校验后原子激活。配置切换和回退均先补齐影子空间，详见 [Worker 运行说明](worker-runtime.md)。

Embedding usage 已支持 OpenAI 的 `prompt_tokens`/`total_tokens` 响应，同时保留现有兼容适配器的
`input_tokens`/`output_tokens` 形式。接口依据：[官方 Embeddings 文档](https://developers.openai.com/api/docs/guides/embeddings)。

## 验证边界

服务器验证使用一次性 PostgreSQL/pgvector、真实 SQL 租约与删除触发器、AES-GCM 凭证解密和真实协议适配器；
provider transport 返回合成响应，不调用真实 provider，不发送 Telegram 消息。
来源编辑/删除、contact/account 删除意图、租约过期或 fencing token 变化、空间退役、凭证销毁、
轮换后的重试、跨账号引用、非首分块、通知补偿及耗尽恢复均有集成回归。

证据目录：`.artifacts/embedding-runtime-20260929/`。Memory runtime 的后续回归在 `.artifacts/memory-runtime-20260929/`，并补强了来源修改确实提交的断言。
上述为 embedding 消费者阶段证据。当前 Worker 的三个 proactive 消费者、影子空间重建和回退均已接入；实际 READY 仍取决于配置、恢复闸门及依赖。最终初验见 [Worker 完成审计](../audits/2026-09-29-worker-complete.md)，完整全链路按用户要求留待后续。
