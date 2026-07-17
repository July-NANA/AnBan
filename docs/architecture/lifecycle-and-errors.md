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
CapabilityInvocation remains `failed` and its Event remains `capability.failed`; a later successful
Run does not rewrite that fact. Timeout, cancellation, protected or missing observations, Registry
and Schema failures, and Persistence/Event failures remain terminal. Completed calls remain in the
anti-replay set during any later response repair.

Physical host paths remain forbidden in Error and Event Metadata. A user-visible final answer may
report a legitimate result path because the Model Adapter has already rejected configured Secret
values on the response surface; sensitive credential forms and output length remain validated.

The v0.1 retry surface is deliberately narrow. The model SDK may retry temporary transport errors
up to the configured limit; this never replays a Capability. Separately, a transported but invalid
model response may trigger at most three contract-only repair requests shared by one Agent Node.
Events record the structural reason, attempt, repairability, exhaustion, and safe transport retry
count without the response, Prompt, or Tool arguments. Waiting/resume and checkpoint behavior
remain outside v0.1.
