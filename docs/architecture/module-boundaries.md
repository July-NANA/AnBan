# Module Boundaries

## Interaction

Owns transport-facing input, output, feedback, and bidirectional event adaptation. It does not own domain lifecycle or execution scheduling.

## Core

Owns authoritative domain identity, relationships, lifecycle terms, and structured data such as a future `TaskGraphSpec`. A specification is data, not an executable graph implementation. Core must not absorb provider clients, persistence mechanics, transport details, or scheduling.

## Runtime

Owns execution order, state transitions, waiting, resumption, and LangGraph coordination. It uses Core definitions and Ports but does not recreate LangGraph primitives or provider-specific behavior.

## Model

Owns the Model Port and its adapters. Model reasoning is deliberately separate from generic executable Capability behavior.

## Capability

Owns interfaces for executable Tools, Skills, MCP services, external Agents, and other actions. Skill-specific details remain behind adapters; a Skill is a specialized Capability.

## Persistence

Owns repositories and storage adapters for state, checkpoints, memory, artifacts, audit data, and traces. PostgreSQL is the business database.

Dependencies point toward Ports and stable Core vocabulary. Adapters depend on external systems; Core never depends on a concrete provider, Skill source, filesystem root, or frontend.

For v0.1, Interaction calls the Runtime application entry. Runtime depends on Core contracts,
ModelPort, CapabilityPort, and Core persistence Protocols. Persistence, provider, Workspace, and
CLI adapters point inward toward those contracts; no reverse dependency or integration-specific
Core path is allowed.
