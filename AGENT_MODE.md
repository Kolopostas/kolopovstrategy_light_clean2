# Режим агента для Kolopovstrategy

## 1. Что такое режим агента
Режим агента — это автоматизированная система, которая:
- Проверяет синтаксис, импорты и структуру кода.
- Автоматически создаёт Pull Request с исправлениями (импорты, форматирование, инфраструктура).
- Запускает безопасный тестовый прогон `positions_guard.py` в режиме `--dry-run`.
- Готовит проект для работы на Railway в режиме **Worker**.
- Работает полностью автоматически при каждом пуше в GitHub.

---

## 2. Что делает агент на каждом пуше
1. **Проверка синтаксиса** всех `.py` файлов.
2. **Проверка импортов** (ловит битые пути и неправильные импорты).
3. **Линтеры**: `flake8`, `bandit`, `vulture` — показывают проблемы, но не валят билд.
4. **Форматирование** кода через `isort` и `black`.
5. **Гарантия инфраструктурных файлов**:
   - `.env.example`
   - `Procfile`
6. **Dry-run `positions_guard.py`** (инициализация без реальных ордеров).
7. **Pull Request** с исправлениями (`agent/fixes`).

---

## 3. Необходимые файлы

### 3.1 CI Workflow
`.github/workflows/agent-ci.yml` — запускает проверки и создаёт PR с правками.

### 3.2 Скрипт агента
`tools/agent_guard.py` — проверка импортов, dry-run, создание недостающих файлов.

### 3.3 Pre-commit (опционально)
`.pre-commit-config.yaml` — локальные хуки для форматирования и линтеров.

### 3.4 Procfile
```
worker: python positions_guard.py
```

### 3.5 .env.example
```
BYBIT_API_KEY=
BYBIT_SECRET_KEY=
PROXY_URL=
DOMAIN=bybit
RISK_FRACTION=0.2
RECV_WINDOW=15000
PAIRS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT
```

---

## 4. Требования к `positions_guard.py`
- Поддержка `--dry-run` (запуск без реальных ордеров).
- Поддержка `--pair SYMBOL` (переопределение пар из `.env`).
- Чистые абсолютные импорты без изменения `PYTHONPATH`.

Пример:
```python
import argparse

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Запуск без ордеров")
    p.add_argument("--pair", type=str, help="Одна пара в формате BTC/USDT:USDT")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
```

---

## 5. Railway Worker режим
1. Тип сервиса: **Worker**.
2. Переменные окружения: из `.env.example` (без ключей в GitHub).
3. Автодеплой включён.

---

## 6. Обработка ошибок Bybit (внести в проект)
- **10001 invalid request** — использовать формат `BTC/USDT:USDT` и `defaultType='swap'` в ccxt.
- **110043 leverage not modified** — не падать, считать как успех.
- Перед отправкой ордера проверять `tickSize`, `stepSize` и `minOrderValue` через `instruments-info`.

---

## 7. Запуск локально
```
python positions_guard.py --dry-run
python positions_guard.py --pair TON/USDT:USDT
```

---

## 8. Чек-лист включения режима агента
- [ ] Добавить файлы `.github/workflows/agent-ci.yml`, `tools/agent_guard.py`, `.pre-commit-config.yaml`, `Procfile`, `.env.example`.
- [ ] Запушить в GitHub.
- [ ] Проверить вкладку **Actions** → job `agent-ci`.
- [ ] Принять PR `agent/fixes`.
- [ ] Railway: Worker + переменные окружения.
