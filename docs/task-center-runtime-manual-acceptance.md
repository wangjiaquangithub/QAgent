# Task Center ↔ Runtime: manual acceptance checklist

For a person exercising the Runtime-driven unattended task by hand, through the
entries that already exist. No new UI is involved, and nothing here needs a code
change.

## Prerequisites

| What | Why |
| --- | --- |
| A reachable QAgent Runtime (its PostgreSQL store) | The run and its state live there; the Task Center only projects them |
| `EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME=1` on the gateway | The opt-in is **off by default**; with it off every step below behaves exactly as it did before this work |
| A task whose `run_mode` is `unattended` (Task Center: 无人值守) | The opt-in only ever applies to unattended tasks |
| Execution approved for the task ("开始执行") | An unauthorized task never reaches the Runtime — that is a precondition, not a bug |
| A local/fake model provider for steps 4–7 | So a run can be made to succeed or fail on demand without calling a real provider |

Steps 1–3 and 8–10 need no provider. Steps 4–7 need one that can be made to fail.

## Checklist

| # | Do this | Expect |
| --- | --- | --- |
| 1 | Create an unattended task in the Task Center | Created as usual; no Runtime activity. `GET /api/tasks/{id}` returns the task and **no** `runtime_run_linkage` / `runtime_run_cursor` |
| 2 | Read the task's execution history before anything runs | Empty, as before |
| 3 | Trigger a queue tick (the scheduler, or `POST /api/tasks/queue/tick`) with the switch on and the task authorized | Exactly **one** Runtime run is created for this task and attempt; the task's own status is untouched by the opt-in itself. Tick again — no second run |
| 4 | Let the run reach its approval gate; refresh the task | History gains a `waiting_approval` record with `approval_required` and a hint. The task status does **not** change. This is expected, not a stall |
| 5 | Approve the run in the Runtime | History gains `approval_granted`; the task status still does not move on the decision alone — it converges from the run status the Runtime reports next |
| 6 | Let the run complete; refresh the task | Task settles `completed`; history gains the completion with the result summary and states whether a displayable result exists; the task's `execution-history` endpoint shows the same list |
| 7 | Repeat 4, then **reject** | History gains `approval_rejected`; the task must **never** show `completed`, and no result appears. A late completion must not replace the rejection |
| 8 | Make a run fail (fake provider) | Task settles `failed` with a safe error code. The message must show no traceback, no path, no endpoint, no token — and if the Runtime's message looked like one of those, the record says the detail was withheld |
| 9 | Cancel a running task from the Task Center | Task becomes `cancelled` and the Runtime is asked to cancel the linked run. Cancel again — still one cancellation record. Then let a stale completion/failure arrive: the task stays `cancelled` |
| 10 | Disconnect (close the tab / restart the gateway) while a run is mid-flight, then reopen the task | The task's state is rebuilt from what is persisted plus a read of the Runtime — no re-execution, no second run, no duplicated history. A run that moved on while you were away is caught up |
| 11 | Hand-edit the stored linkage to another organization's (bad row), then read, cancel and tick | The operation is refused and the other run is never touched or revealed: no run id and no organization id appears in any response. The task still reads normally |

## Not included

Deliberately out of scope for this acceptance:

- **App Runner** tasks;
- the **default LangGraph prompt-only** path (this work must not change it — with
  the switch off, that is the whole point);
- a **multi-instance scheduler** (single gateway only);
- **device** flows.

## Notes

- Run ids, the linkage and the stream cursor are internal plumbing. They must not
  appear in task responses; if one does, that is a defect, not a feature.
- The Runtime is the source of truth for run state. The Task Center keeps a
  projection of it and its own history records; it never keeps a second copy of
  run state, and it never keeps an approval state of its own.
