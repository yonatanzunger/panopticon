"""Stage 7: the report. A single self-contained HTML file: no scripts, fonts, styles, or images
are loaded from anywhere, so opening it sends nothing off the machine.
"""

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, tzinfo
from html import escape
from zoneinfo import ZoneInfo

from panopticon.compress import format_line
from panopticon.events import Event
from panopticon.model import Claim, Trip

_STATUS = {
    # status: (label, icon, css class)
    "kept": ("Verified", "✓", "good"),
    "revised": ("Revised", "~", "warning"),
    "unconfirmed": ("Unconfirmed", "?", "neutral"),
    "unverified": ("Not verified", "–", "neutral"),
}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_SENSITIVITY_LABELS = {
    "health": "Health",
    "sexual_orientation_or_life": "Sexual orientation or sex life",
    "gender_identity": "Gender identity",
    "religion_or_belief": "Religion or belief",
    "political_opinion": "Political opinion",
    "racial_or_ethnic_origin": "Racial or ethnic origin",
    "union_membership": "Union membership",
    "financial": "Finances",
    "precise_location": "Precise location",
    "criminal_or_legal": "Criminal or legal",
    "children": "Children",
    "immigration_status": "Immigration status",
}


def _title(slug: str) -> str:
    return slug.replace("_", " ").capitalize()


def _day(ts: datetime, tz: tzinfo) -> str:
    return ts.astimezone(tz).strftime("%b %-d, %Y")


def _span(start: datetime, end: datetime, tz: tzinfo) -> str:
    a, b = start.astimezone(tz), end.astimezone(tz)
    if a.date() == b.date():
        return f"{a:%b %-d, %Y}"
    if a.year == b.year:
        return f"{a:%b %-d} – {b:%b %-d, %Y}"
    return f"{a:%b %-d, %Y} – {b:%b %-d, %Y}"


def _duration(start: datetime, end: datetime) -> str:
    hours = (end - start).total_seconds() / 3600
    if hours < 20:
        return f"{hours:.0f} hours"
    days = round(hours / 24)
    return f"{days} day{'s' if days != 1 else ''}"


CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --surface-2: #f0efec; --hairline: #e4e3df;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #75746f;
  --series-1: #2a78d6; --series-1-wash: rgba(42, 120, 214, 0.10);
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --neutral: #75746f;
  --good-ink: #006300; --warning-ink: #7a5400; --critical-ink: #a32424;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --surface-2: #262624; --hairline: #383835;
    --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #9a998f;
    --series-1: #3987e5; --series-1-wash: rgba(57, 135, 229, 0.14);
    --good-ink: #5fd35f; --warning-ink: #fab219; --critical-ink: #ff8a8a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --surface-2: #262624; --hairline: #383835;
  --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #9a998f;
  --series-1: #3987e5; --series-1-wash: rgba(57, 135, 229, 0.14);
  --good-ink: #5fd35f; --warning-ink: #fab219; --critical-ink: #ff8a8a;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
main { max-width: 960px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 28px; line-height: 1.2; margin: 0 0 4px; }
h2 { font-size: 20px; margin: 48px 0 12px; }
h3 { font-size: 16px; margin: 28px 0 8px; color: var(--ink-2); }
p { margin: 0 0 12px; }
.sub { color: var(--ink-2); margin-bottom: 24px; }
.card {
  background: var(--surface); border: 1px solid var(--hairline); border-radius: 10px;
  padding: 16px 18px;
}
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 600; }
.summary p { max-width: 70ch; }
.note { color: var(--ink-2); font-size: 13px; }
.bars { display: grid; grid-template-columns: minmax(120px, max-content) 1fr; gap: 6px 12px;
  align-items: center; }
.bars .name { color: var(--ink-2); font-size: 14px; }
.bars .track { display: flex; align-items: center; gap: 8px; min-width: 0; }
.bars .bar { height: 16px; background: var(--series-1); border-radius: 0 4px 4px 0;
  min-width: 3px; }
