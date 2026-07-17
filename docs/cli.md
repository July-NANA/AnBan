# v0.1 CLI Reference

The `anban` console script is the only v0.1 product interface. It enters through Interaction and
Runtime; command handlers do not call a Provider or concrete Capability directly.

## Workspace

```bash
anban workspace init [--json]
```

Creates missing Workspace bootstrap files without replacing existing configuration or Secret
content. Text output contains no physical path. JSON reports only whether the root, configuration,
and Secret file were newly created.

## Execute

```bash
anban run "<task>" [--json]
anban chat [--json]
```

`run` creates Task, ExecutionRun, and General Agent NodeRun identity before external execution. A
successful text result prints the final answer and Run ID. JSON returns stable `status`, `run_id`,
`persisted`, `final`, and `error` fields.

`chat` uses one Task/ExecutionRun and a new General Agent NodeRun for each input. Enter `/exit`,
`/quit`, or EOF to close it. It accepts at most eight user inputs and lasts at most 15 minutes.
History exists only for that process; v0.1 does not resume chat or provide long-term memory.

## Inspect

```bash
anban runs [--limit N] [--json]
anban run show <run-id> [--json]
anban trace <run-id> [--json]
anban artifacts <run-id> [--json]
```

`runs` returns newest-first summaries; the default limit is 20 and the accepted range is 1–100.
`run show` rebuilds bounded Task/Run/Node/Invocation/Artifact facts plus Event observability from
PostgreSQL. It deliberately excludes the original Task request. `trace` returns the ordered,
correlated Event projection and completeness findings. `artifacts` returns only logical URI,
SHA-256, size, media type, identity, correlations, and creation time.

These commands open a database-only application composition. They work in a new OS process and do
not require model or Skill configuration, but still require the external Workspace bootstrap and a
configured development PostgreSQL profile.

## Exit behavior

| Exit | Meaning |
| ---: | --- |
| `0` | Successful command or succeeded persisted execution |
| `1` | Model, Capability, persistence, Event/Audit/Trace, or other execution failure |
| `2` | Configuration or validation failure |
| `124` | Model, Capability, chat, or total execution timeout |
| `130` | User interruption or cancelled execution |

Failures use stable structured codes and bounded messages. Raw exceptions, Provider responses,
process stderr/stdout, credentials, database URLs, Authorization data, and physical Workspace paths
are not failure output. A Run ID is present when durable identity exists and the Runtime can safely
return it; otherwise only the structured top-level error is emitted.

## Execution limits

- Model turns per Agent Node: 8
- Capability calls per Agent Node: 8
- Total Agent execution: 180 seconds
- Repeated identical call limit: failure before the third execution
- Process timeout: 10 seconds by default, 30 seconds maximum
- Process stdout and stderr: 16 KiB each
- Run list: 20 by default, 100 maximum
- Inspection: 8 Nodes, 64 Invocations, 256 Artifacts, and 512 Events per Run

Exceeding a bound is an explicit failure; Anban does not enlarge a timeout, truncate a successful
fact silently, or substitute a mock result.
