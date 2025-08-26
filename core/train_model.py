import argparse
import os

from .env_loader import load_and_check_env
from .predict import train_model_for_pair


def train_many(pairs, timeframe="30m", limit=3000, model_dir="models"):
    os.makedirs(model_dir, exist_ok=True)
    for sym in pairs:
        print(f"\n📈 Обучение модели для {sym}...")
        try:
            acc = train_model_for_pair(
                sym, timeframe=timeframe, limit=limit, model_dir=model_dir
            )
            print(f"✅ {sym} — готово, вал.точность {acc:.4f}")
        except Exception as e:
            print(f"⚠️ {sym} — ошибка обучения: {e}")


def main():
    load_and_check_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str)
    parser.add_argument("--timeframe", type=str, default=os.getenv("TIMEFRAME", "5m"))
    parser.add_argument(
        "--limit", type=int, default=int(os.getenv("TRAIN_LIMIT", "3000"))
    )
    parser.add_argument(
        "--model-dir", type=str, default=os.getenv("MODEL_DIR", "models")
    )
    args = parser.parse_args()

    if args.pairs:
        pairs = [s.strip() for s in args.pairs.split(",") if s.strip()]
        src = "args"
    else:
        env_pairs_raw = os.getenv("PAIRS")
        pairs = [s.strip() for s in (env_pairs_raw or "").split(",") if s.strip()]
        src = ".env"

    print(f"[train_model] source={src} pairs={pairs}")
    if not pairs:
        pairs = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        print(f"[train_model] fallback pairs={pairs}")

    train_many(
        pairs, timeframe=args.timeframe, limit=args.limit, model_dir=args.model_dir
    )


if __name__ == "__main__":
    main()
