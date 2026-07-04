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
from dataclasses import dataclass, asdict
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
        self._verified_upto = 0    # M4: index one past the last record confirmed by verify_incremental
        self._torn_tail: str | None = None   # H5: a dropped, crash-truncated final line, if any
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
                self._verified_upto = i
                return False, i
            head = h
        ok = head == self._head
        self._verified_upto = len(self._records) if ok else len(self._records) - 1
        return ok, (-1 if ok else len(self._records) - 1)

    def verify_incremental(self) -> bool:
        """M4: re-verify only the records appended since the last check — O(new records), not
        O(whole chain). A running service calls this every housekeeping cycle; the prefix was
        already confirmed on load or on a prior cycle, and records are append-only, so re-hashing
        the whole history each tick is wasted work that grows without bound. Any anomaly resets
        the cursor and falls back to the authoritative whole-chain check (fail-safe, not fail-open)."""
        n = len(self._records)
        start = self._verified_upto
        if start > n:                       # records vanished from memory — trust nothing
            self._verified_upto = 0
            return self.verify_chain()[0]
        head = self._meta[start - 1]["hash"] if start > 0 else GENESIS
        for i in range(start, n):
            rec = self._records[i]
            h = _hash(head, rec)
            if h != self._meta[i]["hash"] or self._meta[i]["prev"] != head:
                self._verified_upto = 0     # something moved beneath us — force a full recheck
                return self.verify_chain()[0]
            head = h
        self._verified_upto = n
        return head == self._head

    def _load(self) -> None:
        # H5: a process killed mid-write leaves a truncated final line. That is the exact crash
        # this log claims to survive, so a torn *tail* is dropped (the surviving prefix still
        # hash-verifies) rather than aborting startup. A malformed line anywhere *before* the end
        # is genuine corruption and still raises — we tolerate the interrupted write, not tampering.
        raw_lines = [ln.strip() for ln in open(self._path) if ln.strip()]
        for idx, line in enumerate(raw_lines):
            try:
                entry = json.loads(line)
                rec = AuditRecord(**entry["record"])
            except (ValueError, TypeError, KeyError) as e:
                if idx == len(raw_lines) - 1:
                    self._torn_tail = line     # dropped, recorded for diagnostics
                    break
                raise ValueError(
                    f"audit log at {self._path} has a malformed record at line {idx} "
                    f"(not the final line — this is corruption, not a torn write): {e}")
            self._records.append(rec)
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
