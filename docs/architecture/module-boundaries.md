# Module Boundaries

## Interaction

Owns transport-facing input, output, feedback, and bidirectional event adaptation. It does not own domain lifecycle or execution scheduling.

## Core

Owns authoritative Task, ExecutionRun, NodeRun, CapabilityInvocation, Artifact, Event, and bounded
Task/Session Context identity, relationships, lifecycle terms, structured errors, safe metadata,
and persistence Protocols. Core
must not absorb provider clients, SQLAlchemy models, transport details, or scheduling.

## Runtime

Owns v0.1 execution order, state transitions, the fixed LangGraph, bounded Tool Calling, durable
coordination, and query projections. The v0.5 sufficiency evaluator uses a closed structured Model
decision to select only real, ready inventory targets; Runtime constructs and validates the
authoritative assessment, including general Skill-acquisition justification and explicit
clarification/failure. Waiting, resume, and checkpoints are not v0.1 behavior.

## Model

Owns the Model Port and its adapters. Model reasoning is deliberately separate from generic executable Capability behavior.

## Capability

Owns `CapabilityPort`, `CapabilityHandler`, the Registry, uniform `SKILL.md` discovery/activation,
the general Process Handler, and the read-only `CapabilityInventoryPort` projection. The unified
inventory describes the independently configured Model, registered Capabilities, ready Skills,
Process, implemented Memory, and explicitly unavailable future MCP and sub-agent paths without
invoking any of them or creating a second Registry. The shared Workspace catalog refreshes through the same
parser, bounds, protected-value checks, conflict rules, and logical identities for both inventory
and `skill.activate`; an Application rebuild is not required to observe an installed, changed, or
removed Skill. Multiple activated Skill instructions remain authoritative native Tool Results in
the same bounded model exchange, with a shared hard context limit rather than a second prompt or
execution channel. Production Capability names are `memory.context`, `skill.activate`, and
`process.execute`. The Memory Handler uses the existing Unit of Work Port and the same Registry;
it does not define another backend or discovery path. A concrete tool normally adds a Skill, not a
Handler. No Skill source or installer receives a special branch. MCP and external Agents remain
future categories.

## Persistence

Owns repositories and storage adapters for business state, Artifact metadata, and the authoritative
Event stream. PostgreSQL is the business database; Audit and Trace are Event projections rather
than duplicate stores. Context entries, summaries, and summary-to-entry coverage are durable
PostgreSQL facts. Raw facts survive bounded compression and restart; no vector database is used.
Checkpoints are not implemented in v0.1.

Dependencies point toward Ports and stable Core vocabulary. Adapters depend on external systems; Core never depends on a concrete provider, Skill source, filesystem root, or frontend.

`config` is authorized cross-module infrastructure and is not a seventh product module. New Ports,
Adapters, Handler/Tool names, persistence backends, interaction adapters, or top-level product
packages require explicit architecture authorization; an ADR alone does not grant it.

For v0.1, Interaction calls the Runtime application entry. Runtime depends on Core contracts,
ModelPort, CapabilityPort, and Core persistence Protocols. Persistence, provider, Workspace, and
CLI adapters point inward toward those contracts; no reverse dependency or integration-specific
Core path is allowed.
