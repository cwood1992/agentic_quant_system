# Operational Runbook

Procedures for operating the agentic quant trading system.

> **Note:** Commands below use Python directly for cross-platform compatibility (Windows + Linux/macOS). Shell scripts (`.sh`) are also provided in `scripts/` for Linux/macOS users.

---

## 1. First-Time Setup

### Prerequisites
- Python 3.11+
- Kraken account with API key (trade permissions only, no withdrawal)
- Anthropic API key
- Telegram bot token and chat ID (optional but recommended)

### Steps

1. **Clone and install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Create `.env` from template:**
   ```bash
   cp .env.example .env
   ```
   Fill in:
   - `KRAKEN_API_KEY` / `KRAKEN_API_SECRET`
   - `ANTHROPIC_API_KEY`
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (if using notifications)

3. **Create `config.yaml` from template:**
   ```bash
   cp config.yaml.template config.yaml
   ```
   Review and adjust:
   - Set `dry_run: true` initially
   - Verify `agents.quant_primary.enabled: true`
   - Confirm `risk` limits match your risk tolerance

4. **Initialize the database:**
   ```bash
   python -c "from database.schema import create_all_tables; create_all_tables('data/system.db')"
   ```

5. **Backfill historical data:**
   ```bash
   python data_collector/backfill.py --pairs all --days 180 --timeframes "1m,1h,4h,1d"
   ```
   This fetches 180 days of OHLCV data. Takes 10-30 minutes depending on rate limits.
   > **Windows note:** The quotes around timeframes are required — PowerShell interprets `1d` as a decimal literal without them.

6. **Verify exchange connection:**
   ```bash
   python -c "
   from config import load_config
   from exchange.connector import create_exchange, verify_connection
   config = load_config()
   ex = create_exchange(config)
   print(verify_connection(ex))
   "
   ```
   Confirm `connected: True` and balance is shown.

7. **Start the system:**
   ```bash
   python main.py
   ```
   Watch logs for `Wake controller started` and `Dashboard server started`. The system is now running.

   The dashboard is available at `http://localhost:8501`. For remote access from other machines, ensure port 8501 is open in your firewall.

---

## 2. Daily Operations

### Monitoring
- **Logs:** `logs/system.log` (JSON format, rotated daily, 30-day retention)
- **Dashboard:** Automatically served at `http://localhost:8501` when the system is running. Access from any machine on the network via `http://<host-ip>:8501`. The page auto-refreshes every 60 seconds.

  Dashboard sections:
  - **Strategy Lifecycle Funnel** — visual count at each stage (hypothesis / backtest / robustness / paper / live / graveyard)
  - **Equity Curve** — portfolio value over time
  - **Research Notes** — all notes with status, age, observation, potential edge
  - **Backtest & Robustness Results** — per-strategy metrics (return, Sharpe, drawdown, win rate, robustness percentiles)
  - **Graveyard Analysis** — killed strategies with reasons and failure type distribution
  - **Recent Trades, Risk Gate Log, Agent Messages, Supplementary Feeds, Failed Cycles**

  Configuration in `config.yaml`:
  ```yaml
  dashboard:
    enabled: true
    host: "0.0.0.0"    # bind to all interfaces for remote access
    port: 8501
  ```

  API endpoints:
  - `GET /` — dashboard HTML
  - `GET /api/state` — system state as JSON
  - `GET /health` — health check

  To manually regenerate the dashboard without restarting:
  ```bash
  python -c "
  from dashboard.generator import generate_dashboard
  from config import load_config
  generate_dashboard('data/system.db', load_config())
  "
  ```
- **Telegram:** `/status` shows a live summary

### Checking API Budget
```bash
python -c "
from billing.tracker import APIBudgetTracker
t = APIBudgetTracker('data/system.db')
print(t.get_budget_summary())
"
```
Budget limit: $50/month. If projected spend exceeds budget, the system logs warnings. Consider increasing agent cadence intervals to reduce call frequency.

### Database Backup
Run daily:
```bash
python scripts/backup_db.py
```
Backups are saved to `data/backups/` with timestamps. Copies older than 7 days are auto-removed.

**Linux/macOS** — recommended crontab entry:
```
0 2 * * * cd /path/to/agentic_quant_system && python scripts/backup_db.py
```

**Windows** — create a scheduled task via Task Scheduler or PowerShell:
```powershell
schtasks /create /tn "QuantSystemBackup" /tr "python scripts\backup_db.py" /sc daily /st 02:00 /sd (Get-Date -Format MM/dd/yyyy)
```

---

## 3. Managing Agents

### Via Telegram
- `/agents` -- list all agents with status
- `/pause quant_primary` -- pause a specific agent
- `/pause` -- pause all agents
- `/resume quant_primary` -- resume a specific agent
- `/resume` -- resume all agents and clear circuit breaker
- `/cycle quant_primary` -- force an immediate wake cycle

### Via Config
Edit `config.yaml` and restart the system:
- Enable/disable agents: set `enabled: true/false`
- Change cadence: adjust `base_cadence_hours`
- Adjust capital: modify `capital_allocation_pct` (must sum to <= 1.0 across enabled agents)

### Owner Requests
Agents may create blocking or non-blocking requests requiring your input:
- `/requests` -- list pending requests
- `/resolve <id> [note]` -- resolve a request and unblock the agent

Check requests at least once per day. Blocking requests prevent the agent from continuing certain work until resolved.

---

## 4. Troubleshooting

