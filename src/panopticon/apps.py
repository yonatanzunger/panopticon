"""Short, canonical app names, keyed by Android package id.

App-use records name an app by its Play Store listing title, which is usually the name plus a
tagline: "DoorDash: Food, Grocery, More", "Discord - Talk, Play, Hang Out". Records that link
to the app's Play Store page (Play Store, Ads, Google Analytics) carry its package id, so this
gathers each package's titles, cuts the tagline off, and renames every app-use record of that
app, including ones elsewhere that use the full title without a link. A short name that two
packages would share stays long, so names remain distinct.
"""

import re
from collections import Counter, defaultdict
from urllib.parse import parse_qs, urlparse

from panopticon.events import Kind
from panopticon.sources import Item

# Where a listing title's tagline starts: "Name: tagline", "Name - tagline", "Name- tagline".
_TAGLINE = re.compile(r": | ?[-–—|] ")
MIN_NAME = 3


def package(url: str | None) -> str | None:
    """The package id in a Play Store app URL."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.hostname != "play.google.com" or not parsed.path.endswith("/details"):
        return None
    return parse_qs(parsed.query).get("id", [None])[0]


def short_name(title: str) -> str:
    head = _TAGLINE.split(title, maxsplit=1)[0].strip()
    return head if len(head) >= MIN_NAME else title


def canonical_names(items: list[Item]) -> dict[str, str]:
    """Package id → short name, from the app-use records that link to the Play Store."""
    titles: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        if item.kind == Kind.APP_USE and (pkg := package(item.url)):
            titles[pkg][item.text] += 1
    full = {pkg: names.most_common(1)[0][0] for pkg, names in titles.items()}
    short = {pkg: short_name(title) for pkg, title in full.items()}
    clashes = Counter(short.values())
    return {pkg: name if clashes[name] == 1 else full[pkg] for pkg, name in short.items()}


def rename(items: list[Item]) -> list[Item]:
    """Give every app-use record its app's canonical name."""
    names = canonical_names(items)
    by_title: dict[str, str] = {}
    for item in items:
        if item.kind == Kind.APP_USE and (pkg := package(item.url)) in names:
            by_title[item.text] = names[pkg]
    out = []
    for item in items:
        if item.kind == Kind.APP_USE:
            pkg = package(item.url)
            name = names.get(pkg) if pkg else by_title.get(item.text)
            if name and name != item.text:
                item = item._replace(text=name, source=name if item.source else None)
        out.append(item)
    return out
