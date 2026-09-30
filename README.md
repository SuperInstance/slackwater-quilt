# slackwater-quilt

**The ledger is the build.**

[slackwater-lattice](https://github.com/SuperInstance/slackwater-lattice) gives
you exact Eisenstein A₂ hex geometry: snap, `BuildPlacement` with O(1)
integer collision detection, A* pathfinding, `boundary_points()`. But it
*asserts* exactness rather than *receipting* it — a build is a mutable
in-memory set, and once you mutate it, there is no way to ask what happened,
when, by whom, or to prove what it looked like at receipt #4.

slackwater-quilt applies the quilt-qcells pattern — *"the ledger is the
circuit"* — to geometry: **every build operation becomes an append-only
receipted cell, and the build IS its ledger.** There is no build state that
has a log; there is a log, and the build is what the log says, replayed.

```
#0 PLACE    {"coord": [0, 0], "part": "keel", "agent": "agent-a"}
#1 PLACE    {"coord": [1, 0], "part": "beam", "agent": "agent-a"}
#2 PLACE    {"coord": [2, 0], "part": "beam", "agent": "agent-b"}
#3 TICK     {"t": 1}
#4 PATH     {"from": [0, 0], "to": [4, 0],
             "waypoints": [[0,0], [1,1], [2,1], [3,1], [4,1], [4,0]]}
#5 RELEASE  {"coord": [1, 0]}
#6 TICK     {"t": 2}
#7 PLACE    {"coord": [1, 0], "part": "patch", "agent": "agent-c"}
#8 RELEASE  {"coord": [2, 0]}
tip: 152df7eeef27234874ff1330861c7c201c7a5817f32bcd18ded617515221f6c9
```

## Lineage

- **Seed:** SuperInstance/slackwater-lattice study, wave 66-b — its
  EXPANSION SEED #1 named this repo: *"a slackwater×quilt seed (placement
  ops as receipted cells) would connect the exact-geometry line to the
  receipt-epistemology line."*
- **Pattern:** SuperInstance/quilt-qcells — *"the ledger is the circuit"* —
  applied here as *"the ledger is the build."*
- **Dependency:** `slackwater-lattice>=0.1.0`, installed from PyPI (not
  vendored; no upstream files were copied or modified).

## The receipt doctrine

1. **Every operation receipts itself or does not happen.** A failed
   operation appends nothing; the tip is byte-identical to before the
   attempt. There are no silent no-ops and no partially-applied ops.
2. **The chain is append-only.** Entries are `{seq, kind, payload,
   prev_tip, tip}` with
   `tip = sha256(canonical_json({seq, kind, payload, prev_tip}))`.
   **Hash choice — sha256, not fnv1a-64:** FNV-1a/64 is trivially forgeable;
   a receipt chain's only value is that rewriting history is expensive, so
   the chain uses SHA-256 (64 hex chars, stdlib, collision/preimage
   resistant). If an 8-byte tip is ever needed for wire size, it will be a
   *compression of* a sha256 receipt, never the receipt itself.
3. **Two gates, one truth.** The ledger is deliberately geometry-agnostic:
   it holds intent and maintains the hash chain. Geometry adjudicates twice:
   - *Append-time gate* (the `Build` facade): a PLACE onto an occupied point
     raises `PlacementError` and appends nothing (T3).
   - *Replay-time adjudication* (`replay()`): any receipt whose geometric
     claim is false is refused with `ReplayRefused` — **a hash-valid ledger
     can still lie; only geometry adjudicates** (NC2).
4. **Replay from GENESIS is the build.** `replay(ledger)` reconstructs the
   placement purely from receipts and is byte-exact equal to the live state
   — coords, parts, agents, *and* which PLACE receipt tips each cell (T2).
5. **Exact integers only in receipts.** Payloads carry Eisenstein pairs
   `[a, b]` (a + bω). Pre-snap continuous positions never enter a receipt:
   floats are not part of the exact build state and would break byte-exact
   replay.
6. **Fail-first.** Collisions, releases of nothing, unroutable paths, and
   malformed payloads all fail loudly at the API boundary — loudly meaning:
   exception raised, nothing appended, tip unchanged, chain still verifying.

## Usage

```python
from slackwater_lattice import EisensteinInteger as E
from slackwater_quilt import Build, replay

b = Build(scale=4.0)
b.place_world(0.0, 0.0, part="keel", agent="agent-a")   # snaps, then receipts
b.place(E(1, 0), part="beam", agent="agent-a")
b.tick(1)
b.record_path(E(0, 0), E(4, 0))                          # A* over free points
b.release(E(1, 0))

b.ledger.verify()                  # True: chain intact from GENESIS
b.ledger.tip                       # sha256 chain tip
state = replay(b.ledger)           # the build, replayed from receipts
state.cells()                      # {"cell:0+0w": {...}, ..., "cell:frontier": {...}}
state.state_bytes() == b.state().state_bytes()   # True — byte-exact
```

Cells are quilt cells: `"cell:1+0w"` → `{cell_id, part, agent,
receipt_tip}` — each occupied lattice point wears the tip of the receipt
that placed it. The growth frontier (`BuildPlacement.boundary_points()`) is
exposed as the pseudo-cell `"cell:frontier"`, whose `boundary` lists the
frontier cell ids.

## What counts as failure

