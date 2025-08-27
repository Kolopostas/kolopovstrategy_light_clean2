from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("bybit")


# === Специализированные исключения ===
class BybitAPIError(Exception):
    """Базовое исключение для ошибок Bybit API v5."""

    def __init__(
        self,
        message: str,
        ret_code: Optional[int] = None,
        endpoint: Optional[str] = None,
        request_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.ret_code = ret_code
        self.endpoint = endpoint
        self.request_id = request_id
        self.payload = payload or {}


class BybitInvalidParams(BybitAPIError):
    """Неверные/отсутствующие параметры (10001 и родственные)."""


class BybitAuthError(BybitAPIError):
    """Подпись/доступ/разрешения (10003, 10004, 10005, 10007, 10009, 10010...)."""


class BybitRateLimit(BybitAPIError):
    """Лимиты частоты: retCode=10006 или HTTP 429."""


class BybitNotModified(BybitAPIError):
    """Состояние не изменилось (110043, 34040). Используется как 'мягкая' ошибка при need_raise=True."""


class BybitInsufficientMargin(BybitAPIError):
    """Недостаточно маржи/средств (110044, 110012, 110014, 110045, 110052...)."""


class BybitTemporaryError(BybitAPIError):
    """Временные/серверные проблемы, которые можно безопасно ретраить (10016, 170007, 148019, таймауты и т.п.)."""


# === Нормализация ответа ===
def _normalize_ret_fields(resp: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """
    Возвращает (ret_code, ret_msg) из ответа Bybit.
    Поддерживает варианты ключей v5 и исторические: retCode/retMsg и ret_code/ret_msg.
    """
    ret_code = resp.get("retCode", resp.get("ret_code"))
    ret_msg = resp.get("retMsg", resp.get("ret_msg", "")) or ""
    return ret_code, ret_msg


# === Классификация кодов ===
_IGNORE_AS_SUCCESS_DEFAULT = frozenset(
    {
        110043,  # Set leverage has not been modified
        34040,  # Not modified
    }
)

# Ошибки, которые целесообразно ретраить с бэк-оффом
_RETRYABLE_CODES = frozenset(
    {
        10006,  # Too many visits (rate limit)
        10016,  # Server error (generic)
        170007,  # Timeout waiting for backend
        148019,  # System busy
        170146,  # Order creation timeout
        170147,  # Order cancellation timeout
    }
)

# Недостаток маржи/средств/лимитов (бизнес-логика, не ретраим слепо)
_INSUFFICIENT_FUNDS_CODES = frozenset(
    {
        110044,  # Available margin is insufficient
        110012,  # Insufficient available balance
        110014,  # Insufficient available balance to add additional margin
        110045,  # Wallet balance is insufficient
        110052,  # Available balance insufficient to set price
    }
)

# Ошибки аутентификации/прав доступа/подписи
_AUTH_CODES = frozenset(
    {
        10003,  # API key is invalid / env mismatch
        10004,  # Error sign (signature)
        10005,  # Permission denied
        10007,  # User authentication failed
        10009,  # IP banned
        10010,  # Unmatched IP (bound IPs)
    }
)

# Неверные параметры запроса
_INVALID_PARAM_CODES = frozenset(
    {
        10001,  # Request parameter error
        10002,  # Request time outside window
    }
)


def is_success_response(resp: Dict[str, Any]) -> bool:
    """Успех: retCode==0 или retMsg в стиле OK/success согласно гайду интеграции."""
    ret_code, ret_msg = _normalize_ret_fields(resp)
    if ret_code == 0:
        return True
    # Иногда встречаются ответы с OK/success при retCode==0 — дополнительная страховка.
    ok_markers = {"OK", "ok", "success", "SUCCESS", ""}
    return (ret_code in (0, None)) and (ret_msg in ok_markers)


def is_retryable(ret_code: Optional[int]) -> bool:
    return ret_code in _RETRYABLE_CODES


def handle_bybit_error(
    response: Dict[str, Any],
    *,
    endpoint: Optional[str] = None,
    request_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    ignore_codes: Optional[Iterable[int]] = None,
    raise_on_not_modified: bool = False,
) -> None:
    """
        Унифицированная проверка ответа Bybit v5.
    - Игнорирует 'неизменённые' коды (110043, 34040) как успех, если не указан raise_on_not_modified=True.
        - Классифицирует и поднимает специализированные исключения.
        - Ничего не возвращает при успехе, только исключения при ошибках.

        Параметры:
          response            — JSON dict от Bybit.
          endpoint / request_id / payload — опционально для логов/диагностики.
          ignore_codes        — дополнительные коды, которые считать успешными.
          raise_on_not_modified — если True, 110043/34040 поднимаются как BybitNotModified.
    """
    # HTTP-уровень (если ваш HTTP-клиент прокидывает код ответа сюда)
    http_status = response.get("_http_status")
    if http_status == 429:
        # HTTP 429 = системная частотная защита (см. доки Bybit)
        msg = "HTTP 429 Too Many Requests"
        logger.warning(
            "[Bybit][RATE_LIMIT][HTTP429] %s endpoint=%s reqId=%s",
            msg,
            endpoint,
            request_id,
        )
        raise BybitRateLimit(
            msg,
            ret_code=10006,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )

    # Нормализуем retCode/retMsg
    ret_code, ret_msg = _normalize_ret_fields(response)

    # Быстрый выход на успех
    if is_success_response(response):
        return

    code_to_ignore = set(_IGNORE_AS_SUCCESS_DEFAULT)
    if ignore_codes:
        code_to_ignore.update(ignore_codes)

    if ret_code in code_to_ignore:
        # Либо совсем замалчиваем, либо поднимаем "мягко"
        logger.info(
            "[Bybit][NOT_MODIFIED] retCode=%s msg=%s endpoint=%s",
            ret_code,
            ret_msg,
            endpoint,
        )
        if raise_on_not_modified:
            raise BybitNotModified(
                ret_msg or "Not modified",
                ret_code=ret_code,
                endpoint=endpoint,
                request_id=request_id,
                payload=payload,
            )
        return

    # Классификация
    if ret_code in _INVALID_PARAM_CODES:
        # Частая причина при trading-stop — числа, переданные не как строки
        raise BybitInvalidParams(
            ret_msg or "Invalid parameters",
            ret_code=ret_code,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )

    if ret_code in _AUTH_CODES:
        raise BybitAuthError(
            ret_msg or "Auth/permission error",
            ret_code=ret_code,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )

    if ret_code in _INSUFFICIENT_FUNDS_CODES:
        raise BybitInsufficientMargin(
            ret_msg or "Insufficient margin/balance",
            ret_code=ret_code,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )

    if is_retryable(ret_code):
        # Рекомендуется перехватывать и ретраить с экспон. бэк-оффом вне этого модуля
        raise BybitRateLimit(
            ret_msg or "Rate limited / temporary error",
            ret_code=ret_code,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )

    # Частные коды из UTA/Trade, которые удобно подсветить явно
    if ret_code == 110009:
        raise BybitAPIError(
            "TP/SL/conditional orders limit exceeded",
            ret_code=ret_code,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )
    if ret_code == 110033:
        raise BybitAPIError(
            "Can't set margin without an open position",
            ret_code=ret_code,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
        )

    # Фоллбек
    message = ret_msg or f"Bybit error retCode={ret_code}"
    raise BybitAPIError(
        message,
        ret_code=ret_code,
        endpoint=endpoint,
        request_id=request_id,
        payload=payload,
    )


# === Утилита: безопасная проверка и логирование ===
def assert_bybit_ok(
    response: Dict[str, Any],
    *,
    endpoint: Optional[str] = None,
    request_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    ignore_codes: Optional[Iterable[int]] = None,
    raise_on_not_modified: bool = False,
) -> None:
    """
    Обёртка над handle_bybit_error с расширенным логированием входа/выхода.
    """
    ret_code, ret_msg = _normalize_ret_fields(response)
    logger.debug(
        "[Bybit][RESP] retCode=%s retMsg=%s endpoint=%s reqId=%s",
        ret_code,
        ret_msg,
        endpoint,
        request_id,
    )
    handle_bybit_error(
        response,
        endpoint=endpoint,
        request_id=request_id,
        payload=payload,
        ignore_codes=ignore_codes,
        raise_on_not_modified=raise_on_not_modified,
    )
