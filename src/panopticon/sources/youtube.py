"""Google Takeout "YouTube and YouTube Music": watch and search history, comments, live chats.

history/watch-history.json and history/search-history.json are My Activity rows (the same
format as My Activity/YouTube/MyActivity.json) and are parsed as such: watches carry the
channel as their source, and "Viewed <text>" is a community post. comments/*.csv and
live chats/*.csv are what the user wrote; each row names only a video id, so the video's title
comes from the watch history beside it when that has the video.

Not read: subscriptions (no dates), playlists, and the user's own uploads ("video metadata").
"""

import functools
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from panopticon.events import Kind
from panopticon.sources import Item, Source, my_activity, read_csv, read_json, youtube_video_url

_HISTORY = {"watch-history.json", "search-history.json"}
# Folder → (timestamp column, text column).
_POSTS = {
    "comments": ("Comment Create Timestamp", "Comment Text"),
    "live chats": ("Live Chat Create Timestamp", "Live Chat Text"),
}


def _export_root(path: Path) -> Path:
    return path.parent.parent


def claims(path: Path) -> bool:
    if not _export_root(path).name.startswith("YouTube"):
        return False
    if path.parent.name == "history":
        return path.name in _HISTORY
    return path.parent.name in _POSTS and path.suffix == ".csv"


@functools.cache
def video_titles(root: Path) -> dict[str, str]:
    """Video URL → title, from the watch history in this export."""
    path = root / "history" / "watch-history.json"
    if not path.exists():
        return {}
    titles: dict[str, str] = {}
    for row in read_json(path):
        url = youtube_video_url(row.get("titleUrl"))
        title = row.get("title", "").removeprefix("Watched ")
        if url and not title.startswith(("http://", "https://")):
            titles[url] = title
    return titles


def post_text(raw: str) -> str:
    """Takeout stores a post as JSON fragments, '{"text":"..."},{"text":"..."}', one per run
    of text or mention; join their text."""
    try:
        fragments = json.loads(f"[{raw}]")
    except json.JSONDecodeError:
        return " ".join(raw.split())
    text = "".join(f.get("text", "") for f in fragments if isinstance(f, dict))
    # One transcript line per event, so paragraphs become spaces.
    return " ".join(text.split())


def _posts(path: Path) -> Iterator[Item]:
    ts_column, text_column = _POSTS[path.parent.name]
    titles = video_titles(_export_root(path))
    for row in read_csv(path):
        text = post_text(row.get(text_column, ""))
        if not text or not row.get(ts_column):
            continue
        video = row.get("Video ID")
        url = f"https://www.youtube.com/watch?v={video}" if video else None
        if url and (title := titles.get(url)):
            text = f"{text} (on {title})"
        ts = datetime.fromisoformat(row[ts_column])
        yield Item(ts, "YouTube", Kind.COMMENT, text, url, None, None, None, None, None)


def parse(path: Path) -> Iterator[Item]:
    if path.parent.name == "history":
        for row in read_json(path):
            yield from my_activity.parse_row(row)
    else:
        yield from _posts(path)


SOURCE = Source("youtube", claims, parse)
