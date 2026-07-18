# v0.1 Lifecycle and Error Semantics

The v0.1 execution records use one deliberately small topology. A Task, ExecutionRun, or
NodeRun starts as `created`; a CapabilityInvocation starts as `requested`. The only legal next
state is `running`. From `running`, the record reaches exactly one terminal state. Terminal
records never return to an active state.

| Record | State | Legal incoming | Legal outgoing |
| --- | --- | --- | --- |
| Task | `created` | record creation | `running` |
| Task | `running` | `created` | `succeeded`, `failed`, `cancelled`, `timed_out` |
| Task | terminal states | `running` | none |
| ExecutionRun | `created` | record creation | `running` |
| ExecutionRun | `running` | `created` | `succeeded`, `failed`, `cancelled`, `timed_out` |
| ExecutionRun | terminal states | `running` | none |
| NodeRun | `created` | record creation | `running` |
| NodeRun | `running` | `created` | `succeeded`, `failed`, `cancelled`, `timed_out` |
| NodeRun | terminal states | `running` | none |
| CapabilityInvocation | `requested` | record creation | `running` |
| CapabilityInvocation | `running` | `requested` | `succeeded`, `failed`, `cancelled`, `timed_out` |
| CapabilityInvocation | terminal states | `running` | none |

The Core guards reject same-state updates, skipped states, and every transition out of a terminal
state with `invalid_transition`. They do not write timestamps or persistence records; Runtime and
Persistence will coordinate those operations at their own boundaries.

## Structured failures

`ErrorInfo` is the safe value shared by CLI, Event, Audit, and Trace surfaces. Its stable code maps
to one of configuration, validation, model, capability, persistence, audit/trace, timeout, or
interruption. The human-readable message is bounded, and details use `SafeMetadata`, which rejects
sensitive keys, unbounded values, and absolute host paths. Provider response bodies, prompts,
credentials, raw process output, exception strings, and physical Workspace paths are never error
details.

Persistence and audit/trace failures have separate codes because either one invalidates a normal
success result. Timeout and interruption also remain distinct terminal semantics.

An ordinary Capability failure with a bounded safe observation is returned to the model as the
paired Tool Result so it can adapt instead of being blocked by a recoverable command choice. The
same applies to complete pre-execution argument-validation and availability categories when they
carry a stable safe reason. The CapabilityInvocation is persisted as `failed` before the Tool
Result is exposed; a later successful Run does not rewrite that fact. Unknown Capabilities,
unexpected execution exceptions, unsafe or missing observations, timeout, cancellation, and
Persistence/Event failures remain terminal. Completed calls remain in the anti-replay set during
any later response repair.

If a Capability has returned but its terminal transaction reports failure, Runtime first reads the
authoritative Invocation, Event, and Artifact facts. An actually committed transaction is accepted
without replay. A confirmed uncommitted transaction receives one independent `failed` compensation
and one `capability.failed` Event. Managed snapshots belonging to that result are deleted before
compensation, while source files and other Invocation snapshots are untouched. Unknown commit
state prevents deletion; compensation or cleanup failure remains explicit and never becomes
ordinary success.

Physical host paths remain forbidden in Error and Event Metadata. A user-visible final answer may
report a legitimate result path because the Model Adapter has already rejected configured Secret
values on the response surface; sensitive credential forms and output length remain validated.

The v0.1 retry surface is deliberately narrow. The model SDK may retry temporary transport errors
up to the configured limit; this never replays a Capability. Separately, a transported but invalid
model response may trigger at most three contract-only repair requests shared by one Agent Node.
Events record the structural reason, attempt, repairability, exhaustion, and safe transport retry
count without the response, Prompt, or Tool arguments. Waiting/resume and checkpoint behavior
remain outside v0.1.

The v0.5 Main Agent adds bounded replanning without broadening automatic retries. Completion
assessment is a structured Model decision, not another Capability call. A replan consumes one of a
separate finite budget and selects an exact ready strategy/target; production rejects a different
next path. Failed, completed, and uncertain calls remain in the observed-signature set, so
completion assessment cannot replay an identical side effect. Invalid assessment output, exhausted
budget, missing clarification, and absence of a safe alternative remain explicit terminal errors.

The v0.5 Interaction contract treats correlation as external evidence, never as authority over a
system identity. Malformed and already-expired keys fail envelope validation; one value cannot be
both the resume and deduplication key. A later durable resolver must reject `unknown`, `expired`,
`conflicting`, and `ineligible` resume requests explicitly rather than silently creating new work
or selecting a Run. This delivery defines that closed failure vocabulary but does not perform
lookup, expiry persistence, or Run lifecycle transitions.
