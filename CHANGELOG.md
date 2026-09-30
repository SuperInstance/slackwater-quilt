# Changelog

## 0.1.0 — first receipt

- `slackwater_quilt.ledger.ReceiptLedger`: append-only sha256 hash chain of
  `{seq, kind, payload, prev_tip, tip}` receipts over canonical JSON.
  Kinds: PLACE / RELEASE / PATH / TICK. `verify()` walks from GENESIS and
  reports seq drift, broken prev_tip links, and tip mismatches at any depth.
  Payload schemas validated at append AND replay.
- `slackwater_quilt.cells`: quilt-cell view — `cell:<a>+<b>w` cells wearing
  their PLACE receipt tip, `cell:frontier` growth-frontier pseudo-cell from
  `BuildPlacement.boundary_points()`; `Build` facade (place / place_world /
  release / record_path / tick — every op receipts itself or fails loudly
  with nothing appended); `replay()` reconstructs the build from GENESIS,
  byte-exact vs live, refusing ghost receipts the geometry rejects.
- Tests: 12 passed (T1 determinism, T2 replay, T3 fail-first collision,
  T4 path re-derivation, NC1/NC1b tamper + rehash, NC2 ghost receipts,
  NC3 frontier honesty, schema refusals, JSONL roundtrip).
- Honest finding: PyPI slackwater-lattice 0.1.0 `hex_distance` disagrees
  with `neighbors()` for ±(1+ω) steps; replay adjudicates steps by
  ℤ[ω]-unit norms instead (upstream unmodified; newer upstream revision
  already corrects the formula).
