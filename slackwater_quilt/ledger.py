"""
The receipt ledger — an append-only hash chain of build operations.

Every build operation is a receipted entry:

    {seq, kind, payload, prev_tip, tip}

where ``tip = sha256(canonical_json({seq, kind, payload, prev_tip}))``.

HASH CHOICE — sha256, not fnv1a-64
----------------------------------
FNV-1a/64 is fast but trivially forgeable and collision-prone: an attacker
can mutate a payload and recompute the whole chain in microseconds. A
receipt chain's only value is that tips are *expensive to rewrite*, so the
chain uses SHA-256 (collision- and preimage-resistant, 64 hex chars, in the
standard library). If we ever need embedded 8-byte tips we will treat them
as *compressions of* a sha256 receipt, never as the receipt itself.

REPLAY CONTRACT
---------------
Each payload contains enough to replay the build from GENESIS:

    PLACE  {"coord": [a, b], "part": str, "agent": str}
    RELEASE{"coord": [a, b]}
    PATH   {"from": [a, b], "to": [a, b], "waypoints": [[a, b], ...]}
    TICK   {"t": int}

The ledger itself is deliberately geometry-agnostic: it records intent and
maintains the hash chain. Geometry is adjudicated twice — at append time by
the Build facade (collisions fail loudly, nothing is appended) and again at
replay time (a receipt whose claim the geometry rejects is refused: the
ledger cannot lie about geometry). This split is what makes ghost-receipt
tests possible and honest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterator, Optional

__all__ = [
    "GENESIS_TIP",
    "HASH_NAME",
    "KINDS",
    "ReceiptEntry",
    "ReceiptLedger",
    "LedgerError",
    "LedgerTamperedError",
    "canonical_json",
    "entry_hash",
    "validate_payload",
]

# The tip before the first entry: 64 zeros.
GENESIS_TIP = "0" * 64

# Documented choice: sha256 (see module docstring for why not fnv1a-64).
HASH_NAME = "sha256"

# The four receipt kinds. Anything else is refused at append time.
KINDS = ("PLACE", "RELEASE", "PATH", "TICK")


class LedgerError(Exception):
    """Raised for malformed ledger operations (bad kind, bad payload shape)."""


class LedgerTamperedError(Exception):
    """Raised when a chain fails verification during a protected operation."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"ledger tampered: {len(errors)} problem(s): {errors}")


# ── Payload schemas ──────────────────────────────────────
# Shape validation (NOT geometry): both append() and replay() refuse a
# receipt whose payload does not have the exact schema for its kind.
# Geometry adjudication (occupied/free/adjacent) lives in cells.replay.


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_coord(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_is_int(c) for c in v)


def validate_payload(kind: str, payload: dict) -> None:
    """Raise LedgerError unless payload matches the schema for `kind`."""
    if kind == "PLACE":
        if set(payload) != {"coord", "part", "agent"}:
            raise LedgerError(f"PLACE payload keys must be coord/part/agent: {payload}")
        if not _is_coord(payload["coord"]):
            raise LedgerError(f"PLACE coord must be [a, b] integers: {payload}")
        if not (isinstance(payload["part"], str) and isinstance(payload["agent"], str)):
            raise LedgerError(f"PLACE part and agent must be strings: {payload}")
    elif kind == "RELEASE":
        if set(payload) != {"coord"}:
            raise LedgerError(f"RELEASE payload keys must be coord: {payload}")
        if not _is_coord(payload["coord"]):
            raise LedgerError(f"RELEASE coord must be [a, b] integers: {payload}")
    elif kind == "PATH":
        if set(payload) != {"from", "to", "waypoints"}:
            raise LedgerError(f"PATH payload keys must be from/to/waypoints: {payload}")
        for name in ("from", "to"):
            if not _is_coord(payload[name]):
                raise LedgerError(f"PATH {name} must be [a, b] integers: {payload}")
        wps = payload["waypoints"]
        if not isinstance(wps, list) or not wps or not all(_is_coord(w) for w in wps):
            raise LedgerError(f"PATH waypoints must be a non-empty list of [a, b]: {payload}")
    elif kind == "TICK":
        if set(payload) != {"t"} or not _is_int(payload.get("t")):
            raise LedgerError(f"TICK payload must be {{'t': int}}: {payload}")
    else:
        raise LedgerError(f"unknown receipt kind {kind!r}; kinds are {KINDS}")


