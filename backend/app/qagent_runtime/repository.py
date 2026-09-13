"""Persistence for the independent QAgent Runtime chain.

The production repository is PostgreSQL-backed. Tests may inject an in-memory
SQLite engine as a small deterministic substitute; the service never uses it
as a production fallback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.postgres.config import get_postgres_url

from .events import TERMINAL_STATUSES, EventType, utc_now
from .models import (
    metadata,
    qagent_approvals,
    qagent_recovery_points,
    qagent_run_assets,
    qagent_run_events,
    qagent_runs,
)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _row(result: sa.CursorResult[Any]) -> dict[str, Any] | None:
    item = result.mappings().first()
    return dict(item) if item else None


class RuntimeRepository:
    def __init__(self, engine: Engine, *, create_schema: bool = False) -> None:
        self.engine = engine
        if create_schema:
            metadata.create_all(engine)

    @classmethod
    def from_config(cls, *, create_schema: bool = False) -> RuntimeRepository:
        url = get_postgres_url(required=True)
        engine = sa.create_engine(url, pool_pre_ping=True, future=True)
        return cls(engine, create_schema=create_schema)

    def create_run(
        self,
        *,
        org_id: str = "local",
        task_id: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.engine.begin() as conn:
            if idempotency_key:
                existing = _row(
                    conn.execute(
                        sa.select(qagent_runs).where(
                            qagent_runs.c.org_id == org_id,
                            qagent_runs.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                if existing:
                    return existing
            run_id = _id("run")
            conn.execute(
                qagent_runs.insert().values(
                    run_id=run_id,
                    org_id=org_id,
                    task_id=task_id,
                    status="created",
                    version=1,
                    input_payload=input_payload,
                    executor_key="qagent.server.no_device.v1",
                    idempotency_key=idempotency_key,
                    resume_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._append_event(conn, run_id, EventType.RUN_CREATED, {"task_id": task_id}, occurred_at=now, org_id=org_id)
            queued_update = qagent_runs.update().where(qagent_runs.c.run_id == run_id, qagent_runs.c.org_id == org_id)
            conn.execute(queued_update.values(status="queued", version=2, updated_at=now))
            self._append_event(conn, run_id, EventType.RUN_QUEUED, {}, occurred_at=now, org_id=org_id)
            return dict(conn.execute(sa.select(qagent_runs).where(qagent_runs.c.run_id == run_id)).mappings().one())

    def _append_event(
        self,
        conn: sa.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        occurred_at: datetime | None = None,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        # PostgreSQL serializes event writers for a run on the run row. The
        # unique(run_id, sequence) constraint remains a final correctness guard.
        run_query = sa.select(qagent_runs.c.run_id).where(qagent_runs.c.run_id == run_id)
        if org_id is not None:
            run_query = run_query.where(qagent_runs.c.org_id == org_id)
        run_exists = conn.execute(run_query.with_for_update()).first()
        if not run_exists:
            raise KeyError(run_id)
        occurred_at = occurred_at or utc_now()
        max_seq = conn.execute(sa.select(sa.func.max(qagent_run_events.c.sequence)).where(qagent_run_events.c.run_id == run_id)).scalar()
        sequence = int(max_seq or 0) + 1
        event_id = _id("evt")
        conn.execute(
            qagent_run_events.insert().values(
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                type=str(event_type),
                occurred_at=occurred_at,
                payload=payload,
            )
        )
        return {
            "event_id": event_id,
            "run_id": run_id,
            "sequence": sequence,
            "occurred_at": occurred_at,
            "type": str(event_type),
            "payload": payload,
        }

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        with self.engine.begin() as conn:
            return self._append_event(conn, run_id, event_type, payload or {}, org_id=org_id)

    def get_run(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            query = sa.select(qagent_runs).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                query = query.where(qagent_runs.c.org_id == org_id)
            return _row(conn.execute(query))

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        org_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            run_query = sa.select(qagent_runs.c.run_id).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            if conn.execute(run_query).first() is None:
                return []
            rows = (
                conn.execute(
                    sa.select(qagent_run_events)
                    .where(
                        qagent_run_events.c.run_id == run_id,
                        qagent_run_events.c.sequence > after_sequence,
                    )
                    .order_by(qagent_run_events.c.sequence)
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def _transition(
        self,
        conn: sa.Connection,
        run_id: str,
        status: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        from_statuses: set[str] | None = None,
        org_id: str | None = None,
    ) -> bool:
        now = utc_now()
        current_query = sa.select(qagent_runs.c.status, qagent_runs.c.version).where(qagent_runs.c.run_id == run_id)
        if org_id is not None:
            current_query = current_query.where(qagent_runs.c.org_id == org_id)
        current = _row(conn.execute(current_query.with_for_update()))
        if not current:
            return False
        if from_statuses is not None and current["status"] not in from_statuses:
            return False
        values: dict[str, Any] = {
            "status": status,
            "version": int(current["version"]) + 1,
            "updated_at": now,
        }
        if status == "running":
            values["last_started_at"] = now
        if status in {"completed", "failed", "cancelled", "timed_out"}:
            values["completed_at"] = now
        update = qagent_runs.update().where(
            qagent_runs.c.run_id == run_id,
            qagent_runs.c.version == current["version"],
        )
        if from_statuses is not None:
            update = update.where(qagent_runs.c.status.in_(from_statuses))
        if org_id is not None:
            update = update.where(qagent_runs.c.org_id == org_id)
        changed = conn.execute(update.values(**values)).rowcount
        if changed != 1:
            return False
        self._append_event(conn, run_id, event_type, payload or {}, occurred_at=now, org_id=org_id)
        return True

    def transition(
        self,
        run_id: str,
        status: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        from_statuses: set[str] | None = None,
        org_id: str | None = None,
    ) -> bool:
        with self.engine.begin() as conn:
            return self._transition(conn, run_id, status, event_type, payload, from_statuses=from_statuses, org_id=org_id)

    def claim_execution(
        self,
        run_id: str,
        *,
        org_id: str | None = None,
        emit_event: bool = True,
        reclaim: bool = False,
    ) -> str | None:
        """Atomically claim one execution attempt for a run.

        The claim is durable so a duplicate approval/recovery request cannot
        invoke the provider twice.  ``emit_event=False`` is retained for the
        legacy synchronous executor used by existing callers; the real
        AgentScope path always emits ``run.executing``.
        """
        now = utc_now()
        with self.engine.begin() as conn:
            query = sa.select(qagent_runs).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                query = query.where(qagent_runs.c.org_id == org_id)
            run = _row(conn.execute(query.with_for_update()))
            if not run or run["status"] not in {"running", "executing"}:
                return None
            if run.get("execution_claim") and not reclaim:
                return None
            if run["status"] == "executing" and not reclaim:
                return None
            claim = f"exec_{uuid4().hex}"
            values: dict[str, Any] = {
                "execution_claim": claim,
                "execution_epoch": int(run.get("execution_epoch") or 0) + 1,
                "execution_claimed_at": now,
                "updated_at": now,
            }
            if emit_event and run["status"] == "running":
                values["status"] = "executing"
                values["version"] = int(run["version"]) + 1
            update = qagent_runs.update().where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                update = update.where(qagent_runs.c.org_id == org_id)
            conn.execute(update.values(**values))
            if emit_event and run["status"] == "running":
                self._append_event(
                    conn,
                    run_id,
                    EventType.RUN_EXECUTING,
                    {"execution_epoch": values["execution_epoch"]},
                    occurred_at=now,
                    org_id=org_id,
                )
            return claim

    def get_latest_approval(self, run_id: str, *, org_id: str | None = None) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            query = sa.select(qagent_approvals).where(qagent_approvals.c.run_id == run_id)
            if org_id is not None:
                query = query.where(qagent_approvals.c.org_id == org_id)
            return _row(conn.execute(query.order_by(qagent_approvals.c.requested_at.desc()).limit(1)))

    def set_plan_and_approval(
        self,
        run_id: str,
        plan: dict[str, Any],
        recovery_context: dict[str, Any],
        *,
        org_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.engine.begin() as conn:
            run_query = sa.select(qagent_runs).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            run = _row(conn.execute(run_query.with_for_update()))
            if not run:
                raise KeyError(run_id)
            approval_query = sa.select(qagent_approvals).where(qagent_approvals.c.run_id == run_id)
            if org_id is not None:
                approval_query = approval_query.where(qagent_approvals.c.org_id == org_id)
            approval = _row(conn.execute(approval_query.order_by(qagent_approvals.c.requested_at.desc()).limit(1)))
            # A recovery retry after planning has committed must reuse the
            # existing approval instead of creating another side effect.
            if approval and run.get("plan_payload") is not None:
                return approval
            plan_update = qagent_runs.update().where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                plan_update = plan_update.where(qagent_runs.c.org_id == org_id)
            conn.execute(
                plan_update.values(
                    plan_payload=plan,
                    recovery_context=recovery_context,
                    updated_at=now,
                )
            )
            if not approval:
                approval_id = _id("approval")
                conn.execute(
                    qagent_approvals.insert().values(
                        approval_id=approval_id,
                        org_id=run["org_id"],
                        run_id=run_id,
                        status="requested",
                        requested_at=now,
                    )
                )
                approval = _row(conn.execute(sa.select(qagent_approvals).where(qagent_approvals.c.approval_id == approval_id)))
            return approval or {}

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: str,
        decided_by: str | None,
        reason: str | None,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        now = utc_now()
        with self.engine.begin() as conn:
            approval_query = sa.select(qagent_approvals).where(qagent_approvals.c.approval_id == approval_id)
            if org_id is not None:
                approval_query = approval_query.where(qagent_approvals.c.org_id == org_id)
            if run_id is not None:
                approval_query = approval_query.where(qagent_approvals.c.run_id == run_id)
            approval = _row(conn.execute(approval_query))
            if not approval:
                return None, False
            decision_update = qagent_approvals.update().where(
                qagent_approvals.c.approval_id == approval_id,
                qagent_approvals.c.status == "requested",
            )
            if org_id is not None:
                decision_update = decision_update.where(qagent_approvals.c.org_id == org_id)
            if run_id is not None:
                decision_update = decision_update.where(qagent_approvals.c.run_id == run_id)
            changed = conn.execute(
                decision_update.values(
                    status=status,
                    decided_at=now,
                    decided_by=decided_by,
                    reason=reason,
                )
            ).rowcount
            if changed != 1:
                return approval, False
            approval.update(
                status=status,
                decided_at=now,
                decided_by=decided_by,
                reason=reason,
            )
            event = EventType.APPROVAL_GRANTED if status == "granted" else EventType.APPROVAL_REJECTED
            self._append_event(
                conn,
                approval["run_id"],
                event,
                {
                    "approval_id": approval_id,
                    "decided_by": decided_by,
                    "reason": reason,
                },
                occurred_at=now,
                org_id=approval["org_id"],
            )
            return approval, True

    def decide_approval_atomically(
        self,
        approval_id: str,
        *,
        status: str,
        decided_by: str | None,
        reason: str | None,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Decide an approval and apply its run transition in one transaction.

        The run row is deliberately locked before the approval row.  Every
        operation that can change a run state therefore has the same
        PostgreSQL lock order, which gives approval decisions and cancellation
        a single database serialization point across processes.
        """
        if status not in {"granted", "rejected"}:
            raise ValueError(f"unsupported approval status: {status}")

        now = utc_now()
        with self.engine.begin() as conn:
            approval_lookup = sa.select(qagent_approvals.c.run_id).where(
                qagent_approvals.c.approval_id == approval_id,
            )
            if org_id is not None:
                approval_lookup = approval_lookup.where(qagent_approvals.c.org_id == org_id)
            if run_id is not None:
                approval_lookup = approval_lookup.where(qagent_approvals.c.run_id == run_id)
            approval_ref = _row(conn.execute(approval_lookup))
            if not approval_ref:
                return None, False

            approval_run_id = approval_ref["run_id"]
            run_query = sa.select(qagent_runs).where(qagent_runs.c.run_id == approval_run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            run = _row(conn.execute(run_query.with_for_update()))
            if not run:
                return None, False

            approval_query = sa.select(qagent_approvals).where(
                qagent_approvals.c.approval_id == approval_id,
                qagent_approvals.c.run_id == approval_run_id,
                qagent_approvals.c.org_id == run["org_id"],
            )
            approval = _row(conn.execute(approval_query.with_for_update()))
            if not approval:
                return None, False

            # A decision is only valid while the run is waiting for this
            # approval.  This also makes a cancellation that won the run lock
            # a safe no-op for a later grant/reject retry.
            if approval["status"] != "requested" or run["status"] != "waiting_approval":
                return approval, False

            approval_update = conn.execute(
                qagent_approvals.update()
                .where(
                    qagent_approvals.c.approval_id == approval_id,
                    qagent_approvals.c.org_id == run["org_id"],
                    qagent_approvals.c.run_id == approval_run_id,
                    qagent_approvals.c.status == "requested",
                )
                .values(
                    status=status,
                    decided_at=now,
                    decided_by=decided_by,
                    reason=reason,
                )
            ).rowcount
            if approval_update != 1:
                return approval, False

            error_payload: dict[str, Any] | None = None
            if status == "rejected":
                error_payload = {
                    "code": "approval_rejected",
                    "message": reason or "Approval rejected",
                }
            run_values: dict[str, Any] = {
                "status": "running" if status == "granted" else "failed",
                "version": int(run["version"]) + 1,
                "updated_at": now,
            }
            if status == "granted":
                run_values["last_started_at"] = now
            else:
                run_values["completed_at"] = now
                run_values["error_payload"] = error_payload
            run_changed = conn.execute(
                qagent_runs.update()
                .where(
                    qagent_runs.c.run_id == approval_run_id,
                    qagent_runs.c.org_id == run["org_id"],
                    qagent_runs.c.status == "waiting_approval",
                    qagent_runs.c.version == run["version"],
                )
                .values(**run_values)
            ).rowcount
            if run_changed != 1:
                # This should be unreachable after the row lock, but retaining
                # the guard makes the transaction fail closed if a backend
                # reports an unexpected row-count result.
                raise sa.exc.InvalidRequestError("approval decision lost its run state race")

            approval_event = EventType.APPROVAL_GRANTED if status == "granted" else EventType.APPROVAL_REJECTED
            run_event = EventType.RUN_RUNNING if status == "granted" else EventType.RUN_FAILED
            decision_payload = {
                "approval_id": approval_id,
                "decided_by": decided_by,
                "reason": reason,
            }
            self._append_event(
                conn,
                approval_run_id,
                approval_event,
                decision_payload,
                occurred_at=now,
                org_id=run["org_id"],
            )
            self._append_event(
                conn,
                approval_run_id,
                run_event,
                error_payload if error_payload is not None else {"approval_id": approval_id},
                occurred_at=now,
                org_id=run["org_id"],
            )
            approval.update(
                status=status,
                decided_at=now,
                decided_by=decided_by,
                reason=reason,
            )
            return approval, True

    def get_approval(
        self,
        approval_id: str,
        *,
        org_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            query = sa.select(qagent_approvals).where(qagent_approvals.c.approval_id == approval_id)
            if org_id is not None:
                query = query.where(qagent_approvals.c.org_id == org_id)
            if run_id is not None:
                query = query.where(qagent_approvals.c.run_id == run_id)
            return _row(conn.execute(query))

    def set_result(
        self,
        run_id: str,
        result: dict[str, Any],
        assets: list[dict[str, Any]],
        *,
        org_id: str | None = None,
        execution_claim: str | None = None,
    ) -> None:
        now = utc_now()
        with self.engine.begin() as conn:
            run_query = sa.select(qagent_runs).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            run = _row(conn.execute(run_query.with_for_update()))
            # Cancellation is serialized on the same run row.  Once it has
            # won that lock, an executor that started before cancellation
            # must not attach a result or assets to the cancelled run.
            if not run or run["status"] not in {"running", "executing"} or run.get("result_payload") is not None:
                return
            if execution_claim is not None and run.get("execution_claim") != execution_claim:
                return
            result_update = qagent_runs.update().where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                result_update = result_update.where(qagent_runs.c.org_id == org_id)
            conn.execute(
                result_update.values(
                    result_payload=result,
                    execution_claim=None,
                    execution_claimed_at=None,
                    updated_at=now,
                )
            )
            for asset in assets:
                exists = conn.execute(sa.select(qagent_run_assets.c.asset_id).where(qagent_run_assets.c.asset_id == asset["asset_id"])).first()
                if exists:
                    continue
                conn.execute(
                    qagent_run_assets.insert().values(
                        asset_id=asset["asset_id"],
                        run_id=run_id,
                        asset_type=asset["asset_type"],
                        uri=asset["uri"],
                        name=asset.get("name"),
                        content_type=asset.get("content_type"),
                        metadata=asset.get("metadata") or {},
                        created_at=now,
                    )
                )
                self._append_event(conn, run_id, EventType.ASSET_AVAILABLE, asset, occurred_at=now, org_id=org_id)
            self._transition(
                conn,
                run_id,
                "completed",
                EventType.RUN_COMPLETED,
                {"result": result, "asset_count": len(assets)},
                from_statuses={"running", "executing"},
                org_id=org_id,
            )

    def set_error(
        self,
        run_id: str,
        error: dict[str, Any],
        *,
        org_id: str | None = None,
        execution_claim: str | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            run_query = sa.select(qagent_runs).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            run = _row(conn.execute(run_query.with_for_update()))
            if not run or run["status"] in TERMINAL_STATUSES:
                return
            if execution_claim is not None and run.get("execution_claim") != execution_claim:
                return
            error_update = qagent_runs.update().where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                error_update = error_update.where(qagent_runs.c.org_id == org_id)
            conn.execute(
                error_update.values(
                    error_payload=error,
                    execution_claim=None,
                    execution_claimed_at=None,
                    updated_at=utc_now(),
                )
            )
            self._transition(
                conn,
                run_id,
                "failed",
                EventType.RUN_FAILED,
                error,
                from_statuses={
                    "planning",
                    "running",
                    "executing",
                    "waiting_approval",
                    "queued",
                    "created",
                },
                org_id=org_id,
            )

    def increment_resume(self, run_id: str, *, org_id: str | None = None) -> None:
        with self.engine.begin() as conn:
            where = [qagent_runs.c.run_id == run_id]
            if org_id is not None:
                where.append(qagent_runs.c.org_id == org_id)
            conn.execute(
                qagent_runs.update()
                .where(*where)
                .values(
                    resume_count=qagent_runs.c.resume_count + 1,
                    updated_at=utc_now(),
                )
            )

    def save_recovery_point(
        self,
        run_id: str,
        checkpoint_key: str,
        sequence: int,
        context: dict[str, Any],
        *,
        org_id: str | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            run_query = sa.select(qagent_runs.c.run_id).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            if conn.execute(run_query).first() is None:
                return
            existing = conn.execute(
                sa.select(qagent_recovery_points.c.recovery_point_id).where(
                    qagent_recovery_points.c.run_id == run_id,
                    qagent_recovery_points.c.checkpoint_key == checkpoint_key,
                )
            ).first()
            if existing:
                return
            conn.execute(
                qagent_recovery_points.insert().values(
                    recovery_point_id=_id("recovery"),
                    run_id=run_id,
                    checkpoint_key=checkpoint_key,
                    sequence=sequence,
                    context=context,
                    created_at=utc_now(),
                )
            )

    def list_recovery_points(self, run_id: str, *, org_id: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            run_query = sa.select(qagent_runs.c.run_id).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            if conn.execute(run_query).first() is None:
                return []
            rows = conn.execute(
                sa.select(qagent_recovery_points)
                .where(qagent_recovery_points.c.run_id == run_id)
                .order_by(qagent_recovery_points.c.sequence)
            ).mappings().all()
            return [dict(row) for row in rows]

    def list_incomplete_runs(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(qagent_runs).where(qagent_runs.c.status.in_({"created", "queued", "planning", "waiting_approval", "running", "executing"})).order_by(qagent_runs.c.created_at)).mappings().all()
            return [dict(row) for row in rows]

    def list_assets(self, run_id: str, *, org_id: str | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            run_query = sa.select(qagent_runs.c.run_id).where(qagent_runs.c.run_id == run_id)
            if org_id is not None:
                run_query = run_query.where(qagent_runs.c.org_id == org_id)
            if conn.execute(run_query).first() is None:
                return []
            rows = conn.execute(sa.select(qagent_run_assets).where(qagent_run_assets.c.run_id == run_id).order_by(qagent_run_assets.c.created_at)).mappings().all()
            return [dict(row) for row in rows]
