# core/predict.py
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from .bybit_exchange import create_exchange, normalize_symbol


def pair_key(symbol: str) -> str:
    return normalize_symbol(symbol).upper().replace("/", "").replace(":USDT", "")


def _fetch_ohlcv(
    symbol: str, timeframe: str = "15m", limit: int = 2000
) -> pd.DataFrame:
    ex = create_exchange()
    sym = normalize_symbol(symbol)
    raw = ex.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(
        raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain_ema = (
        pd.Series(gain, index=series.index).ewm(alpha=1 / period, adjust=False).mean()
    )
    loss_ema = (
        pd.Series(loss, index=series.index).ewm(alpha=1 / period, adjust=False).mean()
    )
    rs = gain_ema / (loss_ema + 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Возвращает macd, signal, hist."""
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd = ema_fast - ema_slow
    sig = _ema(macd, signal)
    hist = macd - sig
    return macd, sig, hist


def train_model_for_pair(
    symbol: str, timeframe: str = "5m", limit: int = 3000, model_dir: str = "models"
) -> float:
    df = _fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if df.empty or len(df) < 200:
        raise RuntimeError(f"Недостаточно данных для {symbol}")

    df["ema"] = df["close"].ewm(span=50, adjust=False).mean()
    df["rsi"] = compute_rsi(df["close"], period=14)
    macd, sig, _hist = compute_macd(df["close"])
    df["macd"] = macd
    df["signal"] = sig
    df = df.dropna().reset_index(drop=True)

    future = df["close"].shift(-1)
    y = (future > df["close"]).astype(int).values[:-1]
    X = df[["close", "ema", "rsi", "macd", "signal"]].values[:-1].astype(float)
    if len(X) != len(y):
        raise ValueError(f"Feature/label length mismatch: X={len(X)} vs y={len(y)}")

    split = int(len(X) * 0.8)
    Xtr, Ytr = X[:split], y[:split]
    Xte, Yte = X[split:], y[split:]

    model = XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        objective="binary:logistic",
        n_jobs=2,
        random_state=42,
    )
    model.fit(Xtr, Ytr)
    acc = float((model.predict(Xte) == Yte).mean()) if len(Yte) else 0.0

    Path(model_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(model_dir) / f"model_{pair_key(symbol)}.pkl"
    joblib.dump(model, out_path)
    print(f"✅ {normalize_symbol(symbol)} trained, val_acc={acc:.3f} → {out_path}")
    return acc


def train_many(pairs, timeframe="5m", limit=3000, model_dir="models"):
    for p in pairs:
        try:
            train_model_for_pair(
                p, timeframe=timeframe, limit=limit, model_dir=model_dir
            )
        except Exception as e:
            print(f"⚠️ {p}: {e}")


def predict_trend(
    symbol: str, timeframe: Optional[str] = None, limit: int = 500
) -> Dict[str, Any]:
    tf = timeframe or os.getenv("TIMEFRAME", "5m")
    model_path = (
        Path(os.getenv("MODEL_DIR", "models")) / f"model_{pair_key(symbol)}.pkl"
    )
    if not model_path.exists():
        return {
            "signal": "hold",
            "confidence": 0.0,
            "proba": {"LONG": 0.0, "SHORT": 0.0},
        }

    model = joblib.load(model_path)
    df = _fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    if df.empty:
        return {
            "signal": "hold",
            "confidence": 0.0,
            "proba": {"LONG": 0.0, "SHORT": 0.0},
        }

    df["ema"] = df["close"].ewm(span=50, adjust=False).mean()
    df["rsi"] = compute_rsi(df["close"], period=14)
    macd, sig, _hist = compute_macd(df["close"])
    df["macd"] = macd
    df["signal"] = sig
    df = df.dropna().reset_index(drop=True)

    feats = df[["close", "ema", "rsi", "macd", "signal"]].values[-1:].astype(float)
    try:
        proba = model.predict_proba(feats)[0]
        p_short, p_long = float(proba[0]), float(proba[1])  # [SHORT, LONG]
        signal = "long" if p_long >= p_short else "short"
        conf = max(p_long, p_short)
        return {
            "signal": signal,
            "confidence": conf,
            "proba": {"LONG": p_long, "SHORT": p_short},
        }
    except Exception:
        last_close = float(df["close"].iloc[-1])
        last_ema = float(df["ema"].iloc[-1])
        signal = "long" if last_close > last_ema else "short"
        return {
            "signal": signal,
            "confidence": 0.6,
            "proba": {
                "LONG": 0.6 if signal == "long" else 0.4,
                "SHORT": 0.4 if signal == "long" else 0.6,
            },
        }


# ---- indicators & filters (без дубликатов) ----


def compute_atr(df: pd.DataFrame, period=14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def get_recent_atr(ex, symbol: str, timeframe="1h", period=14, limit=None) -> float:
    limit = limit or (period * 3 + 2)
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
    atr = compute_atr(df[["open", "high", "low", "close"]], period)
    return float(atr.iloc[-1])


def entry_filter_confirm(
    ex,
    symbol: str,
    side: str,
    timeframe="1h",
    rsi_thr_long=55,
    rsi_thr_short=45,
    regime_ema=200,
):
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=max(regime_ema, 260))
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
    close = df["close"]
    ema50 = _ema(close, 50).iloc[-1]
    ema200_now = _ema(close, regime_ema).iloc[-1]
    ema200_prev = _ema(close, regime_ema).iloc[-2]
    rsi = compute_rsi(close, 14).iloc[-1]
    macd, sig, hist = compute_macd(close)
    macd_hist = float(hist.iloc[-1])
    px = float(close.iloc[-1])

    regime_long = px > ema200_now and ema200_now > ema200_prev
    regime_short = px < ema200_now and ema200_now < ema200_prev

    ok_long = (rsi > rsi_thr_long) and (px > ema50) and (macd_hist > 0) and regime_long
    ok_short = (
        (rsi < rsi_thr_short) and (px < ema50) and (macd_hist < 0) and regime_short
    )

    ok = ok_long if side.lower() == "long" else ok_short
    return bool(ok), {
        "price": px,
        "rsi": float(rsi),
        "ema50": float(ema50),
        "ema200": float(ema200_now),
        "macd_hist": macd_hist,
        "regime_ok": regime_long if side.lower() == "long" else regime_short,
    }
