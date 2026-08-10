# ADR-0003: Canonical ModelProfile与Key-only Secret输入

- Status: Accepted
- Date: 2026-08-10

## Context

Main AI、Memory Agent和Proactive Agent需要独立模型、参数和credential；Embedding也是独立用途。目标provider可能使用OpenAI Responses、Chat Completions或Anthropic Messages风格，wire字段不同。Telegram普通消息不适合承载API key，通用Web管理面又会扩大公网攻击面。

## Decision

- 三个generation role保存独立ModelProfile，Embedding profile独立。
- 内部保存canonical endpoint/model/temperature/output limit/timeout/options，由protocol adapter映射wire字段。
- V1支持`openai_responses`、`openai_chat_completions`和`anthropic_messages`；不支持legacy text `/completions`。
- 非secret配置仅通过Control Bot命令和短生命周期输入session管理。
- Telegram Web App只执行API key set/replace/delete，不读取key、不查询状态、不修改其他字段；状态只由Control Bot显示。
- API key以AES-256-GCM应用层加密；数据库ciphertext与host master keyring分离。
- 不可变config只绑定稳定credential identity；key轮换切换active credential version，新run取得并固定当前版本，已启动run可继续读取尚未销毁的旧版本。
- Endpoint默认只允许public HTTPS；private endpoint必须由root policy精确allowlist并通过SSRF/TLS门禁。

## Consequences

- 业务规则不依赖provider字段名称，可独立升级adapter。
- 各role变更与失败互不覆盖。
- Web App公网路径很小，但仍需`initData`、one-time launch、rate limit、CSP和无回显测试。
- `app/control/worker`属于能解密模型credential的trusted boundary。
- Provider能力变化必须创建capability/config version并通过wire fixture，不自动漂移。

## Revisit when

- 新协议无法映射canonical contract；
- 使用外部KMS/HSM替换host keyring；
- Web App需要超出key-only的功能。

任何扩展必须重新评估公网入口、secret泄露、SSRF、Disclosure和兼容fixture。

## References

- `docs/architecture/01-runtime-topology.md`
- `docs/architecture/03-data-model.md`
- `docs/architecture/06-context-contract.md`
- `docs/architecture/08-operations.md`
