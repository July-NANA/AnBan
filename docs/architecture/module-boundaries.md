# Module Boundaries

## Interaction

Owns transport-facing input, output, feedback, and bidirectional event adaptation. It does not own domain lifecycle or execution scheduling.

## Core

Owns authoritative Task, ExecutionRun, NodeRun, CapabilityInvocation, Artifact, and Event identity,
relationships, lifecycle terms, structured errors, safe metadata, and persistence Protocols. Core
must not absorb provider clients, SQLAlchemy models, transport details, or scheduling.

## Runtime

Owns v0.1 execution order, state transitions, the fixed LangGraph, bounded Tool Calling, durable
coordination, and query projections. Waiting, resume, and checkpoints are not v0.1 behavior.

## Model

Owns the Model Port and its adapters. Model reasoning is deliberately separate from generic executable Capability behavior.

## Capability

Owns interfaces and adapters for executable Tools and Skills. A Skill is a specialized Capability.
MCP and external Agents remain future integration categories, not v0.1 implementations.

## Persistence

Owns repositories and storage adapters for business state, Artifact metadata, and the authoritative
Event stream. PostgreSQL is the business database; Audit and Trace are Event projections rather
than duplicate stores. Checkpoints and memory are not implemented in v0.1.

Dependencies point toward Ports and stable Core vocabulary. Adapters depend on external systems; Core never depends on a concrete provider, Skill source, filesystem root, or frontend.

For v0.1, Interaction calls the Runtime application entry. Runtime depends on Core contracts,
ModelPort, CapabilityPort, and Core persistence Protocols. Persistence, provider, Workspace, and
CLI adapters point inward toward those contracts; no reverse dependency or integration-specific
Core path is allowed.
