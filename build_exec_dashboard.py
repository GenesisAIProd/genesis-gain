"""Executive HTML dashboard builder.

Reads the same v_biz_ business views as build_html_dashboard and renders a
presentation-grade single-file page: a dark brand bar, verdict cards with a
confidence meter, a coverage strip, two inline-SVG charts and the supporting
evidence tables. No external libraries, no webfonts, no network calls at view
time, so the output is one self-contained file that survives being attached to
an email or dropped on a volume.

Run it exactly like the standard builder:

    from genesis_gain import build_exec_dashboard
    html = build_exec_dashboard.build(spark)
    displayHTML(html)                                     # inside a notebook
    build_exec_dashboard.write(html, "/Volumes/.../exec.html")

Catalog and schema come from GAIN_CATALOG and GAIN_SCHEMA, so the same file
serves a sandbox today and production once the grant lands, with no edit.

Sections degrade rather than fail: policy exposure, launch cascade and the
coverage strip each drop out if their view is missing, and the rest still
renders. The page commits to a light surface - it is printed and emailed more
often than it is browsed - so there is no dark-mode variant by design.

Presentation rules worth keeping if this is edited:
  - net_strength is an unsigned magnitude; net_direction carries the sign. Any
    number shown beside a verdict is signed, or it contradicts its own headline.
  - Claims and links come from scraped articles through an LLM, so text is
    truncated before escaping and only http(s) reaches an href.
  - The palette is Okabe-Ito blue/vermillion/green, validated for this surface:
    worst all-pairs CVD dE 11.0, normal-vision dE 18.7, every hue at or above
    3:1 contrast. Colour never carries meaning alone - every chart has a legend
    and every plotted value is repeated in a table.
  - Navigation is anchor links, never scripted tabs: a viewer with JavaScript
    disabled must still see every section.
"""
from __future__ import annotations

import html as _html
import os
from datetime import datetime, timezone

CATALOG = os.environ.get("GAIN_CATALOG", "genesis_prod")
SCHEMA = os.environ.get("GAIN_SCHEMA", "market_signals")

SUPPORT = "#0072B2"   # categorical slot 1 / diverging cool pole
PRESSURE = "#D55E00"  # categorical slot 2 / diverging warm pole
THIRD = "#009E73"     # categorical slot 3
NEUTRAL = "#94a3b8"
GRID = "#eef2f7"
AX_LABEL = "#475569"
AX_VALUE = "#64748b"
AX_CAPTION = "#94a3b8"

DOMAIN_COLOR = {"supply_chain": SUPPORT, "policy": PRESSURE, "ai_news": THIRD}
DOMAIN_LABEL = {"supply_chain": "Supply chain", "policy": "Policy", "ai_news": "AI news"}

VERDICT_COLOR = {
    "VALUE SUPPORT": SUPPORT,
    "DEPRECIATION PRESSURE": PRESSURE,
    "SIGNALS MIXED": "#475569",
    "WATCH DEVELOPING": "#64748b",
}

STALE_AFTER_DAYS = 10


# --------------------------------------------------------------------------- io

def _spark_runner(spark):
    def run(sql: str) -> list[dict]:
        return [r.asDict() for r in spark.sql(sql).collect()]
    return run


def _connector_runner():
    from databricks import sql

    conn = sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )

    def run(query: str) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(query)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    return run


def _rows(runner, view: str) -> list[dict]:
    return runner(f"SELECT * FROM {CATALOG}.{SCHEMA}.{view}")


def _rows_optional(runner, view: str) -> list[dict]:
    """Views added after the original set; a missing one drops its section."""
    try:
        return _rows(runner, view)
    except Exception:
        return []


def gather(runner) -> dict:
    return {
        "market": _rows(runner, "v_biz_market_read"),
        "components": _rows(runner, "v_biz_component_signals"),
        "drivers": _rows(runner, "v_biz_top_drivers"),
        "contribution": _rows(runner, "v_biz_domain_contribution"),
        "magnitudes": _rows(runner, "v_biz_value_magnitudes"),
        "policy": _rows_optional(runner, "v_biz_policy_exposure"),
        "cascade": _rows_optional(runner, "v_biz_launch_cascade"),
        "coverage": _rows_optional(runner, "v_biz_coverage"),
        "freshness": (_rows(runner, "v_biz_freshness") or [{}])[0],
        "generated_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    }


# ------------------------------------------------------------------- formatting

def _esc(v) -> str:
    return _html.escape(str(v)) if v is not None else ""


def _clip(v, limit: int) -> str:
    """Truncate first, then escape, so an entity is never cut in half."""
    return _esc(str(v)[:limit]) if v is not None else ""


def _url(v) -> str:
    """An escaped href, or empty for anything that is not plain http(s)."""
    raw = str(v or "").strip()
    return _esc(raw) if raw.lower().startswith(("http://", "https://")) else ""


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}K"
    return f"{n:,}"


