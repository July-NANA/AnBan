# ADR-0004: Multi-method HTTP Capability Boundary

- Status: Superseded by Gate #28
- Date: 2026-07-17

The former `http.get` and `http.request` Handler decision is no longer part of v0.1. Gate #28
removed both names and their Adapter/Policy code. HTTP is ordinary execution knowledge: a Skill
may direct `process.execute` to invoke curl, wget, Python, Node, or another installed program.

This avoids a Handler per concrete tool and keeps Invocation/Event/Audit/Trace ownership in the
existing Runtime path. The tradeoff is broader OS-user and network authority with no v0.1 sandbox,
approval layer, network isolation, or fine-grained file policy. Those controls are deferred and
must not be represented as already present.
