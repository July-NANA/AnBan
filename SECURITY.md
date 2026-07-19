# Security Policy

Report vulnerabilities privately. Never place credentials, private paths, provider responses, or
user data in public Issues.

Secrets belong only in Workspace `secrets.env` or the launching environment. They must not enter
Git, CLI output, model output, logs, Events, Audit, Trace, Artifacts, fixtures, or documentation.
Process Metadata stores only command basename, argument count/hash, logical cwd scope, duration,
exit code, output sizes/hashes, Artifact count, and timeout/cancel flags. Full arguments,
environment, stdout, stderr, and physical paths are excluded. Known configured Secret values in
Process output or declared Artifacts fail safely before those bytes are persisted.
When multiple Artifacts are declared, validation and bounded reads complete before snapshots begin;
snapshot failure removes files created by that collection attempt.

Anban v0.5 deliberately uses the permissions of the OS user that started it. There is no program
allowlist, process sandbox, command approval, network isolation, or fine-grained file permission
layer. Absolute cwd and Artifact paths are allowed. Operators must use an appropriate OS account
and isolated Workspace. These governance controls are deferred; this limitation must never be
hidden by an acceptance-only branch or fake success.

Skills are instructions, not trusted persistence writers. All Skills use the same parser,
activation, Process, and persistence path. Installation metadata does not grant trust or change
behavior. Runtime remains authoritative for Invocation, Artifact, Event, Audit, and Trace records.
