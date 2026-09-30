"""
slackwater-quilt: the ledger is the build.

Every slackwater-lattice build operation becomes an append-only receipted
cell. The build is not a state that has a log; the build IS the log,
replayed.

    >>> from slackwater_quilt import Build, replay
    >>> b = Build()
    >>> b.place_world(0.0, 0.0, part="keel", agent="a1")
    {'seq': 0, 'kind': 'PLACE', ...}
    >>> replay(b.ledger).placement_state() == b.state().placement_state()
    True

Lineage: seed from the SuperInstance/slackwater-lattice study (wave 66-b);
pattern from quilt-qcells — "the ledger is the circuit" — applied here as
"the ledger is the build".
"""

from slackwater_quilt.ledger import (
    GENESIS_TIP,
    HASH_NAME,
    KINDS,
    LedgerError,
    LedgerTamperedError,
    ReceiptEntry,
    ReceiptLedger,
    canonical_json,
    entry_hash,
    validate_payload,
)
from slackwater_quilt.cells import (
    FRONTIER_CELL_ID,
    Build,
    PathNotFound,
    QuiltState,
    ReplayRefused,
    cell_id,
    cell_id_pair,
    coord_from,
    coord_pair,
    replay,
)

__all__ = [
    # ledger
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
    # cells
    "FRONTIER_CELL_ID",
    "Build",
    "QuiltState",
    "ReplayRefused",
    "PathNotFound",
    "cell_id",
    "cell_id_pair",
    "coord_pair",
    "coord_from",
    "replay",
]

__version__ = "0.1.0"
