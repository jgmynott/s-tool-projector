"""
Smoke-test the Alpaca paper-trading connection. Hits /account, /positions,
and /clock — no orders submitted. Run before trusting any trade-execution
code path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def load_env() -> dict[str, str]:
    """Read repo-root .env into a dict. We don't depend on python-dotenv
    here because broker plumbing should have minimal install surface."""
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def alpaca_get(path: str, env: dict[str, str]) -> dict:
    base = env.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2").rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    req = Request(url, headers={
        "APCA-API-KEY-ID": env["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": env["ALPACA_API_SECRET"],
    })
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main() -> int:
    env = {**os.environ, **load_env()}
    if not env.get("ALPACA_API_KEY"):
        print("ALPACA_API_KEY not set in .env or env vars", file=sys.stderr)
        return 1

    print("=== Alpaca paper account smoke-test ===")
    try:
        clock = alpaca_get("clock", env)
        print(f"  market.is_open={clock.get('is_open')}, "
              f"next_open={clock.get('next_open')}, next_close={clock.get('next_close')}")

        acct = alpaca_get("account", env)
        print(f"\nAccount:")
        for k in ("status", "currency", "cash", "equity", "buying_power",
                  "daytrading_buying_power", "regt_buying_power",
                  "multiplier", "pattern_day_trader", "trading_blocked",
                  "daytrade_count"):
            if k in acct:
                print(f"  {k:30s} = {acct[k]}")

        pos = alpaca_get("positions", env)
        print(f"\nOpen positions: {len(pos)}")
        for p in pos[:10]:
            print(f"  {p['symbol']:6s} qty={p['qty']:>8} side={p['side']:>5} "
                  f"avg={p['avg_entry_price']:>10} mkt={p['market_value']:>12} "
                  f"upnl={p['unrealized_pl']:>10}")

        # Pull a recent equity-curve point to confirm portfolio history works.
        hist = alpaca_get("account/portfolio/history?period=1W&timeframe=1D", env)
        eq = hist.get("equity") or []
        ts = hist.get("timestamp") or []
        if eq:
            print(f"\nEquity curve (last 5 points):")
            for t, e in list(zip(ts, eq))[-5:]:
                print(f"  ts={t} equity={e}")
        return 0
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"HTTP {e.code} on {e.url}: {body}", file=sys.stderr)
        return 2
    except (URLError, KeyError) as e:
        print(f"connection error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
