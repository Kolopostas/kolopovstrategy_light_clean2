import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone

from core.bybit_exchange import create_exchange, normalize_symbol
from core.env_loader import load_and_check_env
from core.indicators import compute_snapshot
from core.market_info import (
    cancel_open_orders,
    get_balance,
    get_open_orders,
    get_symbol_price,
    has_open_position,
)
from core.predict import predict_trend, train_model_for_pair
from core.trailing_stop import (
    compute_atr,
    set_stop_loss_only,
    update_trailing_for_symbol,
    verify_trailing_state,
)
from position_manager import open_position

# --- Маяк старта и принудительная небеферизация ---
try:
    # Гарантируем небеферизованный stdout в любом окружении
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    # Локальный файл логов (на Railway тоже полезно)
    Path("logs").mkdir(exist_ok=True)
    with open("logs/boot.log", "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] BOOT: positions_guard.py loaded, cwd={os.getcwd()}\n")
    print("BOOT: positions_guard loaded", flush=True)
except Exception:
    pass


# --- Heartbeat в главном цикле: добавь вспомогательную функцию ---
_last_hb = 0.0
def _heartbeat(msg: str = "HB"):
    """Периодически печатает хартбит, чтобы в Railway были живые логи."""
    global _last_hb
    now = time.time()
    if now - _last_hb >= 15:  # каждые ~15 секунд
        _last_hb = now
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)
        try:
            with open("logs/boot.log", "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

        # --- Режим рынка: фильтр от «пилы» перед входом ---
def _regime_ok(symbol: str, timeframe: str) -> bool:
    """
    Возвращает True, если рынок «здоровый» для входа:
    - ширина BB >= порога,
    - наклон EMA50 достаточный (тренд/импульс),
    - RSI не в «нейтральной» серой зоне.
    """
    try:
        snap = compute_snapshot(symbol, timeframe=timeframe, limit=300)
        bb_width = float(snap.get("bb_width", 0.0) or 0.0)
        ema12 = float(snap.get("ema12", 0.0) or 0.0)
        ema26 = float(snap.get("ema26", 0.0) or 0.0)
        rsi14 = float(snap.get("rsi14", 50.0) or 50.0)

        # Порог ширины BB
        bb_min = float(os.getenv("REGIME_BB_WIDTH_MIN", "0.012"))  # 1.2%
        if bb_width < bb_min:
            return False

        # Наклон EMA50 примерно через ema12-ema26 (проксирует динамику)
        ema_slope_min = float(os.getenv("REGIME_EMA_SLOPE_MIN", "0.0005"))
        base = max(1.0, (ema12 + ema26) / 2.0)
        slope = abs(ema12 - ema26) / base
        if slope < ema_slope_min:
            return False

        # Не торговать в «серой зоне» RSI
        rsi_lo = float(os.getenv("REGIME_RSI_NEUTRAL_LOW", "45"))
        rsi_hi = float(os.getenv("REGIME_RSI_NEUTRAL_HIGH", "55"))
        if rsi_lo <= rsi14 <= rsi_hi:
            return False

        return True
    except Exception as _e:
        print("[REGIME_ERR]", _e)
        # В сомнении лучше НЕ входить
        return False

def _has_trailing(exchange, symbol: str) -> bool:
    """
    Проверяем, установлен ли уже трейлинг по символу. Использует verify_trailing_state().
    """
    try:
        st = verify_trailing_state(exchange, symbol)
        rows = (st.get("result", {}) or {}).get("list") or []
        for r in rows:
            ts = str(r.get("trailingStop") or "").strip()
            if ts not in ("", "0", "0.0", "None"):
                return True
    except Exception:
        pass
    return False

# Память о том, что безубыток уже переведён (по паре и направлению)
_BE_DONE: dict = {}

_PTP_DONE: dict = {}  # key=(symbol, side_l) -> True

def _get_position_info(exchange, symbol: str):
    """
    Возвращает (qty_abs, side_l, entry_price) для открытой позиции по символу.
    side_l: 'long' | 'short' | '' (если позиции нет).
    """
    try:
        positions = exchange.fetch_positions([symbol]) or []
    except Exception:
        positions = []
    qty_abs, side_l, entry_px = 0.0, "", 0.0
    for p in positions:
        if str(p.get("symbol")) != symbol:
            continue
        amt = float(p.get("contracts") or p.get("amount") or 0.0)
        if abs(amt) <= 0:
            continue
        qty_abs = abs(amt)
        side_l = "long" if amt > 0 else "short"
        try:
            entry_px = float(p.get("entryPrice") or p.get("avgPrice") or 0.0)
        except Exception:
            entry_px = 0.0
        break
    return qty_abs, side_l, entry_px

def maybe_partial_take_profit(exchange, symbol: str, entry_px: float, side: str, atr: float) -> None:
    """
    Закрывает reduceOnly частью позиции при достижении 1R (R=ATR*PARTIAL_TP_R_MULT).
    Управляется ENV:
      PARTIAL_TP_ENABLE=1|0
      PARTIAL_TP_PART=0.5       # доля закрытия (0..1)
      PARTIAL_TP_R_MULT=1.0     # во сколько ATR взять R (обычно 1)
      PARTIAL_TP_COOLDOWN_S=0   # минимальный интервал между попытками (сек)
    """
    if os.getenv("PARTIAL_TP_ENABLE", "1") not in ("1", "true", "True"):
        return
    side_l = (side or "").lower()
    if side_l not in ("long", "short"):
        return
    key = (symbol, side_l)
    if _PTP_DONE.get(key):
        return

    part = float(os.getenv("PARTIAL_TP_PART", "0.5"))
    r_mult = float(os.getenv("PARTIAL_TP_R_MULT", "1.0"))

    if atr <= 0 or entry_px <= 0:
        return
    cur = get_symbol_price(symbol)
    passed = (cur >= entry_px + atr * r_mult) if side_l == "long" else (cur <= entry_px - atr * r_mult)
    if not passed:
        return

    qty_abs, _side_now, _entry_now = _get_position_info(exchange, symbol)
    if qty_abs <= 0:
        return
    qty_close = max(0.0, qty_abs * part)
    try:
        qty_close = float(exchange.amount_to_precision(symbol, qty_close))
    except Exception:
        pass
    if qty_close <= 0:
        return

    close_side = "sell" if side_l == "long" else "buy"
    try:
        print("[PTP]", {
            "symbol": symbol, "close_qty": qty_close, "close_side": close_side,
            "cur": float(cur), "entry": float(entry_px), "atr": float(atr), "r_mult": float(r_mult)
        })
        exchange.create_order(
            symbol, type="market", side=close_side, amount=qty_close, params={"reduceOnly": True}
        )
        _PTP_DONE[key] = True
    except Exception as e:
        print("[PTP_ERR]", e)

def _maybe_breakeven(exchange, symbol: str, entry_px: float, side: str) -> None:
    """
    Переносит стоп-лосс в безубыток, если цена прошла достаточное расстояние.
    Правило Bybit: лонг → SL ДОЛЖЕН быть НИЖЕ base_price (≈ entry);
                    шорт → SL ДОЛЖЕН быть ВЫШЕ base_price.
    Эту инварианту обеспечиваем через BE_EPSILON_PCT.
    """
    if os.getenv("ENABLE_BREAKEVEN", "1") != "1":
        return

    sid = (side or "").lower()
    key = (symbol, sid)
    if _BE_DONE.get(key):
        return

    be_mode = os.getenv("BE_MODE", "atr").lower()    # "atr" | "pct"
    be_offset_pct = float(os.getenv("BE_OFFSET_PCT", "0.0005"))  # ваш смещение BE
    eps = float(os.getenv("BE_EPSILON_PCT", "0.0001"))           # ~0.01% страховка

    # Текущая цена
    cur = get_symbol_price(symbol)
    should_move = False

    if be_mode == "atr":
        k = float(os.getenv("BE_ATR_K", "0.5"))
        tf = os.getenv("ATR_TIMEFRAME", "5m")
        per = int(os.getenv("ATR_PERIOD", "14"))
        atr, _ = compute_atr(exchange, symbol, tf, per)
        if atr > 0:
            if sid in ("long", "buy"):
                should_move = cur >= entry_px + k * atr
            else:
                should_move = cur <= entry_px - k * atr
    else:
        trig = float(os.getenv("BE_TRIGGER_PCT", "0.004"))
        if sid in ("long", "buy"):
            should_move = cur >= entry_px * (1 + trig)
        else:
            should_move = cur <= entry_px * (1 - trig)

    if not should_move:
        return

    # --- КОРРЕКТНЫЙ BE ДЛЯ BYBIT (кламп вокруг entry) ---
    try:
        if sid in ("long", "buy"):
            # ваша целевая точка BE (с учётом offset)
            desired = entry_px * (1 + be_offset_pct)
            # но Bybit требует SL < base_price, поэтому клампим ниже entry:
            be_raw = min(desired, entry_px * (1 - eps))
        else:
            desired = entry_px * (1 - be_offset_pct)
            # для шорта SL > base_price:
            be_raw = max(desired, entry_px * (1 + eps))

        be_price = float(exchange.price_to_precision(symbol, be_raw))
    except Exception:
        # Фолбэк: ставим минимально допустимый с эпсилоном от entry
        be_price = (
            entry_px * (1 - eps) if sid in ("long", "buy") else entry_px * (1 + eps)
        )
        try:
            be_price = float(exchange.price_to_precision(symbol, be_price))
        except Exception:
            pass

    print("[BE] move SL to", be_price)
    try:
        set_stop_loss_only(exchange, symbol, be_price, side=sid)
        _BE_DONE[key] = True
    except Exception as e:
        print("[BE_ERR]", e)

def _get_entry_price(exchange, symbol: str) -> float:
        """Возвращает entry price по символу (Bybit v5 через CCXT)."""
        try:
            poss = exchange.fetch_positions([symbol])
            for p in poss or []:
             if (p.get("symbol") or "").upper() == symbol.upper():
                ep = p.get("entryPrice") or p.get("entry_price") or p.get("avgPrice") or 0
                if not ep and isinstance(p.get("info"), dict):
                    info = p["info"]
                    ep = info.get("avgPrice") or info.get("entryPrice") or info.get("entry_price") or 0
                return float(ep or 0)
        except Exception as e:
         print(f"[POS_ERR] fetch_positions {symbol}: {e}")
        return 0.0

def apply_trailing_after_entry(sym: str, signal: str, res: dict, dry_run: bool) -> None:
    """
    Вешает трейлинг-стоп и переводит SL в безубыток сразу после успешного входа.
    Использует update_trailing_for_symbol и _maybe_breakeven().
    """
    if (
        dry_run
        or not isinstance(res, dict)
        or res.get("status") in {"error", "retryable"}
    ):
        print(
            "[TS_SKIP]",
            {
                "dry_run": dry_run,
                "status": res.get("status") if isinstance(res, dict) else "?",
            },
        )
        return

    try:
        entry_px = float(res.get("price") or 0.0)
        if entry_px <= 0:
            try:
                entry_px = get_symbol_price(sym)
            except Exception:
                ex_tmp = create_exchange()
                tkr = ex_tmp.fetch_ticker(sym)
                entry_px = float(tkr.get("last") or tkr.get("close") or 0.0)

        ex_ts = create_exchange()

        if os.getenv("USE_TRAILING_STOP", "1") in ("1", "true", "True"):
            if not _has_trailing(ex_ts, sym):
                print("[TS_CALL]", {"symbol": sym, "entry": entry_px, "side": signal})
                ts_resp = update_trailing_for_symbol(ex_ts, sym, entry_px, signal)
                print("[TS_OK]", ts_resp)
            else:
                print("[TS_SKIP] already has trailing for", sym)

            _maybe_breakeven(ex_ts, sym, entry_px, signal)
        else:
            print("[TS_SKIP] trailing disabled by USE_TRAILING_STOP")
    except Exception as e:
        print("[TS_ERR]", e)


@contextmanager
def single_instance_lock(name: str = "positions_guard.lock"):
    """
    Предохраняет от одновременного запуска нескольких копий скрипта.
    Создаёт файл-замок в /tmp, удаляет его по завершении.
    """
    path = os.path.join(tempfile.gettempdir(), name)
    if os.path.exists(path):
        raise RuntimeError(f"Already running: {path}")
    try:
        open(path, "w").close()
        yield
    finally:
        try:
            os.remove(path)
        except Exception as e:
            print(f"[WARN] lock cleanup failed: {e}")

def ensure_models_exist(pairs, timeframe="15m", limit=2000, model_dir="models"):
    """
    Проверяет наличие моделей ML для всех пар, которые мы торгуем.
    Если модели нет – обучаем с нуля (train_model_for_pair).
    """
    os.makedirs(model_dir, exist_ok=True)
    missing = []
    for p in pairs:
        key = normalize_symbol(p).upper().replace("/", "").replace(":USDT", "")
        mpath = os.path.join(model_dir, f"model_{key}.pkl")
        if not os.path.exists(mpath):
            missing.append(p)
    if missing:
        print(f"🧠 Нет моделей для: {missing} — обучаем...")
        for p in missing:
            try:
                train_model_for_pair(
                    p, timeframe=timeframe, limit=limit, model_dir=model_dir
                )
            except Exception as e:
                print(f"⚠️ {p}: {e}")


def _one_pass(pairs, args, dry_run):
    """Один проход: обслуживание открытых позиций + попытка новых входов по всем парам."""
    ex_loop = create_exchange()  # один клиент на итерацию
    for p in pairs:
        sym = normalize_symbol(p)
        price = get_symbol_price(sym)
        _heartbeat(f"cycle {p}")

        # --- Блок сопровождения: частичный выход 50% на 1R, если есть позиция ---
        try:
            if has_open_position(sym):
                ex_chk = create_exchange()
                atr_val, _ = compute_atr(
                    ex_chk, sym,
                    os.getenv("ATR_TIMEFRAME", "5m"),
                    int(os.getenv("ATR_PERIOD", "14")),    
                )
                qty_abs, side_pos, entry_pos = _get_position_info(ex_chk, sym)
                if qty_abs > 0 and side_pos:
                    maybe_partial_take_profit(ex_chk, sym, entry_pos, side_pos, atr_val)
        except Exception as _e:
            print("[PTP_WRAP_ERR]", _e)            

        # A) Обслуживание уже открытой позиции — восстановить трейл/проверить BE
        try:
            if has_open_position(sym):
                ent = _get_entry_price(ex_loop, sym)
                if ent > 0:
                    if not _has_trailing(ex_loop, sym):
                        print("[TS_RESTORE]", {"symbol": sym, "entry": ent})
                        try:
                            # Подсказка стороны из текущего сигнала (fallback=long)
                            side_hint = "long"
                            try:
                                s = str(predict_trend(sym, timeframe=args.timeframe).get("signal", "long")).lower()
                                if s in ("short", "sell"):
                                    side_hint = "short"
                            except Exception:
                                pass
                            ts_resp = update_trailing_for_symbol(ex_loop, sym, ent, side_hint)
                            print("[TS_OK]", ts_resp)
                        except Exception as e:
                            print("[TS_ERR]", e)
                    # Проверка BE
                    try:
                        side_hint = "long"
                        s2 = str(predict_trend(sym, timeframe=args.timeframe).get("signal", "long")).lower()
                        if s2 in ("short", "sell"):
                            side_hint = "short"
                    except Exception:
                        side_hint = "long"
                    _maybe_breakeven(ex_loop, sym, ent, side_hint)
        except Exception as e:
            print("[MAINTAIN_ERR]", e)

        # B) Открытые ордера?
        opened = get_open_orders(sym)
        if opened:
            print(f"⏳ Есть открытые ордера по {sym}: {len(opened)}")
            if args.auto_cancel:
                n = cancel_open_orders(sym)
                print(f"🧹 Отменил {n} ордер(ов).")
            else:
                print("⏸ Пропускаю вход (запусти с --auto-cancel, чтобы чистить хвосты).")
                continue

        # C) Пирамидинг?
        if args.no_pyramid and has_open_position(sym):
            print(f"🏕 Уже есть позиция по {sym} — пирамидинг выключен (--no-pyramid). Пропуск.")
            continue

        # D) Прогноз → вход
        pred = predict_trend(sym, timeframe=args.timeframe)
        signal = str(pred.get("signal", "hold")).lower()
        conf = float(pred.get("confidence", 0.0))

        if not _regime_ok(sym, timeframe=args.timeframe):
            print("Режим рынка невалиден (BB/EMA/RSI) - пропуск входа.")
            continue
        
        if os.getenv("DEBUG_INDICATORS", "0") == "1":
            try:
                snap = compute_snapshot(sym, timeframe=args.timeframe, limit=max(args.limit, 200))
                print("[IND]", sym, snap)
            except Exception as _e:
                print("[IND_ERR]", _e)

        print(f"🔮 {sym} @ {price:.4f} → signal={signal} conf={conf:.2f} proba={pred.get('proba', {})}")
        if dry_run or signal not in ("long", "short") or conf < args.threshold:
            print("⏸ Условия входа не выполнены (или DRY).")
            continue

        res = open_position(sym, side=signal)
        print("🧾 Результат:", res)
        apply_trailing_after_entry(sym, signal, res, dry_run)


def main():
    print(">>> ENTER main()", flush=True)
    load_and_check_env()
    print(">>> After load_and_check_env()", flush=True)

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Один проход и выход")
    parser.add_argument("--check-interval", type=int, default=int(os.getenv("CHECK_INTERVAL", "30")), help="Пауза между циклами (сек), если не указан --once")
    parser.add_argument("--pair", type=str)
    parser.add_argument("--threshold", type=float, default=float(os.getenv("CONF_THRESHOLD", "0.65")))
    parser.add_argument("--no-lock", action="store_true", help="Запуск без single-instance lock")
    parser.add_argument("--timeframe", type=str, default=os.getenv("TIMEFRAME", "5m"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRAIN_LIMIT", "3000")))
    parser.add_argument("--live", action="store_true", help="Разрешить реальные сделки")
    parser.add_argument("--autotrain", action="store_true", help="Обучить недостающие модели перед стартом",)
    parser.add_argument("--auto-cancel", action="store_true", help="Автоотмена открытых ордеров перед входом",)
    parser.add_argument("--no-pyramid", action="store_true", help="Не входить, если уже есть позиция")
    args = parser.parse_args()
    print(f">>> Pairsed args: {args}", flush=True)
    pairs = [args.pair] if args.pair else [s.strip() for s in os.getenv("PAIRS", "").split(",") if s.strip()]
    print(f">>> Pairs resolved: {pairs}", flush=True)
    if not pairs:
        raise ValueError("PAIRS пуст — заполни в .env")

    min_balance = float(os.getenv("MIN_BALANCE_USDT", "5"))
    dry_run = not args.live

    if dry_run:
        os.environ["DRY_RUN"] = "1"
    else:
        os.environ["DRY_RUN"] = "0"    

    print("──────── Kolopovstrategy guard ────────")
    print("⏱ ", datetime.now(timezone.utc).isoformat())
    print(f"Mode: {'LIVE' if not dry_run else 'DRY'} | Threshold={args.threshold}")
    print("📈 Pairs:", ", ".join(pairs))

    if args.autotrain:
        ensure_models_exist(pairs, timeframe=args.timeframe, limit=args.limit)

    lock_ctx = nullcontext() if args.no_lock else single_instance_lock()
    with lock_ctx:
        print("DEBUG PROXY_URL:", os.getenv("PROXY_URL"))
        print(">>> Before get_balance('USDT')", flush=True)
        usdt = get_balance("USDT")
        print(f">>> After get_balance: {usdt:.2f}", flush=True)
        print(f"💰 Баланс USDT: {usdt:.2f}")
        if usdt < min_balance:
            print(f"⛔ Баланс ниже минимума ({min_balance} USDT) — торговля пропущена.")
            return

       
        # 🔄 Новый блок вместо for p in pairs:
    interval = max(1, int(args.check_interval))
    if args.once:
     _one_pass(pairs, args, dry_run)
    else:
        print(f"∞ Run loop started, CHECK_INTERVAL={interval}s", flush=True)
        while True:
            t0 = time.time()
            _one_pass(pairs, args, dry_run)
            _heartbeat("sleep")
            dt = time.time() - t0
            left = max(0.0, interval - dt)
            if left > 0:
                time.sleep(left)

            # Больше ничего не делаем: apply_trailing_after_entry() ставит трейл и переводит в BE

if __name__ == "__main__":
    try:
        print(">>> starting positions_guard", flush=True)
    except Exception:
        pass
    main()
