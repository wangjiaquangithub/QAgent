# Task Center ↔ Runtime: local acceptance commands

The Automation / Task Center / unattended-task domain on the QAgent Runtime. Every
test here is local: no database server, no network, no provider key.

## 1. The whole domain

```bash
cd backend
PYTHONPATH=".:packages/harness" python -m pytest app/tests/test_task_runtime_*.py -q
```

Expected: all pass. Roughly a minute, most of it in the flow tests.

If FastAPI is not installed, the files that drive the real Task Center routes
(`*_flow.py`, `test_task_runtime_api_reads.py`) **skip themselves** rather than
erroring, and the rest still run — so this one command is safe in either
environment. They use an in-process ASGI client, so no server is needed either
way.

## 2. One flow at a time while iterating

```bash
cd backend
PYTHONPATH=".:packages/harness" python -m pytest app/tests/test_task_runtime_org_isolation_flow.py -q
```

Or all of them at once with the shell glob:

```bash
PYTHONPATH=".:packages/harness" python -m pytest app/tests/test_task_runtime_*_flow.py -q
```

Note: pass files as a shell glob, not through `$(ls … | grep …)`. pytest's
`--ignore` / `--ignore-glob` only filter directory collection, so they do not
narrow an explicit file list.

## 3. Scoped lint

```bash
uvx ruff check <the files you changed>
```

`ruff check` only. Do **not** run `ruff format` here: `ruff.toml` sets
`line-length = 240`, and the pre-existing repository code does not satisfy
`format`, so running it rewrites unrelated files.

## What the flow files cover

| File | Scope |
| --- | --- |
| `test_task_runtime_unattended_success.py` | create → authorize → opt-in → link → projection → result → terminal, through the real entries |
| `test_task_runtime_approval_flow.py` | the approval gate, grant and reject (a reject must never look like a success) |
| `test_task_runtime_failure_flow.py` | a failed run and everything that must not leak into the row or the API |
| `test_task_runtime_cancellation_flow.py` | cancel through the real route, plus stale write-backs that must not resurrect the task |
| `test_task_runtime_recovery_flow.py` | a lagging projection caught up by reconcile / reconnect / the queue |
| `test_task_runtime_org_isolation_flow.py` | two organizations: refusal, no disclosure, and forged org fields ignored |
| `test_task_runtime_api_reads.py` | what the real read routes return, linked and unlinked |
| `test_task_runtime_manual_entry_flow.py` | the manual entry — `authorize-execution` then `start` (run now) — reaches the Runtime exactly once, and the switch-off path is unchanged |
| `test_task_runtime_retry_entry_flow.py` | attempt retry (the queue tick requeues; run now starts the new attempt) vs. subtask retry (`/retry`, which touches nothing) |
| `test_task_runtime_cancel_entry_flow.py` | `cancel` plus every manual trigger after it: nothing restarts the cancelled attempt, and a foreign linkage is never revealed |
| `test_task_runtime_automation_entry_flow.py` | `POST /api/automation/tasks/{id}/run` routing: the opt-in creates one Task Center task and one run, the default path is unchanged, `app_id` is unaffected |
| `test_task_runtime_scheduler_entry_flow.py` | the queue tick: a duplicate tick starts no second run, the two switches stay independent, and the default-off path is preserved |

Shared plumbing for those files lives in `_runtime_flow_support.py` (not a test
module, not production code). It replaces the Runtime with a recording fake at its
public boundary and fails any outbound connection to a non-loopback address, so a
flow that accidentally starts calling out fails by name instead of hanging.

Two helpers there exist because `POST /api/tasks/queue/tick` is global, and knowing
how to make it hermetic is easy to get wrong: `park_other_unattended_tasks(task_id)`
pauses every other unattended task the store accumulated (otherwise a leftover is
dragged into the legacy path, or the concurrency cap leaves the tick with no slots),
and `open_the_retry_window(task_id)` clears `unattended_next_retry_at`, because a
failed task is only picked up once its backoff has elapsed.

This is the domain's own acceptance surface — not a repository-wide test or
format run.
