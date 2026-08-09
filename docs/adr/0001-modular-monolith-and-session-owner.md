# ADR-0001: 模块化单体与单Telethon Session Owner

- Status: Accepted
- Date: 2026-08-10

## Context

系统需要同时处理Telegram user account、Control Bot、模型、记忆、主动消息和后台任务。Telethon `.session` 等价于账号登录权限，并且SQLite session文件和MTProto连接不能由多个无协调进程共同持有。

过早拆成大量微服务会扩大credential、网络、部署和分布式事务边界；把全部职责放进一个进程又会让Control Bot随Telegram连接故障一起失效，并让后台任务阻塞实时回复。

## Decision

V1采用一个代码库和模块化单体，运行时拆分为：

- `app`：固定单实例，唯一持有Telethon Session并执行全部真人账号副作用；
- `control`：独立Control Bot和key-only Web App，不持有Session；
- `worker`：Memory、Embedding、Proactive和补偿任务，不直接调用Telethon；
- PostgreSQL、Redis、Caddy及one-shot migration/backup/export辅助服务。

一个Telegram账号只能有一个`app` owner。V1只运行一个账号；未来多账号采用每账号独立`app`与Session，不共享Session文件。

## Consequences

- Control面可在`app`故障时继续报告状态和设置pause。
- Main AI实时路径不等待Memory任务。
- 最终发送门禁和Session权限集中在`app`，减少重复副作用面。
- 进程间仍需PostgreSQL/Redis契约、幂等和恢复，不能依赖内存调用顺序。
- V1不提供多主机HA；单机故障通过backup、restart和reconciliation恢复。

## Revisit when

- 一个账号需要active-active owner；
- 多账号数量使单worker/control成为实测瓶颈；
- 监管或租户隔离要求独立部署边界。

任何重访都必须先解决Session ownership、消息幂等、credential隔离和跨服务事务，不能只增加副本。

## References

- `docs/architecture/01-runtime-topology.md`
- `docs/architecture/04-conversation-orchestrator.md`
- `docs/architecture/08-operations.md`