.bars .val { font-size: 13px; color: var(--ink); font-variant-numeric: tabular-nums; }
.timeline { width: 100%; overflow-x: auto; }
.timeline svg { display: block; width: 100%; min-width: 640px; height: auto; }
.timeline .axis { stroke: var(--hairline); stroke-width: 1; }
.timeline .tick { fill: var(--ink-3); font-size: 11px; }
.timeline .trip .bar { fill: var(--series-1); }
.timeline .trip .hit { fill: transparent; }
.timeline .trip text { fill: var(--ink); font-size: 12px; }
.timeline .trip:hover .bar { opacity: 0.8; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--hairline); }
th { color: var(--ink-2); font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; text-align: right; }
.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 8px 0 4px;
  font-size: 14px; color: var(--ink-2); }
.filters select, .filters input { font: inherit; }
.claim { margin: 10px 0; }
.claim.hidden { display: none; }
.claim .text { font-weight: 600; margin-bottom: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.chip { font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--hairline);
  color: var(--ink-2); background: var(--surface-2); white-space: nowrap; }
.status { font-weight: 600; background: transparent; }
.status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 5px; vertical-align: 1px; }
.status.good { color: var(--good-ink); } .status.good .dot { background: var(--good); }
.status.warning { color: var(--warning-ink); } .status.warning .dot { background: var(--warning); }
.status.critical { color: var(--critical-ink); } .status.critical .dot { background: var(--critical); }
.status.neutral .dot { background: var(--neutral); }
.claim .detail, .claim .meta { color: var(--ink-2); font-size: 14px; margin: 4px 0; }
.claim .meta b { color: var(--ink); font-weight: 600; }
.claim ul { margin: 4px 0; padding-left: 20px; color: var(--ink-2); font-size: 14px; }
details { margin-top: 8px; }
summary { cursor: pointer; color: var(--ink-2); font-size: 14px; }
pre.lines { margin: 8px 0 0; padding: 10px 12px; background: var(--surface-2); border-radius: 8px;
  font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap;
  overflow-wrap: anywhere; }
