from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from core.bybit_exchange import create_exchange, normalize_symbol
from core.market_info import adjust_qty_price
from core.trade_log import append_trade_event
from core.indicators import atr_latest_from_ohlcv


def _calc_order_qty(balance_usdt: float, price: float, risk_fraction: float, leverage: int) -> float:
    """
    Deprecated helper (не используется с ATR‑риском).

    Оставлена для совместимости. Сейчас размер позиции рассчитывается
    через ATR и процент риска в open_position().
    """
    notional = max(0.0, balance_usdt) * max(0.0, risk_fraction) * max(1, leverage)
    return (notional / price) if price > 1e-12 else 0.0


def _wait_fill(ex, sym: str, order_id: str, timeout_s: int = 8) -> Dict[str, Any]:
    """
    Ожидание исполнения ордера best‑effort: опрос раз в 0.5с до timeout_s.
    Возвращает последний известный статус ордера.
    """
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout_s:
        try:
            o = ex.fetch_order(order_id, sym)
            last = o or last
            st = (o.get("status") or "").lower()
            if st in ("closed", "canceled", "rejected"):
                return o
        except Exception:
            # игнорируем разовые сбои сети/таймауты
            pass
        time.sleep(0.5)
    return last


def open_position(symbol: str, side: str, price: Optional[float] = None) -> Dict[str, Any]:
    """
    MARKET‑ордер с TP/SL и ATR‑расчётом. Игнорирует 'leverage not modified' (110043),
    помечает 10001 как retryable. Логирует: order_placed / order_filled / order_error.
    DRY_RUN=1 — не отправляет ордера.
    """
    # DRY mode: ничего не отправляем
    if os.getenv("DRY_RUN", "").strip() == "1":
        return {"status": "dry", "reason": "DRY_RUN=1", "symbol": symbol, "side": side}

    ex = create_exchange()
    sym = normalize_symbol(symbol)

    # Баланс
    bal = ex.fetch_balance()
    usdt = float(bal.get("USDT", {}).get("free", 0.0) or 0.0)

    # Цена (если не передали)
    if price is None:
        t = ex.fetch_ticker(sym)
        price = float(t.get("last") or t.get("close") or 0.0)

    # Округляем цену входа по тик‑шагу
    px = float(ex.price_to_precision(sym, price))

    # Сторона ордера
    order_side = "buy" if side.lower() == "long" else "sell"

    # Устанавливаем плечо (110043 = already set — не считается ошибкой)
    leverage = int(os.getenv("LEVERAGE", "3"))
    try:
        ex.set_leverage(leverage, sym)
    except Exception as e:
        if "110043" not in str(e):
            print("⚠️ set_leverage:", e)

    # --- ATR‑базированный риск ---
    tf = os.getenv("TIMEFRAME", "5m")
    atr_period = int(os.getenv("ATR_PERIOD", "14"))
    sl_mult = float(os.getenv("SL_ATR_MULT", "1.8"))
    tp_mult = float(os.getenv("TP_ATR_MULT", "2.2"))
    risk_pct = float(os.getenv("RISK_PCT", "0.007"))  # 0.7% от депозита

    # Получаем ATR для стоп‑дистанции
    ohlcv = ex.fetch_ohlcv(sym, timeframe=tf, limit=max(atr_period + 1, 200))
    atr, _last_close = atr_latest_from_ohlcv(ohlcv, period=atr_period)

    # Дистанция SL от точки входа
    stop_dist = max(atr * sl_mult, 1e-9)

    # Размер позиции (в базовой валюте) = риск/стоп‑дистанция
    risk_usdt = max(1e-6, usdt * risk_pct)
    qty_raw = risk_usdt / stop_dist if stop_dist > 0 else 0.0

    # Корректируем количество и цену под шаг лота/тик
    qty, px, _ = adjust_qty_price(sym, qty_raw, px)
    if qty <= 0:
        return {
            "status": "error",
            "reason": "qty<=0 after adjust",
            "balance": usdt,
            "qty_raw": qty_raw,
        }

    # TP/SL по ATR
    if order_side == "buy":
        sl_price = float(ex.price_to_precision(sym, px - stop_dist))
        tp_price = float(ex.price_to_precision(sym, px + tp_mult * atr))
    else:
        sl_price = float(ex.price_to_precision(sym, px + stop_dist))
        tp_price = float(ex.price_to_precision(sym, px - tp_mult * atr))

    params = {"takeProfit": tp_price, "stopLoss": sl_price}

    # Отладка
    print(
        "🔎 DEBUG ORDER:",
        {
            "symbol": sym,
            "side": order_side,
            "qty_raw": qty_raw,
            "qty": qty,
            "entry_price": px,
            "TP": tp_price,
            "SL": sl_price,
            "lev": leverage,
        },
    )

    try:
        # Размещение
        o = ex.create_order(sym, type="market", side=order_side, amount=qty, price=None, params=params)

        # Лог: размещён
        try:
            append_trade_event(
                {
                    "ts": time.time(),
                    "event": "order_placed",
                    "symbol": sym,
                    "side": order_side,
                    "qty": qty,
                    "price": px,
                    "tp": tp_price,
                    "sl": sl_price,
                    "order_id": o.get("id") or o.get("orderId"),
                    "link_id": o.get("clientOrderId")
                    or o.get("orderLinkId")
                    or (o.get("info", {}) or {}).get("orderLinkId"),
                    "mode": "LIVE",
                }
            )
        except Exception as _e:
            print("[WARN] trade-log placed:", _e)

        # Дождаться исполнения (best‑effort)
        oid = o.get("id") or o.get("orderId")
        if oid:
            o = _wait_fill(ex, sym, oid)

        # Успех
        return {
            "status": (o.get("status") or "unknown"),
            "order": o,
            "qty": qty,
            "price": px,
            "tp": tp_price,
            "sl": sl_price,
            "balance": usdt,
        }

    except Exception as e:
        msg = str(e)

        # Лог: ошибка
        try:
            append_trade_event(
                {
                    "ts": time.time(),
                    "event": "order_error",
                    "symbol": sym,
                    "side": order_side,
                    "qty": qty,
                    "price": px,
                    "tp": tp_price,
                    "sl": sl_price,
                    "order_id": None,
                    "link_id": None,
                    "mode": "LIVE",
                    "extra": msg,
                }
            )
        except Exception as _e:
            print("[WARN] trade-log error:", _e)

        # Коды Bybit
        if "10001" in msg:
            return {
                "status": "retryable",
                "reason": "10001 invalid request",
                "error": msg,
            }
        if "110043" in msg:
            return {
                "status": "ok_with_warning",
                "warning": "110043 leverage not modified",
                "qty": qty,
            }

        return {"status": "error", "error": msg, "qty": qty, "price": px}
