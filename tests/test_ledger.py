"""
slackwater-quilt test battery.

Required receipts (from the 66-b expansion seed):
  T1  determinism     — same op sequence twice -> byte-exact ledger, same tip
  T2  replay          — replay(ledger) == live BuildPlacement state
  T3  fail-first      — PLACE on occupied coord fails loudly, nothing appended
  T4  path            — A* PATH entry over free points replays to same waypoints
  NC1 tamper          — flipped payload byte mid-chain -> verify() False + tip mismatch
  NC2 ghost receipt   — hash-valid entry the geometry rejects cannot replay
  NC3 boundary honesty— frontier from replay == frontier from live state
"""

import json
import os
import tempfile

import pytest

from slackwater_lattice import EisensteinInteger
from slackwater_lattice.pathfinding import LatticePathfinder
from slackwater_lattice.placement import PlacementError

from slackwater_quilt import (
    GENESIS_TIP,
    Build,
    LedgerError,
    LedgerTamperedError,
    PathNotFound,
    ReceiptEntry,
    ReceiptLedger,
    ReplayRefused,
    canonical_json,
    cell_id,
    entry_hash,
    replay,
)

E = EisensteinInteger


# ── The canonical deterministic build script ─────────────

OPS = [
    ("PLACE",  lambda b: b.place_world(0.0, 0.0, part="keel", agent="agent-a")),  # E(0,0)
    ("PLACE",  lambda b: b.place_world(4.0, 0.0, part="beam", agent="agent-a")),  # E(1,0)
    ("PLACE",  lambda b: b.place_world(8.0, 0.0, part="beam", agent="agent-b")),  # E(2,0)
    ("TICK",   lambda b: b.tick(1)),
    ("PATH",   lambda b: b.record_path(E(0, 0), E(4, 0))),  # around the beam line
    ("RELEASE", lambda b: b.release(E(1, 0))),
    ("TICK",   lambda b: b.tick(2)),
    ("PLACE",  lambda b: b.place(E(1, 0), part="patch", agent="agent-c")),  # re-place
    ("RELEASE", lambda b: b.release(E(2, 0))),
]


def build_script(stop_after: int | None = None) -> Build:
    """
    One fixed op sequence, used by T1/T2/T4/NC1/NC3. No randomness.
    With stop_after=k, stops once the ledger holds k+1 receipts (seq 0..k).
    """
    b = Build(scale=4.0)
    for _, op in OPS:
        op(b)
        if stop_after is not None and len(b.ledger) >= stop_after + 1:
            break
    return b


def path_seq(b: Build) -> int:
    return next(e.seq for e in b.ledger if e.kind == "PATH")


def ghost_fork(b: Build, *extra: tuple[str, dict]) -> ReceiptLedger:
    """
    Fork the honest chain and append raw receipts. A fork is hash-valid by
    construction — which is exactly the point: hash-valid is not the same
    as true. Geometry adjudicates at replay.
    """
    fork = ReceiptLedger.from_chain(b.ledger.entries)
    for kind, payload in extra:
        fork.append(kind, payload)
    return fork


# ── T1: determinism — the ledger tip is a function of the ops ──


def test_t1_determinism_same_ops_same_tip():
    b1, b2 = build_script(), build_script()
    assert b1.ledger.tip == b2.ledger.tip
    assert b1.ledger.tip != GENESIS_TIP
    # byte-exact: the serialized ledgers are identical, byte for byte
    assert b1.ledger.to_jsonl().encode("utf-8") == b2.ledger.to_jsonl().encode("utf-8")
    assert b1.state().state_bytes() == b2.state().state_bytes()
    for e1, e2 in zip(b1.ledger, b2.ledger):
        assert e1.tip == e2.tip and e1.seq == e2.seq and e1.payload == e2.payload


def test_clean_chain_verifies_and_empty_ledger_sits_at_genesis():
    b = build_script()
    assert b.ledger.verify() is True
    assert b.ledger.verify_errors() == []
    assert len(b.ledger) == len(OPS) == 9
    empty = ReceiptLedger()
    assert empty.tip == GENESIS_TIP
    assert empty.verify() is True


# ── T2: replay — the ledger replays the build, byte-exact ──


