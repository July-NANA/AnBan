# Real Environment Policy

Development readiness verifies actual integrations and explicit failure behavior.

- Model checks use configured real provider credentials and native Tool Calling.
- Capability checks perform an isolated real filesystem operation.
- Database checks connect to real PostgreSQL instances and use rollback-safe probes.
- Skill checks use a real installed ClawHub Skill and a real bounded network request.
- Browser checks launch a real Chromium process.

Fake Models, Fake Capabilities, Mock Providers, Placeholder Executors, JSON-simulated Tool Calls, mock success, and silent fallback are prohibited. A missing credential, unsupported native feature, unavailable database, or failed network dependency produces a named failure or blocker and a non-zero exit code.

Checks emit allowlisted summaries only. Credentials, Authorization data, raw provider responses, database passwords, and sensitive URLs are never logged or documented.
