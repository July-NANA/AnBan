# Security Policy

## Reporting

Report suspected vulnerabilities privately to the repository owner. Do not open a public issue containing credentials, exploit details, private paths, provider responses, or user data.

## Secret Handling

- Never commit `.env`, API keys, database passwords, Authorization headers, or managed Workspace content.
- Real credentials live in the Workspace `secrets.env` with mode 0600.
- Logs, audit data, model prompts and responses, documentation, fixtures, and CI output must not contain Secret values.
- Readiness checks may test whether required values exist and may use them in-process, but must emit only allowlisted, sanitized results.
- Missing real credentials or unsupported native behavior must fail explicitly; do not add mock or fallback success.
- Provider content and Tool arguments containing a known configured API key fail before
  persistence or Capability invocation.

## External Execution

Use bounded, low-risk validation operations. Development readiness may make real model requests, perform isolated local file writes, query dedicated development databases, and execute read-only public weather requests. It must not create uncontrolled production side effects.

The v0.1 process Capability uses no shell, accepts only explicitly mapped executables, filters its
environment, confines its working directory, terminates the process group on timeout/cancellation,
and bounds stdout and stderr. This is not a strong container sandbox; the default CLI maps no
executable.
