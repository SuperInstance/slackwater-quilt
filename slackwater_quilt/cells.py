"""
The quilt-cell view: the build IS its ledger.

A quilt cell is one occupied lattice point wearing its receipt:

    "cell:<a>+<b>w"  ->  {"cell_id": ..., "part": ..., "agent": ..., "receipt_tip": ...}

Plus one pseudo-cell, the growth frontier:

    "cell:frontier"  ->  {"cell_id": "cell:frontier", "boundary": [sorted cell ids]}

which is exactly ``BuildPlacement.boundary_points()`` — occupied points that
still have a free neighbor — expressed as cells.

TWO GATES, ONE TRUTH
--------------------
1. Append-time gate (Build facade): a PLACE onto an occupied point raises
   PlacementError and appends NOTHING; the tip is unchanged.
2. Replay-time adjudication (replay()): a receipt whose geometric claim is
   false is refused. A hash-valid ledger can still lie; only geometry
   adjudicates. replay() therefore reconstructs the build from GENESIS with
   full validation and returns a QuiltState whose cells are byte-exact equal
   to the live state (T2), including which receipt tipped each cell.
"""

from __future__ import annotations

from typing import Optional

from slackwater_lattice import BuildPlacement, EisensteinInteger, LatticePathfinder

from slackwater_quilt.ledger import (
    LedgerError,
    ReceiptLedger,
    canonical_json,
    validate_payload,
)

__all__ = [
    "FRONTIER_CELL_ID",
    "QuiltState",
    "Build",
    "ReplayRefused",
    "PathNotFound",
    "cell_id",
    "cell_id_pair",
    "coord_pair",
    "coord_from",
    "replay",
]


FRONTIER_CELL_ID = "cell:frontier"


class ReplayRefused(Exception):
    """
    Raised when replay() encounters a receipt whose claim the geometry
    rejects. The ledger cannot lie about geometry: a hash-valid entry
    that claims a PLACE on an occupied point (or a RELEASE of a free
    point, or a PATH through walls) cannot be replayed.
    """


class PathNotFound(Exception):
    """Raised when a PATH operation finds no route over free points."""


# ── Coord encoding ───────────────────────────────────────
# Receipt payloads carry exact integer pairs [a, b] (Eisenstein a + bω).
# No floats ever enter a receipt: the pre-snap continuous position is not
# part of the exact build state, and floats would break byte-exact replay.


def coord_pair(z: EisensteinInteger) -> list[int]:
    return [z.a, z.b]


def coord_from(pair) -> EisensteinInteger:
    a, b = pair
    return EisensteinInteger(int(a), int(b))


def cell_id_pair(a: int, b: int) -> str:
    return f"cell:{a}+{b}w"


def cell_id(z: EisensteinInteger) -> str:
    return cell_id_pair(z.a, z.b)


# ── The quilt-cell state ─────────────────────────────────


class QuiltState:
    """
    Cell view of a build: occupied points + their tipping receipts + frontier.

    state_bytes() is the canonical serialization used for byte-exact
    comparison between the live state and the replayed state.
    """

    def __init__(self, placement: BuildPlacement, receipt_tips: dict[EisensteinInteger, str]):
        self.placement = placement
        self.receipt_tips = receipt_tips

    def occupied_cells(self) -> dict[str, dict]:
        """{cell_id: {cell_id, part, agent, receipt_tip}} sorted by cell_id."""
        cells: dict[str, dict] = {}
        for point in sorted(self.placement.occupied):
            meta = self.placement.metadata.get(point, {})
            cid = cell_id(point)
            cells[cid] = {
                "cell_id": cid,
                "part": meta.get("part"),
                "agent": meta.get("agent"),
                "receipt_tip": self.receipt_tips.get(point),
            }
        return cells

    def frontier(self) -> list[str]:
        """Sorted cell ids of the growth frontier (boundary_points())."""
        return sorted(cell_id(p) for p in self.placement.boundary_points())

    def cells(self) -> dict[str, dict]:
        """Occupied cells plus the 'cell:frontier' growth-frontier pseudo-cell."""
        cells = self.occupied_cells()
        cells[FRONTIER_CELL_ID] = {"cell_id": FRONTIER_CELL_ID, "boundary": self.frontier()}
        return cells

    def state_bytes(self) -> bytes:
        """Canonical byte form of the full cell state (sorted keys, no whitespace)."""
        return canonical_json(self.cells()).encode("utf-8")

    def placement_state(self) -> dict[str, dict]:
        """Plain {cell_id: {part, agent}} view (coords + parts + agents)."""
        out: dict[str, dict] = {}
        for point in sorted(self.placement.occupied):
            meta = self.placement.metadata.get(point, {})
            out[cell_id(point)] = {"part": meta.get("part"), "agent": meta.get("agent")}
        return out


