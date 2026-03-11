"""Dashboard generator for the agentic quant trading system.

Produces a single-file HTML dashboard with inline CSS/JS. Sections cover
equity curves, strategy lifecycle, risk gate log, agent messages, and
supplementary feeds.
"""

import json
from datetime import datetime, timezone
from html import escape

from database.schema import get_db
from logging_config import get_logger

logger = get_logger("dashboard.generator")


def generate_dashboard(
    db_path: str,
    config: dict,
    output_path: str = "dashboard/index.html",
) -> str:
    """Generate a single-file HTML dashboard.

    Args:
        db_path: Path to the SQLite database.
        config: Application configuration dict.
        output_path: File path for the generated HTML.

    Returns:
        The output_path where the dashboard was written.
    """
    conn = get_db(db_path)

    # --- Gather data ---
    equity_data = _get_equity_data(conn)
    strategies = _get_strategies(conn)
    risk_log = _get_risk_log(conn)
    messages = _get_messages(conn)
    feeds = _get_feeds(conn)
    trades = _get_recent_trades(conn)
    system_state = _get_system_state(conn)

    conn.close()

    # --- Build HTML ---
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = _build_html(
        equity_data=equity_data,
        strategies=strategies,
        risk_log=risk_log,
        messages=messages,
        feeds=feeds,
        trades=trades,
        system_state=system_state,
        config=config,
        generated_at=generated_at,
    )

    import os
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info("Dashboard generated at %s", output_path)
    return output_path


# --------------------------------------------------------------------------
# Data queries
# --------------------------------------------------------------------------

def _get_equity_data(conn) -> list[dict]:
    """Get equity snapshots from events table."""
    rows = conn.execute(
        "SELECT timestamp, payload FROM events "
        "WHERE event_type IN ('cycle_complete', 'equity_snapshot') "
        "ORDER BY timestamp DESC LIMIT 200"
    ).fetchall()
    results = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
            equity = payload.get("equity") or payload.get("portfolio_value")
            if equity is not None:
                results.append({"timestamp": r["timestamp"], "equity": float(equity)})
        except (json.JSONDecodeError, TypeError):
            pass
    results.reverse()
    return results