def test_t2_replay_equals_live_state():
    b = build_script()
    r = replay(b.ledger)
    # byte-exact cell state: coords + parts + agents + receipt provenance + frontier
    assert r.state_bytes() == b.state().state_bytes()
    # coords + parts + agents directly against the live BuildPlacement
    assert r.placement.occupied == b.placement.occupied
    for point in b.placement.occupied:
        live_meta = b.placement.metadata[point]
        replay_meta = r.placement.metadata[point]
        assert live_meta["part"] == replay_meta["part"]
        assert live_meta["agent"] == replay_meta["agent"]
    # re-placed E(1,0) is tipped by its SECOND place receipt, not its first
    place_tips = [e.tip for e in b.ledger
                  if e.kind == "PLACE" and e.payload["coord"] == [1, 0]]
    assert len(place_tips) == 2
    assert r.receipt_tips[E(1, 0)] == place_tips[-1]
    assert r.receipt_tips[E(1, 0)] != place_tips[0]
    # the released E(2,0) is absent from the replayed state too
    assert E(2, 0) not in r.placement.occupied
    assert r.placement_state() == b.state().placement_state()


# ── T3: fail-first collision — loud, and the ledger never hears of it ──


def test_t3_collision_fails_loudly_nothing_appended():
    b = build_script()
    before_len, before_tip = len(b.ledger), b.ledger.tip
    with pytest.raises(PlacementError):
        b.place(E(0, 0), part="interloper", agent="agent-x")
    assert len(b.ledger) == before_len      # nothing appended
    assert b.ledger.tip == before_tip       # tip unchanged
    assert b.ledger.verify() is True        # chain still intact
    assert E(0, 0) in b.placement           # original occupant intact
    assert b.placement.metadata[E(0, 0)]["part"] == "keel"
    # same at world coords that snap onto an occupied point
    with pytest.raises(PlacementError):
        b.place_world(0.5, 0.0, part="interloper2", agent="agent-x")
    assert len(b.ledger) == before_len and b.ledger.tip == before_tip


# ── T4: path receipts replay to the same waypoints ──


def test_t4_path_replays_to_same_waypoints():
    b = build_script()
    pseq = path_seq(b)
    receipt = b.ledger[pseq]
    assert receipt.kind == "PATH"
    receipted = receipt.payload["waypoints"]
    pts = [E(*p) for p in receipted]
    assert pts[0] == E(0, 0) and pts[-1] == E(4, 0)
    for a, z in zip(pts, pts[1:]):
        assert (a - z).norm() == 1                   # exact lattice steps (Z[ω] units)
    # prefix replay rebuilds the geometry as it stood at the PATH receipt;
    # interior waypoints were free on that geometry
    prefix = replay(b.ledger, upto=pseq)
    for interior in pts[1:-1]:
        assert interior not in prefix.placement.occupied
    # full replay accepts the receipt without refusal
    replay(b.ledger)
    # a fresh A* over the prefix-replayed state re-derives the same waypoints
    fresh = LatticePathfinder(prefix.placement).find_path(E(0, 0), E(4, 0))
    assert fresh is not None
    assert [[p.a, p.b] for p in fresh.points] == receipted
    # an occupied goal or a degenerate route fails loudly, nothing appended
    before_len, before_tip = len(b.ledger), b.ledger.tip
    with pytest.raises(PathNotFound):
        b.record_path(E(0, 0), E(1, 0))   # goal occupied ("patch")
    with pytest.raises(PathNotFound):
        b.record_path(E(0, 0), E(0, 0))   # start == goal: not a route
    assert len(b.ledger) == before_len and b.ledger.tip == before_tip


# ── NC1: tamper — one flipped byte mid-chain breaks verification ──


def test_nc1_tampered_payload_detected_with_tip_mismatch(tmp_path):
    b = build_script()
    path = tmp_path / "ledger.jsonl"
    b.ledger.save(path)

    lines = path.read_text(encoding="utf-8").splitlines()
    victim_seq = 2  # middle of the chain: the second PLACE ("beam")
    victim = json.loads(lines[victim_seq])
    assert victim["kind"] == "PLACE"
    # flip exactly one payload byte
    raw = canonical_json(victim["payload"])
    i = raw.index("beam")
    flipped = raw[: i + 1] + ("X" if raw[i + 1] != "X" else "Y") + raw[i + 2:]
    assert flipped != raw and len(flipped) == len(raw)
    victim["payload"] = json.loads(flipped)
    lines[victim_seq] = canonical_json(victim)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered = ReceiptLedger.load(str(path))
    assert tampered.verify() is False
    problems = tampered.verify_errors()
    assert any(p["error"] == "tip-mismatch" and p["seq"] == victim_seq
               for p in problems), problems
    assert tampered[victim_seq].recomputed_tip() != tampered[victim_seq].tip
    with pytest.raises(LedgerTamperedError):
        replay(tampered)  # replay refuses to walk a broken chain


