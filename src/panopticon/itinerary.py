"""Stage 4: reconstruct where the user was over time, and localize timestamps accordingly.

Evidence comes from two sources: place atoms from stage 3, and Google's own location fixes.
Evidence is clustered into places (~metro areas), then a Viterbi pass over hourly bins picks
the most plausible place for each hour. Each hour, every place scores the weight of the
evidence for it in that hour; home also gets a small baseline, so that with no evidence the
itinerary drifts back home; and changing places costs SWITCH_COST, so it takes real evidence
to start a trip, and a lone weak signal can't.
"""

import math
from collections import defaultdict
from collections.abc import Iterable
from itertools import pairwise
from datetime import UTC, date, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from panopticon.events import Event
from panopticon.features import Home, timezone_at

BIN = timedelta(hours=1)

STRENGTH = {"strong": 3.0, "moderate": 1.5, "weak": 0.5}
# Atoms about places the user was only planning or curious about are not point evidence.
RELATION = {"present": 1.0, "in_transit": 0.5}
FIX_WEIGHT = {
    "device": 2.0,
    "home": 2.0,
    "work": 2.0,
    "location history": 1.0,
    "past activity": 0.5,
}
# A dated plan (a hotel stay, a booked flight) supports its place for every hour it covers,
# at this fraction of its strength per hour: a strong 3-day booking adds ~11 in total.
PLAN_PER_HOUR = 0.05
HOME_PRIOR = 0.05
SWITCH_COST = 3.0
# Away from home, places are metro areas (a trip to "Denver" and one to "Boulder" are the same
# trip); around home they are tighter, so a day in the city still shows up.
CLUSTER_KM = 80.0
HOME_KM = 25.0
# Between two sightings of the user in the same place at most BRIDGE_MAX apart, assume they
# stayed: each hour of the gap gets this much support, a little more than home's baseline,
# so it takes contrary evidence (a home location fix, say) to break the stay.
BRIDGE_MAX = timedelta(days=4)
BRIDGE_PER_HOUR = 0.1


class Evidence(NamedTuple):
    start: datetime
    # Exclusive. For point evidence, start + BIN.
    end: datetime
    lat: float
    lon: float
    # Weight per bin covered.
    weight: float
    label: str
    event_ids: tuple[int, ...]
    kind: str  # "fix", "atom", "plan", or "bridge"


class Place(NamedTuple):
    index: int
    lat: float
    lon: float
    tz: str
    label: str
    is_home: bool


class Segment(NamedTuple):
    start: datetime
    end: datetime
    place: Place
    # Total evidence weight supporting this place within the segment, and its event ids.
    weight: float
    event_ids: list[int]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _day_start(day: str, tz: str | None) -> datetime:
    zone = ZoneInfo(tz) if tz else UTC
    return datetime.combine(date.fromisoformat(day), time(), zone).astimezone(UTC)


def evidence_from_atoms(atoms: Iterable[dict], events: dict[int, Event]) -> list[Evidence]:
    out: list[Evidence] = []
    for atom in atoms:
        ids = tuple(i for i in atom["event_ids"] if i in events)
        if not ids:
            continue
        strength = STRENGTH[atom["strength"]]
        relation = atom["relation"]
        ts = min(events[i].ts for i in ids)
        # Atoms from before the schema had a region fall back to the specific place.
        label = atom.get("region") or atom["place"]

        start = end = None
        if relation != "remote_interest" and atom.get("starts"):
            try:
                start = _day_start(atom["starts"], atom.get("tz"))
                end = _day_start(atom["ends"], atom.get("tz")) if atom.get("ends") else start
            except ValueError:
                start = end = None
        if start is not None and end is not None:
            # A one-day plan (no end, or ending the day it starts) covers that whole day.
            end = max(end, start + timedelta(days=1))
            out.append(
                Evidence(start, end, atom["lat"], atom["lon"], strength * PLAN_PER_HOUR,
                         label, ids, "plan")
            )

        # Point evidence of being there when the events happened. A lookup made before the
        # date it is about (checking tomorrow's flight) is a plan, not presence.
        if relation in RELATION and (start is None or ts >= start):
            out.append(
                Evidence(ts, ts + BIN, atom["lat"], atom["lon"], strength * RELATION[relation],
                         label, ids, "atom")
            )
    return out


def evidence_from_fixes(events: Iterable[Event]) -> list[Evidence]:
    return [
        Evidence(e.ts, e.ts + BIN, e.lat, e.lon, FIX_WEIGHT.get(e.loc_source or "", 1.0),
                 f"location fix ({e.loc_source})", (e.id,), "fix")
        for e in events
        if e.lat is not None and e.lon is not None
    ]


def cluster(evidence: list[Evidence], home: Home | None) -> tuple[list[Place], list[int]]:
    """Greedily group evidence into places of radius CLUSTER_KM, strongest evidence first so
    that cluster centers sit on the best-attested points. Returns the places and, for each
    evidence item, the index of its place."""
    centers: list[tuple[float, float]] = []
    labels: list[dict[str, float]] = []
    if home:
        centers.append((home.lat, home.lon))
        labels.append({"home": math.inf})

    assignment = [0] * len(evidence)
    order = sorted(range(len(evidence)), key=lambda i: -evidence[i].weight)
    for i in order:
        e = evidence[i]
        for c, (lat, lon) in enumerate(centers):
            radius = HOME_KM if home and c == 0 else CLUSTER_KM
            if haversine_km(lat, lon, e.lat, e.lon) <= radius:
                break
        else:
            c = len(centers)
            centers.append((e.lat, e.lon))
            labels.append({})
        assignment[i] = c
        if e.kind != "fix":
            labels[c][e.label] = labels[c].get(e.label, 0.0) + e.weight

    places = []
    for c, (lat, lon) in enumerate(centers):
        is_home = home is not None and c == 0
        label = (
            "home"
            if is_home
            else max(labels[c], key=labels[c].get) if labels[c] else f"near {lat:.2f},{lon:.2f}"
        )
        places.append(Place(c, lat, lon, timezone_at(lat, lon) or "UTC", label, is_home))
    return places, assignment


