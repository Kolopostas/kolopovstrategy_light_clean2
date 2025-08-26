import importlib
import sys

mods = ["ccxt", "pybit", "xgboost", "dotenv", "requests"]
fail = False
for m in mods:
    try:
        importlib.import_module(m)
        print(f"[OK] import {m}")
    except Exception as e:
        print(f"[FAIL] import {m}: {e.__class__.__name__}: {e}")
        fail = True

if fail:
    sys.exit(1)
