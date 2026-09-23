"""Let the report owner change podcast and YouTube choices by Telegram reply."""

import fcntl
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests


def project_dirs(kind, root):
    root = Path(root)
    parent = root.parent
    podcast = root if kind == "podcast" else parent / "leave-podcasts"
    youtube = root if kind == "youtube" else parent / "leave-youtube"
    podcast = Path(os.environ.get("LEAVE_PODCASTS_PROJECT", podcast))
    youtube = Path(os.environ.get("LEAVE_YOUTUBE_PROJECT", youtube))
    return podcast if (podcast / "app.py").exists() else None, youtube if (youtube / "app.py").exists() else None


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


@contextmanager
def choices_lock(root):
    path = root / "data" / "settings.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def normal(value):
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def reply_context(message, podcast, youtube):
    reply = message.get("reply_to_message") or {}
    key = str(reply.get("message_id", ""))
    if key and podcast:
        mapping = read_json(podcast / "data" / "sent_messages.json", {})
        if key in mapping:
            return mapping[key]
    if key and youtube:
        mapping = read_json(youtube / "data" / "sent_messages.json", {})
        if key in mapping:
            return mapping[key]
    return None


def search_podcast(query):
    response = requests.get("https://itunes.apple.com/search",
                            params={"term": query, "media": "podcast", "entity": "podcast",
                                    "limit": 10}, timeout=20)
    response.raise_for_status()
    options = [(item.get("collectionName", "").strip(), item.get("feedUrl", "").strip())
               for item in response.json().get("results", []) if item.get("feedUrl")]
    matches = [(name, feed) for name, feed in options if normal(name) == normal(query)]
    if len(matches) == 1:
        return matches[0], ""
    if len(options) == 1:
        return options[0], ""
    if options:
        return None, "Which podcast? Send its full name: " + "; ".join(name for name, _ in options[:3])
    return None, "I could not find that podcast. Send its full name."


def change_podcast(root, action, name, feed=""):
    if not root:
        return "Podcast choices are not available here."
    path = root / "data" / "settings.json"
    with choices_lock(root):
        value = read_json(path, {"shows": [], "blocked_shows": []})
        shows = value.setdefault("shows", [])
        blocked = value.setdefault("blocked_shows", [])
        if action == "kill":
            target = next((item for item in shows if (feed and item["feed"] == feed) or
                           normal(item["name"]) == normal(name)), None)
            if target:
                shows.remove(target)
                name, feed = target["name"], target["feed"]
            if not any((feed and item.get("feed") == feed) or normal(item["name"]) == normal(name)
                       for item in blocked):
                blocked.append({"name": name, "feed": feed})
            save_json(path, value)
            return name + " removed. It will stay out of future podcast reports."
        blocked[:] = [item for item in blocked if not ((feed and item.get("feed") == feed) or
                      normal(item["name"]) == normal(name))]
        if not any(item["feed"] == feed for item in shows):
            shows.append({"name": name, "feed": feed, "count": 1})
        save_json(path, value)
        return name + " added to future podcast reports."


def change_youtube(root, action, name, channel_id=""):
    if not root:
        return "YouTube choices are not available here."
    path = root / "data" / "settings.json"
    with choices_lock(root):
        value = read_json(path, {"blocked": [], "followed": [], "subjects": []})
        blocked = value.setdefault("blocked", [])
        followed = value.setdefault("followed", [])
        same = lambda item: (channel_id and item.get("channel_id") == channel_id) or normal(item.get("name", "")) == normal(name)
        if action == "kill":
            followed[:] = [item for item in followed if not same(item)]
            if not any(same(item) for item in blocked):
                blocked.append({"name": name, "channel_id": channel_id})
            save_json(path, value)
            return name + " blocked from future YouTube reports."
        blocked[:] = [item for item in blocked if not same(item)]
        if not any(same(item) for item in followed):
            followed.append({"name": name, "channel_id": channel_id, "count": 2})
        save_json(path, value)
        return name + " added to future YouTube reports."


def change_subject(root, action, name):
    if not root:
        return "YouTube choices are not available here."
    path = root / "data" / "settings.json"
    with choices_lock(root):
        value = read_json(path, {"subjects": []})
        subjects = value.setdefault("subjects", [])
        existing = next((item for item in subjects if normal(item["name"]) == normal(name)), None)
        if action == "kill":
            if existing:
                subjects.remove(existing)
            result = name + " removed from future YouTube reports."
        else:
            if not existing:
                subjects.append({"name": name, "query": name, "count": 3})
            result = name + " added as a YouTube subject."
        save_json(path, value)
        return result


