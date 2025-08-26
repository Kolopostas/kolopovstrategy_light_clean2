import os
from pathlib import Path

TARGET_FILE = Path("position_manager.py")

def main():
    use_trailing = os.getenv("USE_TRAILING_STOP", "false").lower() == "true"

    if not use_trailing:
        print("⚠️ USE_TRAILING_STOP не включён в .env — патч не применяется")
        return

    if not TARGET_FILE.exists():
        print(f"❌ Не найден {TARGET_FILE}")
        return

    code = TARGET_FILE.read_text(encoding="utf-8")

    # Если трейлинг-стоп уже встроен — ничего не делаем
    if "trailingStop" in code:
        print("✅ trailingStop уже есть в position_manager.py")
        return

    # --- Патчим open_position: добавляем поддержку trailingStop ---
    patched = code.replace(
        "order = ex.create_order(",
        "order = ex.create_order(\n"
        "            params={\"reduceOnly\": False, \"timeInForce\": \"GoodTillCancel\", \"trailingStop\": True},"
    )

    if patched == code:
        print("⚠️ Не удалось вставить трейлинг-стоп — проверь сигнатуру create_order")
        return

    TARGET_FILE.write_text(patched, encoding="utf-8")
    print(f"✅ Патч применён: trailingStop включён в {TARGET_FILE}")

if __name__ == "__main__":
    main()