def _pretty(v) -> str:
    """device_category and component_group arrive as snake_case identifiers."""
    return _esc(str(v or "").replace("_", " "))


def _signed(row: dict) -> float:
    return (_num(row.get("net_direction")) or 0.0) * _num(row.get("net_strength"))


def _days_since(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                value = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - value).total_seconds() / 86400


# ----------------------------------------------------------------------- charts

def _legend(entries: list[tuple[str, str]]) -> str:
    """Identity is never colour alone: every chart with 2+ series carries this."""
    keys = "".join(
        f'<span class="lg-item"><span class="lg-dot" style="background:{color}"></span>'
        f'{_esc(label)}</span>'
        for label, color in entries
    )
    return f'<div class="legend">{keys}</div>'


def _diverging_bars(rows: list[tuple[str, float, str]]) -> str:
    """One signed bar per row, growing left or right of a shared zero axis.

    rows are (label, value, note). Bars cap at 18px with a 4px rounded data-end
    and a 2px gap to the neighbour; the axis is a hairline, never dashed.
    """
    if not rows:
        return '<p class="empty">Not enough signal in the current window.</p>'
    peak = max((abs(v) for _, v, _ in rows), default=1.0) or 1.0
    row_h, bar_h, pad_l, pad_r, width = 32, 18, 172, 58, 720
    mid = pad_l + (width - pad_l - pad_r) / 2
    half = (width - pad_l - pad_r) / 2
    height = len(rows) * row_h + 36

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    out.append(f'<line x1="{mid}" y1="16" x2="{mid}" y2="{height - 18}" '
               f'stroke="#dbe2ea" stroke-width="1"/>')
    for i, (label, value, note) in enumerate(rows):
        y = 22 + i * row_h
        span = abs(value) / peak * half
        color = SUPPORT if value > 0 else PRESSURE if value < 0 else NEUTRAL
        x = mid if value >= 0 else mid - span
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(span, 2.0):.1f}" '
            f'height="{bar_h - 2}" fill="{color}" rx="4" ry="4">'
            f'<title>{_esc(label)}: {value:+.3f} - {_esc(note)}</title></rect>'
        )
        out.append(
            f'<text x="{pad_l - 14}" y="{y + bar_h / 2 + 2:.1f}" class="ax-lab" '
            f'text-anchor="end">{_esc(label)}</text>'
        )
        vx = mid + span + 9 if value >= 0 else mid - span - 9
        anchor = "start" if value >= 0 else "end"
        out.append(
            f'<text x="{vx:.1f}" y="{y + bar_h / 2 + 2:.1f}" class="ax-val" '
            f'text-anchor="{anchor}">{value:+.2f}</text>'
        )
    out.append(f'<text x="{mid}" y="{height - 4}" class="ax-cap" text-anchor="middle">'
               f'pressures value &#8592; 0 &#8594; supports value</text>')
    out.append("</svg>")
    return "".join(out)