| # | Failure | Detected by | Observable behavior |
|---|---------|-------------|---------------------|
| 1 | Same ops, different tips (nondeterminism) | T1 | test fails; ledger tips differ |
| 2 | Replay ≠ live state | T2 | `state_bytes()` mismatch |
| 3 | PLACE on occupied point | append-time gate + T3 | `PlacementError`; nothing appended; tip unchanged |
| 4 | PATH unroutable / occupied goal / start == goal | append-time gate + T4 | `PathNotFound`; nothing appended |
| 5 | Payload byte flipped in chain | NC1 (verify) | `verify() False`; `tip-mismatch` at tampered seq; `replay()` raises `LedgerTamperedError` |
| 6 | Tamper + honest rehash of the entry | NC1b (verify) | `verify() False`; `prev-tip-mismatch` at the next seq |
| 7 | Ghost receipt: hash-valid PLACE on occupied point | NC2 (replay) | `ReplayRefused` — "the ledger cannot lie" |
| 8 | Ghost RELEASE of a free point | NC2 (replay) | `ReplayRefused` |
| 9 | PATH with teleport step / through occupied interior / to occupied goal | NC2 (replay) | `ReplayRefused` |
| 10 | Frontier differs between replay and live | NC3 | frontier cell mismatch |
| 11 | Unknown kind or malformed payload | schema gate (append + replay) | `LedgerError`; nothing appended |
| 12 | RELEASE of an unoccupied point (live) | Build facade | `ValueError`; nothing appended |

Not failure: a PATH whose route is *valid but not shortest* replays fine —
validity is adjudicated (unit lattice steps, free interior, free goal);
optimality is the planner's business, not the ledger's.

## Test receipts (v0.1.0)

`pytest tests/ -v` — **12 passed** in ~0.1s (Python 3.12.14, pytest 9.0.2,
slackwater-lattice 0.1.0 from PyPI):

- `test_t1_determinism_same_ops_same_tip` — identical op sequence twice →
  byte-identical JSONL, identical tip `152df7ee…21f6c9`
- `test_clean_chain_verifies_and_empty_ledger_sits_at_genesis`
- `test_t2_replay_equals_live_state` — coords + parts + agents + per-cell
  receipt tips byte-exact; re-placed cell tipped by its second PLACE
- `test_t3_collision_fails_loudly_nothing_appended`
- `test_t4_path_replays_to_same_waypoints` — fresh A* over prefix-replayed
  geometry re-derives the receipted waypoints exactly
- `test_nc1_tampered_payload_detected_with_tip_mismatch` — one flipped
  payload byte at seq 2 of 9
- `test_nc1b_rehashed_tamper_still_caught_by_chain_link`
- `test_nc2_ghost_place_refused_by_replay` — the ghost fork **verifies
  True** and is still refused: hash-valid ≠ true
- `test_nc2_ghost_release_and_bad_paths_refused`
- `test_nc3_frontier_honesty` — frontier from replay == frontier from live,
  full state and at a prefix
- `test_unknown_kind_and_bad_payloads_refused`
- `test_jsonl_roundtrip_replays_identically`

## Honest limitations

- **No multi-agent locking.** `agent` is a receipt field, not a permission.
  One ledger assumes one writer; concurrent builders will need reservations
  with TTLs or a consensus layer above this. Nothing here prevents two
  processes from appending to *different* ledgers that diverge.
- **No Rust twin wiring.** slackwater-rust's lattice-core reimplements the
  same A₂ math; quilt speaks only to the Python package today. A
  cross-implementation conformance suite (receipts verified by the Rust
  twin) is future work.
- **Single build per ledger.** One `ReceiptLedger` is one build. There is
  no cross-build namespace, no multi-build quilt, no merge of two ledgers.
- **No persistence layer.** JSONL save/load only. No Merkle batching, no
  HMAC-outside-process, no sync contract — see the fleet's edge-ledger for
  the heavyweight version.
- **No signatures.** The hash chain detects *accidental* tampering and
  forces any rewrite to be internally consistent (NC1/NC1b), but anyone with
  write access can re-append a fully rehashed chain. Authenticity (who
  signed receipt #k) is out of scope.
- **Ghost defense is replay-time.** `verify()` alone cannot catch a
  geometry-lie (its hashes are valid); the chain and the geometry are
  separate courts, and only their conjunction is the truth.
- **Replay is O(n)** with no snapshotting/checkpointing; very long builds
  will want periodic state seals.
- **Frontier is occupancy-based** (`boundary_points()`): points with a free
  neighbor. It is not a semantic growth plan and does not reserve anything.
- **Upstream finding (not patched):** the PyPI `slackwater-lattice==0.1.0`
  `hex_distance` (`max(|da|,|db|,|da+db|)`) disagrees with its own
  `neighbors()` for the ±(1+ω) directions — e.g. (0,0)→(1,1) is one
  neighbor step but scores 2. This repo's replay therefore adjudicates
  lattice steps by ℤ[ω] units (`norm(prev - nxt) == 1`), which is exactly
  the neighbor graph A* walks. We did not modify upstream; the newer git
  revision of slackwater-lattice already uses a corrected formula.

## Install & test

```bash
pip install -e .[dev]     # pulls slackwater-lattice>=0.1.0 from PyPI
pytest tests/ -v
```

License: MIT.
