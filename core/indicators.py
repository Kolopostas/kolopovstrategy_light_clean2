from typing import Dict, List
from core.bybit_exchange import create_exchange


def _sma(values: List[float], period: int) -> float:
    """
    Простейшее скользящее среднее. Возвращает среднее последних 'period' значений.
    Если данных меньше, чем период, возвращает среднее всего списка.
    """
    if len(values) < period or period <= 0:
        return 0.0
    return sum(values[-period:]) / float(period)


def atr_latest_from_ohlcv(ohlcv: List[List[float]], period: int = 14) -> tuple[float, float]:
    """
    Рассчитывает ATR (Average True Range) по последним 'period' свечам
    и возвращает кортеж (atr, last_close).

    True Range = max(high - low, |high - prev_close|, |low - prev_close|).
    ATR = SMA(True Range, period).

    Параметры:
      ohlcv  — список свечей [timestamp, open, high, low, close, volume].
      period — период ATR.

    Возвращает:
      (atr_value, last_close)
    """
    if not ohlcv:
        return 0.0, 0.0

    if len(ohlcv) < period + 1:
        last_close = float(ohlcv[-1][4])
        return 0.0, last_close

    true_ranges: List[float] = []

    for i in range(1, len(ohlcv)):
        # текущие данные
        _, _, high_price, low_price, _, _ = ohlcv[i]
        # предыдущий close
        _, _, _, _, prev_close, _ = ohlcv[i - 1]

        range_high_low = float(high_price) - float(low_price)
        range_high_prev = abs(float(high_price) - float(prev_close))
        range_low_prev = abs(float(low_price) - float(prev_close))

        true_range = max(range_high_low, range_high_prev, range_low_prev)
        true_ranges.append(true_range)

    atr_value = float(_sma(true_ranges, period))
    last_close = float(ohlcv[-1][4])
    return atr_value, last_close


def _ema_last(vals: List[float], period: int) -> float:
    """
    Возвращает последнее значение экспоненциального скользящего среднего (EMA)
    для списка значений vals по заданному периоду.
    """
    alpha = 2.0 / (period + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _rsi_last(vals: List[float], period: int = 14) -> float:
    """
    Рассчитывает последнее значение индекса относительной силы (RSI).
    """
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(vals)):
        change = vals[i] - vals[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    if len(gains) < period:
        return 50.0

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bb_last(vals: List[float], period: int = 20) -> Dict[str, float]:
    """
    Рассчитывает последние значения полос Боллинджера (BB) и их ширину.
    Возвращает dict с mid, up, dn и width.
    """
    if len(vals) < period:
        mid = sum(vals) / len(vals)
        return {"mid": mid, "up": mid, "dn": mid, "width": 0.0}

    recent_vals = vals[-period:]
    mid = sum(recent_vals) / period
    variance = sum((x - mid) ** 2 for x in recent_vals) / period
    sd = variance ** 0.5
    up = mid + 2 * sd
    dn = mid - 2 * sd
    width = (up - dn) / mid if mid else 0.0

    return {"mid": mid, "up": up, "dn": dn, "width": width}


def compute_snapshot(symbol: str, timeframe: str = "5m", limit: int = 200) -> Dict[str, float]:
    """
    Возвращает краткий набор индикаторов для дальнейшего анализа или логирования:
    EMA12, EMA26, MACD, MACD‑signal (по сигнальной EMA9), RSI14, Bollinger Bands и текущее закрытие.
    """
    ex = create_exchange()
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    closes = [float(c[4]) for c in ohlcv]

    if len(closes) < 60:
        return {}

    ema12 = _ema_last(closes, 12)
    ema26 = _ema_last(closes, 26)
    macd = ema12 - ema26

    # “Сигнальная линия” MACD: EMA(9) от последовательности macd‑значений;
    # для простоты считаем на последнем отрезке (последние 60 значений).
    macd_series: List[float] = []
    for i in range(26, len(closes)):
        ema12_i = _ema_last(closes[: i + 1], 12)
        ema26_i = _ema_last(closes[: i + 1], 26)
        macd_series.append(ema12_i - ema26_i)

    macd_signal = _ema_last(macd_series, 9) if macd_series else 0.0
    rsi = _rsi_last(closes, 14)
    bb = _bb_last(closes, 20)

    return {
        "ema12": round(ema12, 6),
        "ema26": round(ema26, 6),
        "macd": round(macd, 6),
        "macd_signal": round(macd_signal, 6),
        "rsi14": round(rsi, 3),
        "bb_mid": round(bb["mid"], 6),
        "bb_up": round(bb["up"], 6),
        "bb_dn": round(bb["dn"], 6),
        "bb_width": round(bb["width"], 6),
        "close": closes[-1],
    }
