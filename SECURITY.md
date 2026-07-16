# Security Policy

## Reporting

Report suspected vulnerabilities privately to the repository owner. Do not open a public issue containing credentials, exploit details, private paths, provider responses, or user data.

## Secret Handling

- Never commit `.env`, API keys, database passwords, Authorization headers, or managed Workspace content.
- Real credentials live in the Workspace `secrets.env` with mode 0600.
- Logs, audit data, model prompts and responses, documentation, fixtures, and CI output must not contain Secret values.
- Readiness checks may test whether required values exist and may use them in-process, but must emit only allowlisted, sanitized results.
- Missing real credentials or unsupported native behavior must fail explicitly; do not add mock or fallback success.

## External Execution

Use bounded, low-risk validation operations. Development readiness may make real model requests, perform isolated local file writes, query dedicated development databases, and execute read-only public weather requests. It must not create uncontrolled production side effects.