def canonical_json(obj: Any) -> str:
    """
    Canonical JSON: sorted keys, no whitespace, ASCII-escaped.

    Two operations with the same content always produce the same bytes,
    which is what makes the chain byte-exact reproducible (T1 determinism).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def entry_hash(seq: int, kind: str, payload: dict, prev_tip: str) -> str:
    """sha256 over the canonical JSON of everything except the tip itself."""
    material = {"seq": seq, "kind": kind, "payload": payload, "prev_tip": prev_tip}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReceiptEntry:
    """One immutable receipted build operation."""

    seq: int
    kind: str
    payload: dict
    prev_tip: str
    tip: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "payload": self.payload,
            "prev_tip": self.prev_tip,
            "tip": self.tip,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReceiptEntry":
        for field in ("seq", "kind", "payload", "prev_tip", "tip"):
            if field not in d:
                raise LedgerError(f"entry missing field {field!r}")
        return cls(
            seq=d["seq"], kind=d["kind"], payload=d["payload"],
            prev_tip=d["prev_tip"], tip=d["tip"],
        )

    def recomputed_tip(self) -> str:
        return entry_hash(self.seq, self.kind, self.payload, self.prev_tip)

    def __str__(self) -> str:  # human-readable receipt line
        return f"#{self.seq} {self.kind} {canonical_json(self.payload)} tip={self.tip[:12]}"


class ReceiptLedger:
    """
    Append-only chain of receipts. There is no edit, no delete, no rewrite.

    Invariants (checked by verify()):
      * entries[i].seq == i
      * entries[0].prev_tip == GENESIS_TIP
      * entries[i].prev_tip == entries[i-1].tip
      * entries[i].tip == entry_hash(seq, kind, payload, prev_tip)
    """

    def __init__(self) -> None:
        self._entries: list[ReceiptEntry] = []

    # ── Appending ────────────────────────────────────────

    def append(self, kind: str, payload: dict) -> ReceiptEntry:
        """
        Append one receipt. Refuses unknown kinds and non-dict payloads.
        Note: this method does NOT know geometry — the Build facade gates
        geometric validity before appending, and replay() adjudicates again.
        """
        if kind not in KINDS:
            raise LedgerError(f"unknown receipt kind {kind!r}; kinds are {KINDS}")
        if not isinstance(payload, dict):
            raise LedgerError("payload must be a dict")
        validate_payload(kind, payload)  # malformed receipts never enter the chain
        seq = len(self._entries)
        prev_tip = self._entries[-1].tip if self._entries else GENESIS_TIP
        entry = ReceiptEntry(
            seq=seq,
            kind=kind,
            payload=payload,
            prev_tip=prev_tip,
            tip=entry_hash(seq, kind, payload, prev_tip),
        )
        self._entries.append(entry)
        return entry

    # ── Reading ──────────────────────────────────────────

    @property
    def entries(self) -> tuple[ReceiptEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[ReceiptEntry]:
        return iter(self._entries)

    def __getitem__(self, idx: int) -> ReceiptEntry:
        return self._entries[idx]

    @property
    def tip(self) -> str:
        """Tip of the last appended entry (GENESIS_TIP for an empty ledger)."""
        return self._entries[-1].tip if self._entries else GENESIS_TIP

    # ── Verification ─────────────────────────────────────

    def verify_errors(self) -> list[dict]:
        """
        Walk the whole chain from GENESIS and return a list of problems.
        Empty list == the chain is intact. Detects:
          * seq drift
          * broken prev_tip links
          * tip mismatches (payload tampering, at ANY depth, not just the head)
        """
        problems: list[dict] = []
        expected_prev = GENESIS_TIP
        for i, entry in enumerate(self._entries):
            if entry.seq != i:
                problems.append({"seq": entry.seq, "error": "seq-drift", "expected": i})
            if entry.prev_tip != expected_prev:
                problems.append({
                    "seq": entry.seq,
                    "error": "prev-tip-mismatch",
                    "expected": expected_prev,
                    "found": entry.prev_tip,
                })
            recomputed = entry.recomputed_tip()
            if recomputed != entry.tip:
                problems.append({
                    "seq": entry.seq,
                    "error": "tip-mismatch",
                    "expected": recomputed,
                    "found": entry.tip,
                })
            # The next entry must link to THIS entry's *stored* tip; a
            # tampered payload therefore breaks both this tip and, if an
            # attacker rehashed it, the next link as well.
            expected_prev = entry.tip
        return problems

    def verify(self) -> bool:
        """True iff the chain is intact from GENESIS to tip."""
        return not self.verify_errors()

    def require_intact(self) -> None:
        """Raise LedgerTamperedError if the chain fails verification."""
        problems = self.verify_errors()
        if problems:
            raise LedgerTamperedError(problems)

    @classmethod
    def from_chain(cls, entries) -> "ReceiptLedger":
        """
        Rebuild a ledger from existing receipt entries (forks, loads, tests).
        Entries are taken as-given and NOT re-hashed; run verify() to judge
        what you were handed.
        """
        ledger = cls()
        ledger._entries = list(entries)
        return ledger

    # ── Serialization (JSONL: one receipt per line) ──────

    def to_jsonl(self) -> str:
        return "\n".join(canonical_json(e.to_dict()) for e in self._entries)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_jsonl() + ("\n" if self._entries else ""))

    @classmethod
    def load(cls, path: str) -> "ReceiptLedger":
        ledger = cls()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ledger._entries.append(ReceiptEntry.from_dict(json.loads(line)))
        return ledger
