"""
SOL's price in USD (Coinbase's spot price), to turn models' USD prices into lamports.
"""

import json
import threading
import time
from urllib.request import Request, urlopen

SPOT = "https://api.coinbase.com/v2/prices/SOL-USD/spot"
REFRESH = 60  # seconds a price is reused

usd, fetched = None, 0
lock = threading.Lock()


def sol_usd():
    """USD per SOL: refreshed every REFRESH seconds, the last one if Coinbase is unreachable, or None."""
    global usd, fetched
    with lock:
        if time.time() - fetched > REFRESH:
            try:
                with urlopen(Request(SPOT, headers={"User-Agent": "sinference"}), timeout=10) as response:
                    usd = float(json.load(response)["data"]["amount"])
                fetched = time.time()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"Couldn't get SOL's price ({exc}); using the last one ({usd}).", flush=True)
        return usd
