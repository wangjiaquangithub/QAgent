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

## API sequences (copy-pasteable)

Each sequence drives the same entries the automated flow tests drive, so every
expectation below has a test behind it — `backend/app/tests/test_task_runtime_*_entry_flow.py`
plus `test_task_runtime_scheduler_entry_flow.py`. Set the gateway once:

```bash
BASE=http://localhost:8080        # the gateway you are accepting against
TASK=                             # filled in by the create command below
```

### A. Manual entry: authorize, then run now

```bash
TASK=$(curl -s -X POST "$BASE/api/tasks" -H 'Content-Type: application/json' \
  -d '{"name":"每周经营简报","description":"给管理层的周报","run_mode":"unattended"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "$TASK"

curl -s -X POST "$BASE/api/tasks/$TASK/authorize-execution" | python3 -m json.tool
# → execution_authorized true. Authorizing alone creates no Runtime run.

curl -s -X POST "$BASE/api/tasks/$TASK/start" | python3 -m json.tool
# → advance.action "runtime" with a runtime_run_id — once. Run it again and the
#   same run id comes back; no second run is created.

curl -s "$BASE/api/tasks/$TASK" | python3 -m json.tool
curl -s "$BASE/api/tasks/$TASK/execution-history" | python3 -m json.tool
# → both readable, and the task body carries NO runtime_run_linkage / runtime_run_cursor.
```

Run-now on a task that is **still failed** is the one case that does not start a new
attempt: it answers with the run that attempt already has
(`advance.runtime_action == "reused_terminal_run"`), because the queue, not run-now,
is the attempt-retry entry.

### B. Retry: one tick requeues, run now starts the new attempt

```bash
curl -s -X POST "$BASE/api/tasks/queue/tick" | python3 -m json.tool
# → the failed task's entry: action "requeued_for_retry", status back to pending,
#   unattended_attempts + 1, authorization cleared. The failed run id is still the
#   linked one — it is superseded, not rewritten.

curl -s -X POST "$BASE/api/tasks/$TASK/authorize-execution" >/dev/null
curl -s -X POST "$BASE/api/tasks/$TASK/start" | python3 -m json.tool
# → a NEW runtime_run_id for the new attempt; the previous run stays in
#   execution-history. A late frame from the old run is refused, so it cannot
#   overwrite the new attempt.
```

`POST /api/tasks/$TASK/retry` is a **different** thing: it retries failed
*subtasks* of the plan. It leaves the Runtime link, the authorization and the
failed run exactly as it found them.

### C. Cancel, and the triggers after it

```bash
curl -s -X POST "$BASE/api/tasks/$TASK/cancel" | python3 -m json.tool
# → status cancelled; the linked run is asked to cancel. Cancel again: still one
#   cancellation record, still one explainable status.

curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/tasks/$TASK/dispatch-execution"
# → 4xx: the authorization was withdrawn by the cancel.

curl -s -X POST "$BASE/api/tasks/$TASK/start" | python3 -m json.tool
# → creates no run and does not move the link; the cancelled task still reads
#   normally. A late approval/completion for the old run cannot resurrect it.
```

### D. Automation rule run-now

```bash
AUTOMATION=                       # an automation rule id
curl -s -X POST "$BASE/api/automation/tasks/$AUTOMATION/run" | python3 -m json.tool
```

With the opt-in **off** (the default) this is the unchanged prompt-only LangGraph
run. With it on (`execution_mode=task_center` / `plan` / `unattended`, or
`EVOFLOW_AUTOMATION_VIA_TASK=1`) a Task Center unattended task is created
(`run_mode=unattended`, `raised_by=automation`) and kicked. Note the precondition the
tests pin: the Runtime run is established only when the created task carries a
**trusted identity** (organization + owner scope + creator); without it the kick
safely defers to the legacy path. An `app_id`-bound automation is routed to the App
Runner before this switch is consulted and is unaffected either way.

### E. Scheduler tick

```bash
curl -s -X POST "$BASE/api/tasks/queue/tick" | python3 -m json.tool
# → results[]: one entry per task the tick walked, each with its action. For a task
#   whose attempt already has a run: action "already_scheduled" plus that run id,
#   and no advance. Tick again in the same window: identical. No second run.

curl -s "$BASE/api/tasks/queue/status" | python3 -m json.tool
# → the scheduler's own view: enabled, slots, active, queued_candidates, last_tick.
```

Two switches, independent of each other:

```bash
# EVOFLOW_TASK_QUEUE_ENABLED=0 → the tick does nothing at all:
#     {"skipped": true, "reason": "disabled"}
# EVOFLOW_AUTOMATION_UNATTENDED_RUNTIME unset → the tick still picks tasks up
#     (the Runtime guard is inert) and hands them to the legacy pipeline, which is
#     the whole point of default-off.
```

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
