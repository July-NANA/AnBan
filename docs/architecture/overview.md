# Architecture Overview

The six product modules are `interaction`, `core`, `runtime`, `model`, `capability`, and
`persistence`. `config` is authorized cross-module infrastructure, not a seventh product module.

```text
Interaction -> Runtime -> ModelPort
                       -> CapabilityPort -> skill.activate
                                         -> process.execute
                                         -> memory.context
                       -> UnitOfWorkFactory -> PostgreSQL
```

Core owns identities, lifecycles, Artifact/Event facts, `ExecutionRepository`, `UnitOfWork`, and
`UnitOfWorkFactory`, including Task/Session Context vocabulary. Model remains an independent Port.
Capability owns the Registry and its three Handlers. Runtime owns the fixed Agent loop,
Tool-call correctness, repair without side-effect
replay, persistence coordination, and Trace projection. Persistence implements the one PostgreSQL
backend; Interaction supplies the CLI loop.

Every Skill follows `SKILL.md -> uniform parser -> SkillPackage -> skill.activate ->
process.execute`. No production code selects behavior by source, installer, registry, publisher,
slug, Lock/Origin format, or fingerprint. Package and Workspace roots differ only in their logical
read-only root. Skill resources are referenced by that root and loaded only when instructions need
them.

`process.execute` understands only general process concepts: executable resolution, arguments,
environment overlays, cwd, stdin, bounded output, timeout/cancellation, process-group cleanup, and
declared single-file Artifacts. One invocation may collect multiple files atomically at the
snapshot layer. It has no HTTP, ClawHub, Git, Weather, PDF, or tool-specific branch.

PostgreSQL stores lifecycle and ordered Event facts. Managed Artifact bytes use logical
`anban://artifact/...` URIs. Audit and Trace are projections of the same Event stream. Database or
Event write failures cannot become ordinary success.

`memory.context` is one ordinary registered Capability. It resolves Task identity through the
current Run and Session identity from Runtime-owned invocation metadata. PostgreSQL stores raw
Context entries, summaries, and ordered summary coverage. Compression is atomic: a valid summary
marks covered entries superseded but never deletes them; any validation or write failure rolls the
whole operation back. No vector database, second registry, source-specific loader, or Memory
backend was introduced.