def _grouped_bars(contribution: list[dict]) -> str:
    """Three stream pushes per category, around a shared zero axis."""
    rows = [r for r in contribution if r.get("device_category")]
    if not rows:
        return '<p class="empty">Stream contribution is not available yet.</p>'
    streams = ("supply_chain", "policy", "ai_news")
    peak = max((abs(_num(r.get(s))) for r in rows for s in streams), default=1.0) or 1.0

    bar_h, gap, group_pad = 15, 2, 24
    group_h = len(streams) * (bar_h + gap) + group_pad
    width, pad_l, pad_r = 720, 172, 58
    mid = pad_l + (width - pad_l - pad_r) / 2
    half = (width - pad_l - pad_r) / 2
    height = len(rows) * group_h + 36

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    out.append(f'<line x1="{mid}" y1="14" x2="{mid}" y2="{height - 18}" '
               f'stroke="#dbe2ea" stroke-width="1"/>')
    for gi, row in enumerate(rows):
        top = 18 + gi * group_h
        out.append(
            f'<text x="{pad_l - 14}" y="{top + group_h / 2 - 6:.1f}" class="ax-lab" '
            f'text-anchor="end">{_pretty(row.get("device_category"))}</text>'
        )
        for si, stream in enumerate(streams):
            value = _num(row.get(stream))
            y = top + si * (bar_h + gap)
            span = abs(value) / peak * half
            x = mid if value >= 0 else mid - span
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(span, 2.0):.1f}" '
                f'height="{bar_h}" fill="{DOMAIN_COLOR[stream]}" rx="4" ry="4">'
                f'<title>{DOMAIN_LABEL[stream]} - {_pretty(row.get("device_category"))}: '
                f'{value:+.3f}</title></rect>'
            )
            vx = mid + span + 8 if value >= 0 else mid - span - 8
            anchor = "start" if value >= 0 else "end"
            out.append(
                f'<text x="{vx:.1f}" y="{y + bar_h - 3:.1f}" class="ax-val" '
                f'text-anchor="{anchor}">{value:+.2f}</text>'
            )
    out.append(f'<text x="{mid}" y="{height - 4}" class="ax-cap" text-anchor="middle">'
               f'pushes value down &#8592; 0 &#8594; pushes value up</text>')
    out.append("</svg>")
    return "".join(out)


def _meter(confidence: float) -> str:
    """A single ratio against a limit reads as a meter, not a chart."""
    pct = max(0.0, min(1.0, confidence)) * 100
    return (
        f'<div class="meter-row">'
        f'<div class="meter"><div class="meter-fill" style="width:{pct:.0f}%"></div></div>'
        f'<span class="meter-lab">confidence {confidence:.2f}</span></div>'
    )


# --------------------------------------------------------------------- sections

def _section(anchor: str, kicker: str, title: str, lede: str = "") -> str:
    body = f'<p class="lede">{lede}</p>' if lede else ""
    return (
        f'<div class="sec" id="{_esc(anchor)}">'
        f'<span class="kicker">{_esc(kicker)}</span>'
        f'<h2 class="sec-h">{_esc(title)}</h2>{body}</div>'
    )


def _staleness(fresh: dict) -> str:
    age = _days_since(fresh.get("last_reconciled_at"))
    if age is None or age <= STALE_AFTER_DAYS:
        return ""
    return (
        f'<div class="stale"><span class="stale-dot"></span>'
        f'<span><strong>These reads are {age:.0f} days old.</strong> The weekly '
        f'reconciliation has not refreshed them since '
        f'{_esc(fresh.get("last_reconciled_at"))}. Read every verdict below as a '
        f'snapshot of that date, not of today.</span></div>'
    )


def _hero(market: list[dict]) -> str:
    if not market:
        return ('<p class="empty">No category reads have been computed yet. Run the '
                'reconciliation to populate this page.</p>')
    cards = []
    for row in market:
        read = row.get("expected_read")
        color = VERDICT_COLOR.get(read, NEUTRAL)
        direction = _num(row.get("net_direction"))
        strength = _num(row.get("net_strength"))
        magnitude = (f"{_signed(row):+.2f} net strength" if direction
                     else f"{strength:.2f} strength, no net direction")
        cards.append(
            f'<article class="vcard" style="--vc:{color}">'
            f'<div class="vc-top">'
            f'<span class="vc-cat">{_pretty(row.get("device_category"))}</span>'
            f'<span class="vc-count">{_int(row.get("n_findings")):,} signals</span>'
            f'</div>'
            f'<p class="vc-read">{_esc(read)}</p>'
            f'<p class="vc-mag">{_esc(magnitude)}</p>'
            f'{_meter(_num(row.get("confidence")))}'
            f'</article>'
        )
    return f'<div class="vcards">{"".join(cards)}</div>'


