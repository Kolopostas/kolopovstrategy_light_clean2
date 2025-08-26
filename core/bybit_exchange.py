import os

import ccxt


def create_exchange() -> ccxt.bybit:
    """
    Создает подключение к Bybit с поддержкой PROXY_URL и unified аккаунта.
    """
    proxy = os.getenv("PROXY_URL")
    recv_window = int(os.getenv("RECV_WINDOW", "20000"))

    exchange = ccxt.bybit(
        {
            "apiKey": os.getenv("BYBIT_API_KEY"),
            "secret": os.getenv("BYBIT_SECRET_KEY"),
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # Для деривативов
                "adjustForTimeDifference": True,
                "recvWindow": recv_window,
            },
        }
    )

    # Настройка прокси
    if proxy:
        exchange.proxies = {"http": proxy, "https": proxy}

    try:
        exchange.load_markets(reload=True)
    except ccxt.AuthenticationError:
        print("⛔ Ошибка аутентификации: проверь BYBIT_API_KEY и BYBIT_SECRET_KEY.")
        raise
    except ccxt.NetworkError:
        print("🌐 Сетевая ошибка: проверь PROXY_URL или интернет.")
        raise
    except Exception as e:
        print(f"⚠️ Неизвестная ошибка при загрузке рынков: {e}")
        raise

    return exchange


def get_balance(coin: str):
    """
    Получает баланс в Unified аккаунте.
    """
    exchange = create_exchange()
    try:
        balance = exchange.fetch_balance(params={"accountType": "UNIFIED"})
        return balance[coin]["free"]
    except Exception as e:
        print(f"Ошибка получения баланса для {coin}: {e}")
        return None


def normalize_symbol(symbol: str) -> str:
    """
    BTC/USDT -> BTC/USDT:USDT
    """
    s = symbol.upper().replace(" ", "")
    if ":" not in s:
        base, quote = s.split("/")
        s = f"{base}/{quote}:{quote}"
    return s
