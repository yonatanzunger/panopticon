import json
from datetime import UTC, datetime

import pytest

from panopticon.events import Kind
from panopticon.ingest import discover, load
from panopticon.sources import read_csv
from panopticon.sources.my_activity import classify, parse_row, unwrap_url

MAP = "https://www.google.com/maps/@?api=1&map_action=map&center=37.383159,-122.095106&zoom=12"


def test_classify_prefixes():
    assert classify("Searched for weather lima") == (Kind.SEARCH, "weather lima")
    assert classify("Searched with an image") == (Kind.IMAGE_SEARCH, "")
    assert classify("Visited Caltrain: Home") == (Kind.VISIT, "Caltrain: Home")
    assert classify("Used Spotify - Apps on Google Play") == (Kind.APP_USE, "Spotify")
    assert classify("Selected Maps") == (Kind.APP_USE, "Maps")
    assert classify("Viewed Walgreens Pharmacy") == (Kind.PLACE_VIEW, "Walgreens Pharmacy")
    assert classify("3 notifications") == (Kind.NOTIFICATIONS, "")
    assert classify("Searched for foo\n\nbar") == (Kind.SEARCH, "foo bar")


def test_unwrap_url():
    wrapped = "https://www.google.com/url?q=https://example.com/a%3Fb%3D1&usg=xyz"
    assert unwrap_url(wrapped) == "https://example.com/a?b=1"
    assert unwrap_url("https://example.com/") == "https://example.com/"
    assert unwrap_url(None) is None


def test_search_with_home_location():
    (item,) = parse_row(
        {
            "header": "Search",
            "title": "Searched for plant nursery",
            "titleUrl": "https://www.google.com/search?q=plant+nursery",
            "time": "2026-06-27T20:29:00.000Z",
            "products": ["Search"],
            "locationInfos": [
                {"name": "At this general area", "url": MAP, "source": "From your places (Home)"}
            ],
        }
    )
    assert item.kind == Kind.SEARCH
    assert item.url is None
    assert item.source is None
    assert (item.lat, item.lon) == (37.383159, -122.095106)
    assert item.loc_source == "home"
    assert item.loc_precise is False


def test_ads_product_visit_keeps_site_and_real_url():
    (item,) = parse_row(
        {
            "header": "smbc-comics.com",
            "title": "Visited https://smbc-comics.com/",
            "titleUrl": "https://www.google.com/url?q=https://smbc-comics.com/&usg=abc",
            "time": "2026-10-15T21:20:05Z",
            "products": ["Ads"],
            "details": [{"name": "From Google Ads"}],
        }
    )
    # In the Ads product, "From Google Ads" means observed via the ad network, not a click.
    assert item.kind == Kind.VISIT
    assert item.url == "https://smbc-comics.com/"
    assert item.source == "smbc-comics.com"


def test_search_ad_click():
    (item,) = parse_row(
        {
            "header": "Search",
            "title": "Visited https://shop.example/",
            "titleUrl": "https://www.google.com/url?q=https://shop.example/",
            "time": "2026-08-16T16:46:00Z",
            "products": ["Search"],
            "details": [{"name": "From Google Ads"}],
        }
    )
    assert item.kind == Kind.AD_CLICK


def test_notifications_keep_topics():
    (item,) = parse_row(
        {
            "header": "Search",
            "title": "2 notifications",
            "subtitles": [
                {"name": "Including topics:"},
                {"name": "Weather - dismissed"},
                {"name": "Artificial intelligence - clicked"},
            ],
            "time": "2026-10-15T21:46:27Z",
            "products": ["Search"],
        }
    )
    assert item.kind == Kind.NOTIFICATIONS
    assert item.text == "Weather - dismissed; Artificial intelligence - clicked"


def test_ai_mode_follow_ups_become_events():
    html = (
        "<p><strong>Your prompt:</strong><br>\nasus xt8 factory reset</p>"
        "<p><strong>Search&#39;s response:</strong><br>\nHold the button.</p>"
        "<p><strong>Your prompt:</strong><br>\nwhat if the light is <em>pink</em>?</p>"
    )
    items = list(
        parse_row(
            {
                "header": "AI Mode",
                "title": "Searched for asus xt8 factory reset",
                "time": "2026-08-01T21:42:00Z",
                "products": ["AI Mode"],
                "safeHtmlItem": [{"html": html}],
            }
        )
    )
    assert [i.text for i in items] == ["asus xt8 factory reset", "what if the light is pink?"]
    assert all(i.product == "AI Mode" for i in items)


