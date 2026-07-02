"""Append-only audit log + rollback registry.

Every command attempt — executed, refused, prohibited, recommended — plus rollbacks and manual
overrides is recorded. This is the evidence trail the DESIGN.md permission model promises
("L4/L5 refused via AI path, and logged"). NOTE: this reference implementation keeps records in
an in-memory list; a production build persists them to a tamper-evident/append-only store
(e.g. WORM storage or a hash-chained log). It is "append-only" by convention here, not enforced.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class AuditRecord:
    tick: int
    operator: str
    house_id: str
    subsystem: str
    target: str
    action: str
    args: dict
    level: int | None
    status: str            # executed | confirm_required | refused | prohibited | recommend_only | recommended
    message: str
    rollback_token: str | None = None


class AuditLog:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._rollback: dict[str, dict[str, Any]] = {}

    def record(self, rec: AuditRecord) -> None:
        self._records.append(rec)

    def register_rollback(self, token: str, undo: dict[str, Any]) -> None:
        self._rollback[token] = undo

    def rollback(self, token: str) -> dict[str, Any] | None:
        return self._rollback.get(token)

    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def by_status(self, status: str) -> list[AuditRecord]:
        return [r for r in self._records if r.status == status]

    def dump(self) -> list[dict]:
        return [asdict(r) for r in self._records]
