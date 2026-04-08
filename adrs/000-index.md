# ADR Index

This index tracks all Architecture Decision Records in this repository.
See [GUIDE.md](GUIDE.md) for how and when to write ADRs.

## Proxy architecture and protocol bridging

- [ADR-001: Route OpenCode Through ACP Proxy to copilot-language-server](001-acp-proxy-architecture.md)
- [ADR-003: System Prompt Injection as Primary Control Surface](003-system-prompt-injection.md)
- [ADR-004: Extract Only the Last User Message for ACP Sessions](004-last-user-message-extraction.md)
- [ADR-007: The ACP Server Owns Tools — Do Not Inject or Override](007-tool-ownership.md)
- [ADR-010: Two-Agent-Runtime Collision — The ACP Path Interposes a Full Agent Loop](010-two-agent-runtime-collision.md)
- [ADR-011: Context Injection — Proxy Responsibilities and Consumer Boundary](011-context-injection-boundary.md)

## Session and conversation management

- [ADR-002: Session-per-Conversation via First-Message Hash](002-session-per-conversation.md)
- [ADR-009: Intra-Process Session Scaling](009-intra-process-session-scaling.md)

## Binary lifecycle and deployment

- [ADR-006: Binary Discovery — IntelliJ IDEA 2025.3 Only](006-binary-discovery.md)
- [ADR-008: Proxy as Substrate — Installable Command, cwd as Workspace](008-proxy-as-substrate.md)

## Testing and quality

- [ADR-005: Fail-Loud Testing — No Skips](005-fail-loud-testing.md)
