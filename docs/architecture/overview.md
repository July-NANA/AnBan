# Architecture Overview

Anban separates external interaction, domain authority, execution discipline, model reasoning, executable capabilities, persistence, and presentation. The backend is Python and FastAPI; LangGraph supplies graph scheduling primitives; PostgreSQL supplies durable business storage; React supplies the frontend.

## Components

- **Interaction** closes the loop between external input, output, feedback, and asynchronous events.
- **Core** owns stable identities, relationships, lifecycle vocabulary, and structured specifications while remaining thin.
- **Runtime** orders execution and coordinates state, waiting, recovery, and LangGraph scheduling.
- **Model** is an independent Port for reasoning and generation.
- **Capability** represents executable Tool, Skill, MCP, external Agent, and future abilities. A Skill is a specialized Capability.
- **Persistence** stores memory, state, checkpoints, artifacts, audit information, and traces.
- **Frontend** presents interaction and state without owning backend domain rules.

Harness engineering is cross-cutting: observability, bounded execution, reproducibility, failure clarity, and validation apply across modules without creating a Harness module or HarnessProfile.

Future integrations enter only through Interaction, Model, or Capability Adapters. A provider, source, or individual Skill must not receive a core bypass.