### System will not start
1. Check `.env` has all required variables
2. Check `config.yaml` exists and is valid YAML
3. Verify database: `python -c "from database.schema import create_all_tables; create_all_tables('data/system.db')"`
4. Check exchange connectivity (see First-Time Setup step 6)

### Agent cycle failures
1. Check `logs/system.log` for the error
2. Look at failed_cycles table:
   ```bash
   python -c "
   from database.schema import get_db
   conn = get_db('data/system.db')
   for r in conn.execute('SELECT * FROM failed_cycles ORDER BY id DESC LIMIT 5').fetchall():
       print(dict(r))
   conn.close()
   "
   ```
3. Common causes:
   - API key expired or rate limited -- check Anthropic dashboard
   - Exchange connectivity -- verify internet and Kraken status
   - Budget exhausted -- check `billing.tracker.get_budget_summary()`

### Agent auto-paused after 3 consecutive failures
The system auto-pauses agents after repeated failures. To resume:
1. Investigate the root cause in logs
2. Fix the issue
3. `/resume <agent_id>` via Telegram

### Orders not filling (paper mode)
Paper trades fill at the next available price. If the order book is stale, fills may be delayed. Check that the data collector is running:
```bash
grep "DataCollector" logs/system.log | tail -5
```

### Dashboard not loading
1. Verify the system is running (`python main.py`)
2. Check for port conflicts — another process may be using port 8501:
   ```bash
   # Linux/macOS:
   lsof -i :8501
   # Windows (PowerShell):
   netstat -ano | findstr :8501
   ```
3. Change the port in `config.yaml` under `dashboard.port` if needed
4. Check logs for `Dashboard server started` — if missing, the server failed to bind

### Dashboard shows stale data
1. Verify agent cycles are completing — check `STATE.md` for the last updated timestamp
2. The dashboard regenerates after each cycle; if cycles are failing, data won't update
3. Check `logs/system.log` for `Dashboard generation failed` errors

### High API costs
1. Check per-agent usage: query `api_usage` table grouped by agent_id
2. Increase `base_cadence_hours` for expensive agents
3. Verify agents are not in a trigger loop (check trigger fire counts in events table)

---

## 5. Circuit Breaker Procedures

The circuit breaker triggers when portfolio drawdown from the high-water mark exceeds 30%.

### When triggered:
1. All positions are closed automatically
2. All agents are paused
3. A Telegram alert is sent (if configured)
4. The system continues running but no new trades are placed

### Recovery procedure:
1. **Investigate the cause:**
   - Review recent trades and their PnL
   - Check for any anomalous market conditions
   - Review agent reasoning in `data/response_log/`

2. **Determine if it is safe to resume:**
   - Was this a genuine drawdown or a data/pricing issue?
   - Has the market condition that caused losses changed?

3. **Resume trading:**
   - Via Telegram: `/resume` (clears circuit breaker and unpauses all agents)
   - The high-water mark remains at its previous value
   - Agents will begin their next cycle on schedule

4. **Post-incident:**
   - Document what happened and why
   - Consider adjusting risk limits if the drawdown was expected behavior
   - Review strategy performance to decide if any should be killed

---

## 6. System Improvement Reviews

Agents submit improvement requests when they identify system limitations. Review these weekly.

### Generate report:
```bash
python scripts/generate_review_report.py
```

### Process improvements:
- **Ship:** `python scripts/mark_shipped.py --requests 1,2,3 --note "Deployed in v0.5"`
- **Ship via Telegram:** `/ship <id>`
- **Decline via Telegram:** `/decline <id> Not feasible at this time`
- **Review via Telegram:** `/review`

### Prioritization:
- **Critical:** Address within 24 hours -- the agent has identified a bug or risk issue
- **High:** Address within a week -- meaningful performance improvement expected
- **Normal:** Address when convenient -- nice-to-have improvements
- **Low:** Backlog -- may auto-decline after 8 weeks per config

---

## 7. Backup and Recovery

### Regular Backups
```bash
python scripts/backup_db.py
```
Creates timestamped copies in `data/backups/`. Old backups (>7 days) are auto-removed.

### Restoring from Backup
1. Stop the system (`Ctrl+C` or `kill <pid>`)
2. Copy the backup over the live database:
   ```bash
   cp data/backups/system_20260101_020000.db data/system.db
   ```
3. Remove WAL/SHM files if present:
   ```bash
   # Linux/macOS:
   rm -f data/system.db-wal data/system.db-shm
   # Windows (PowerShell):
   Remove-Item data/system.db-wal, data/system.db-shm -ErrorAction SilentlyContinue
   ```
4. Restart: `python main.py`

### Full System Recovery
If the system host fails:
1. Provision a new host with the same Python version
2. Clone the repo and install dependencies
3. Restore `.env` and `config.yaml` from secure storage
4. Restore the latest database backup
5. Re-run `python data_collector/backfill.py --pairs all --days 180 --timeframes "1m,1h,4h,1d"` if OHLCV data is stale
6. Start the system

---

## 8. Stopping the System

### Graceful Shutdown
Send SIGINT (Ctrl+C) or SIGTERM. The system will:
1. Stop the wake controller (no new cycles start)
2. Wait for any in-progress cycle to complete (up to 30s)
3. Stop the data collector
4. Checkpoint the WAL (flush writes to main DB file)
5. Exit

### Emergency Stop
Send a second SIGINT/SIGTERM to force-exit immediately. In-progress cycles will be abandoned, but the database should remain consistent due to WAL mode.

### Pausing Without Stopping
If you want to keep the system running but stop all trading:
```
/pause
```
via Telegram. This pauses all agents but keeps the data collector running. Resume with `/resume`.