def test_nc1b_rehashed_tamper_still_caught_by_chain_link():
    """A smarter attacker also rehashes the tampered entry — the NEXT
    entry's prev_tip then mismatches. Depth-2 integrity, not just the head."""
    b = build_script()
    fork = ReceiptLedger.from_chain(b.ledger.entries)
    old = fork[2]
    payload = dict(old.payload)
    payload["agent"] = "agent-hijacked"
    fork._entries[2] = ReceiptEntry(
        seq=old.seq, kind=old.kind, payload=payload, prev_tip=old.prev_tip,
        tip=entry_hash(old.seq, old.kind, payload, old.prev_tip),
    )
    assert fork[2].recomputed_tip() == fork[2].tip   # its own tip is now valid
    assert fork.verify() is False                    # but the chain still catches it
    assert any(p["error"] == "prev-tip-mismatch" and p["seq"] == 3
               for p in fork.verify_errors())


# ── NC2: ghost receipt — the ledger cannot lie about geometry ──


def test_nc2_ghost_place_refused_by_replay():
    b = build_script()
    fork = ghost_fork(b, ("PLACE", {"coord": [0, 0], "part": "ghost-part",
                                    "agent": "ghost-agent"}))
    assert fork.verify() is True          # the hash chain is intact — and still a lie
    assert len(fork) == len(b.ledger) + 1
    with pytest.raises(ReplayRefused) as excinfo:
        replay(fork)
    assert "cannot lie" in str(excinfo.value)
    # no honest state ever contained the ghost: E(0,0) is still the keel
    honest = replay(b.ledger)
    assert honest.cells()["cell:0+0w"]["part"] == "keel"
    assert honest.cells()["cell:0+0w"]["agent"] == "agent-a"


def test_nc2_ghost_release_and_bad_paths_refused():
    b = build_script()
    # RELEASE of a point the geometry does not occupy
    with pytest.raises(ReplayRefused):
        replay(ghost_fork(b, ("RELEASE", {"coord": [9, 9]})))
    # PATH with a teleport step (not a lattice step)
    with pytest.raises(ReplayRefused):
        replay(ghost_fork(b, ("PATH", {"from": [0, 0], "to": [5, 0],
                                       "waypoints": [[0, 0], [3, 3], [5, 0]]})))
    # PATH through an occupied interior ([1,0] is the "patch")
    with pytest.raises(ReplayRefused):
        replay(ghost_fork(b, ("PATH", {"from": [0, 0], "to": [2, 2],
                                       "waypoints": [[0, 0], [1, 0], [2, 0],
                                                     [2, 1], [2, 2]]})))
    # PATH to an occupied goal
    with pytest.raises(ReplayRefused):
        replay(ghost_fork(b, ("PATH", {"from": [3, 0], "to": [0, 0],
                                       "waypoints": [[3, 0], [2, 0], [1, 1],
                                                     [0, 1], [0, 0]]})))


# ── NC3: boundary honesty — frontier cells from replay == live ──


def test_nc3_frontier_honesty():
    b = build_script()
    live_frontier = b.state().frontier()
    replayed_frontier = replay(b.ledger).frontier()
    assert live_frontier == replayed_frontier
    assert len(live_frontier) > 0
    # the frontier pseudo-cell is identical in the full cell map
    assert (b.state().cells()["cell:frontier"]
            == replay(b.ledger).cells()["cell:frontier"])
    # frontier membership matches lattice boundary_points() exactly
    expected = sorted(cell_id(p) for p in b.placement.boundary_points())
    assert live_frontier == expected
    # and a prefix replay's frontier equals the live build frozen at that seq
    pseq = path_seq(b)
    prefix = replay(b.ledger, upto=pseq)
    live_at_path = build_script(stop_after=pseq)
    assert prefix.frontier() == live_at_path.state().frontier()
    assert prefix.state_bytes() == live_at_path.state().state_bytes()


# ── Supporting receipts ──────────────────────────────────


def test_unknown_kind_and_bad_payloads_refused():
    ledger = ReceiptLedger()
    with pytest.raises(LedgerError):
        ledger.append("VOODOO", {})
    with pytest.raises(LedgerError):
        ledger.append("PLACE", {"coord": [0, 0], "part": "x"})  # missing agent
    with pytest.raises(LedgerError):
        ledger.append("TICK", {"t": "soon"})
    with pytest.raises(LedgerError):
        ledger.append("PATH", {"from": [0, 0], "to": [1, 0], "waypoints": "straight"})
    b = Build()
    with pytest.raises(ValueError):
        b.release(E(0, 0))  # releasing nothing is a lie — refused, nothing appended
    assert len(b.ledger) == 0


def test_jsonl_roundtrip_replays_identically():
    b = build_script()
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "ledger.jsonl")
        b.ledger.save(p)
        loaded = ReceiptLedger.load(p)
        assert loaded.tip == b.ledger.tip
        assert loaded.verify() is True
        assert replay(loaded).state_bytes() == b.state().state_bytes()
