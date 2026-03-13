import ccxt
import time
import logging

logger = logging.getLogger(__name__)

STABLECOINS = {"USD", "USDC", "USDT"}


def create_exchange(config: dict) -> ccxt.Exchange:
    """Initialize a Kraken exchange instance from config."""
    exchange_cfg = config["exchange"]
    exchange = ccxt.kraken({
        "apiKey": exchange_cfg["api_key"],
        "secret": exchange_cfg["api_secret"],
    })
    if exchange_cfg.get("sandbox", False):
        exchange.set_sandbox_mode(True)
        logger.info("Exchange sandbox mode enabled")
    logger.info("Kraken exchange instance created")
    return exchange


def verify_connection(exchange) -> dict:
    """Verify exchange connectivity by fetching balance."""
    try:
        balance = exchange.fetch_balance()
        totals = balance.get("total", {})
        total_usd = sum(
            float(totals.get(sym, 0.0) or 0.0)
            for sym in ("USD", "USDC", "USDT")
        )
        logger.info("Exchange connection verified, total USD: %.2f", total_usd)
        return {"connected": True, "total_usd": total_usd}
    except Exception as e:
        logger.error("Exchange connection failed: %s", e)
        return {"connected": False, "error": str(e)}


def get_total_equity(exchange) -> float:
    """Return total portfolio value in USD, converting all holdings at spot prices.

    Stablecoins (USD, USDC, USDT) are counted at face value.
    All other assets are priced via fetch_ticker(ASSET/USD).
    """
    balance = exchange.fetch_balance()
    totals = balance.get("total", {})
    equity = 0.0

    for asset, amount in totals.items():
        amount = float(amount or 0.0)
        if amount <= 0:
            continue

        if asset in STABLECOINS:
            equity += amount
        else:
            # Try to get a USD price for this asset
            for quote in ("USD", "USDT"):
                try:
                    ticker = exchange.fetch_ticker(f"{asset}/{quote}")
                    price = ticker.get("last") or ticker.get("close") or 0.0
                    equity += amount * float(price)
                    break
                except Exception:
                    continue

    logger.info("Total equity calculated: $%.2f", equity)
    return equity


def fetch_ticker(exchange, pair: str) -> dict:
    """Fetch ticker with retry logic: 3 attempts, exponential backoff."""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            ticker = exchange.fetch_ticker(pair)
            return ticker
        except ccxt.NetworkError as e:
            if attempt < max_attempts - 1:
                delay = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    "NetworkError fetching ticker for %s (attempt %d/%d), "
                    "retrying in %ds: %s",
                    pair, attempt + 1, max_attempts, delay, e,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Failed to fetch ticker for %s after %d attempts: %s",
                    pair, max_attempts, e,
                )
                raise
