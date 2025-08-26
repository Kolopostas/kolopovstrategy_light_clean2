from typing import Dict, List, Tuple

from .bybit_exchange import create_exchange, normalize_symbol


def get_balance(asset: str = "USDT") -> float:
    ex = create_exchange()
    bal = ex.fetch_balance()
    return float(bal.get(asset, {}).get("free", 0.0) or 0.0)


def get_symbol_price(symbol: str) -> float:
    ex = create_exchange()
    sym = normalize_symbol(symbol)
    t = ex.fetch_ticker(sym)
    return float(t.get("last") or t.get("close") or 0.0)


def adjust_qty_price(
    symbol: str, qty: float, price: float
) -> Tuple[float, float, Dict]:
    """Коррекция qty/price под биржевые шаги и минимальные требования (min amount / min cost)."""
    ex = create_exchange()
    sym = normalize_symbol(symbol)
    market = ex.market(sym)

    qty_adj = float(ex.amount_to_precision(sym, qty))
    price_adj = float(ex.price_to_precision(sym, price))

    min_amount = market.get("limits", {}).get("amount", {}).get("min")
    min_cost = market.get("limits", {}).get("cost", {}).get("min")

    need_qty = qty_adj
    if min_amount:
        need_qty = max(need_qty, float(min_amount))
    if min_cost:
        need_qty = max(need_qty, float(min_cost) / max(price_adj, 1e-12))

    if need_qty > qty_adj:
        qty_adj = float(ex.amount_to_precision(sym, need_qty))
        if qty_adj < need_qty:  # страховка от float
            qty_adj = float(ex.amount_to_precision(sym, need_qty * 1.0000001))

    return qty_adj, price_adj, market


# ======== ДОБАВЛЕНО: проверки ордеров/позиций ========


def get_open_orders(symbol: str) -> List[Dict]:
    """Список открытых ордеров по символу (не исполнены/не отменены)."""
    ex = create_exchange()
    sym = normalize_symbol(symbol)
    try:
        return ex.fetch_open_orders(sym)
    except Exception:
        return []


def cancel_open_orders(symbol: str) -> int:
    """Отменяет ВСЕ открытые ордера по символу. Возвращает число отменённых."""
    ex = create_exchange()
    sym = normalize_symbol(symbol)
    try:
        opened = ex.fetch_open_orders(sym)
        for o in opened:
            try:
                ex.cancel_order(o["id"], sym)
            except Exception as e:
                # Не глушим: фиксируем и идём дальше
                print(f"[WARN] cancel_order failed symbol={sym} id={o.get('id')}: {e}")
        return len(opened)
    except Exception as e:
        print(f"[ERROR] fetch_open_orders failed symbol={sym}: {e}")
        return 0


def has_open_position(symbol: str) -> bool:
    """Есть ли нетто‑позиция по символу (size != 0)."""
    ex = create_exchange()
    sym = normalize_symbol(symbol)
    try:
        poss = ex.fetch_positions([sym])
        for p in poss:
            # Bybit/ccxt: contracts / size / info
            size = p.get("contracts") or p.get("size") or 0
            try:
                size = float(size)
            except Exception:
                size = 0.0
            if abs(size) > 0:
                return True
        return False
    except Exception:
        return False