def _get_strategies(conn) -> list[dict]:
    """Get all strategies with their current stage."""
    rows = conn.execute(
        "SELECT strategy_id, agent_id, namespace, stage, created_at, updated_at "
        "FROM strategy_registry ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_risk_log(conn) -> list[dict]:
    """Get recent risk gate decisions."""
    rows = conn.execute(
        "SELECT id, created_at, agent_id, instruction_type, status, risk_check_result "
        "FROM instruction_queue ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_messages(conn) -> list[dict]:
    """Get recent agent messages."""
    rows = conn.execute(
        "SELECT created_at, from_agent, to_agent, message_type, priority, "
        "substr(payload, 1, 200) as payload_preview, status "
        "FROM agent_messages ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_feeds(conn) -> list[dict]:
    """Get latest supplementary feed values."""
    rows = conn.execute(
        "SELECT feed_name, timestamp, value, source "
        "FROM supplementary_feeds "
        "GROUP BY feed_name "
        "HAVING timestamp = MAX(timestamp) "
        "ORDER BY feed_name"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_recent_trades(conn) -> list[dict]:
    """Get recent trades."""
    rows = conn.execute(
        "SELECT timestamp, agent_id, strategy_id, pair, action, size_usd, "
        "price, fill_price, paper, status "
        "FROM trades ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_system_state(conn) -> dict:
    """Get all system_state key-value pairs."""
    rows = conn.execute("SELECT key, value FROM system_state").fetchall()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    return result


# --------------------------------------------------------------------------
# HTML builder
# --------------------------------------------------------------------------

def _build_html(
    equity_data: list[dict],
    strategies: list[dict],
    risk_log: list[dict],
    messages: list[dict],
    feeds: list[dict],
    trades: list[dict],
    system_state: dict,
    config: dict,
    generated_at: str,
) -> str:
    """Assemble the full HTML dashboard."""

    # --- Equity chart as inline SVG ---
    equity_svg = _build_equity_svg(equity_data)

    # --- Strategy table ---
    strategy_rows = ""
    stage_colors = {
        "hypothesis": "#888",
        "backtest": "#d4a017",
        "robustness": "#e07000",
        "paper": "#2196F3",
        "live": "#4CAF50",
        "graveyard": "#f44336",
    }
    for s in strategies:
        color = stage_colors.get(s.get("stage", ""), "#888")
        strategy_rows += (
            f"<tr>"
            f"<td>{escape(s.get('strategy_id', ''))}</td>"
            f"<td>{escape(s.get('agent_id', ''))}</td>"
            f"<td>{escape(s.get('namespace', ''))}</td>"
            f"<td style='color:{color};font-weight:bold'>{escape(s.get('stage', ''))}</td>"
            f"<td>{escape(str(s.get('created_at', ''))[:10])}</td>"
            f"<td>{escape(str(s.get('updated_at', ''))[:10])}</td>"
            f"</tr>\n"
        )

    # --- Risk gate log table ---
    risk_rows = ""
    for r in risk_log:
        status = r.get("status", "")
        status_class = "approved" if status == "approved" else (
            "rejected" if status == "rejected" else ""
        )
        check_result = ""
        if r.get("risk_check_result"):
            try:
                check = json.loads(r["risk_check_result"])
                check_result = check.get("reason", "")
            except (json.JSONDecodeError, TypeError):
                check_result = str(r.get("risk_check_result", ""))
        risk_rows += (
            f"<tr>"
            f"<td>{r.get('id', '')}</td>"
            f"<td>{escape(str(r.get('created_at', ''))[:16])}</td>"
            f"<td>{escape(r.get('agent_id', ''))}</td>"
            f"<td>{escape(r.get('instruction_type', ''))}</td>"
            f"<td class='{status_class}'>{escape(status)}</td>"
            f"<td>{escape(check_result[:60])}</td>"
            f"</tr>\n"
        )

    # --- Agent messages table ---
    msg_rows = ""
    for m in messages:
        msg_rows += (
            f"<tr>"
            f"<td>{escape(str(m.get('created_at', ''))[:16])}</td>"
            f"<td>{escape(m.get('from_agent', ''))}</td>"
            f"<td>{escape(m.get('to_agent', ''))}</td>"
            f"<td>{escape(m.get('message_type', ''))}</td>"
            f"<td>{escape(m.get('priority', ''))}</td>"
            f"<td>{escape(m.get('status', ''))}</td>"
            f"</tr>\n"
        )

    # --- Supplementary feeds table ---
    feed_rows = ""
    for f_ in feeds:
        val = f_.get("value")
        val_str = f"{val:.4f}" if isinstance(val, (int, float)) and val is not None else str(val)
        feed_rows += (
            f"<tr>"
            f"<td>{escape(f_.get('feed_name', ''))}</td>"
            f"<td>{val_str}</td>"
            f"<td>{escape(str(f_.get('timestamp', ''))[:16])}</td>"
            f"<td>{escape(f_.get('source', ''))}</td>"
            f"</tr>\n"
        )

    # --- Recent trades table ---
    trade_rows = ""
    for t in trades:
        mode = "paper" if t.get("paper") else "live"
        trade_rows += (
            f"<tr>"
            f"<td>{escape(str(t.get('timestamp', ''))[:16])}</td>"
            f"<td>{escape(t.get('agent_id', ''))}</td>"
            f"<td>{escape(t.get('pair', ''))}</td>"
            f"<td>{escape(t.get('action', ''))}</td>"
            f"<td>${t.get('size_usd', 0):,.2f}</td>"
            f"<td>${t.get('price', 0):,.2f}</td>"
            f"<td>{mode}</td>"
            f"<td>{escape(t.get('status', ''))}</td>"
            f"</tr>\n"
        )

    # --- System state ---
    cb_status = system_state.get("circuit_breaker_status", {})
    cb_label = cb_status.get("status", "normal") if isinstance(cb_status, dict) else str(cb_status)
    hwm = system_state.get("high_water_mark", {})
    hwm_val = hwm.get("amount", 0.0) if isinstance(hwm, dict) else 0.0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agentic Quant System Dashboard</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
    h1 {{ color: #4fc3f7; margin-bottom: 5px; }}
    h2 {{ color: #81d4fa; margin: 25px 0 10px 0; border-bottom: 1px solid #333; padding-bottom: 5px; }}
    .meta {{ color: #888; font-size: 0.85em; margin-bottom: 20px; }}
    .status-bar {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px; }}
    .status-card {{ background: #16213e; padding: 15px 20px; border-radius: 8px;
                   min-width: 150px; }}
    .status-card .label {{ color: #888; font-size: 0.8em; text-transform: uppercase; }}
    .status-card .value {{ font-size: 1.4em; font-weight: bold; margin-top: 3px; }}
    .status-card .value.ok {{ color: #4CAF50; }}
    .status-card .value.warn {{ color: #f44336; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px;
            background: #16213e; border-radius: 8px; overflow: hidden; }}
    th {{ background: #0f3460; padding: 10px 12px; text-align: left; font-size: 0.85em;
         text-transform: uppercase; color: #81d4fa; }}
    td {{ padding: 8px 12px; border-top: 1px solid #222; font-size: 0.9em; }}
    tr:hover {{ background: #1a1a40; }}
    .approved {{ color: #4CAF50; font-weight: bold; }}
    .rejected {{ color: #f44336; font-weight: bold; }}
    .svg-chart {{ background: #16213e; border-radius: 8px; padding: 15px; margin-bottom: 20px; }}
    .section {{ margin-bottom: 30px; }}
</style>
</head>
<body>
<h1>Agentic Quant System</h1>
<div class="meta">Generated: {generated_at}</div>

<div class="status-bar">
    <div class="status-card">
        <div class="label">Circuit Breaker</div>
        <div class="value {'ok' if cb_label == 'normal' else 'warn'}">{cb_label.upper()}</div>
    </div>
    <div class="status-card">
        <div class="label">High-Water Mark</div>
        <div class="value">${hwm_val:,.2f}</div>
    </div>
    <div class="status-card">
        <div class="label">Strategies</div>
        <div class="value">{len(strategies)}</div>
    </div>
    <div class="status-card">
        <div class="label">Recent Trades</div>
        <div class="value">{len(trades)}</div>
    </div>
</div>

<div class="section">
<h2>Equity Curve</h2>
<div class="svg-chart">
{equity_svg}
</div>
</div>

<div class="section">
<h2>Recent Trades</h2>
<table>
<tr><th>Time</th><th>Agent</th><th>Pair</th><th>Action</th><th>Size</th><th>Price</th><th>Mode</th><th>Status</th></tr>
{trade_rows if trade_rows else "<tr><td colspan='8'>No trades recorded</td></tr>"}
</table>
</div>

<div class="section">
<h2>Strategy Lifecycle</h2>
<table>
<tr><th>Strategy</th><th>Agent</th><th>Namespace</th><th>Stage</th><th>Created</th><th>Updated</th></tr>
{strategy_rows if strategy_rows else "<tr><td colspan='6'>No strategies registered</td></tr>"}
</table>
</div>

<div class="section">
<h2>Risk Gate Log</h2>
<table>
<tr><th>ID</th><th>Time</th><th>Agent</th><th>Type</th><th>Status</th><th>Reason</th></tr>
{risk_rows if risk_rows else "<tr><td colspan='6'>No instructions processed</td></tr>"}
</table>
</div>

<div class="section">
<h2>Agent Messages</h2>
<table>
<tr><th>Time</th><th>From</th><th>To</th><th>Type</th><th>Priority</th><th>Status</th></tr>
{msg_rows if msg_rows else "<tr><td colspan='6'>No messages</td></tr>"}
</table>
</div>

<div class="section">
<h2>Supplementary Feeds</h2>
<table>
<tr><th>Feed</th><th>Value</th><th>Timestamp</th><th>Source</th></tr>
{feed_rows if feed_rows else "<tr><td colspan='4'>No feeds configured</td></tr>"}
</table>
</div>

</body>
</html>"""


def _build_equity_svg(equity_data: list[dict]) -> str:
    """Build a simple SVG line chart for equity data."""
    if not equity_data or len(equity_data) < 2:
        return "<p style='color:#888;text-align:center;padding:30px;'>Insufficient data for equity chart</p>"

    width = 800
    height = 250
    margin = 40

    values = [d["equity"] for d in equity_data]
    min_val = min(values) * 0.99
    max_val = max(values) * 1.01
    val_range = max_val - min_val
    if val_range == 0:
        val_range = 1

    n = len(values)
    x_step = (width - 2 * margin) / max(n - 1, 1)

    # Build polyline points
    points = []
    for i, v in enumerate(values):
        x = margin + i * x_step
        y = margin + (1 - (v - min_val) / val_range) * (height - 2 * margin)
        points.append(f"{x:.1f},{y:.1f}")

    polyline = " ".join(points)

    # Y-axis labels
    y_labels = ""
    for frac in [0, 0.25, 0.5, 0.75, 1.0]:
        val = min_val + frac * val_range
        y = margin + (1 - frac) * (height - 2 * margin)
        y_labels += (
            f'<text x="{margin - 5}" y="{y + 4}" text-anchor="end" '
            f'font-size="10" fill="#888">${val:,.0f}</text>\n'
            f'<line x1="{margin}" y1="{y}" x2="{width - margin}" y2="{y}" '
            f'stroke="#333" stroke-width="0.5"/>\n'
        )

    return f"""<svg viewBox="0 0 {width} {height}" width="100%" xmlns="http://www.w3.org/2000/svg">
{y_labels}
<polyline points="{polyline}" fill="none" stroke="#4fc3f7" stroke-width="2"/>
<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height - margin}" stroke="#555" stroke-width="1"/>
<line x1="{margin}" y1="{height - margin}" x2="{width - margin}" y2="{height - margin}" stroke="#555" stroke-width="1"/>
</svg>"""