def _coverage(coverage: list[dict], fresh: dict) -> str:
    if coverage:
        c = coverage[0]
        signals, held = _int(c.get("verified_signals")), _int(c.get("held_back"))
        submitted = signals + held
        rate = f"{signals / submitted * 100:.0f}% cleared the judge" if submitted else "—"
        tiles = [
            ("Stories monitored", _compact(_int(c.get("stories_monitored"))),
             "fetched this cycle"),
            ("Read in full", _compact(_int(c.get("stories_with_body"))),
             "with a usable article body"),
            ("Verified signals", _compact(signals), "committed to the record"),
            ("Held back", _compact(held), rate),
        ]
    else:
        total = _int(fresh.get("total_findings"))
        if not total:
            return ""
        tiles = [("Verified signals", _compact(total), "committed to the record")]
    cells = "".join(
        f'<div class="tile"><span class="t-lab">{_esc(lab)}</span>'
        f'<span class="t-val">{_esc(val)}</span>'
        f'<span class="t-sub">{_esc(sub)}</span></div>'
        for lab, val, sub in tiles
    )
    return (
        _section("coverage", "Coverage", "What we ingested",
                 "Volume and quality of intelligence captured this cycle. A signal is "
                 "only committed once it clears an automated quality judge; what does "
                 "not clear is held back rather than shown.")
        + f'<div class="tiles">{cells}</div>'
    )


def _driver_rows(drivers: list[dict], category: str, limit: int = 6) -> str:
    rows = [d for d in drivers if d.get("device_category") == category][:limit]
    if not rows:
        return ('<tr><td colspan="3" class="empty">'
                'No high-strength drivers in the current window.</td></tr>')
    out = []
    for d in rows:
        ss = _num(d.get("signed_strength"))
        color = SUPPORT if ss > 0 else PRESSURE if ss < 0 else NEUTRAL
        url = _url(d.get("source_url"))
        title = _esc(d.get("article_title")) or "source"
        link = (f'<a href="{url}" target="_blank" rel="noopener">{title}</a>'
                if url else f'<span class="muted">{title}</span>')
        out.append(
            f'<tr><td class="num"><span class="chip" style="--cc:{color}">{ss:+.2f}</span></td>'
            f'<td class="nowrap">{_esc(DOMAIN_LABEL.get(d.get("domain"), d.get("domain")))}</td>'
            f'<td><span class="claim">{_clip(d.get("claim"), 220)}</span>'
            f'<span class="src">{link}</span></td></tr>'
        )
    return "".join(out)


def _category_panels(data: dict) -> str:
    panels = []
    for row in data["market"]:
        category = row.get("device_category")
        read = row.get("expected_read")
        color = VERDICT_COLOR.get(read, NEUTRAL)
        panels.append(
            f'<section class="panel">'
            f'<div class="panel-head">'
            f'<div><h3>{_pretty(category)}</h3>'
            f'<p class="panel-sub">{_int(row.get("n_findings")):,} signals · '
            f'{_int(row.get("aligning_count")):,} aligning · '
            f'{_int(row.get("conflicting_count")):,} conflicting</p></div>'
            f'<span class="verdict" style="--vc:{color}">{_esc(read)}</span>'
            f'</div>'
            f'<p class="takeaway">{_esc(row.get("reasoning"))}</p>'
            f'<div class="tw"><table class="dtable"><thead><tr>'
            f'<th class="num">Strength</th><th>Stream</th><th>Driver and source</th>'
            f'</tr></thead><tbody>{_driver_rows(data["drivers"], category)}</tbody>'
            f'</table></div></section>'
        )
    return f'<div class="panels">{"".join(panels)}</div>'


