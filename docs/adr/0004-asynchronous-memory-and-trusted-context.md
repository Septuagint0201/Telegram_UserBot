# ADR-0004: 异步Memory与可追溯Trusted Context

- Status: Accepted
- Date: 2026-08-10

## Context

Main AI需要低延迟读取长期记忆，但Memory提取、summary和embedding会失败、积压或使用不同模型。模型派生的memory和历史AI文本也可能包含错误事实或prompt injection，不能与系统instruction拥有相同权限。

## Decision

Memory Pipeline与Main AI回复解耦。Main AI读取最近一次已提交active memory/summary，同时直接读取最新canonical message revision；Memory滞后时扩大有界recent window，不等待Memory Agent。

Memory Agent只生成proposal。应用按schema、scope、evidence、source trust、version、conflict和confidence验证后事务提交；低置信度、歧义与image-only进入人工candidate。

Context Builder为每个item保存source、trust、slice、reason、score、token和version。Identity/personality/relationship/memory/summary/history/forward/reply/image中的模型或用户文本始终是data；只有系统与人工配置instruction可以发出指令。

Embedding模型变化使用隔离shadow space完整重建并原子切换，不混合不同空间。

## Consequences

- Memory失败不阻塞ingest和实时回复，但freshness必须可见。
- 需要manifest、watermark、source membership和递归erasure关系。
- AI/proactive output不能单独证明联系人事实或真人风格。
- Forget/delete/purge必须使派生memory、summary和embedding退出active context并重建/清理。
- Context token预算和选择算法必须稳定、可重放并有fixture。

## Revisit when

- 需要同步强一致Memory才能回复；
- 引入可验证外部知识或新的evidence root；
- 模型上下文能力使预算/检索层发生根本变化。

重访不得取消source provenance、instruction/data隔离或erasure传播。

## References

- `docs/architecture/05-memory-pipeline.md`
- `docs/architecture/06-context-contract.md`
- `docs/architecture/03-data-model.md`
