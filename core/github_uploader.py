import base64
import os
from datetime import datetime

import requests


def upload_trades_to_github(file_path: str = "logs/trades.csv") -> None:
    """Заливает trades.csv в GitHub через Contents API (без git push)."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")

    if not token or not repo:
        print("❌ GITHUB_TOKEN или GITHUB_REPO не заданы — пропуск загрузки")
        return

    if not os.path.exists(file_path):
        print(f"⚠️ Файл {file_path} не найден — пропуск")
        return

    url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    headers = {"Authorization": f"token {token}"}

    # читаем локальный файл
    with open(file_path, "rb") as f:
        content = f.read()
    encoded = base64.b64encode(content).decode("utf-8")

    # получаем sha, если файл уже есть
    r = requests.get(url, headers=headers, timeout=20)
    sha = r.json().get("sha") if r.status_code == 200 else None

    data = {
        "message": f"update({file_path}) {datetime.utcnow().isoformat()}",
        "content": encoded,
        "branch": branch,
    }
    if sha:
        data["sha"] = sha

    r = requests.put(url, headers=headers, json=data, timeout=30)
    if r.status_code in (200, 201):
        print(f"✅ {file_path} загружен в GitHub")
    else:
        print(f"❌ upload error {r.status_code}: {r.text}")
