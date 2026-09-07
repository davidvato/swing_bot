import os
import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import config
from dotenv import load_dotenv

# Try to import alpaca, fail gracefully if not configured properly
try:
    from alpaca.trading.client import TradingClient
    alpaca_available = True
except ImportError:
    alpaca_available = False

load_dotenv()

app = FastAPI(title="Swing Trading Bot Dashboard")

from fastapi import Request

@app.middleware("http")
async def add_no_cache_header(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response
def get_db_connection():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/api/config")
def get_bot_config():
    """Returns basic configuration rules of the bot."""
    return {
        "Kelly Fraction": config.KELLY_FRACTION,
        "Max Position %": f"{config.MAX_POSITION_PCT * 100}%",
        "Take Profit": f"{config.TAKE_PROFIT_PCT * 100}%",
        "Stop Loss": f"{config.STOP_LOSS_PCT * 100}%",
        "Max Hold Days": config.MAX_HOLD_DAYS,
        "Universe": config.TICKERS
    }


@app.get("/api/universe")
def get_universe():
    """
    Returns the active trading universe selected by the dynamic screener.

    Reads from the universe_cache.json file written by UniverseScreener.
    If the cache doesn't exist yet (first run before Monday), returns the
    static fallback TICKERS from config.py.

    Response fields:
      - active_universe: list of selected ticker symbols
      - scores: dict {ticker: liquidity_score} (price * avg_volume)
      - last_updated: ISO timestamp of the last screener run (null if fallback)
      - source: 'dynamic_screener' | 'static_fallback'
      - universe_size: number of active tickers
      - candidate_pool_size: total candidates evaluated by the screener
      - next_update: 'Every Monday at 09:35 EST'
    """
    import json
    from pathlib import Path

    cache_path = Path(config.UNIVERSE_CACHE_FILE)

    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["next_update"] = "Every Monday at 09:35 EST"
            return payload
        except Exception as e:
            pass  # Fall through to static fallback

    # Static fallback
    return {
        "active_universe": config.TICKERS,
        "scores": {},
        "last_updated": None,
        "source": "static_fallback",
        "universe_size": len(config.TICKERS),
        "candidate_pool_size": 0,
        "next_update": "Every Monday at 09:35 EST",
    }

@app.get("/api/budget")
def get_budget():
    """Returns the available budget (Buying Power and Equity) from Alpaca."""
    API_KEY = os.getenv("ALPACA_API_KEY")
    SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    PAPER = os.getenv("ALPACA_PAPER", "True") == "True"
    
    # Check if Alpaca is configured and available
    if alpaca_available and API_KEY and SECRET_KEY:
        try:
            client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
            account = client.get_account()
            return {
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "source": "Alpaca API"
            }
        except Exception as e:
            print(f"Error fetching Alpaca account: {e}")
            pass
            
    # Fallback to calculating an estimated equity from initial+pnl if no Alpaca
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT SUM(pnl) as total_pnl FROM trades WHERE pnl IS NOT NULL")
        row = cur.fetchone()
        total_pnl = row["total_pnl"] if row["total_pnl"] else 0
        conn.close()
        # Default starting mock equity of $10,000 for demonstration purposes
        return {
            "equity": 10000 + total_pnl,
            "buying_power": 10000 + total_pnl,
            "source": "Local Estimate ($10k base)"
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/trades")
def get_trades():
    """
    Returns trades grouped as round-trips (BUY + SELL paired into one record).

    Algorithm: sort all rows chronologically, then for each ticker maintain a
    FIFO queue of pending BUYs. Each SELL_* dequeues the oldest matching BUY
    and emits a 'CLOSED' record. Remaining BUYs without a matching SELL are
    emitted as 'OPEN' records.

    Sort order: OPEN positions first (most recent entry), then CLOSED sorted
    by exit_date descending.
    """
    try:
        from collections import defaultdict, deque
        from datetime import datetime as _dt

        conn = get_db_connection()
        cur = conn.cursor()
        # Ascending order is critical for the FIFO matching to work correctly
        cur.execute("SELECT * FROM trades ORDER BY date ASC, id ASC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        pending_buys: dict = defaultdict(deque)   # ticker -> deque of BUY dicts
        paired: list = []

        for row in rows:
            ticker = row["ticker"]
            if row["trade_type"] == "BUY":
                pending_buys[ticker].append(row)
            elif row["trade_type"].startswith("SELL"):
                if pending_buys[ticker]:
                    buy = pending_buys[ticker].popleft()
                    # Compute holding duration in calendar days
                    try:
                        d1 = _dt.strptime(buy["date"].split(" ")[0], "%Y-%m-%d")
                        d2 = _dt.strptime(row["date"].split(" ")[0], "%Y-%m-%d")
                        duration_days = (d2 - d1).days
                    except Exception:
                        duration_days = None

                    paired.append({
                        "status": "CLOSED",
                        "ticker": ticker,
                        "sell_type": row["trade_type"],
                        "entry_date": buy["date"],
                        "exit_date": row["date"],
                        "entry_price": buy["entry_price"],
                        "exit_price": row["exit_price"],
                        "qty": buy["qty"],
                        "notional": buy["notional"],
                        "pnl": row["pnl"],
                        "pnl_pct": row["pnl_pct"],
                        "kelly_pct": buy["kelly_pct"],
                        "duration_days": duration_days,
                        "buy_id": buy["id"],
                        "sell_id": row["id"],
                    })
                # Orphan SELL with no matching BUY — silently skip

        # Remaining unmatched BUYs → OPEN positions
        for ticker, buy_queue in pending_buys.items():
            for buy in buy_queue:
                paired.append({
                    "status": "OPEN",
                    "ticker": ticker,
                    "sell_type": None,
                    "entry_date": buy["date"],
                    "exit_date": None,
                    "entry_price": buy["entry_price"],
                    "exit_price": None,
                    "qty": buy["qty"],
                    "notional": buy["notional"],
                    "pnl": None,
                    "pnl_pct": None,
                    "kelly_pct": buy["kelly_pct"],
                    "duration_days": None,
                    "buy_id": buy["id"],
                    "sell_id": None,
                })

        # Sort: OPEN first (newest entry first), then CLOSED by exit_date DESC
        open_trades = sorted(
            [t for t in paired if t["status"] == "OPEN"],
            key=lambda x: x["entry_date"],
            reverse=True,
        )
        closed_trades = sorted(
            [t for t in paired if t["status"] == "CLOSED"],
            key=lambda x: x["exit_date"],
            reverse=True,
        )
        return open_trades + closed_trades

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/metrics")
def get_metrics():
    """Calculates performance metrics based on trade history."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # We consider a trade completed if it has a PNL (usually sell orders)
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE pnl > 0")
        winning_trades = cur.fetchone()["cnt"]
        
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE pnl <= 0 AND pnl IS NOT NULL")
        losing_trades = cur.fetchone()["cnt"]
        
        cur.execute("SELECT SUM(pnl) as total_pnl FROM trades WHERE pnl IS NOT NULL")
        row = cur.fetchone()
        total_pnl = row["total_pnl"] if row["total_pnl"] else 0
        
        conn.close()
        
        total_completed = winning_trades + losing_trades
        win_rate = (winning_trades / total_completed * 100) if total_completed > 0 else 0
        
        return {
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "total_trades": total_completed
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chart/{ticker}")
def get_chart_data(ticker: str, entry_price: float = None):
    """Returns historical daily bars for a ticker to plot on the chart.
    
    Optionally accepts entry_price to compute TP (+10%) and SL (-5%) levels.
    Response is a dict with 'bars' (OHLCV list) and optional 'levels' (TP/SL).
    """
    API_KEY = os.getenv("ALPACA_API_KEY")
    SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
    
    if not (API_KEY and SECRET_KEY):
        raise HTTPException(status_code=500, detail="Alpaca keys not configured in .env")
        
    try:
        from data.ingestion import DataClient
        client = DataClient(API_KEY, SECRET_KEY)
        data_dict = client.get_historical_data([ticker], lookback_days=100)
        
        if ticker not in data_dict:
            raise HTTPException(status_code=404, detail=f"No data found for {ticker}")
            
        df = data_dict[ticker]
        
        # Convert to lightweight-charts format
        bars = []
        for index, row in df.iterrows():
            bars.append({
                "time": index.strftime('%Y-%m-%d'),
                "open": round(float(row["open"]), 4),
                "high": round(float(row["high"]), 4),
                "low": round(float(row["low"]), 4),
                "close": round(float(row["close"]), 4),
            })
        
        # Compute TP/SL levels if entry_price was provided
        levels = None
        if entry_price and entry_price > 0:
            levels = {
                "entry": round(entry_price, 4),
                "tp": round(entry_price * (1 + config.TAKE_PROFIT_PCT), 4),
                "sl": round(entry_price * (1 - config.STOP_LOSS_PCT), 4),
                "tp_pct": config.TAKE_PROFIT_PCT * 100,
                "sl_pct": config.STOP_LOSS_PCT * 100,
            }
            
        return {"bars": bars, "levels": levels}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Crypto Endpoints ─────────────────────────────────────────────────────────

@app.get("/api/crypto/universe")
def get_crypto_universe():
    """
    Returns the active crypto universe from CoinGecko screener cache.
    Falls back to static CRYPTO_FALLBACK_TICKERS if cache doesn't exist.
    """
    import json
    from pathlib import Path
    cache_path = Path(config.CRYPTO_UNIVERSE_CACHE)
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["next_update"] = "Every Monday at 00:01 UTC (CoinGecko)"
            return payload
        except Exception:
            pass
    return {
        "active_universe": config.CRYPTO_FALLBACK_TICKERS,
        "scores": {},
        "last_updated": None,
        "source": "static_fallback",
        "universe_size": len(config.CRYPTO_FALLBACK_TICKERS),
        "next_update": "Every Monday at 00:01 UTC (CoinGecko)",
    }


@app.get("/api/crypto/prices")
def get_crypto_prices():
    """
    Returns latest cached crypto prices (updated every hour by the bot).
    Falls back to static universe with null prices if cache not ready.
    """
    import json
    from pathlib import Path
    cache_path = Path(config.CRYPTO_CACHE_FILE)
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "prices": {sym: None for sym in config.CRYPTO_FALLBACK_TICKERS},
        "last_updated": None,
        "source": "cache_not_ready",
    }


@app.get("/api/crypto/trades")
def get_crypto_trades():
    """Returns the full crypto trade history from the crypto_trades table, grouped into OPEN and CLOSED."""
    try:
        from collections import defaultdict, deque
        from datetime import datetime as _dt

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {config.CRYPTO_DB_TABLE} ORDER BY date ASC, id ASC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        pending_buys: dict = defaultdict(deque)
        paired: list = []

        for row in rows:
            ticker = row["ticker"]
            if row["trade_type"] == "CRYPTO_BUY":
                pending_buys[ticker].append(row)
            elif row["trade_type"].startswith("CRYPTO_SELL"):
                if pending_buys[ticker]:
                    buy = pending_buys[ticker].popleft()
                    
                    try:
                        d1 = _dt.strptime(buy["date"].split(" ")[0], "%Y-%m-%d")
                        d2 = _dt.strptime(row["date"].split(" ")[0], "%Y-%m-%d")
                        duration_days = (d2 - d1).days
                    except Exception:
                        duration_days = None

                    paired.append({
                        "status": "CLOSED",
                        "ticker": ticker,
                        "sell_type": row["trade_type"],
                        "entry_date": buy["date"],
                        "exit_date": row["date"],
                        "entry_price": buy["entry_price"],
                        "exit_price": row["exit_price"],
                        "qty": buy["qty"],
                        "notional": buy["notional"],
                        "pnl": row["pnl"],
                        "pnl_pct": row["pnl_pct"],
                        "atr_at_entry": buy.get("atr_at_entry"),
                        "duration_days": duration_days,
                    })

        for ticker, buy_queue in pending_buys.items():
            for buy in buy_queue:
                paired.append({
                    "status": "OPEN",
                    "ticker": ticker,
                    "sell_type": None,
                    "entry_date": buy["date"],
                    "exit_date": None,
                    "entry_price": buy["entry_price"],
                    "exit_price": None,
                    "qty": buy["qty"],
                    "notional": buy["notional"],
                    "pnl": None,
                    "pnl_pct": None,
                    "atr_at_entry": buy.get("atr_at_entry"),
                    "duration_days": None,
                })

        open_trades = sorted(
            [t for t in paired if t["status"] == "OPEN"],
            key=lambda x: x["entry_date"],
            reverse=True,
        )
        closed_trades = sorted(
            [t for t in paired if t["status"] == "CLOSED"],
            key=lambda x: x["exit_date"],
            reverse=True,
        )
        return open_trades + closed_trades

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/crypto/metrics")
def get_crypto_metrics():
    """Returns isolated performance metrics for the crypto portfolio."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                COUNT(*) as total_trades,
                SUM(pnl) as total_pnl,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades
            FROM {config.CRYPTO_DB_TABLE}
            WHERE trade_type LIKE 'CRYPTO_SELL%' AND pnl IS NOT NULL
            """
        )
        row = dict(cur.fetchone())
        conn.close()
        total = row.get("total_trades") or 0
        wins = row.get("winning_trades") or 0
        total_pnl = row.get("total_pnl") or 0.0
        return {
            "total_pnl": round(total_pnl, 4),
            "win_rate": round(wins / total * 100, 2) if total > 0 else 0.0,
            "winning_trades": wins,
            "losing_trades": row.get("losing_trades") or 0,
            "total_trades": total,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/crypto/chart/{symbol}")
def get_crypto_chart_data(symbol: str, entry_price: float = None):
    """
    Returns historical daily bars for a crypto pair to plot on the chart.

    symbol   : e.g. 'BTC' or 'BTC/USD' — normalised internally.
    entry_price (optional): if provided, TP (+ATR*2) and SL (-ATR*1) levels
                            are estimated using the crypto ATR ratio from config.

    Response: { bars: [...], levels: {...} | null }
    """
    API_KEY = os.getenv("ALPACA_API_KEY")
    SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

    if not (API_KEY and SECRET_KEY):
        raise HTTPException(status_code=500, detail="Alpaca keys not configured in .env")

    # Normalise symbol: accept both 'BTC' and 'BTC/USD'
    clean = symbol.upper().replace("/USD", "").replace("USD", "").strip()
    alpaca_sym = f"{clean}/USD"

    try:
        from alpaca.data.historical.crypto import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timedelta

        client = CryptoHistoricalDataClient()  # no auth needed for crypto data
        request = CryptoBarsRequest(
            symbol_or_symbols=alpaca_sym,
            timeframe=TimeFrame.Day,
            start=datetime.utcnow() - timedelta(days=120),
            end=datetime.utcnow(),
        )
        bars_df = client.get_crypto_bars(request).df

        if bars_df is None or bars_df.empty:
            raise HTTPException(status_code=404, detail=f"No data for {alpaca_sym}")

        # Multi-index: (symbol, timestamp) — reset to get flat index
        if hasattr(bars_df.index, "levels"):
            bars_df = bars_df.xs(alpaca_sym, level=0)

        bars = []
        for ts, row in bars_df.iterrows():
            date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            bars.append({
                "time": date_str,
                "open":  round(float(row["open"]),  4),
                "high":  round(float(row["high"]),  4),
                "low":   round(float(row["low"]),   4),
                "close": round(float(row["close"]), 4),
            })

        # Deduplicate by date (keep last bar per day)
        seen = {}
        for b in bars:
            seen[b["time"]] = b
        bars = sorted(seen.values(), key=lambda x: x["time"])

        # Optional TP / SL levels
        levels = None
        if entry_price and entry_price > 0:
            # Use ATR-based multipliers from config if available, else sensible defaults
            tp_mult = getattr(config, "CRYPTO_ATR_TP_MULT", 2.0)
            sl_mult = getattr(config, "CRYPTO_ATR_SL_MULT", 1.0)
            # Estimate ATR as ~3 % of price (crypto rule-of-thumb) when real ATR unavailable
            atr_est = entry_price * 0.03
            levels = {
                "entry": round(entry_price, 4),
                "tp": round(entry_price + tp_mult * atr_est, 4),
                "sl": round(entry_price - sl_mult * atr_est, 4),
                "tp_pct": tp_mult * 3,
                "sl_pct": sl_mult * 3,
            }

        return {"bars": bars, "levels": levels}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("dashboard_app:app", host="0.0.0.0", port=port)
