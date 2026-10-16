"""The normalized event model that every pipeline stage consumes."""

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple


class Kind(StrEnum):
    SEARCH = "search"
    IMAGE_SEARCH = "image_search"
    VISIT = "visit"
    # A click on an ad in search results.
    AD_CLICK = "ad_click"
    APP_USE = "app_use"
    # A place or knowledge card ("Viewed Walgreens Pharmacy"); a strong location signal.
    PLACE_VIEW = "place_view"
    # A digest of notifications; the text is the list of topics and how each was handled.
    NOTIFICATIONS = "notifications"
    OTHER = "other"


class Event(NamedTuple):
    # Stable and chronological. Every downstream claim cites events by this id.
    id: int
    # Always UTC; localization happens only at render time, once the itinerary is known.
    ts: datetime
    # The Takeout product: "Search", "AI Mode", or "Ads". "Ads" is activity Google observed
    # through its ad network on third-party sites and apps, not ads the user clicked.
    product: str
    kind: Kind
    # The payload, with the verb prefix ("Searched for ", "Visited ", ...) stripped.
    text: str
    # The destination URL, unwrapped from Google's redirector, when there is one.
    url: str | None
    # The site domain or app name, when Takeout reports one (from the "header" field).
    source: str | None
    lat: float | None
    lon: float | None
    # How Google located this event: "device", "home", "work", "past activity", ...
    loc_source: str | None = None
    # True for "At this place", False for "At this general area" (a ~zoom-12 map tile).
    loc_precise: bool | None = None

    def to_json(self) -> dict:
        return self._asdict() | {"ts": self.ts.isoformat()}

    @classmethod
    def from_json(cls, data: dict) -> "Event":
        return cls(
            **(data | {"ts": datetime.fromisoformat(data["ts"]), "kind": Kind(data["kind"])})
        )


def write_events(events: list[Event], path: Path) -> None:
    with open(path, "w", encoding="utf8") as f:
        for event in events:
            f.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")


def read_events(path: Path) -> list[Event]:
    with open(path, encoding="utf8") as f:
        return [Event.from_json(json.loads(line)) for line in f if line.strip()]
