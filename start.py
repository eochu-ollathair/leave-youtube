#!/usr/bin/env python3
# READ FIRST: /root/app/leave-youtube/PROJECT_NOTES.md
"""Guide a Mac or Linux owner through starting their own Leave YouTube copy."""

import getpass
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import venv
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
VENV = ROOT / ".venv"
PYTHON = VENV / "bin" / "python"
PRIVATE_FILE = DATA / "telegram.json"


def telegram_call(token, method):
    address = "https://api.telegram.org/bot" + token + "/" + method
    with urllib.request.urlopen(address, timeout=20) as reply:
        answer = json.load(reply)
    if not answer.get("ok"):
        raise ValueError("Telegram did not accept that bot code.")
    return answer["result"]


def saved_telegram():
    if PRIVATE_FILE.exists():
        saved = json.loads(PRIVATE_FILE.read_text(encoding="utf-8"))
        if saved.get("token") and saved.get("chat"):
            return saved

    print("\nTelegram setup")
    print("In Telegram, open BotFather, send /newbot, and follow its two questions.")
    print("BotFather then gives you a long bot code. Paste it here; it stays on this computer.")
    while True:
        token = getpass.getpass("Bot code: ").strip()
        try:
            bot = telegram_call(token, "getMe")
            break
        except (ValueError, urllib.error.URLError, json.JSONDecodeError):
            print("That code did not work. Check the code BotFather gave you.")

    print("\nIn Telegram, open @" + bot["username"] + " and send it /start.")
    while True:
        input("After sending /start, press Enter here: ")
        try:
            updates = telegram_call(token, "getUpdates")
        except (ValueError, urllib.error.URLError, json.JSONDecodeError):
            print("Telegram could not be reached. Check your internet connection.")
            continue
        chats = [item.get("message", {}).get("chat", {}) for item in updates]
        chats = [chat for chat in chats if chat.get("type") == "private" and chat.get("id")]
        if not chats:
            print("No message has arrived yet. Send /start to your new bot and try again.")
            continue
        chat = chats[-1]
        name = chat.get("first_name") or chat.get("username") or str(chat["id"])
        if input("Send reports to " + name + "? [Y/n] ").strip().lower() in ("", "y", "yes"):
            break
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    saved = {"token": token, "chat": str(chat["id"])}
    fd = os.open(PRIVATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(saved, output)
    return saved


def install_program():
    digest = hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest()
    marker = VENV / ".leave-youtube-ready"
    if PYTHON.exists() and marker.exists() and marker.read_text().strip() == digest:
        return
    print("\nPreparing Leave YouTube. This can take a few minutes the first time.", flush=True)
    if not PYTHON.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
                   cwd=ROOT, check=True)
    marker.write_text(digest + "\n", encoding="utf-8")


def opening_key():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = DATA / "access-key"
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(key + "\n")
    return key


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Install Python 3.10 or newer, then start this file again.")
    if os.name == "nt":
        raise SystemExit("This download currently starts on Mac and Linux. A Windows starter is not ready yet.")
    print("Leave YouTube — your own copy")
    saved = saved_telegram()
    install_program()
    key = opening_key()
    environment = os.environ.copy()
    environment.update(TELEGRAM_BOT_TOKEN=saved["token"], TELEGRAM_CHAT_ID=saved["chat"],
                       LEAVE_REPLIES="1",
                       MORNING_EMBEDDED_DAILY="1",
                       PATH=str(VENV / "bin") + os.pathsep + environment.get("PATH", ""))
    port = int(os.environ.get("LEAVE_YOUTUBE_START_PORT", "19133"))
    if not 1 <= port <= 65535:
        raise SystemExit("Choose a valid local port.")
    url = "http://127.0.0.1:" + str(port) + "/#key=" + key
    print("\nYour private settings page: " + url)
    print("The page will open in your browser. Leave this window open for morning reports.", flush=True)
    threading.Timer(2, lambda: webbrowser.open(url)).start()
    try:
        subprocess.run([str(PYTHON), str(ROOT / "app.py"), "serve", "--port", str(port)], cwd=ROOT,
                       env=environment, check=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