# ── The Build facade: geometry gate + ledger append ──────


class Build:
    """
    A build is its ledger. Every operation either receipts itself or
    fails loudly with NOTHING appended.

    Operations:
      * place(coord, part, agent)   -> PLACE receipt
      * release(coord)              -> RELEASE receipt
      * record_path(start, goal)    -> PATH receipt (A* over free points)
      * tick(t)                     -> TICK receipt
    """

    def __init__(self, scale: float = 4.0, ledger: Optional[ReceiptLedger] = None):
        self.scale = scale
        self.placement = BuildPlacement(scale=scale)
        self.ledger = ledger if ledger is not None else ReceiptLedger()
        self.pathfinder = LatticePathfinder(self.placement)

    # ── Operations ───────────────────────────────────────

    def place(self, coord: EisensteinInteger, part: str, agent: str) -> dict:
        """
        Reserve an exact lattice point and receipt it. Raises PlacementError
        on collision — nothing is appended, the tip is unchanged.
        """
        # Geometry gate FIRST: fail loudly before the ledger hears about it.
        self.placement.reserve(coord, {"part": part, "agent": agent})
        entry = self.ledger.append("PLACE", {
            "coord": coord_pair(coord),
            "part": part,
            "agent": agent,
        })
        return entry.to_dict()

    def place_world(self, x: float, z: float, part: str, agent: str) -> dict:
        """Snap continuous world position to the lattice, then place()."""
        return self.place(self.placement.snap_position(x, z), part, agent)

    def release(self, coord: EisensteinInteger) -> dict:
        """
        Release an occupied point and receipt it. Raises ValueError if the
        point is not occupied (a release of nothing is a lie — refuse loudly).
        """
        if not self.placement.check_collision(coord):
            raise ValueError(f"RELEASE refused: {cell_id(coord)} is not occupied")
        self.placement.release(coord)
        entry = self.ledger.append("RELEASE", {"coord": coord_pair(coord)})
        return entry.to_dict()

    def record_path(self, start: EisensteinInteger, goal: EisensteinInteger) -> dict:
        """
        Run A* over free points and receipt the waypoints. Raises
        PathNotFound (nothing appended) if no route exists.

        Semantics mirror LatticePathfinder.find_path exactly: the route
        runs over free points, the start may be an occupied cell (a crane
        on a built part), the goal must be free, and start == goal is not
        a route. Replay adjudicates these same rules.
        """
        if start == goal:
            raise PathNotFound(f"start == goal ({cell_id(start)}): nothing to route")
        path = self.pathfinder.find_path(start, goal)
        if path is None:
            raise PathNotFound(f"no path from {cell_id(start)} to {cell_id(goal)}")
        waypoints = [coord_pair(p) for p in path.points]
        entry = self.ledger.append("PATH", {
            "from": coord_pair(start),
            "to": coord_pair(goal),
            "waypoints": waypoints,
        })
        return entry.to_dict()

    def tick(self, t: int) -> dict:
        """Receipt a build tick (a timestep marker in the build's history)."""
        if not isinstance(t, int) or isinstance(t, bool):
            raise LedgerError(f"tick t must be int, got {t!r}")
        entry = self.ledger.append("TICK", {"t": t})
        return entry.to_dict()

    # ── State ────────────────────────────────────────────

    def state(self) -> QuiltState:
        """Live cell view, including which PLACE receipt tips each cell."""
        return QuiltState(self.placement, _live_receipt_tips(self))

    def __len__(self) -> int:
        return len(self.placement)


