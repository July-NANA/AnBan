# ADR-0006: MCP Stdio Capability Boundary

- Status: Accepted for D29
- Date: 2026-07-19
- Scope: v0.5 MCP discovery and structured Tool invocation
- Authorization: Delivery Issue #64

## Context

D29 requires real MCP discovery and invocation without adding an MCP-specific Runtime, Registry,
or Core bypass. An MCP server is an external process with server-defined Tool names, schemas,
results, side effects, and failure behavior. Its command and credentials are deployment facts,
while the Model and Runtime must see only bounded logical Capability descriptors and results.

## Decision

Anban uses the official stable v1 MCP Python SDK through one stdio Adapter in `capability`.
Workspace configuration declares at most eight logical servers. A declaration contains a command,
bounded arguments, a Workspace-relative cwd, and environment-variable references; actual values
resolve from process environment or mode-0600 Workspace `secrets.env` and never enter TOML,
inventory, Events, Audit, Trace, or errors.

Application composition initializes each configured server, follows paginated `tools/list`, and
registers one instance of the shared MCP Tool Handler per discovered Tool in the existing
Capability Registry. The stable logical Capability name combines the logical server name, a
bounded normalized Tool-name fragment, and a digest. Before registration, Anban requires its
closed bounded JSON Schema subset and rejects duplicate identities, protected descriptor content,
unsupported descriptors, or excess Tools. No Tool name creates a new Handler class or production
branch.

Invocation opens a fresh protocol session, rediscovers the selected Tool, verifies the descriptor
digest, validates arguments through the Registry, and performs `tools/call`. Native text and
`structuredContent` become one bounded Tool Result. Binary/resource content, malformed protocol,
Tool errors, timeout, descriptor drift, unavailable transport, protected output, and oversized
output fail explicitly. SDK raw-protocol parse logging is disabled because a hostile server could
place protected values in a rejected line. `_meta`, command, arguments, environment values,
physical cwd, and raw provider/protocol responses are never persisted.

MCP Tools use the ordinary Interaction -> Runtime -> Capability path. Their Invocation and ordered
Capability Events are persisted by the existing Unit of Work; Audit and Trace receive only safe
logical server/protocol/digest/count metadata. A new Application performs discovery again and a
new invocation reconnects. Synchronous Tool results are returned through the originating
Interaction flow. If a future MCP operation accepts background work, its result-ready signal must
use the already-authorized asynchronous Interaction path rather than introducing another channel.

## Consequences

This delivery adds one authorized MCP stdio Adapter, one shared dynamic MCP Tool Handler, bounded
MCP configuration, and the `mcp>=1.27,<2` dependency. It adds no Core entity, Port, persistence
backend, product module, domain-specific Tool, or MCP-specific Runtime branch. MCP discovery is
real and therefore a configured unavailable or malformed server prevents Application composition
instead of degrading to unavailable placeholder success.

Arguments are validated before a Tool call and are retry-safe at that point. After `tools/call`
begins, transport failure, server error, protected output, or output rejection may follow an
external side effect; Anban does not automatically replay it. Cancellation terminates the active
SDK session/process tree, but it does not claim rollback or exactly-once external effects.
