# ADR-0005: Mode-gated Proactive与Outbound Safety

- Status: Accepted
- Date: 2026-08-10

## Context

系统以真人Telegram身份发送AI与主动消息。模式切换、真人接管、新incoming、模型迟到、worker重试和RPC unknown都可能让原本合法的结果过期。主动模型若能自行发现联系人或绕过时间/预算规则，会扩大误发和滥用风险。

## Decision

- Account default与conversation override提供AUTO/HUMAN/COPILOT；pause、maintenance、temporary HUMAN和operational BLOCKED作为高优先级overlay。
- 每个run/turn/draft/intent保存account control、mode、content、evidence、policy和activity version；副作用前在conversation lock内重验。
- AI、proactive、COPILOT-approved输出先创建durable delivery group与ordered intents，由`app`使用稳定random ID发送和对账。
- Proactive候选只能来自deterministic reason allowlist与typed evidence；无candidate时零模型调用。
- Quiet hours、absolute no-send、activity suppression、daily budget、minimum interval和atomic reservation由规则层执行。
- AUTO可在全部门禁后发送；COPILOT只生成待批准draft；HUMAN/PAUSED/maintenance/BLOCKED不运行主动模型或补backlog。

## Consequences

- 系统偏向少发而不是误发。
- 模型不能扩大候选、预算或权限。
- 真人outgoing和mode变化会使未开始的AI副作用stale。
- RPC unknown必须保守对账和计费，不能盲目replacement。
- 需要大量race、state-machine、crash和real Telegram adapter测试。

## Revisit when

- 新增自动恢复/ask-backlog策略；
- 支持group/channel或多个账号；
- 改变主动消息默认预算、quiet规则或审批模式。

所有变化必须保留最终`app`门禁、provenance、幂等与管理员即时停止能力。

## References

- `docs/architecture/02-message-lifecycle.md`
- `docs/architecture/04-conversation-orchestrator.md`
- `docs/architecture/07-proactive-pipeline.md`
