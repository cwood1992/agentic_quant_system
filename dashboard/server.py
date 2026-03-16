"""FastAPI server for the agentic quant dashboard.

Serves the generated dashboard HTML over HTTP so it can be accessed
from any machine on the network.
"""

import json
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from logging_config import get_logger

logger = get_logger("dashboard.server")

app = FastAPI(title="Agentic Quant Dashboard")

# Set at startup by start_server()
_dashboard_path: str = "dashboard/output"
_db_path: str = "data/system.db"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Serve the latest generated dashboard HTML."""
    try:
        with open(_dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Dashboard not yet generated</h1>"
            "<p>Waiting for the first agent cycle to complete.</p>",
            status_code=200,
        )


@app.get("/api/state", response_class=JSONResponse)
def api_state():
    """Return system state as JSON for programmatic access."""
    from database.schema import get_db

    try:
        conn = get_db(_db_path)
        rows = conn.execute("SELECT key, value FROM system_state").fetchall()
        state = {}
        for r in rows:
            try:
                state[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                state[r["key"]] = r["value"]
        conn.close()
        return JSONResponse(content=state)
    except Exception as exc:
        logger.error("Failed to read system state: %s", exc)
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.get("/health")
def health():
    """Simple health check endpoint."""
    return {"status": "ok", "dashboard_exists": os.path.exists(_dashboard_path)}


def start_server(
    host: str = "0.0.0.0",
    port: int = 8501,
    dashboard_path: str = "dashboard/output",
    db_path: str = "data/system.db",
) -> None:
    """Start the dashboard server (blocking — run in a thread).

    Args:
        host: Bind address.
        port: Port number.
        dashboard_path: Path to the generated dashboard HTML file.
        db_path: Path to the SQLite database.
    """
    global _dashboard_path, _db_path
    _dashboard_path = dashboard_path
    _db_path = db_path

    import uvicorn

    logger.info("Starting dashboard server on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
