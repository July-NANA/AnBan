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
| Checkpoint | `waiting` | record creation | `resumed`, `cancel_requested`, `failed`, `cancelled`, `timed_out` |
| Checkpoint | `resumed` | `waiting` | `completed`, `failed`, `cancel_requested`, `cancelled`, `timed_out` |
| Checkpoint | `cancel_requested` | `waiting`, `resumed` | `failed`, `cancelled`, `timed_out` |
| Checkpoint | terminal states | `waiting`, `resumed`, `cancel_requested` | none |

For v0.5 background execution, `accepted` is a Capability result signal rather than a Core record
state. The Invocation remains `running`; `capability.background_started` and one or more
`capability.progressed` Events share its system-owned identity. Only the real completed, failed,
cancelled, or timed-out result performs the existing terminal transition and Artifact transaction.
Progress sequence must increase monotonically, and a repeated wait cannot replay the operation.

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
count without the response, Prompt, or Tool arguments.

The v0.5 background Process extension does not broaden automatic retry. Runtime may inspect
bounded progress while awaiting one already-started process, and cancellation targets that exact
Invocation. Async execution persists `checkpoint.created`, `checkpoint.waiting`, and `run.waiting`
before returning control. Resume or cancel first commits its Checkpoint and Run Events, then releases
or terminates the live execution. Detach is distinct from cancel: it unwinds only the local Agent
coroutine and leaves the Checkpoint and Invocation non-terminal. A failed transition write leaves
the execution waiting and is safe to retry; a completed or uncertain Capability is never invoked
again. After service exit, recovery requires the original deadline fact, restores the Capability by
Invocation identity, records one recovery attempt, and consumes its real result. Missing, corrupt,
expired, or dead-worker state fails explicitly. A recovered result is quoted to the Model as bounded
non-executable evidence, never reconstructed as a provider Tool Call and never used to open another
execution channel.

The v0.5 Main Agent adds bounded replanning without broadening automatic retries. Completion
assessment is a structured Model decision, not another Capability call. A replan consumes one of a
separate finite budget and selects an exact ready strategy/target; production rejects a different
next path. Failed, completed, and uncertain calls remain in the observed-signature set, so
completion assessment cannot replay an identical side effect. Invalid assessment output, exhausted
budget, missing clarification, and absence of a safe alternative remain explicit terminal errors.

The v0.5 Interaction contract treats correlation as external evidence, never as authority over a
system identity. Malformed and already-expired keys fail envelope validation; one value cannot be
both the resume and deduplication key. D22 resolves only the opaque key emitted for a durable
waiting Checkpoint. Unknown, conflicting, and terminal/ineligible bindings fail explicitly rather
than creating new work. A valid supplemental update is classified before recovery; Context and an
optional replacement revision commit atomically. Fixed-Agent work accepts context-only updates but
rejects structural replacement because no safe graph action identity exists. D23 preserves the
active action and its complete input ancestry. Each completed NodeRun is independently reusable
only while its node definition, incoming control, and transitive inputs remain equivalent. A pure
invalidated result may execute again as a new NodeRun; an invalidated result with any Capability
Invocation is rejected if the replacement would execute it again. Removal invalidates the old
result without replay, and the old Artifact remains historical evidence. These decisions commit
with the revision as `graph.result_reused` or `graph.result_invalidated`; rejected changes record
`graph.result_invalidation_rejected` without linking the proposed revision. General expiry,
deduplication, and inbox lifecycle remain later scope.
