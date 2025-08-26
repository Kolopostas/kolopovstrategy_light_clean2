import os
from typing import Any, Dict, Iterable, Optional

from dotenv import load_dotenv


def load_and_check_env(required_keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    load_dotenv()
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if proxy_url:
        os.environ.setdefault("HTTP_PROXY", proxy_url)
        os.environ.setdefault("HTTPS_PROXY", proxy_url)

    if required_keys:
        missing = [k for k in required_keys if not os.getenv(k)]
        if missing:
            raise ValueError(f"Missing required env keys: {', '.join(missing)}")

    cfg = {
        "API_KEY": os.getenv("BYBIT_API_KEY", ""),
        "API_SECRET": os.getenv("BYBIT_SECRET_KEY", ""),
        "DOMAIN": os.getenv("DOMAIN", "bybit"),
        "PROXY_URL": proxy_url,
        "PAIRS": [
            p.strip()
            for p in os.getenv("PAIRS", os.getenv("PAIR", "TON/USDT")).split(",")
            if p.strip()
        ],
        "LEVERAGE": int(os.getenv("LEVERAGE", "3")),
        "AMOUNT": float(os.getenv("AMOUNT", "5")),
        "RISK_FRACTION": float(os.getenv("RISK_FRACTION", "0.05")),
        "RECV_WINDOW": int(os.getenv("RECV_WINDOW", "15000")),
        "DRY_RUN": os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes"),
    }
    return cfg


def normalize_symbol(pair: str) -> str:
    return pair.replace("/", "").upper()
