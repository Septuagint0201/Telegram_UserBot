# ADR-0002: PostgreSQL事实源与Redis Dispatch

- Status: Accepted
- Date: 2026-08-10

## Context

Telegram update、模型调用、worker执行和消息发送都可能重复、乱序、超时或在结果未知时崩溃。Redis适合queue、cache、通知和短租约，但清空、AOF损坏或驱逐不能导致canonical message、outbound intent或Memory watermark丢失。

## Decision

PostgreSQL是canonical message/revision、配置、memory、summary、job、watermark、outbox、budget reservation、outbound group/intent和audit的durable事实源。

Redis/arq只负责dispatch、cache、notification、heartbeat和短期加速。领域事务在PostgreSQL中同时写入事实与outbox；worker从PostgreSQL job取得lease并以CAS提交。Redis丢失后从outbox、pending job和watermark重建。

交付语义为at-least-once。正确性来自业务唯一键、稳定幂等键、row/advisory lock、version gate、CAS和Telegram random ID对账，而不是“queue只投递一次”。

## Consequences

- Redis不可用会降低可用性，但不会成为静默数据丢失来源。
- 所有副作用必须先有durable intent，结果未知必须reconcile。
- 数据库schema和migration成为核心兼容面，必须使用真实PostgreSQL测试。
- PostgreSQL不可用时模型和自动发送fail closed。
- 运维需要pgBackRest、WAL、restore和erasure overlay。

## Revisit when

- 需要跨区域多主数据库；
- 实测证明PostgreSQL outbox/job吞吐无法满足目标；
- 引入具有同等durability与事务能力的新消息基础设施。

变更不能弱化canonical事实、erasure、random ID或unknown-send恢复。

## References

- `docs/architecture/02-message-lifecycle.md`
- `docs/architecture/03-data-model.md`
- `docs/architecture/08-operations.md`
