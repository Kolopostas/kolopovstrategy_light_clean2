import py_compile
import shutil
import subprocess
import sys

git = shutil.which("git") or "git"
res = subprocess.run(
    [git, "ls-files", "*.py"], check=True, capture_output=True, text=True
)
files = res.stdout.splitlines

if not files:
    print("No Python files found.")
    sys.exit(0)

for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"[OK] {f}")
    except Exception as e:
        print(f"[FAIL] {f}: {e}")
        sys.exit(1)

print("Syntax OK")
