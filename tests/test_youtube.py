import json
from datetime import UTC, datetime

from panopticon.compress import format_line
from panopticon.events import Event, Kind
from panopticon.ingest import discover, load
from panopticon.sources import youtube_video_url
from panopticon.sources.chrome import parse_visit
from panopticon.sources.my_activity import parse_row
from panopticon.sources.youtube import post_text

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def watch(title: str, url: str = VIDEO, header: str = "YouTube", time: str = "2026-10-01T12:00:00Z",
          channel: str | None = "Some Channel") -> dict:
    row = {"header": header, "title": title, "titleUrl": url, "time": time,
           "products": ["YouTube"], "activityControls": ["YouTube watch history"]}
    if channel:
        row["subtitles"] = [{"name": channel, "url": "https://www.youtube.com/channel/UC1"}]
    return row


def test_video_urls_are_canonical():
    for url in (VIDEO, "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD",
                "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=30", "https://youtu.be/dQw4w9WgXcQ",
                "https://www.youtube.com/shorts/dQw4w9WgXcQ"):
        assert youtube_video_url(url) == VIDEO
    assert youtube_video_url("https://www.youtube.com/results?search_query=x") is None
    assert youtube_video_url("https://example.com/watch?v=dQw4w9WgXcQ") is None


def test_watch_rows():
    (item,) = parse_row(watch("Watched Never Gonna Give You Up"))
    assert (item.product, item.kind, item.text, item.url, item.source) == (
        "YouTube", Kind.WATCH, "Never Gonna Give You Up", VIDEO, "Some Channel")
    (music,) = parse_row(watch("Watched A Song", "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
                               header="YouTube Music"))
    assert (music.product, music.url) == ("YouTube Music", VIDEO)
    assert format_line(Event(1, *music), UTC) == "#1 12:00 W* A Song @youtube:Some Channel"


def test_viewed_in_youtube_is_a_post_not_a_place():
    (item,) = parse_row(watch("Viewed Happy birthday!", "https://www.youtube.com/post/Ugkx1",
                              channel=None))
    assert (item.kind, item.text) == (Kind.VISIT, "Happy birthday!")


def test_post_text_joins_fragments():
    assert post_text('{"text":"Hi "},{"text":"@someone"},{"text":" there"}') == "Hi @someone there"
    assert post_text("not json") == "not json"
    assert post_text('{"text":"One.\\n\\nTwo."}') == "One. Two."


def test_export_reads_history_and_comments_with_titles(tmp_path):
    root = tmp_path / "Takeout" / "YouTube and YouTube Music"
    (root / "history").mkdir(parents=True)
    (root / "comments").mkdir()
    (root / "subscriptions").mkdir()
    (root / "history" / "watch-history.json").write_text(json.dumps([watch("Watched Rick")]))
    (root / "history" / "search-history.json").write_text(json.dumps([
        {"header": "YouTube", "title": "Searched for rick astley",
         "titleUrl": "https://www.youtube.com/results?search_query=rick+astley",
         "time": "2026-10-01T11:59:00Z", "products": ["YouTube"]}]))
    (root / "comments" / "comments.csv").write_text(
        "Comment ID,Channel ID,Comment Create Timestamp,Price,Video ID,Comment Text\n"
        'c1,UC9,2026-10-01T12:05:00+00:00,0,dQw4w9WgXcQ,"{""text"":""Classic""}"\n')
    (root / "subscriptions" / "subscriptions.csv").write_text("Channel Id,Channel Url,Channel Title\n")
    events = load(discover([tmp_path])).events
    assert [(e.kind, e.text) for e in events] == [
        (Kind.SEARCH, "rick astley"), (Kind.WATCH, "Rick"), (Kind.COMMENT, "Classic (on Rick)")]


def test_chrome_copies_merge_into_youtube_and_engines_stay_apart(tmp_path):
    t = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    chrome_row = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=4", "title": "Rick - YouTube",
                  "time_usec": int(t.timestamp() * 1e6), "client_id": "c"}
    item = parse_visit(chrome_row, "desktop: d")
    assert (item.kind, item.url) == (Kind.WATCH, VIDEO)
    yt_search = parse_visit(chrome_row | {"url": "https://www.youtube.com/results?search_query=rick"},
                            None)
    assert (yt_search.kind, yt_search.text) == (Kind.SEARCH, "rick")

    root = tmp_path / "YouTube and YouTube Music" / "history"
    root.mkdir(parents=True)
    (root / "watch-history.json").write_text(json.dumps([watch("Watched Rick")]))
    (tmp_path / "Chrome").mkdir()
    (tmp_path / "Chrome" / "History.json").write_text(json.dumps({"Browser History": [
        chrome_row,
        chrome_row | {"url": "https://www.google.com/search?q=rick"},  # a Google search: kept
        chrome_row | {"url": "https://www.youtube.com/results?search_query=rick"},
    ]}))
    (tmp_path / "My Activity" / "Search").mkdir(parents=True)
    (tmp_path / "My Activity" / "Search" / "MyActivity.json").write_text(json.dumps([
        {"header": "Search", "title": "Searched for rick", "time": "2026-10-01T12:00:00Z",
         "products": ["Search"]}]))
    loaded = load(discover([tmp_path]))
    # The Chrome watch merges into YouTube's; the Chrome Google search into My Activity's; the
    # YouTube search in Chrome has no YouTube-export copy, and must not merge with Google's.
    assert loaded.duplicates == 2
    assert sorted((e.product, e.kind.value, e.text) for e in loaded.events) == [
        ("Chrome", "search", "rick"), ("Search", "search", "rick"), ("YouTube", "watch", "Rick")]
