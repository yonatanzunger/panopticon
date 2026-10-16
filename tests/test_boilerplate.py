import argparse

from panopticon import cli, prepare
from panopticon.boilerplate import Boilerplate

SUBJECTS = ["Many thoughts", "Podcast", "OMG!!!", "Returning assets", "Lunch?", "Re: plans",
            "Your order shipped", "Invoice 12", "Hike!", "Book cover"]


def gmail(subject: str, account: str = "me@gmail.com") -> str:
    return f"{subject} - {account} - Gmail"


def test_single_account_suffix_is_stripped():
    b = Boilerplate({"mail.google.com": [gmail(s) for s in SUBJECTS] + ["Inbox - me@gmail.com - Gmail"]})
    assert b.strip("mail.google.com", gmail("Many thoughts")) == "Many thoughts"
    assert b.strip("mail.google.com", "Inbox - me@gmail.com - Gmail") == "Inbox"


def test_multiple_accounts_are_kept():
    titles = [gmail(s) for s in SUBJECTS] + [gmail(s, "me@work.com") for s in SUBJECTS[:5]]
    b = Boilerplate({"mail.google.com": titles})
    # "Gmail" is boilerplate; which account each page was in is signal.
    assert b.strip("mail.google.com", gmail("Podcast")) == "Podcast - me@gmail.com"
    assert b.strip("mail.google.com", gmail("Hike!", "me@work.com")) == "Hike! - me@work.com"


def test_competing_values_are_kept():
    titles = [f"Doc {i} - Google Docs" for i in range(8)] + [f"Sheet {i} - Google Sheets"
                                                             for i in range(4)]
    b = Boilerplate({"docs.google.com": titles})
    assert b.strip("docs.google.com", "Doc 1 - Google Docs") == "Doc 1 - Google Docs"


def test_prefix_truncation_and_original_separators():
    titles = [f"Google Calendar - Week of July {d}, 2026" for d in range(1, 8)]
    b = Boilerplate({"calendar.google.com": titles,
                     "news.example": [f"Story {i} | Part {i} - The Daily Example" for i in range(6)]
                     + ["Long story cut - The Daily ...", "Another - The ..."]})
    assert b.strip("calendar.google.com", titles[0]) == "Week of July 1, 2026"
    assert b.strip("news.example", "Story 1 | Part 1 - The Daily Example") == "Story 1 | Part 1"
    assert b.strip("news.example", "Long story cut - The Daily ...") == "Long story cut"


def test_never_strips_to_nothing_and_ignores_small_or_unknown_sites():
    b = Boilerplate({"x.example": [f"Post {i} - X" for i in range(6)] + ["X"],
                     "tiny.example": ["A - Tiny", "B - Tiny"]})
    assert b.strip("x.example", "X") == "X"
    assert b.strip("tiny.example", "A - Tiny") == "A - Tiny"
    assert b.strip("other.example", "A - Other") == "A - Other"
    assert b.strip(None, "A - X") == "A - X"


def test_a_few_titles_on_one_subject_are_not_boilerplate():
    # After "Wikipedia", 4 of 5 three-segment titles share a segment, but they're 4 of 30.
    titles = [f"Article {i} - Wikipedia" for i in range(25)]
    titles += [f"Part {i} - Big Topic - Wikipedia" for i in range(4)] + ["Q - Other - Wikipedia"]
    b = Boilerplate({"wikipedia.org": titles})
    assert b.strip("wikipedia.org", "Part 1 - Big Topic - Wikipedia") == "Part 1 - Big Topic"


def flags(directory, **kw) -> argparse.Namespace:
    base = {"dir": directory, "input": None, "sources": None, "skip_sources": None,
            "all_sources": False, "since": None, "until": None, "days": None,
            "all_history": False}
    return argparse.Namespace(**(base | kw))


def test_flag_groups_replace_only_their_own_settings(tmp_path):
    cli._update_settings(flags(tmp_path, input=[tmp_path], days=120))
    s = cli._update_settings(flags(tmp_path, skip_sources=["chrome"]))
    assert (s.days, s.sources) == (120, ["my_activity"])
    s = cli._update_settings(flags(tmp_path, days=30))
    assert (s.days, s.sources, s.inputs) == (30, ["my_activity"], [tmp_path.resolve()])
    s = cli._update_settings(flags(tmp_path, all_sources=True, all_history=True))
    assert (s.days, s.sources) == (None, None)
    assert prepare.load_settings(tmp_path) == s
