"""A page a reviewer can open to see what the rails flagged.

Cases are useless sitting in a list in memory. Somebody has to read them, decide,
and have that decision recorded - and the decision is the point of the wire rail,
which never acts on its own.

Deliberately a static page written to disk rather than a service. The simulation
is not a long-running product, and a reviewer looking at a run afterwards wants
a file they can open, not a server they have to start. Everything the page shows
comes from the case list and the decision log, so it cannot disagree with them.

**The card rail's state is shown honestly.** If no model is loaded the page says
so at the top, because a console showing "0 card cases" looks identical whether
the rail is clean or switched off.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>Fraud review</title>
<style>
  :root {{
    --ground: #0a0d12; --panel: #131820; --line: #232c3a;
    --text: #d8e0ea; --muted: #7d8b9e;
    --ok: #4c9a5a; --warn: #c9a227; --crit: #d2504f; --flow: #5fa8d3;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--ground); color: var(--text);
         font: 13px/1.5 "IBM Plex Sans", system-ui, sans-serif; }}
  header {{ padding: 20px 24px; border-bottom: 1px solid var(--line); }}
  h1 {{ margin: 0 0 4px; font-size: 17px; font-weight: 600; }}
  .sub {{ color: var(--muted); font-size: 12px; }}
  .banner {{ margin: 16px 24px 0; padding: 10px 14px; border-radius: 4px;
             background: rgba(201,162,39,.12); border: 1px solid var(--warn);
             color: #e6c85a; font-size: 12px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px; padding: 20px 24px; }}
  .tile {{ background: var(--panel); border: 1px solid var(--line);
           border-radius: 4px; padding: 12px 14px; }}
  .tile .n {{ font: 600 22px/1.2 "IBM Plex Mono", ui-monospace, monospace; }}
  .tile .k {{ color: var(--muted); font-size: 11px; text-transform: uppercase;
              letter-spacing: .04em; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--line);
            vertical-align: top; }}
  th {{ color: var(--muted); font-size: 11px; text-transform: uppercase;
        letter-spacing: .04em; font-weight: 600; }}
  td.mono {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px; }}
  .rail {{ display: inline-block; padding: 1px 7px; border-radius: 3px;
           font-size: 11px; font-weight: 600; }}
  .rail.card {{ background: rgba(210,80,79,.15); color: #e57a79; }}
  .rail.wire {{ background: rgba(95,168,211,.15); color: var(--flow); }}
  .reason {{ color: var(--muted); font-size: 12px; }}
  section {{ padding: 0 24px 28px; }}
  h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
        color: var(--muted); margin: 20px 0 8px; }}
  .empty {{ color: var(--muted); padding: 16px 0; }}
</style>
<header>
  <h1>Fraud review</h1>
  <div class="sub">{generated} &middot; {run}</div>
</header>
{banner}
<div class="tiles">{tiles}</div>
<section>
  <h2>Open cases</h2>
  {cases}
  <h2>Decisions taken</h2>
  {decisions}
</section>
"""


def _tile(value: str, label: str) -> str:
    return (f'<div class="tile"><div class="n">{html.escape(value)}</div>'
            f'<div class="k">{html.escape(label)}</div></div>')


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.0f}"


def render(summary: dict, cases: list, decisions: list | None = None,
           card_model_loaded: bool = False, run_label: str = "") -> str:
    """Build the page. Every value is escaped; none of it is trusted as markup."""
    banner = ""
    if not card_model_loaded:
        banner = ('<div class="banner"><strong>The card rail has no trained '
                  'model.</strong> Every payment is being allowed. Card cases '
                  'below will be empty because nothing is scoring them, not '
                  'because nothing was found.</div>')

    tiles = "".join([
        _tile(f"{summary.get('scored', 0):,}", "transactions scored"),
        _tile(f"{summary.get('flagged', 0):,}", "flagged"),
        _tile(f"{summary.get('flag_rate', 0):.2%}", "flag rate"),
        _tile(f"{summary.get('blocked', 0):,}", "blocked"),
        _tile(f"{summary.get('review', 0):,}", "for review"),
        _tile(f"{summary.get('accounts_tracked', 0):,}", "accounts tracked"),
    ])

    if cases:
        rows = "".join(
            f"<tr>"
            f'<td class="mono">{html.escape(str(c.tx_id))}</td>'
            f'<td><span class="rail {html.escape(c.rail)}">'
            f"{html.escape(c.rail)}</span></td>"
            f"<td>{html.escape(c.action)}</td>"
            f'<td class="mono">{c.score:.2f}</td>'
            f'<td class="mono">{_rupees(c.amount_paise)}</td>'
            f'<td class="reason">{html.escape(c.reason)}</td>'
            f"</tr>"
            for c in cases)
        cases_html = ("<table><tr><th>Transaction</th><th>Rail</th><th>Action</th>"
                      "<th>Score</th><th>Amount</th><th>Why</th></tr>"
                      f"{rows}</table>")
    else:
        cases_html = '<div class="empty">No open cases.</div>'

    if decisions:
        rows = "".join(
            f"<tr>"
            f'<td class="mono">{html.escape(str(d.case_id))}</td>'
            f"<td>{html.escape(d.action)}</td>"
            f"<td>{html.escape(d.reviewer)}</td>"
            f'<td class="reason">{html.escape(d.reason)}</td>'
            f'<td class="mono">{html.escape(d.at)}</td>'
            f"</tr>"
            for d in decisions)
        decisions_html = ("<table><tr><th>Case</th><th>Action</th><th>Reviewer</th>"
                          f"<th>Reason</th><th>When</th></tr>{rows}</table>")
    else:
        decisions_html = '<div class="empty">No decisions recorded yet.</div>'

    return TEMPLATE.format(
        generated=html.escape(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        run=html.escape(run_label or "unnamed run"),
        banner=banner, tiles=tiles, cases=cases_html, decisions=decisions_html)


def write(path: str | Path, summary: dict, cases: list,
          decisions: list | None = None, card_model_loaded: bool = False,
          run_label: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(summary, cases, decisions, card_model_loaded,
                           run_label), encoding="utf-8")
    return path
