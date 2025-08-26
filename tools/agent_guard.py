# tools/agent_guard.py
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(args, check: bool = False) -> int:
    """
    Безопасный запуск процесса без shell=True.
    args: list[str] или строка (будет разбита по пробелам).
    """
    if isinstance(args, str):
        args = args.split()
    print("$", " ".join(args))
    rc = subprocess.run(args, check=check).returncode
    if check and rc != 0:
        sys.exit(rc)
    return rc


def run_capture(args) -> str:
    """Выполнить команду и вернуть stdout (без shell=True)."""
    if isinstance(args, str):
        args = args.split()
    res = subprocess.run(args, capture_output=True, text=True, check=True)
    return res.stdout


def ensure_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")
        print(f"Created {path.as_posix()}")
    else:
        print(f"Exists {path.as_posix()}")


def ensure_env_example() -> None:
    content = textwrap.dedent(
        """\
        BYBIT_API_KEY=
        BYBIT_SECRET_KEY=
        PROXY_URL=
        DOMAIN=bybit
        RISK_FRACTION=0.2
        RECV_WINDOW=15000
        PAIRS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT
        """
    )
    ensure_file(ROOT / ".env.example", content)


def ensure_procfile() -> None:
    content = "worker: python positions_guard.py\n"
    ensure_file(ROOT / "Procfile", content)


def try_imports() -> bool:
    """
    Пытаемся импортировать все .py модули из репозитория.
    Если где-то ошибка импорта — печатаем и продолжаем.
    """
    ok = True
    git = shutil.which("git") or "git"
    try:
        out = run_capture([git, "ls-files", "*.py"])
    except Exception as e:
        print(f"[WARN] git ls-files failed: {e}")
        return False

    for py in out.splitlines():
        # переводим путь в модуль: a/b/c.py -> a.b.c
        mod = py[:-3].replace("/", ".").replace("\\", ".")
        if mod.endswith(".__init__"):
            continue
        try:
            __import__(mod)
            print(f"[import OK] {mod}")
        except Exception as e:
            ok = False
            print(f"[import FAIL] {py}: {e}")
    return ok


def dry_run_positions_guard() -> None:
    pg = ROOT / "positions_guard.py"
    if not pg.exists():
        print("positions_guard.py not found — skip dry-run.")
        return

    # Пытаемся запустить с одной парой (DRY)
    rc = run([sys.executable, str(pg), "--pair", "BTC/USDT:USDT", "--dry-run"])
    if rc != 0:
        # fallback — без аргументов
        run([sys.executable, str(pg), "--dry-run"])


def main() -> None:
    print("─" * 50)
    print("Agent Guard: start")

    ensure_env_example()
    ensure_procfile()

    _ = try_imports()
    dry_run_positions_guard()

    print("Agent Guard: done")
    print("─" * 50)
    sys.exit(0)


if __name__ == "__main__":
    main()
