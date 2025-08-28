from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("trailing_stop")

# Не критично, но пусть импорт будет безопасным
try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

# Базовая задержка, чтобы не ловить 10006/429
_RATE_DELAY = float(os.getenv("BYBIT_RATE_LIMIT_DELAY", "0.4"))  # ~3 rps


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _market_id(exchange, unified_symbol: str) -> str:
    """
    Преобразует унифицированный символ CCXT (например 'BTC/USDT:USDT')
    в биржевой id Bybit v5 (например 'BTCUSDT').
    """
    exchange.load_markets(reload=False)
    m = exchange.market(unified_symbol)
    return m["id"]

# core/trailing_stop.py  [ADD near helpers]
def _position_idx_for_side(side: str | None) -> int:
    """Bybit v5: 1 = Long, 2 = Short (для линейных контрактов, tpslMode=Full)."""
    s = (side or "").lower()
    return 1 if s in ("long", "buy") else 2


def _assert_ok(resp: Dict[str, Any]) -> None:
    """
    Бросаем исключение, если Bybit вернул ошибку.
    retCode=110043 ("not modified") трактуем как OK с предупреждением.
    """
    rc = resp.get("retCode")
    if rc in (0, "0", None):
        return
    if str(rc) == "110043":
        logger.warning("Bybit retCode=110043 (not modified) — считаем как OK")
        return
    raise RuntimeError(
        f"Bybit error retCode={rc}, retMsg={resp.get('retMsg')}, result={resp.get('result')}"
    )


def _backoff_sleep(attempt: int) -> None:
    """Экспоненциальный бэкофф, но не больше 2с."""
    delay = min(_RATE_DELAY * (2 ** (attempt - 1)), 2.0)
    time.sleep(delay)

def _dbg(*args, **kwargs):
    """Печататет отладку по трейлингу, если DEBUG_TRAILING=1."""
    try:
        if os.getenv("DEBUG_TRAILING","0") == "1":
            print(*args, **kwargs, flush=True)
    except Exception:
        pass        


def _fetch_ohlcv(
    exchange, symbol: str, timeframe: str, limit: int
) -> List[List[float]]:
    # Формат: [ts, open, high, low, close, volume]
    return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)


def _sma(values: List[float], period: int) -> float:
    n = len(values)
    if period <= 0:
        return 0.0
    if n < period:
        return sum(values) / max(1, n)
    return sum(values[-period:]) / float(period)


# ---------------------------------------------------------------------
# Индикаторы
# ---------------------------------------------------------------------
def compute_atr(
    exchange,
    symbol: str,
    timeframe: str = "5m",
    period: int = 14,
    *,
    limit: int | None = None,
) -> tuple[float, float]:
    """
    Возвращает (atr, last_close).
    TR = max(H-L, |H-C_prev|, |L-C_prev|)
    ATR = SMA(TR, period)
    """
    if limit is None:
        limit = max(period + 1, 100)

    ohlcv = _fetch_ohlcv(exchange, symbol, timeframe, limit)
    if len(ohlcv) < period + 1:
        last_close = float(ohlcv[-1][4]) if ohlcv else 0.0
        return 0.0, last_close

    trs: List[float] = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    atr = _sma(trs, period)
    last_close = float(ohlcv[-1][4])
    return float(atr), last_close


# ---------------------------------------------------------------------
# Низкоуровневые врапперы Bybit v5 (через ccxt)
# ---------------------------------------------------------------------
# core/trailing_stop.py  [REPLACE function set_trailing_stop_ccxt]
def set_trailing_stop_ccxt(
    exchange,
    symbol: str,
    activation_price: float,
    callback_rate: float = 1.0,
    *,
    category: str = "linear",
    tpsl_mode: str = "Full",
    position_idx: int | None = None,  # 0/None(one-way), 1(Long), 2(Short)
    trigger_by: str = "LastPrice",
    max_retries: int = 3,
    side: str | None = None,  # <--- NEW: если задано, переопределим position_idx
) -> Dict[str, Any]:
    """
    POST /v5/position/trading-stop (ccxt: privatePostV5PositionTradingStop)
    Важно: числовые параметры — строками. Для хедж-режима обязательно positionIdx.
    """
    bybit_symbol = _market_id(exchange, symbol)
    # Определим корректный positionIdx по стороне
    if position_idx is None or position_idx == 0:
        position_idx = _position_idx_for_side(side)

    payload = {
        "category": category,
        "symbol": bybit_symbol,
        "tpslMode": tpsl_mode,
        "positionIdx": str(position_idx),
        "trailingStop": f"{callback_rate}",       # строка, % (0.1..5.0)
        "activePrice": f"{activation_price}",     # строка
        "tpOrderType": "Market",
        "slOrderType": "Market",
        "tpTriggerBy": trigger_by,
        "slTriggerBy": trigger_by,
    }

    # Отправка с ретраями / backoff
    for attempt in range(1, max_retries + 1):
        try:
            resp = exchange.private_post_v5_position_trading_stop(payload)
            _assert_ok(resp)
            return resp
        except Exception as e:
            logger.debug("trailing_stop retry %s/%s for %s: %s", attempt, max_retries, payload.get("symbol"), e)
            if attempt >= max_retries:
                raise
            _backoff_sleep(attempt)