def _row(title: str, time: str) -> dict:
    return {"header": "Search", "title": title, "time": time, "products": ["Search"]}


def test_load_ids_are_stable_across_ranges(tmp_path):
    (tmp_path / "My Activity" / "Search").mkdir(parents=True)
    rows = [
        _row("Searched for c", "2026-03-01T00:00:00Z"),
        _row("Searched for a", "2026-01-01T00:00:00Z"),
        _row("Searched for b", "2026-02-01T00:00:00Z"),
    ]
    (tmp_path / "My Activity" / "Search" / "MyActivity.json").write_text(json.dumps(rows))

    everything = load(discover([tmp_path])).events
    assert [(e.id, e.text) for e in everything] == [(0, "a"), (1, "b"), (2, "c")]

    subset = load(discover([tmp_path]), since=datetime(2026, 2, 1, tzinfo=UTC)).events
    assert [(e.id, e.text) for e in subset] == [(1, "b"), (2, "c")]

    subset = load(discover([tmp_path]), until=datetime(2026, 2, 1, tzinfo=UTC)).events
    assert [(e.id, e.text) for e in subset] == [(0, "a")]


def test_discover_finds_claimed_files_anywhere_and_skips_the_rest(tmp_path):
    for folder in ("Search", "YouTube"):
        (tmp_path / "My Activity" / folder).mkdir(parents=True)
        (tmp_path / "My Activity" / folder / "MyActivity.json").write_text("[]")
    (tmp_path / "archive_browser.html").write_text("<html>")
    found = discover([tmp_path, tmp_path / "My Activity" / "Search"])  # overlapping inputs
    assert sorted(f.parent.name for _, f in found) == ["Search", "YouTube"]
    assert {s.name for s, _ in found} == {"my_activity"}


def test_discover_rejects_unreadable_or_missing_inputs(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    with pytest.raises(ValueError):
        discover([tmp_path / "notes.txt"])
    with pytest.raises(FileNotFoundError):
        discover([tmp_path / "nope"])


def test_read_csv_strips_byte_order_mark(tmp_path):
    (tmp_path / "a.csv").write_bytes("\ufeffChannel Id,Title\nUC1,\"Hello, world\"\n".encode())
    assert list(read_csv(tmp_path / "a.csv")) == [{"Channel Id": "UC1", "Title": "Hello, world"}]


def _activity(tmp_path, product: str, rows: list[dict]) -> None:
    (tmp_path / "My Activity" / product).mkdir(parents=True, exist_ok=True)
    (tmp_path / "My Activity" / product / "MyActivity.json").write_text(json.dumps(rows))


def test_my_activity_products_merge_and_chrome_rows_follow_chrome_rules(tmp_path):
    t = "2026-10-10T17:52:29Z"
    _activity(tmp_path, "Search", [
        _row("Searched for california dmv", t) | {"locationInfos": [
            {"name": "At this general area", "url": MAP, "source": "From your places (Home)"}]},
        {"header": "Search", "title": "Visited Renew - California DMV", "time": t,
         "titleUrl": "https://www.google.com/url?q=https://www.dmv.ca.gov/portal/renew/",
         "products": ["Search"]},
    ])
    _activity(tmp_path, "Chrome", [
        {"header": "Chrome", "title": "Visited california dmv - Google Search", "time": t,
         "titleUrl": "https://www.google.com/search?q=california+dmv&oq=califo",
         "products": ["Chrome"]},
        {"header": "Chrome", "title": "Visited Renew - California DMV", "time": t,
         "titleUrl": "https://www.dmv.ca.gov/portal/renew/?utm_source=x", "products": ["Chrome"]},
        {"header": "Chrome", "title": "Visited Another page", "time": t,
         "titleUrl": "https://www.dmv.ca.gov/other", "products": ["Chrome"]},
    ])
    _activity(tmp_path, "Google Analytics", [
        {"header": "www.dmv.ca.gov/other", "title": "Visited www.dmv.ca.gov/other",
         "titleUrl": "http://www.dmv.ca.gov/other", "time": t, "products": ["Google Analytics"]},
    ])
    loaded = load(discover([tmp_path]))
    # Chrome's search page merges into the Search search; Chrome's DMV page (with tracking
    # query) into Search's result click; Analytics's bare URL into Chrome's titled page.
    assert loaded.duplicates == 3
    assert sorted((e.product, e.kind.value, e.text) for e in loaded.events) == [
        ("Chrome", "visit", "Another page"),
        ("Search", "search", "california dmv"),
        ("Search", "visit", "Renew - California DMV"),
    ]