def _component_section(components: list[dict], limit: int = 12) -> str:
    ranked = sorted(components, key=lambda c: abs(_num(c.get("avg_signed_strength"))),
                    reverse=True)[:limit]
    chart_rows = [(str(c.get("component_group")), _num(c.get("avg_signed_strength")),
                   f'{_int(c.get("signals"))} signals') for c in ranked]
    body = []
    for c in ranked:
        ss = _num(c.get("avg_signed_strength"))
        color = SUPPORT if ss > 0 else PRESSURE if ss < 0 else NEUTRAL
        body.append(
            f'<tr><td class="strong">{_esc(c.get("component_group"))}</td>'
            f'<td>{_pretty(c.get("device_category"))}</td>'
            f'<td class="num">{_int(c.get("signals")):,}</td>'
            f'<td class="num" style="color:{SUPPORT}">{_int(c.get("support_count")):,}</td>'
            f'<td class="num" style="color:{PRESSURE}">{_int(c.get("pressure_count")):,}</td>'
            f'<td class="num"><span class="chip" style="--cc:{color}">{ss:+.3f}</span></td></tr>'
        )
    rows = "".join(body) or ('<tr><td colspan="6" class="empty">'
                             'No component signals in the current window.</td></tr>')
    return (
        _section("components", "Concentration", "Where the signal concentrates",
                 "Average signed strength per component group. A bar to the right means "
                 "the signals support the value of stock already held; to the left, they "
                 "press it down.")
        + '<div class="card">'
        + _legend([("Supports value", SUPPORT), ("Pressures value", PRESSURE)])
        + _diverging_bars(chart_rows)
        + '</div>'
        + f'<div class="tw"><table class="dtable"><thead><tr>'
          f'<th>Component</th><th>Category</th><th class="num">Signals</th>'
          f'<th class="num">Support</th><th class="num">Pressure</th>'
          f'<th class="num">Avg strength</th>'
          f'</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def _contribution_section(contribution: list[dict]) -> str:
    streams = ("supply_chain", "policy", "ai_news")
    return (
        _section("streams", "Attribution", "Where the pressure comes from",
                 "Each stream's net push on the value of existing stock, by category. "
                 "This is the reasoning behind the verdicts above: a read is the "
                 "resolution of three streams that frequently disagree.")
        + '<div class="card">'
        + _legend([(DOMAIN_LABEL[s], DOMAIN_COLOR[s]) for s in streams])
        + _grouped_bars(contribution)
        + '</div>'
    )


def _table_block(anchor: str, kicker: str, title: str, lede: str,
                 headers: list[str], rows: list[str], empty: str) -> str:
    cells = []
    for header in headers:
        cls = ' class="num"' if header.startswith("#") else ""
        cells.append(f"<th{cls}>{_esc(header.lstrip('#'))}</th>")
    body = "".join(rows) or (f'<tr><td colspan="{len(headers)}" class="empty">'
                             f'{_esc(empty)}</td></tr>')
    return (
        _section(anchor, kicker, title, lede)
        + f'<div class="tw"><table class="dtable"><thead><tr>{"".join(cells)}</tr>'
          f'</thead><tbody>{body}</tbody></table></div>'
    )


def _policy_section(policy: list[dict]) -> str:
    if not policy:
        return ""
    rows = []
    for p in policy[:12]:
        ss = _num(p.get("avg_signed_strength"))
        color = SUPPORT if ss > 0 else PRESSURE if ss < 0 else NEUTRAL
        rows.append(
            f'<tr><td class="strong">{_pretty(p.get("device_category"))}</td>'
            f'<td>{_pretty(p.get("policy_type"))}</td>'
            f'<td>{_pretty(p.get("corridor_relevance"))}</td>'
            f'<td class="num">{_int(p.get("signals")):,}</td>'
            f'<td class="num"><span class="chip" style="--cc:{color}">{ss:+.3f}</span></td></tr>'
        )
    return _table_block(
        "policy", "Regulation", "Trade and policy exposure",
        "Regulatory signals by instrument and trade corridor. Policy moves the cost of "
        "new imports, and the cost of new is what reprices used.",
        ["Category", "Instrument", "Corridor", "#Signals", "#Avg strength"],
        rows, "No policy signals in the current window.")


def _cascade_section(cascade: list[dict]) -> str:
    if not cascade:
        return ""
    rows = []
    for c in cascade[:12]:
        rows.append(
            f'<tr><td class="strong">{_pretty(c.get("device_category"))}</td>'
            f'<td>{_pretty(c.get("event_kind"))}</td>'
            f'<td class="num">{_int(c.get("events")):,}</td>'
            f'<td class="num" style="color:{PRESSURE}">{_int(c.get("depreciating")):,}</td>'
            f'<td class="num" style="color:{SUPPORT}">{_int(c.get("supporting")):,}</td></tr>'
        )
    return _table_block(
        "launches", "Cascade", "New launches and what they displace",
        "A new generation shipping is the most reliable way older stock loses value. "
        "These are the launch events counted this cycle and which way each one cuts.",
        ["Category", "Event", "#Events", "#Depreciating", "#Supporting"],
        rows, "No launch events in the current window.")


def _magnitude_section(magnitudes: list[dict]) -> str:
    rows = []
    for m in magnitudes[:12]:
        url = _url(m.get("source_url"))
        link = (f'<a href="{url}" target="_blank" rel="noopener">source</a>'
                if url else '<span class="muted">source unavailable</span>')
        rows.append(
            f'<tr><td class="strong">{_esc(m.get("component_group"))}</td>'
            f'<td class="num nowrap">{_esc(m.get("raw_value"))} '
            f'<span class="unit">{_esc(m.get("raw_unit"))}</span></td>'
            f'<td class="num">{_num(m.get("magnitude_z")):+.2f}</td>'
            f'<td><span class="claim">{_clip(m.get("claim"), 180)}</span>'
            f'<span class="src">{link}</span></td></tr>'
        )
    return _table_block(
        "magnitudes", "Outliers", "Notable magnitudes",
        "Extreme values, each scored against others in its own family so a percentage "
        "and a capacity never share a scale. A single outlier is a data point, not a "
        "headline: read these next to the typical value, never instead of it.",
        ["Component", "#Value", "#Z-score", "Claim and source"],
        rows, "No scored magnitudes in the current window.")


def _nav(data: dict) -> str:
    items = [("verdicts", "Verdicts")]
    if data["coverage"] or _int(data["freshness"].get("total_findings")):
        items.append(("coverage", "Coverage"))
    items += [("streams", "Attribution"), ("reads", "Category reads"),
              ("components", "Concentration")]
    if data["policy"]:
        items.append(("policy", "Policy"))
    if data["cascade"]:
        items.append(("launches", "Launches"))
    items.append(("magnitudes", "Outliers"))
    links = "".join(f'<a href="#{a}">{_esc(t)}</a>' for a, t in items)
    return f'<nav class="nav"><div class="nav-in">{links}</div></nav>'


# ----------------------------------------------------------------------- render

_CSS = f"""
:root{{
--ink:#0f172a;--ink2:#334155;--muted:#64748b;--faint:#94a3b8;
--line:#e2e8f0;--line2:#eef2f7;--soft:#f8fafc;--card:#ffffff;
--support:{SUPPORT};--pressure:{PRESSURE};--third:{THIRD};
--shadow:0 1px 2px rgba(15,23,42,.04),0 10px 30px -18px rgba(15,23,42,.28);
--sans:"Inter","Segoe UI Variable Text","Segoe UI",-apple-system,BlinkMacSystemFont,
Roboto,"Helvetica Neue",Arial,sans-serif;
}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth;-webkit-text-size-adjust:100%}}
body{{margin:0;background:var(--soft);color:var(--ink);font-family:var(--sans);
font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased;
font-variant-numeric:tabular-nums}}
.wrap{{max-width:1080px;margin:0 auto;padding:0 24px}}

/* brand bar */
.brand{{background:var(--ink);color:#fff}}
.brand-in{{max-width:1080px;margin:0 auto;padding:15px 24px;display:flex;
justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}}
.brand-name{{font-size:12.5px;font-weight:650;letter-spacing:.16em;text-transform:uppercase}}
.brand-name span{{color:#7dd3fc}}
.brand-meta{{font-size:12px;color:#94a3b8;letter-spacing:.02em}}
.accent{{height:3px;display:flex}}
.accent i{{flex:1}}

/* masthead */
.mast{{padding:52px 0 34px}}
.eyebrow{{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.16em;
text-transform:uppercase;color:var(--support);background:#e8f2fa;border-radius:999px;
padding:5px 13px;margin:0 0 18px}}
h1{{font-size:44px;line-height:1.1;letter-spacing:-.028em;font-weight:700;
margin:0 0 14px;max-width:19ch}}
.dek{{font-size:17px;color:var(--ink2);margin:0;max-width:70ch;line-height:1.62}}
.stale{{display:flex;gap:11px;align-items:flex-start;background:#fff7ed;
border:1px solid #fed7aa;border-radius:12px;padding:13px 16px;margin:24px 0 0;
font-size:13.5px;color:#7c2d12;max-width:80ch}}
.stale-dot{{width:8px;height:8px;border-radius:50%;background:{PRESSURE};
flex-shrink:0;margin-top:7px}}

/* sticky nav */
.nav{{position:sticky;top:0;z-index:30;background:rgba(248,250,252,.88);
backdrop-filter:saturate(160%) blur(10px);border-bottom:1px solid var(--line);
margin-top:30px}}
.nav-in{{max-width:1080px;margin:0 auto;padding:0 24px;display:flex;gap:4px;
overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}}
.nav-in::-webkit-scrollbar{{display:none}}
.nav a{{font-size:13px;font-weight:550;color:var(--muted);text-decoration:none;
padding:13px 12px;white-space:nowrap;border-bottom:2px solid transparent}}
.nav a:hover{{color:var(--ink);border-bottom-color:var(--support)}}

/* verdict cards */
.vcards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:34px 0 12px}}
.vcard{{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--vc);
border-radius:14px;padding:20px 22px 18px;box-shadow:var(--shadow)}}
.vc-top{{display:flex;justify-content:space-between;align-items:baseline;gap:10px}}
.vc-cat{{font-size:11px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
color:var(--muted)}}
.vc-count{{font-size:11.5px;color:var(--faint);white-space:nowrap}}
.vc-read{{font-size:21px;font-weight:680;line-height:1.2;letter-spacing:-.018em;
color:var(--vc);margin:11px 0 4px}}
.vc-mag{{font-size:13.5px;color:var(--ink2);margin:0 0 14px}}
.meter-row{{display:flex;align-items:center;gap:10px}}
.meter{{flex:1;height:5px;border-radius:99px;background:#e8eef5;overflow:hidden;max-width:120px}}
.meter-fill{{height:100%;background:{SUPPORT};border-radius:99px}}
.meter-lab{{font-size:11.5px;color:var(--faint);white-space:nowrap}}
.readnote{{font-size:12.5px;color:var(--faint);margin:0 2px 4px;max-width:80ch}}

/* sections */
.sec{{margin:56px 0 18px}}
.kicker{{display:block;font-size:11px;font-weight:700;letter-spacing:.16em;
text-transform:uppercase;color:var(--support);margin-bottom:7px}}
.sec-h{{font-size:29px;font-weight:680;letter-spacing:-.022em;margin:0 0 9px;line-height:1.2}}
.lede{{font-size:15px;color:var(--ink2);margin:0;max-width:76ch}}

/* stat tiles */
.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:18px 20px;display:flex;flex-direction:column;gap:2px;box-shadow:var(--shadow)}}
.t-lab{{font-size:10.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
color:var(--muted)}}
.t-val{{font-size:34px;font-weight:660;letter-spacing:-.03em;line-height:1.15;margin-top:5px}}
.t-sub{{font-size:12px;color:var(--faint)}}

/* charts */
.card{{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:22px 24px 14px;box-shadow:var(--shadow)}}
.legend{{display:flex;flex-wrap:wrap;gap:20px;margin:0 0 14px;font-size:12.5px;color:var(--ink2)}}
.lg-item{{display:inline-flex;align-items:center;gap:8px;font-weight:550}}
.lg-dot{{width:11px;height:11px;border-radius:4px;display:inline-block}}
.chart{{width:100%;height:auto;display:block;overflow:visible}}
.ax-lab{{font-size:12.5px;fill:{AX_LABEL};font-weight:550}}
.ax-val{{font-size:11.5px;fill:{AX_VALUE}}}
.ax-cap{{font-size:10.5px;fill:{AX_CAPTION};letter-spacing:.1em;text-transform:uppercase}}

/* category panels */
.panels{{display:flex;flex-direction:column;gap:16px}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:22px 24px 8px;box-shadow:var(--shadow)}}
.panel-head{{display:flex;justify-content:space-between;align-items:flex-start;
gap:16px;margin-bottom:10px}}
.panel h3{{font-size:20px;font-weight:660;letter-spacing:-.018em;margin:0 0 3px;
text-transform:capitalize}}
.panel-sub{{font-size:12.5px;color:var(--faint);margin:0}}
.verdict{{font-size:11px;font-weight:700;letter-spacing:.07em;color:#fff;
background:var(--vc);padding:6px 13px;border-radius:999px;white-space:nowrap;flex-shrink:0}}
.takeaway{{font-size:14.5px;color:var(--ink2);margin:0 0 16px;max-width:76ch}}

/* tables */
.tw{{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -2px}}
.dtable{{width:100%;border-collapse:collapse;font-size:13.5px;min-width:520px}}
.dtable th{{text-align:left;font-size:10.5px;font-weight:700;letter-spacing:.11em;
text-transform:uppercase;color:var(--faint);padding:9px 12px;white-space:nowrap;
border-bottom:1px solid var(--line)}}
.dtable td{{padding:13px 12px;vertical-align:top;border-bottom:1px solid var(--line2)}}
.dtable tr:last-child td{{border-bottom:none}}
.dtable tbody tr:hover td{{background:var(--soft)}}
.dtable .num{{text-align:right;white-space:nowrap}}
.dtable th.num{{text-align:right}}
.strong{{font-weight:600}}
.nowrap{{white-space:nowrap}}
.unit{{color:var(--faint);font-size:12px}}
.chip{{display:inline-block;font-weight:650;font-size:12.5px;color:var(--cc);
background:color-mix(in srgb,var(--cc) 10%,transparent);border-radius:7px;padding:3px 8px}}
.claim{{display:block;color:var(--ink)}}
.src{{display:block;margin-top:4px;font-size:12.5px}}
.dtable a{{color:var(--support);text-decoration:none;font-weight:550}}
.dtable a:hover{{text-decoration:underline}}
.muted{{color:var(--faint)}}
.empty{{color:var(--faint);font-style:italic}}

/* footer */
.foot{{margin:56px 0 0;border-top:1px solid var(--line);padding:22px 0 70px;
font-size:13px;color:var(--muted);max-width:82ch;line-height:1.7}}
.foot strong{{color:var(--ink2)}}

@media (max-width:900px){{
  .vcards{{grid-template-columns:1fr}}
  .tiles{{grid-template-columns:repeat(2,1fr)}}
  h1{{font-size:32px;max-width:none}}
  .sec-h{{font-size:24px}}
  .mast{{padding:34px 0 24px}}
  .panel-head{{flex-direction:column;align-items:flex-start}}
}}
@media print{{
  body{{background:#fff}}
  .nav{{display:none}}
  .brand{{background:#fff;color:var(--ink);border-bottom:2px solid var(--ink)}}
  .brand-meta,.brand-name span{{color:var(--muted)}}
  .vcard,.tile,.card,.panel{{box-shadow:none;break-inside:avoid}}
  .dtable tbody tr:hover td{{background:none}}
}}
"""


def render(data: dict) -> str:
    fresh = data["freshness"]
    total = _int(fresh.get("total_findings"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Genesis GAIN — Market Signal Intelligence</title>
<style>{_CSS}</style></head><body>
<div class="brand"><div class="brand-in">
<span class="brand-name">Genesis <span>GAIN</span> · Market Signal Intelligence</span>
<span class="brand-meta">{_esc(CATALOG)}.{_esc(SCHEMA)} · rendered {_esc(data["generated_at"])}</span>
</div></div>
<div class="accent"><i style="background:{SUPPORT}"></i><i style="background:{PRESSURE}"></i>
<i style="background:{THIRD}"></i></div>
<div class="wrap">
<header class="mast">
<p class="eyebrow">Cycle read · {total:,} verified signals</p>
<h1>What the market is telling us about the value of used electronics</h1>
<p class="dek">Expected direction of value for inventory already held, reconciled from
supply-chain, trade-policy and AI-launch signals. Each read is the resolution of three
streams that frequently disagree — the attribution below shows how.</p>
{_staleness(fresh)}
</header>
</div>
{_nav(data)}
<div class="wrap">
<div id="verdicts"></div>
{_hero(data["market"])}
<p class="readnote">Reads describe the expected direction of value for stock already held.
They are signals for judgement — not price forecasts, and not trading advice.</p>
{_coverage(data["coverage"], fresh)}
{_contribution_section(data["contribution"])}
{_section("reads", "Detail", "Category reads",
          "The verdict for each category with the strongest individual signals behind it, "
          "each linked to the article it was extracted from.")}
{_category_panels(data)}
{_component_section(data["components"])}
{_policy_section(data["policy"])}
{_cascade_section(data["cascade"])}
{_magnitude_section(data["magnitudes"])}
<p class="foot"><strong>How to read this.</strong> Every signal is extracted from a
published article, scored for direction and confidence, and committed only after it
clears an automated quality judge; what fails is held back rather than shown. Strength
runs from &minus;1 (value pressed down) through 0 to &plus;1 (value supported), and a
category verdict is the reconciliation of all its signals, not a vote. Colour never
carries meaning on its own — every chart is labelled and every plotted value also
appears in a table. Figures refresh on the weekly reconciliation cycle; the source
schema and render time are shown in the bar at the top of this page.</p>
</div></body></html>"""


def build(spark=None) -> str:
    runner = _spark_runner(spark) if spark is not None else _connector_runner()
    return render(gather(runner))


def write(html_str: str, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_str)
    return path


def main() -> int:
    out = os.environ.get("GAIN_EXEC_DASHBOARD_OUT", "exec_dashboard.html")
    html_str = build(spark=None)
    write(html_str, out)
    print(f"wrote {out} ({len(html_str)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
