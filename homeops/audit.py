"""Tamper-evident, append-only audit log + rollback registry.

Every command attempt — executed, refused, prohibited, recommended — plus rollbacks and manual
overrides is recorded into a **hash chain**: each record's hash covers the previous record's hash,
so any insertion, deletion, or edit anywhere in the history is detectable via `verify_chain()`.
Records optionally persist to an append-only JSONL file and are reloaded (and re-verified) on
startup, so the evidence trail survives a crash.

This is the compliance/insurance-grade evidence trail the STRATEGY.md moat depends on. It is not a
substitute for WORM storage or an external notary in production, but it makes tampering *detectable*
rather than silent.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import hashlib
import json
import os
from typing import Any

GENESIS = "0" * 64


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
    status: str            # executed | confirm_required | refused | prohibited | recommend_only | recommended | rollback | manual_override
    message: str
    rollback_token: str | None = None


def _canonical(rec: AuditRecord) -> str:
    return json.dumps(asdict(rec), sort_keys=True, default=str)


def _hash(prev: str, rec: AuditRecord) -> str:
    return hashlib.sha256((prev + _canonical(rec)).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | None = None) -> None:
        self._records: list[AuditRecord] = []
        self._meta: list[dict] = []            # {seq, prev, hash}
        self._rollback: dict[str, dict[str, Any]] = {}
        self._path = path
        self._head = GENESIS
        if path and os.path.exists(path):
            self._load()

    def record(self, rec: AuditRecord) -> None:
        h = _hash(self._head, rec)
        seq = len(self._records)
        self._records.append(rec)
        self._meta.append({"seq": seq, "prev": self._head, "hash": h})
        self._head = h
        if self._path:
            with open(self._path, "a") as f:
                f.write(json.dumps({"seq": seq, "prev": self._meta[-1]["prev"],
                                    "hash": h, "record": asdict(rec)}) + "\n")

    def verify_chain(self) -> tuple[bool, int]:
        """Recompute the whole chain. Returns (ok, first_bad_index) — (True, -1) if intact."""
        head = GENESIS
        for i, rec in enumerate(self._records):
            h = _hash(head, rec)
            if i >= len(self._meta) or h != self._meta[i]["hash"] or self._meta[i]["prev"] != head:
                return False, i
            head = h
        return (head == self._head), (-1 if head == self._head else len(self._records) - 1)

    def _load(self) -> None:
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                self._records.append(AuditRecord(**entry["record"]))
                self._meta.append({"seq": entry["seq"], "prev": entry["prev"], "hash": entry["hash"]})
                self._head = entry["hash"]
        ok, bad = self.verify_chain()
        if not ok:
            raise ValueError(f"audit log at {self._path} failed integrity check at record {bad}")

    # --- rollback registry ---------------------------------------------------
    def register_rollback(self, token: str, undo: dict[str, Any], meta: dict | None = None) -> None:
        self._rollback[token] = {"undo": undo, "meta": dict(meta or {})}

    def rollback(self, token: str) -> dict[str, Any] | None:
        """Look up a rollback entry {'undo':..., 'meta':...} WITHOUT consuming it."""
        return self._rollback.get(token)

    def consume_rollback(self, token: str) -> None:
        """Rollback tokens are single-use: consumed by the router before actuation."""
        self._rollback.pop(token, None)

    # --- reads ---------------------------------------------------------------
    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    @property
    def head(self) -> str:
        return self._head

    def by_status(self, status: str) -> list[AuditRecord]:
        return [r for r in self._records if r.status == status]

    def dump(self) -> list[dict]:
        return [asdict(r) for r in self._records]
