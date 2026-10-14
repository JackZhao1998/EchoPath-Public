# Segmented memory replay

Consolidation can describe an ordered workflow of semantic segments. Each segment
owns a contiguous range of the retained recording and defines a goal, completion
condition, inputs and an execution policy:

| Policy | Runtime behavior |
| --- | --- |
| `replay` | Execute the recorded actions with flexible inputs and visual grounding. |
| `native` | Yield a bounded subgoal to the calling agent's native computer tools. |
| `conditional` | Verify explicit reuse conditions on the live screen; replay only when verified, otherwise yield. Missing visual target evidence also selects native execution. |

The consolidator sees the full recorded trace before removing exploration. Action
source metadata includes the planner's step description and assessment for newly
recorded planned runs. Visual-change metrics remain supporting evidence; they do
not establish the meaning of an action on their own. The original actions remain
example evidence for native segments, and are never executed by replay there.
Consolidation generalizes request-specific attribute names and units as well as
values. Saved final-goal verification supplies semantic evidence for the final
recorded action. Runtime verification is already required at segment boundaries,
so checking an outcome alone does not require delegating its navigation to native
execution.

## Segment contracts

The consolidation result adds `segments`. Each entry contains `segment_id`,
`source_step_ids`, `goal`, `execution_policy`, `replay_conditions`,
`completion_condition`, `inputs`, `outputs`, `output_bindings`, and `rationale`.
Goals and completion conditions describe the current request rather than the
original selected item. The repository derives action bounds from step IDs after
consolidation and dependency preservation.

Segments must cover all retained actions exactly once in order. Inputs name
`request` or an earlier native segment's outputs. Outputs are uniquely named,
nonempty strings; this version supports producing outputs on `native` segments.
An output binding names an output, a later `step_id`, and `parameter_path: ["text"]`.
The target must already be a declared flexible `type.text` or `paste_text.text`
input. Unknown dependencies, stale bounds, overlaps and invalid bindings are
rejected before actions run.

Existing memories without segments retain their previous behavior. Memories
with segments use the GUI agent's segment dispatcher; the standalone
`MemoryReplayExecutor` rejects them so it cannot replay a native example by mistake.

## Host protocol

Call `echopath_run_task` with both `allow_desktop_control=true` and
`allow_native_handoff=true` only when the calling agent can use its own computer
tools on the same desktop. The model bridge continues to supply reasoning; it
does not take over the desktop itself.

When the response has `status: "awaiting_native"`:

1. Read `native_handoff`: the current request, subgoal, inputs, required outputs,
   return condition, environment, current observation, session ID and continuation token.
2. Use native computer tools to complete that subgoal on the indicated desktop.
3. Call `echopath_resume(session_id, continuation_token, outputs)` with exactly
   the declared string outputs, or `{}` when none are declared.
4. Inspect the returned status. Another `awaiting_native` response either requests
   another segment or indicates that return-state verification failed. Use its new
   token for the next attempt.

EchoPath takes a fresh observation and verifies the return condition and reported
outputs before advancing. It reacquires replay targets and verifies segment
completion. Replay repair cannot rewind across segment boundaries, and whole-task
planning fallback cannot bypass a future native segment. Existing per-action
verification settings are preserved; boundary and final goal checks are required.

Continuation tokens are single use. They hold a frozen execution plan in the server
process, so a resumed call does not retrieve a new memory or repeat the completed
prefix. An invalid output shape leaves the token available for correction. A failed
return-state check returns a replacement token without executing the suffix.
Unexpected execution failures consume the token and stop the run rather than retrying
possibly completed actions. A server restart expires outstanding tokens.

Call `echopath_resume(..., cancel=true)` to abandon the continuation without further
desktop actions. Pending native work reserves this EchoPath service: new tasks are
rejected until it is resumed or cancelled. Status inspection remains available.

### Tracked tasks

`echopath_start_task` accepts `allow_native_handoff=true` with
`allow_desktop_control=true`, so a segmented memory can replay inside the tracked
worker (on macOS, the authorized EchoPath.app). At a native boundary the task's
status becomes `awaiting_native` and `echopath_get_task` returns `native_handoff`
without a token; the worker keeps the frozen plan. Complete the subgoal, then call
`echopath_resume_task(run_id, outputs)` and poll the task again. Outputs that do not
match the declared names leave the segment pending with the reason. Stopping the run,
`cancel=true`, or 30 minutes without an answer cancels the continuation.

`result.success` reports successful handling of the tool request; task completion is
reported in `result.output.execution`. An `awaiting_native` response is neither a
completed task nor a failed native action. Do not describe it as task completion.

## Learning and recording

Each EchoPath-controlled portion has a recording. Native actions are external to
that recorder. Segmented runs are marked in their manifests and are excluded from
automatic or manual consolidation and legacy action-trajectory promotion. This
preserves the original segment contracts instead of learning an incomplete fixed
sequence. Importing full native action traces is a future extension.

## Amazon test scenario

Use a signed-out browser and no personal information. The initial request searches
for paper towel; the second searches for bottled water:

1. Replay search navigation with the current query.
2. Native segment: choose a matching product and add one unit to the cart.
3. Replay cart navigation and verify the requested product is present.

Stop at cart review, without entering an address, billing data or placing an order.
Remove the test item between cases and restore an empty cart afterward. The second
case must never execute the recorded paper towel selection clicks. Also test a
wrong-product return state, duplicate resume, cancellation, conditional dispatch,
output rebinding, malformed contracts and a legacy memory without segments.

Local test recordings, scripts and reports belong in the Git-ignored `tests/`
directory, as required by the project protocol.