def bridges(
    evidence: list[Evidence], places: list[Place], assignment: list[int]
) -> tuple[list[Evidence], list[int]]:
    """Support for staying put between consecutive sightings in the same place (away from
    home) that are at most BRIDGE_MAX apart."""
    by_place: dict[int, list[Evidence]] = defaultdict(list)
    for e, p in zip(evidence, assignment):
        if e.kind in ("atom", "fix") and not places[p].is_home:
            by_place[p].append(e)
    out: list[Evidence] = []
    out_assignment: list[int] = []
    for p, sightings in by_place.items():
        sightings.sort(key=lambda e: e.start)
        for a, b in pairwise(sightings):
            if a.end < b.start and b.start - a.end <= BRIDGE_MAX:
                out.append(Evidence(a.end, b.start, places[p].lat, places[p].lon,
                                    BRIDGE_PER_HOUR, places[p].label, (), "bridge"))
                out_assignment.append(p)
    return out, out_assignment


def solve(
    evidence: list[Evidence],
    places: list[Place],
    assignment: list[int],
    start: datetime,
    end: datetime,
) -> list[Segment]:
    """Viterbi over hourly bins in [start, end). Switching places costs SWITCH_COST; because
    that cost is the same between any two places, each step only needs the best previous
    score, so this runs in O(bins × places)."""
    n_bins = max(1, math.ceil((end - start) / BIN))
    n = len(places)
    emit: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for e, p in zip(evidence, assignment):
        first = max(0, int((e.start - start) / BIN))
        last = min(n_bins, max(first + 1, math.ceil((e.end - start) / BIN)))
        for b in range(first, last):
            emit[b][p] += e.weight
    prior = [HOME_PRIOR if place.is_home else 0.0 for place in places]

    score = [prior[s] + emit.get(0, {}).get(s, 0.0) for s in range(n)]
    # For each bin, the best previous state, and for each state whether it switched into
    # that state from the best previous one (1) or stayed (0).
    best_prev: list[int] = [0] * n_bins
    switched: list[bytearray] = [bytearray(n)]
    for b in range(1, n_bins):
        best = max(range(n), key=score.__getitem__)
        best_prev[b] = best
        moved = bytearray(n)
        bin_emit = emit.get(b, {})
        new = [0.0] * n
        for s in range(n):
            stay, switch = score[s], score[best] - SWITCH_COST
            if switch > stay:
                moved[s] = 1
                stay = switch
            new[s] = stay + prior[s] + bin_emit.get(s, 0.0)
        score = new
        switched.append(moved)

    path = [0] * n_bins
    path[-1] = max(range(n), key=score.__getitem__)
    for b in range(n_bins - 1, 0, -1):
        path[b - 1] = best_prev[b] if switched[b][path[b]] else path[b]

    segments: list[Segment] = []
    run_start = 0
    for b in range(1, n_bins + 1):
        if b == n_bins or path[b] != path[run_start]:
            p = path[run_start]
            weight = 0.0
            ids: set[int] = set()
            for e, q in zip(evidence, assignment):
                lo = max(run_start, int((e.start - start) / BIN))
                hi = min(b, math.ceil((e.end - start) / BIN))
                if q == p and hi > lo:
                    weight += e.weight * (hi - lo)
                    ids.update(e.event_ids)
            segments.append(
                Segment(start + run_start * BIN, start + b * BIN, places[p], weight, sorted(ids))
            )
            run_start = b
    return segments


def build(
    atoms: list[dict], events: list[Event], home: Home | None
) -> tuple[list[Segment], list[Place]]:
    """The full itinerary for the time span of `events`."""
    if not events:
        return [], []
    by_id = {e.id: e for e in events}
    evidence = evidence_from_atoms(atoms, by_id) + evidence_from_fixes(events)
    places, assignment = cluster(evidence, home)
    if not places:
        return [], []
    extra, extra_assignment = bridges(evidence, places, assignment)
    evidence, assignment = evidence + extra, assignment + extra_assignment
    start = events[0].ts.replace(minute=0, second=0, microsecond=0)
    segments = solve(evidence, places, assignment, start, events[-1].ts + BIN)
    return segments, places


class Localizer:
    """Maps a UTC timestamp to the zone the itinerary places the user in at that moment."""

    def __init__(self, segments: list[Segment], default: str = "UTC") -> None:
        self._segments = segments
        self._starts = [s.start for s in segments]
        self._default = ZoneInfo(default)

    def __call__(self, ts: datetime) -> ZoneInfo:
        import bisect

        i = bisect.bisect_right(self._starts, ts) - 1
        if 0 <= i < len(self._segments) and ts < self._segments[i].end:
            return ZoneInfo(self._segments[i].place.tz)
        return self._default