def handle(message, podcast, youtube):
    raw = str(message.get("text", "")).strip()
    text = re.sub(r"\s+", " ", raw).strip(" .!?")
    context = reply_context(message, podcast, youtube)
    parts = re.split(r"\s+and\s+(?=(?:add|follow|kill|remove|stop|block)\s+(?:podcast|youtube|creator|channel|subject)\s+)",
                     text, flags=re.I)
    if len(parts) > 1:
        answers = [handle({**message, "text": part}, podcast, youtube) for part in parts]
        return "\n".join(answer for answer in answers if answer)
    if re.fullmatch(r"(?:kill|remove|stop|block) that (?:podcast|show)", text, re.I):
        if not context or context.get("kind") != "podcast":
            return "Reply to one podcast report, or say: kill podcast [name]."
        return change_podcast(podcast, "kill", context["name"], context.get("feed", ""))
    if re.fullmatch(r"(?:add|follow) that (?:podcast|show)", text, re.I):
        if not context or context.get("kind") != "podcast":
            return "Reply to one podcast report, or say: add podcast [name]."
        return change_podcast(podcast, "add", context["name"], context.get("feed", ""))
    if re.fullmatch(r"(?:kill|remove|stop|block) that (?:youtube|video|creator|channel)", text, re.I):
        if not context or context.get("kind") != "youtube":
            return "Reply to one YouTube report, or say: kill YouTube [creator name]."
        creators = context.get("creators", [])
        if len(creators) != 1:
            names = ", ".join(item["name"] for item in creators[:5])
            return "That report has several creators. Say: kill YouTube [creator name]. " + names
        creator = creators[0]
        return change_youtube(youtube, "kill", creator["name"], creator.get("channel_id", ""))
    if re.fullmatch(r"(?:add|follow) that (?:youtube|video|creator|channel)", text, re.I):
        if not context or context.get("kind") != "youtube" or len(context.get("creators", [])) != 1:
            return "Reply to a report with one creator, or say: add YouTube [creator name]."
        creator = context["creators"][0]
        return change_youtube(youtube, "add", creator["name"], creator.get("channel_id", ""))
    command = re.fullmatch(r"(add|follow|kill|remove|stop|block)\s+(podcast|youtube|creator|channel|subject)\s+(.+)", text, re.I)
    if not command:
        return None
    verb, kind, name = command.groups()
    action = "kill" if verb.casefold() in ("kill", "remove", "stop", "block") else "add"
    kind = kind.casefold()
    name = name.strip(" .!?")[:100]
    if kind == "youtube":
        name = re.sub(r"^(?:creator|channel)\s+", "", name, flags=re.I)
    if not name:
        return "Give me the podcast, creator or subject name."
    if kind == "podcast":
        if action == "add":
            found, error = search_podcast(name)
            return error if error else change_podcast(podcast, "add", *found)
        return change_podcast(podcast, "kill", name)
    if kind == "subject":
        return change_subject(youtube, action, name)
    return change_youtube(youtube, action, name)


def listen(kind, root, token, chat):
    podcast, youtube = project_dirs(kind, root)
    key = hashlib.sha256(token.encode()).hexdigest()[:20]
    state = Path.home() / ".local" / "share" / "leave-replies" / (key + ".json")
    lock_path = state.with_suffix(".lock")
    state.parent.mkdir(parents=True, exist_ok=True)
    base = "https://api.telegram.org/bot" + token + "/"
    with lock_path.open("a+") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(10)
        offset = read_json(state, {}).get("next")
        if offset is None:
            response = requests.get(base + "getUpdates", params={"timeout": 0}, timeout=15)
            response.raise_for_status()
            old = response.json().get("result", [])
            offset = max((item["update_id"] for item in old), default=-1) + 1
            save_json(state, {"next": offset})
        while True:
            try:
                response = requests.get(base + "getUpdates", params={"offset": offset, "timeout": 20,
                                            "allowed_updates": json.dumps(["message"])}, timeout=30)
                response.raise_for_status()
                for item in response.json().get("result", []):
                    message = item.get("message") or {}
                    if str((message.get("chat") or {}).get("id")) == str(chat) and message.get("text"):
                        answer = handle(message, podcast, youtube)
                        if answer:
                            sent = requests.post(base + "sendMessage",
                                                 json={"chat_id": chat, "text": answer,
                                                       "reply_to_message_id": message["message_id"]}, timeout=20)
                            sent.raise_for_status()
                            if not sent.json().get("ok"):
                                raise RuntimeError("Telegram did not accept the reply")
                    offset = item["update_id"] + 1
                    save_json(state, {"next": offset})
            except Exception as exc:
                print("Telegram reply could not be handled: " + str(exc)[:120], flush=True)
                time.sleep(10)


def start_listener(kind, root):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TEST_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat and os.environ.get("LEAVE_REPLIES") == "1":
        threading.Thread(target=listen, args=(kind, root, token, chat), daemon=True).start()
