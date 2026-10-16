import json
from datetime import UTC, datetime, timedelta

from panopticon.events import Kind
from panopticon.ingest import discover, load, merge_duplicates
from panopticon.sources import Item
from panopticon.sources.chrome import clean_title, parse_visit

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def row(url: str, title: str = "A page", at: datetime = T0, client: str = "c1") -> dict:
    return {"url": url, "title": title, "time_usec": int(at.timestamp() * 1e6),
            "client_id": client, "favicon_url": "", "page_transition_qualifier": "CLIENT_REDIRECT"}


def test_visit():
    item = parse_visit(row("https://example.com/a?b=1", "Example"), "phone: Pixel")
    assert item is not None
    assert (item.ts, item.product, item.kind) == (T0, "Chrome", Kind.VISIT)
    assert (item.text, item.url, item.device) == ("Example", "https://example.com/a?b=1",
                                                  "phone: Pixel")


def test_untitled_visit_uses_url():
    assert parse_visit(row("https://example.com/", ""), None).text == "https://example.com/"


def test_google_search_becomes_search_and_redirector_is_dropped():
    item = parse_visit(row("https://www.google.com/search?q=plant+nursery&sca_esv=x"), None)
    assert (item.kind, item.text, item.url) == (Kind.SEARCH, "plant nursery", None)
    assert parse_visit(row("https://www.google.com/url?q=https://example.com/"), None) is None
    assert parse_visit(row("chrome-extension://abc/page.html"), None) is None


def test_clean_title_drops_unread_counts():
    assert clean_title("(3) Facebook") == "Facebook"
    assert clean_title("Inbox (1,234) - me@example.com - Gmail") == "Inbox - me@example.com - Gmail"
    assert clean_title("Census (2020) results") == "Census (2020) results"


def test_devices_from_device_information(tmp_path):
    chrome = tmp_path / "Takeout" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "History.json").write_text(json.dumps({"Browser History": [
        row("https://a.example/", at=T0, client="mac"),
        row("https://b.example/", at=T0 + timedelta(minutes=1), client="gone"),
    ]}))
    (chrome / "Device Information.json").write_text(json.dumps({"Device Info": [
        {"cache_guid": "mac", "device_form_factor": "DEVICE_FORM_FACTOR_DESKTOP",
         "client_name": "ember"},
    ]}))
    (chrome / "Bookmarks.html").write_text("<html>")
    events = load(discover([tmp_path])).events
    assert [e.device for e in events] == ["desktop: ember", "unknown device"]


def item(product: str, kind: Kind, text: str, url: str | None, at: datetime) -> Item:
    return Item(at, product, kind, text, url, None, None, None, None, None)


def test_merge_duplicates_across_sources_only():
    activity = [
        item("Search", Kind.SEARCH, "Plant  Nursery", None, T0),
        item("Search", Kind.SEARCH, "plant nursery", None, T0 + timedelta(seconds=5)),
        item("Ads", Kind.VISIT, "Shop", "https://shop.example/", T0),
    ]
    chrome = [
        item("Chrome", Kind.SEARCH, "plant nursery", None, T0 + timedelta(seconds=1)),
        item("Chrome", Kind.SEARCH, "plant nursery", None, T0 + timedelta(hours=1)),
        item("Chrome", Kind.VISIT, "Page", "https://x.example/", T0),
        # One Ads record, three Chrome loads: one merges, two are real reloads.
        *[item("Chrome", Kind.VISIT, "Shop", "https://shop.example/", T0 + timedelta(seconds=s))
          ._replace(device="phone: P") for s in (2, 30, 60)],
    ]
    kept, merged = merge_duplicates([activity, chrome])
    # Both My Activity repeats stay (repeats within a source are real); the nearest Chrome copy
    # of each goes; the later search and the unmatched visit stay.
    assert merged == 2
    assert [(i.product, i.ts - T0) for i in kept] == [
        ("Search", timedelta(0)), ("Search", timedelta(seconds=5)), ("Ads", timedelta(0)),
        ("Chrome", timedelta(hours=1)), ("Chrome", timedelta(0)),
        ("Chrome", timedelta(seconds=30)), ("Chrome", timedelta(seconds=60)),
    ]
    # The kept Ads record picks up the device from the Chrome copy merged into it.
    assert kept[2].device == "phone: P"


def test_discover_can_skip_sources(tmp_path):
    (tmp_path / "Chrome").mkdir()
    (tmp_path / "Chrome" / "History.json").write_text('{"Browser History": []}')
    (tmp_path / "My Activity" / "Search").mkdir(parents=True)
    (tmp_path / "My Activity" / "Search" / "MyActivity.json").write_text("[]")
    assert {s.name for s, _ in discover([tmp_path])} == {"my_activity", "chrome"}
    assert {s.name for s, _ in discover([tmp_path], ["my_activity"])} == {"my_activity"}