def _live_receipt_tips(build: Build) -> dict[EisensteinInteger, str]:
    """
    Which PLACE receipt currently tips each occupied cell: walk the ledger
    once and keep the last PLACE per coord. Replay produces the identical
    map by construction, so cell provenance is part of the byte-exact state.
    """
    tips: dict[EisensteinInteger, str] = {}
    for entry in build.ledger:
        if entry.kind == "PLACE":
            tips[coord_from(entry.payload["coord"])] = entry.tip
    return tips


# ── Replay: the ledger replays the build from GENESIS ────


def replay(ledger: ReceiptLedger, verify_first: bool = True, upto: Optional[int] = None) -> QuiltState:
    """
    Reconstruct the build purely from receipts, from GENESIS.

    With ``upto=k`` only entries with seq <= k are applied (prefix replay:
    the build as it stood at receipt k — used to re-derive receipts like
    PATH against the geometry that existed when they were written).

    Every entry is applied with full geometric adjudication:
      * PLACE  onto an occupied point  -> ReplayRefused (ghost receipt)
      * RELEASE of a free point        -> ReplayRefused
      * PATH   non-adjacent waypoints, empty chain, endpoints mismatch,
               occupied interior, or occupied goal -> ReplayRefused
      * malformed payload / kind       -> LedgerError
    With verify_first=True (default) a tampered chain raises
    LedgerTamperedError before any geometry is applied.
    """
    if verify_first:
        ledger.require_intact()

    placement = BuildPlacement()
    receipt_tips: dict[EisensteinInteger, str] = {}

    for entry in ledger:
        if upto is not None and entry.seq > upto:
            break
        validate_payload(entry.kind, entry.payload)
        payload = entry.payload

        if entry.kind == "PLACE":
            point = coord_from(payload["coord"])
            if point in placement.occupied:
                raise ReplayRefused(
                    f"ghost receipt at seq {entry.seq}: PLACE claims {cell_id(point)} "
                    f"but the geometry already occupies it — the ledger cannot lie"
                )
            placement.reserve(point, {"part": payload["part"], "agent": payload["agent"]})
            receipt_tips[point] = entry.tip

        elif entry.kind == "RELEASE":
            point = coord_from(payload["coord"])
            if point not in placement.occupied:
                raise ReplayRefused(
                    f"ghost receipt at seq {entry.seq}: RELEASE claims {cell_id(point)} "
                    f"but the geometry does not occupy it — the ledger cannot lie"
                )
            placement.release(point)
            receipt_tips.pop(point, None)

        elif entry.kind == "PATH":
            start = coord_from(payload["from"])
            goal = coord_from(payload["to"])
            waypoints = [coord_from(w) for w in payload["waypoints"]]
            if waypoints[0] != start or waypoints[-1] != goal:
                raise ReplayRefused(
                    f"ghost receipt at seq {entry.seq}: PATH endpoints do not match waypoints"
                )
            if len(waypoints) < 2:
                raise ReplayRefused(
                    f"ghost receipt at seq {entry.seq}: PATH needs at least two waypoints"
                )
            for prev, nxt in zip(waypoints, waypoints[1:]):
                # A lattice step is subtraction by a unit of Z[omega]:
                # norm(prev - nxt) == 1. This is exactly the six-element
                # neighbor set A* moves along. (We deliberately do NOT use
                # hex_distance here: the PyPI 0.1.0 formula max(|da|,|db|,|da+db|)
                # disagrees with neighbors() for the (±1,±1) directions —
                # e.g. (0,0)->(1,1) is one neighbor step but scores 2.
                # Geometry adjudicates by the neighbor graph itself.)
                if (prev - nxt).norm() != 1:
                    raise ReplayRefused(
                        f"ghost receipt at seq {entry.seq}: PATH step "
                        f"{cell_id(prev)} -> {cell_id(nxt)} is not a lattice step"
                    )
            for interior in waypoints[1:-1]:
                if interior in placement.occupied:
                    raise ReplayRefused(
                        f"ghost receipt at seq {entry.seq}: PATH passes through "
                        f"occupied {cell_id(interior)} — the ledger cannot lie"
                    )
            if goal in placement.occupied:
                raise ReplayRefused(
                    f"ghost receipt at seq {entry.seq}: PATH goal {cell_id(goal)} "
                    f"is occupied — the ledger cannot lie"
                )

        elif entry.kind == "TICK":
            pass  # a tick carries time only; nothing for geometry to judge

    return QuiltState(placement, receipt_tips)
