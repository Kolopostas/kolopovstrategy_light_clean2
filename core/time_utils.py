from datetime import datetime, timezone
from typing import Tuple

import requests


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def get_bybit_server_time() -> int:
    # Public endpoint; works without auth
    r = requests.get("https://api.bybit.com/v5/market/time", timeout=10)
    r.raise_for_status()
    data = r.json()
    return int(data.get("result", {}).get("timeSecond", 0))


def compare_bybit_time() -> Tuple[float, int]:
    server_sec = get_bybit_server_time()
    server_dt = datetime.fromtimestamp(server_sec, tz=timezone.utc)
    delta = abs((server_dt - now_utc()).total_seconds())
    return delta, server_sec