pre.lines .weak { color: var(--ink-3); }
footer { margin-top: 56px; color: var(--ink-2); font-size: 13px; }
@media (max-width: 560px) { h1 { font-size: 23px; } .bars { grid-template-columns: 1fr; } }
"""

SCRIPT = """
(() => {
  const claims = [...document.querySelectorAll('.claim')];
  const sens = document.getElementById('f-sens');
  const conf = document.getElementById('f-conf');
  const unconfirmed = document.getElementById('f-unconfirmed');
  const count = document.getElementById('f-count');
  const order = {high: 0, medium: 1, low: 2};
  function apply() {
    let shown = 0;
    for (const c of claims) {
      const okSens = !sens.value || (c.dataset.sens || '').split(' ').includes(sens.value)
        || (sens.value === '_any' && c.dataset.sens);
      const okConf = order[c.dataset.conf] <= order[conf.value];
      const okUnconfirmed = unconfirmed.checked || c.dataset.status !== 'unconfirmed';
      const show = okSens && okConf && okUnconfirmed;
      c.classList.toggle('hidden', !show);
      if (show) shown++;
    }
    count.textContent = shown + ' shown';
    for (const g of document.querySelectorAll('.group')) {
      g.hidden = !g.querySelector('.claim:not(.hidden)');
    }
  }
  for (const el of [sens, conf, unconfirmed]) el.addEventListener('change', apply);
  apply();
})();
"""


def _timeline_svg(trips: list[Trip], start: datetime, end: datetime, tz: tzinfo) -> str:
    """Trips as bars on one time axis, labels staggered into rows so they never overlap."""
    width, pad, row_h, axis_h = 920, 12, 34, 28
    span = max((end - start).total_seconds(), 1.0)

    def x(ts: datetime) -> float:
        return pad + (ts - start).total_seconds() / span * (width - 2 * pad)

    # (trip, bar start, bar end, label start, row). A label starts at its bar unless that
    # would run it off the right edge, in which case it ends at the edge instead.
    placed: list[tuple[Trip, float, float, float, int]] = []
    row_ends: list[float] = []
    for trip in trips:
        x0, x1 = x(trip.start_dt), max(x(trip.end_dt), x(trip.start_dt) + 3)
        label_w = 7 * len(trip.place)
        lx = min(x0, width - pad - label_w)
        left, right = min(x0, lx), max(x1, lx + label_w + 8)
        row = next((r for r, last in enumerate(row_ends) if last < left), len(row_ends))
        if row == len(row_ends):
            row_ends.append(right)
        else:
            row_ends[row] = right
        placed.append((trip, x0, x1, lx, row))
    rows = max(1, len(row_ends))
    height = rows * row_h + axis_h + 8

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Timeline of {len(trips)} trips away from home">']
    axis_y = rows * row_h + 6
    parts.append(f'<line class="axis" x1="{pad}" x2="{width - pad}" y1="{axis_y}" y2="{axis_y}"/>')
    # Month ticks.
    local = start.astimezone(tz)
    month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while month <= end.astimezone(tz):
        if month >= local:
            mx = x(month)
            parts.append(f'<line class="axis" x1="{mx:.1f}" x2="{mx:.1f}" y1="{axis_y}" '
                         f'y2="{axis_y + 5}"/>')
            parts.append(f'<text class="tick" x="{mx:.1f}" y="{axis_y + 19}" '
                         f'text-anchor="middle">{month:%b %Y}</text>')
        month = month.replace(year=month.year + month.month // 12, month=month.month % 12 + 1)
    for trip, x0, x1, lx, row in placed:
        y = row * row_h + 18
        tip = (f"{trip.place}: {_span(trip.start_dt, trip.end_dt, trip.zone)} "
               f"({_duration(trip.start_dt, trip.end_dt)})")
        parts.append(
            f'<g class="trip"><title>{escape(tip)}</title>'
            f'<rect class="bar" x="{x0:.1f}" y="{y}" width="{x1 - x0:.1f}" height="10" rx="3"/>'
            f'<text x="{lx:.1f}" y="{y - 5}">{escape(trip.place)}</text>'
            f'<rect class="hit" x="{min(x0, lx) - 4:.1f}" y="{y - 18}" '
            f'width="{max(x1, lx + 7 * len(trip.place)) - min(x0, lx) + 8:.1f}" '
            f'height="32"/></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _claim_card(
    claim: Claim, events: dict[int, Event], tz_at: Callable[[datetime], tzinfo], anchor: str
) -> str:
    status = claim.status
    label, icon, cls = _STATUS[status]
    verification = claim.verification or {}
    confidence = claim.final_confidence
    about = claim.final_about
    sensitivity = claim.final_sensitivity or []
    text = claim.final_claim

    chips = [f'<span class="chip status {cls}" title="{escape(label)}">'
             f'<span class="dot"></span><span aria-hidden="true">{icon}</span> {escape(label)}</span>',
             f'<span class="chip">{escape(confidence)} confidence</span>']
    if about != "self":
        chips.append(f'<span class="chip">about {escape(about.replace("_", " "))}</span>')
    for s in sensitivity:
        chips.append(f'<span class="chip">{escape(_SENSITIVITY_LABELS.get(s, s))}</span>')

    body = [f'<div class="text">{escape(text)}</div>', f'<div class="chips">{"".join(chips)}</div>',
            f'<p class="detail">{escape(claim.detail)}</p>']
    if text != claim.claim:
        body.append(f'<p class="meta"><b>Originally claimed</b> ({escape(claim.confidence)} '
                    f'confidence): {escape(claim.claim)}</p>')
    if claim.alternatives:
        body.append('<p class="meta"><b>Other explanations</b></p><ul>'
                    + "".join(f"<li>{escape(a)}</li>" for a in claim.alternatives) + "</ul>")
    if verification.get("issues"):
        body.append('<p class="meta"><b>Verifier’s concerns</b></p><ul>'
                    + "".join(f"<li>{escape(i)}</li>" for i in verification["issues"]) + "</ul>")
    when = ""
    if claim.first_seen:
        when = claim.first_seen
        if claim.last_seen and claim.last_seen != claim.first_seen:
            when += f" to {claim.last_seen}"
    cited = sorted((events[i] for i in claim.event_ids if i in events), key=lambda e: e.ts)
    supporting = set(verification.get("supporting_event_ids") or [e.id for e in cited])
    lines = "\n".join(
        f'<span class="{"" if e.id in supporting else "weak"}">'
        f"{escape(format_line(e, tz_at(e.ts), with_date=True))}</span>"
        for e in cited
    )
    body.append(
        f"<details><summary>Source lines ({len(cited)}{', ' + escape(when) if when else ''})"
        f"</summary><pre class=\"lines\">{lines}</pre>"
        + ('<p class="note">Dimmed lines were cited but judged not to bear on the claim.</p>'
           if len(supporting) < len(cited) else "")
        + "</details>"
    )
    return (
        f'<div class="card claim" id="{anchor}" data-status="{status}" '
        f'data-conf="{escape(confidence)}" data-sens="{escape(" ".join(sensitivity))}">'
        + "".join(body) + "</div>"
    )


def render(
    summary: str,
    claims: list[Claim],
    trips: list[Trip],
    events: list[Event],
    tz_at: Callable[[datetime], tzinfo],
    home_tz: str,
    api_usage: list[str],
    summary_is_final: bool = False,
) -> str:
    """summary_is_final: the summary was written from the verified claims (by `summarize`),
    rather than being synthesis's pre-verification draft."""
    tz = ZoneInfo(home_tz)
    by_id = {e.id: e for e in events}
    start, end = (events[0].ts, events[-1].ts) if events else (None, None)
    live = [c for c in claims if c.status != "unconfirmed"]
    statuses = Counter(c.status for c in claims)
    sensitive = [c for c in live if c.final_sensitivity]
    by_sensitivity = Counter(s for c in live for s in (c.final_sensitivity or []))
    days = len({e.ts.astimezone(tz_at(e.ts)).date() for e in events})

    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>Search History Report</title>',
           f"<style>{CSS}</style></head><body><main>"]
    out.append("<h1>What your Google activity reveals</h1>")
    if start and end:
        out.append(f'<p class="sub">{_span(start, end, tz)} · {len(events):,} recorded '
                   f"events on {days} days</p>")

    tiles = [("Events", f"{len(events):,}"), ("Trips away", str(len(trips))),
             ("Claims", str(len(live))), ("Sensitive claims", str(len(sensitive))),
             ("High confidence", str(sum(1 for c in live if c.final_confidence == "high")))]
    out.append('<div class="tiles">' + "".join(
        f'<div class="card tile"><div class="label">{escape(k)}</div>'
        f'<div class="value">{escape(v)}</div></div>' for k, v in tiles) + "</div>")

    out.append("<h2>Summary</h2>")
    out.append('<div class="card summary">'
               + "".join(f"<p>{escape(p)}</p>" for p in summary.split("\n") if p.strip()))
    changed = statuses["revised"] + statuses["unconfirmed"]
    if changed and not summary_is_final:
        out.append(f'<p class="note">This is a draft written before verification, which '
                   f'revised {statuses["revised"]} claims and left {statuses["unconfirmed"]} '
                   f"unconfirmed; where they disagree, trust the claims below. Run "
                   f"<code>summarize</code> for a summary written from the verified claims.</p>")
    out.append("</div>")

    out.append("<h2>Sensitive inferences</h2>")
    if by_sensitivity:
        out.append('<p class="sub">Claims touching each kind of sensitive information. Most '
                   "privacy law treats these as special categories.</p>")
        top = max(by_sensitivity.values())
        out.append('<div class="card"><div class="bars">' + "".join(
            f'<div class="name">{escape(_SENSITIVITY_LABELS.get(s, s))}</div>'
            f'<div class="track" title="{escape(_SENSITIVITY_LABELS.get(s, s))}: {n} claims">'
            f'<div class="bar" style="width:{max(n / top * 85, 1):.1f}%"></div>'
            f'<span class="val">{n}</span></div>'
            for s, n in by_sensitivity.most_common()) + "</div></div>")
    else:
        out.append('<p class="sub">No claims touch sensitive categories.</p>')

    out.append("<h2>Where you were</h2>")
    if trips and start and end:
        out.append('<p class="sub">Trips away from home, reconstructed from searches, visits, '
                   "and the locations Google recorded. Hover a trip for its dates.</p>")
        out.append(f'<div class="card timeline">{_timeline_svg(trips, start, end, tz)}</div>')
        rows = []
        for t in trips:
            rows.append(f"<tr><td>{escape(_span(t.start_dt, t.end_dt, t.zone))}</td>"
                        f"<td>{escape(t.place)}</td>"
                        f"<td>{escape(_duration(t.start_dt, t.end_dt))}</td>"
                        f'<td class="num">{len(t.event_ids)}</td></tr>')
        out.append('<div class="card" style="margin-top:12px"><table><thead><tr><th>Dates</th>'
                   '<th>Place</th><th>Length</th><th style="text-align:right">Evidence lines</th>'
                   "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    else:
        out.append('<p class="sub">No trips away from home were found.</p>')

    out.append("<h2>Everything it suggests</h2>")
    sens_options = "".join(
        f'<option value="{escape(s)}">{escape(_SENSITIVITY_LABELS.get(s, s))}</option>'
        for s, _ in by_sensitivity.most_common()
    )
    out.append(
        '<div class="filters">'
        '<label>Show <select id="f-sens"><option value="">all claims</option>'
        f'<option value="_any">any sensitive</option>{sens_options}</select></label>'
        '<label>at <select id="f-conf"><option value="low">any confidence</option>'
        '<option value="medium">medium or high</option><option value="high">high only'
        "</option></select></label>"
        '<label><input type="checkbox" id="f-unconfirmed" checked> include unconfirmed claims'
        "</label>"
        '<span id="f-count"></span></div>'
    )
    groups: dict[str, list[Claim]] = {}
    for c in claims:
        groups.setdefault(c.category, []).append(c)
    n = 0
    for category, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.append(f'<section class="group"><h3>{escape(_title(category))}</h3>')
        items.sort(key=lambda c: (c.status == "unconfirmed",
                                  _CONFIDENCE_ORDER[c.final_confidence]))
        for c in items:
            out.append(_claim_card(c, by_id, tz_at, f"claim-{n}"))
            n += 1
        out.append("</section>")

    out.append("<footer><p><b>How this was made.</b> Panopticon read your Google Takeout "
               "“My Activity” data, compressed it into a transcript, and used Claude to "
               "extract evidence, reconstruct trips, synthesize these claims, and then check "
               "each claim against only the source lines it cites. Claims are inferences, not "
               "facts: each shows its confidence, other explanations, and the exact lines it "
               "rests on.</p>")
    if api_usage:
        out.append("<p>Sent to the Claude API: " + "; ".join(escape(u) for u in api_usage)
                   + ". This report itself loads nothing from the network.</p>")
    out.append("</footer>")
    out.append(f"</main><script>{SCRIPT}</script></body></html>")
    return "".join(out)


def api_usage(log_paths: dict[str, object]) -> list[str]:
    """Describe what each API stage sent, from the logs the stages wrote."""
    out: list[str] = []
    extract_log = log_paths.get("extract")
    if isinstance(extract_log, list) and extract_log:
        tokens = sum(r["usage"]["input_tokens"] for r in extract_log)
        out.append(f"the transcript, in {len(extract_log)} chunks ({tokens:,} input tokens), "
                   "for extraction")
    profile = log_paths.get("synthesize")
    if isinstance(profile, dict) and profile.get("usage"):
        out.append(f"extracted facts and activity statistics "
                   f"({profile['usage']['input_tokens']:,} input tokens) for synthesis")
    verified = log_paths.get("verify")
    if isinstance(verified, dict) and verified.get("usage"):
        out.append(f"each claim with its cited lines "
                   f"({verified['usage']['input_tokens']:,} input tokens) for verification")
    summary = log_paths.get("summarize")
    if isinstance(summary, dict) and summary.get("usage"):
        out.append(f"the verified claims ({summary['usage']['input_tokens']:,} input tokens) "
                   "for the summary")
    return out


def load_json(path) -> object | None:
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else None
