from datetime import UTC, datetime

from panopticon.apps import canonical_names, package, rename, short_name
from panopticon.events import Kind
from panopticon.sources import Item

T = datetime(2026, 10, 1, tzinfo=UTC)


def use(name: str, pkg: str | None, product: str = "Google Play Store") -> Item:
    url = f"https://play.google.com/store/apps/details?id={pkg}" if pkg else None
    return Item(T, product, Kind.APP_USE, name, url, name, None, None, None, None)


def test_package():
    assert package("https://play.google.com/store/apps/details?id=com.dd.doordash") == "com.dd.doordash"
    assert package("https://example.com/details?id=x") is None
    assert package(None) is None


def test_short_name():
    assert short_name("DoorDash: Food, Grocery, More") == "DoorDash"
    assert short_name("Discord - Talk, Play, Hang Out") == "Discord"
    assert short_name("Gett- Corporate Ground Travel") == "Gett"
    assert short_name("mconnect Player Lite – Cast AV") == "mconnect Player Lite"
    assert short_name("Signal Private Messenger") == "Signal Private Messenger"
    assert short_name("X: the everything app") == "X: the everything app"  # too short to cut


def test_clashing_short_names_stay_long():
    names = canonical_names([use("Notes: Simple", "a.notes"), use("Notes - Pro", "b.notes"),
                             use("Yelp: Food", "com.yelp")])
    assert names == {"a.notes": "Notes: Simple", "b.notes": "Notes - Pro", "com.yelp": "Yelp"}


def test_rename_reaches_unlinked_records_and_leaves_others():
    items = rename([use("DoorDash: Food, Grocery, More", "com.dd.doordash"),
                    use("DoorDash: Food, Grocery, More", None, "Ads"),
                    use("Digital Wellbeing", None, "Android")])
    assert [(i.text, i.source) for i in items] == [
        ("DoorDash", "DoorDash"), ("DoorDash", "DoorDash"), ("Digital Wellbeing", "Digital Wellbeing")]
