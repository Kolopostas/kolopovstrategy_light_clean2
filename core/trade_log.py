# core/trade_log.py
import csv
import os
import time
from pathlib import Path
from typing import Dict

LOG_PATH = Path(os.getenv("TRADE_LOG_PATH", "logs/trades.csv"))
LOG_TO_STDOUT = (
    os.getenv("LOG_TO_STDOUT", "1") != "0"
)  # по умолчанию печатаем в логи Railway

FIELDS = [
    "ts",
    "event",
    "symbol",
    "side",
    "qty",
    "price",
    "sl",
    "tp",
    "order_id",
    "link_id",
    "mode",
    "extra",
]


def append_trade_event(row: Dict) -> None:
    # подготовка CSV
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not LOG_PATH.exists()

    # значения по умолчанию
    row = dict(row)
    row.setdefault("ts", time.time())
    row.setdefault("extra", "")
    row.setdefault("tp", "")
    row.setdefault("sl", "")
    row.setdefault("order_id", "")
    row.setdefault("link_id", "")
    row.setdefault("mode", "LIVE")

    # запись в CSV
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})
        f.flush()

    # печать в stdout (Railway logs)
    if LOG_TO_STDOUT:
        print(
            f"[TRADE] event={row.get('event')} "
            f"sym={row.get('symbol')} side={row.get('side')} qty={row.get('qty')} "
            f"px={row.get('price')} sl={row.get('sl')} tp={row.get('tp')} "
            f"order_id={row.get('order_id')} link_id={row.get('link_id')} mode={row.get('mode')}"
        )
