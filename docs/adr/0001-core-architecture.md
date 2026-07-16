# ADR-0001: Core Architecture

- Status: Accepted
- Date: 2026-07-17

## Context

Anban needs explicit boundaries that support real execution without coupling domain concepts to providers, transports, storage mechanics, or an individual Skill ecosystem.

## Decision

The system is divided into Interaction, Core, Runtime, Model, Capability, Persistence, and Frontend.

- Backend services use Python and FastAPI.
- Runtime orchestration uses LangGraph without reimplementing its graph primitives.
- Frontend uses React and TypeScript.
- Business persistence uses PostgreSQL.
- Skill is a specialized Capability.
- Model remains an independent Port.
- A future `TaskGraphSpec` is structured data owned by Core, not provider code or an executable business graph.
- Harness engineering is a cross-cutting quality requirement, not a module or profile.
- New external systems integrate through Interaction, Model, or Capability Adapters.

Core remains thin and authoritative. Runtime owns execution discipline. No provider, source, or Skill receives a core bypass. Missing real execution conditions fail explicitly.

## Consequences

Initial packages may remain empty until product work authorizes behavior. Architecture readiness does not create domain schemas, migrations, registries, runtime loops, or UI workflows.
