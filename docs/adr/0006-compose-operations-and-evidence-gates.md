# ADR-0006: Compose生产基线与Evidence Gates

- Status: Accepted
- Date: 2026-08-10

## Context

系统持有Telethon Session、私聊、长期记忆、模型key与backup credential，且需要长期运行。V1资源有限，不适合同时维护Compose和Ubuntu原生两套生产方式，也不能在没有restore/soak证据时承诺RPO、RTO或容量。

## Decision

- Ubuntu Server 24.04 LTS amd64上的Docker Compose是唯一一等生产方式；原生Python只用于开发/排障。
- V1资源profile为2 vCPU、4 GiB RAM、40 GiB SSD，一个worker容器、worker concurrency 2。
- Caddy只公开TCP 443 key-only Web App；PostgreSQL、Redis、health、metrics和media不公开。
- PostgreSQL使用pgBackRest/WAL，Session使用app停止后的restic快照，恢复必须叠加独立erasure ledger并保持bootstrap maintenance。
- 测试采用pytest/Hypothesis、Testcontainers和Compose分层；真实Telegram/provider只在隔离受保护环境。
- 发布证据使用`PASS/FAIL/NOT RUN/BLOCKED`，全局coverage 85/80；2/4/40首次部署与资源敏感变更要求24小时soak。

## Consequences

- 单机没有自动HA，恢复依赖可验证backup与runbook。
- 完整监控栈不常驻2/4/40主机，需要独立外部告警。
- Digest、dependency、runner、backup target和实际性能必须在实现/部署manifest固定。
- 文档静态通过不能冒充runtime、restore或soak通过。
- 公开push仍需签名、secret scan、Disclosure和dual-use review。

## Revisit when

- 切换Kubernetes、多主机HA或另一生产OS/architecture；
- 修改公网入口、backup介质、resource profile或监控部署；
- CI/live测试credential边界或发布目标变化。

变更必须产生新的迁移、恢复、资源与public-release证据。

## References

- `docs/architecture/08-operations.md`
- `docs/architecture/09-test-strategy.md`
- `DISCLOSURE`