def verify_trailing_state(
    exchange, symbol: str, *, category: str = "linear"
) -> Dict[str, Any]:
    """GET /v5/position/list — текущее состояние позиции (есть ли trailingStop/stopLoss)."""
    bybit_symbol = _market_id(exchange, symbol)
    return exchange.privateGetV5PositionList(
        {"category": category, "symbol": bybit_symbol}
    )


def set_stop_loss_only(
    exchange,
    symbol: str,
    stop_price: float,
    *,
    category: str = "linear",
    tpsl_mode: str = "Full",
    position_idx: int | None = None,
    trigger_by: str = "LastPrice",
    side: str | None = None,  # <--- NEW
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Установить только SL для текущей позиции.
    Для правильной стороны используем positionIdx (1=Long, 2=Short).
    """
    bybit_symbol = _market_id(exchange, symbol)
    if position_idx is None or position_idx == 0:
        position_idx = _position_idx_for_side(side)

    payload = {
        "category": category,
        "symbol": bybit_symbol,
        "tpslMode": tpsl_mode,
        "positionIdx": str(position_idx),
        "stopLoss": f"{stop_price}",
        "slOrderType": "Market",
        "slTriggerBy": trigger_by,
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = exchange.private_post_v5_position_trading_stop(payload)
            _assert_ok(resp)
            return resp
        except Exception as e:
            logger.debug("stop_loss_only %s/%s for %s: %s", attempt, max_retries, payload.get("symbol"), e)
            if attempt >= max_retries:
                raise
            _backoff_sleep(attempt)



def move_stop_loss(
    exchange,
    symbol: str,
    new_sl_price: float,
    *,
    category: str = "linear",
    position_idx: int = 0,
    trigger_by: str = "LastPrice",
) -> Dict[str, Any]:
    """Синоним set_stop_loss_only для читаемости."""
    return set_stop_loss_only(
        exchange,
        symbol,
        new_sl_price,
        category=category,
        position_idx=position_idx,
        trigger_by=trigger_by,
    )


# ---------------------------------------------------------------------
# Логика активации/параметров трейлинга (ATR/PCT) и брейк-ивен
# ---------------------------------------------------------------------
def compute_trailing_from_atr(
    entry: float,
    side: str,
    atr: float,
    *,
    k_activate: float,
    min_up_pct: float,
    min_down_pct: float,
    cb_from_atr_k: float,
    cb_fixed_pct: float,
    auto_cb: bool,
) -> tuple[float, float]:
    """
    Возвращает (activation_price, callback_rate_pct).
    Long:  entry + max(k*ATR, min_up_pct*entry)
    Short: entry - max(k*ATR, min_down_pct*entry)
    callback_rate либо фиксированный %, либо из ATR: 100 * (cb_from_atr_k * ATR / entry)
    """
    side_l = side.lower()
    if side_l in ("long", "buy"):
        activation_price = entry + max(k_activate * atr, entry * min_up_pct)
    else:
        activation_price = entry - max(k_activate * atr, entry * min_down_pct)

    if auto_cb:
        cb = 100.0 * (cb_from_atr_k * atr / max(entry, 1e-12))
        cb = float(max(0.1, min(cb, 5.0)))  # лимиты Bybit: 0.1..5.0 %
    else:
        cb = float(cb_fixed_pct)

    return float(activation_price), cb


def maybe_breakeven(
    entry: float,
    side: str,
    last: float,
    atr: float,
    *,
    be_mode: str,
    be_atr_k: float,
    be_trigger_pct: float,
    be_offset_pct: float,
) -> float | None:
    """
    Вернёт целевую цену SL для BE либо None.
    - ATR-режим: как только профит >= be_atr_k*ATR → SL в район entry*(1±offset)
    - %-режим: триггер по проценту от entry (be_trigger_pct)
    """
    side_l = side.lower()
    if be_mode == "atr":
        in_profit = (last - entry) if side_l in ("long", "buy") else (entry - last)
        if in_profit >= be_atr_k * atr:
            return (
                entry * (1.0 + be_offset_pct)
                if side_l in ("long", "buy")
                else entry * (1.0 - be_offset_pct)
            )
    else:
        need = entry * be_trigger_pct
    if (side_l in ("long", "buy") and last >= entry + need) or (
        side_l in ("short", "sell") and last <= entry - need
    ):
        return (
            entry * (1.0 + be_offset_pct)
            if side_l in ("long", "buy")
            else entry * (1.0 - be_offset_pct)
        )
    return None


def update_trailing_for_symbol(
    exchange,
    symbol: str,
    entry_price: float,
    side: str,
    *,
    activation_mode: str | None = None,  # "atr" | "pct"
    atr_timeframe: str | None = None,
    atr_period: int | None = None,
    atr_k: float | None = None,
    up_pct: float | None = None,
    down_pct: float | None = None,
    callback_rate: float | None = None,
    auto_callback: bool | None = None,
    auto_cb_k: float | None = None,
) -> Dict[str, Any]:
    """
    Устанавливает трейлинг-стоп:
      mode="atr":  LONG → entry + K*ATR ; SHORT → entry - K*ATR
      mode="pct":  LONG → entry*(1+up_pct) ; SHORT → entry*(1-down_pct)
    Все параметры можно задать через .env.
    """
    activation_mode = (
        activation_mode or os.getenv("TS_ACTIVATION_MODE", "atr")).lower()

    # Параметры ATR/процентов
    atr_timeframe = atr_timeframe or os.getenv("ATR_TIMEFRAME", "5m")
    atr_period = int(atr_period or int(os.getenv("ATR_PERIOD", "14")))
    atr_k = float(atr_k or float(os.getenv("TS_ACTIVATION_ATR_K", "1.0")))

    up_pct = (
        float(os.getenv("TS_ACTIVATION_UP_PCT", "0.003"))
        if up_pct is None
        else float(up_pct)
    )
    down_pct = (
        float(os.getenv("TS_ACTIVATION_DOWN_PCT", "0.003"))
        if down_pct is None
        else float(down_pct)
    )
    min_up_pct = float(os.getenv("TS_ACTIVATION_MIN_UP_PCT", "0.001"))
    min_dn_pct = float(os.getenv("TS_ACTIVATION_MIN_DOWN_PCT", "0.001"))

    auto_callback = (
        bool(int(os.getenv("TS_CALLBACK_RATE_AUTO", "0")))
        if auto_callback is None
        else bool(auto_callback)
    )
    auto_cb_k = (
        float(os.getenv("TS_CALLBACK_RATE_ATR_K", "0.75"))
        if auto_cb_k is None
        else float(auto_cb_k)
    )
    callback_rate = (
        float(os.getenv("TS_CALLBACK_RATE", "1.0"))
        if callback_rate is None
        else float(callback_rate)
    )

    side_l = (side or "").lower()

   # --- Расчёт активации и шага ---
    active = None
    cb_pct = None

    if activation_mode == "atr":
        atr, _ = compute_atr(exchange, symbol, atr_timeframe, atr_period)
        if atr > 0.0:
            active, cb_pct = compute_trailing_from_atr(
                entry_price,
                side_l,
                atr,
                k_activate=atr_k,
                min_up_pct=min_up_pct,
                min_down_pct=min_dn_pct,
                cb_from_atr_k=auto_cb_k,
                cb_fixed_pct=callback_rate,
                auto_cb=auto_callback,
            )
        else:
            # Фолбэк на процентовый режим, если ATR=0
            activation_mode = "pct"

    if activation_mode != "atr":
        if side_l in ("long", "buy"):
            active = entry_price * (1.0 + max(min_up_pct, up_pct))
        else:
            active = entry_price * (1.0 - max(min_dn_pct, down_pct))
        cb_pct = callback_rate

    # Отладка расчёта (до округления)
    try:
        print(
            "[TS_CALC]",
            {
                "symbol": symbol,
                "mode": activation_mode,
                "entry": float(entry_price),
                "side": side_l,
                "atr_period": atr_period,
                "atr_tf": atr_timeframe,
                "activation_raw": float(active),
                "callback_pct_raw": float(cb_pct),
            },
            flush=True,
        )
    except Exception:
        pass

    # --- Подгон к шагу цены ---
    try:
        active_precise = float(exchange.price_to_precision(symbol, active))
    except Exception:
        active_precise = float(active)

    # --- Кламп шага трейлинга (Bybit: 0.1..5.0 %) ---
    try:
        cb_pct = max(0.1, min(float(cb_pct), 5.0))
    except Exception:
        cb_pct = 1.0

    # Отладочный снимок конечных параметров
    try:
        atr_dbg = None
        if activation_mode == "atr":
            atr_dbg, _ = compute_atr(exchange, symbol, atr_timeframe, atr_period)
        print(
            "[TS_PARAMS]",
            {
                "symbol": symbol,
                "mode": activation_mode,
                "entry": float(entry_price),
                "atr": float(atr_dbg) if atr_dbg is not None else None,
                "activePrice": float(active_precise),
                "callback_pct": float(cb_pct),
            },
            flush=True,
        )
    except Exception as _e:
        print("[TS_PARAMS_ERR]", _e, flush=True)

    # --- Установка трейлинга ---
    return set_trailing_stop_ccxt(
        exchange=exchange,
        symbol=symbol,
        activation_price=active_precise,
        callback_rate=cb_pct,
        category="linear",
        tpsl_mode="Full",
        position_idx=None,
        trigger_by="LastPrice",
        side=side
    )