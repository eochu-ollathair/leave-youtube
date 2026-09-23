#!/usr/bin/env python3
# READ FIRST: /root/app/leave-youtube/PROJECT_NOTES.md
"""Choose popular recent videos, condense their speech, and send a morning brief."""

import argparse
import concurrent.futures
import fcntl
import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from flask import Flask, abort, jsonify, make_response, redirect, render_template, request, send_file


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MORNING_DATA_DIR", str(ROOT / "data")))
CONFIG_FILE = DATA_DIR / "settings.json"
STATE_FILE = DATA_DIR / "last_run.json"
DELIVERY_FILE = DATA_DIR / "last_delivery.json"
LOCK_FILE = DATA_DIR / "run.lock"
SOURCE_FILE = ROOT / "dist" / "leave-youtube-source.zip"
ACCESS_LABEL = "Leave YouTube access key: "
BASE = os.environ.get("LEAVE_YOUTUBE_BASE", "").rstrip("/")
LOCAL_AI = os.environ.get("MORNING_MODEL_URL", "http://127.0.0.1:11434/v1/chat/completions")
MODEL = os.environ.get("MORNING_MODEL", "")
OWNER_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
app = Flask(__name__)
job_lock = threading.Lock()
job_state = {"running": False, "stage": "Ready", "kind": None, "error": None, "updated": None}


def initial_settings():
    return {
        "enabled": os.environ.get("MORNING_DEFAULT_ENABLED", "0") == "1",
        "time": "08:00",
        "timezone": os.environ.get("MORNING_DEFAULT_TIMEZONE", "Europe/Dublin"),
        "subjects": [
            {"name": "Phones", "query": "phone review", "count": 3},
            {"name": "Politics", "query": "politics news", "count": 3},
            {"name": "AI", "query": "AI news", "count": 3},
            {"name": "Fashion", "query": "fashion news", "count": 3},
        ],
        "blocked": [],
    }


def atomic_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def settings():
    if not CONFIG_FILE.exists():
        atomic_json(CONFIG_FILE, initial_settings())
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def validate_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid settings")
    raw_subjects = value.get("subjects")
    if not isinstance(raw_subjects, list) or len(raw_subjects) > 12:
        raise ValueError("Use up to 12 subjects")
    subjects = []
    for item in raw_subjects:
        name = str(item.get("name", "")).strip()[:60]
        query = str(item.get("query", "")).strip()[:100]
        try:
            count = int(item.get("count", 3))
        except (TypeError, ValueError):
            raise ValueError("Video count must be a whole number") from None
        if not name or not query or not 1 <= count <= 10:
            raise ValueError("Each subject needs a name, a search, and 1 to 10 videos")
        subjects.append({"name": name, "query": query, "count": count})
    raw_blocked = value.get("blocked", [])
    if not isinstance(raw_blocked, list) or len(raw_blocked) > 200:
        raise ValueError("Too many blocked creators")
    blocked = []
    seen = set()
    for item in raw_blocked:
        name = str(item.get("name", "")).strip()[:100]
        channel_id = str(item.get("channel_id", "")).strip()[:100]
        if not name and not channel_id:
            continue
        key = (channel_id or name).casefold()
        if key not in seen:
            seen.add(key)
            blocked.append({"name": name, "channel_id": channel_id})
    send_time = str(value.get("time", "08:00"))
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("Choose a valid morning time")
    timezone_name = str(value.get("timezone", "Europe/Dublin")).strip()[:80]
    try:
        ZoneInfo(timezone_name)
    except Exception:
        raise ValueError("Choose a valid time zone, such as Europe/Dublin") from None
    return {
        "enabled": bool(value.get("enabled", True)),
        "time": send_time,
        "timezone": timezone_name,
        "subjects": subjects,
        "blocked": blocked,
    }


def access_key():
    if os.environ.get("MORNING_ACCESS_KEY"):
        return os.environ["MORNING_ACCESS_KEY"]
    credentials_path = os.environ.get("MORNING_CREDENTIALS_FILE")
    if credentials_path:
        path = Path(credentials_path)
        label = ACCESS_LABEL
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "access-key"
        label = ""
    path.touch(mode=0o600, exist_ok=True)
    with path.open("r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        for line in stream:
            if line.startswith(label) and line[len(label):].strip():
                return line[len(label):].strip()
        key = secrets.token_urlsafe(32)
        stream.seek(0, os.SEEK_END)
        stream.write(("\n## Morning video brief\n" if label else "") + label + key + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        return key


def authenticated():
    supplied = request.cookies.get("morning_access", "")
    return bool(supplied) and secrets.compare_digest(supplied, access_key())


def require_auth():
    if not authenticated():
        abort(403)


def require_action():
    require_auth()
    if request.headers.get("X-Morning-Action") != "yes":
        abort(403)


def stamp(stage, **extra):
    job_state.update(stage=stage, updated=datetime.now(timezone.utc).isoformat(), **extra)


def command_output(args, timeout=50):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Video search failed")[-350:])
    return result.stdout


def flat_search(query, how_many=20):
    raw = command_output([
        "yt-dlp", "--no-update", "--no-warnings", "--socket-timeout", "12",
        "--playlist-end", str(how_many), "--flat-playlist", "--print",
        "%(id)s\t%(title)s\t%(channel)s\t%(channel_id)s\t%(view_count)s",
        "ytsearchdate" + str(how_many) + ":" + query,
    ], timeout=55)
    videos = []
    for line in raw.splitlines():
        cells = line.split("\t")
        if len(cells) != 5 or not re.fullmatch(r"[\w-]{11}", cells[0]):
            continue
        try:
            views = int(cells[4])
        except ValueError:
            views = 0
        videos.append({"id": cells[0], "title": cells[1], "channel": cells[2],
                       "channel_id": cells[3] if cells[3] != "NA" else "", "views": views})
    return videos


def full_metadata(item):
    raw = command_output([
        "yt-dlp", "--no-update", "--no-warnings", "--socket-timeout", "12",
        "--skip-download", "--no-playlist", "--print",
        "%(id)s\t%(title)s\t%(channel)s\t%(channel_id)s\t%(view_count)s\t%(upload_date)s\t%(duration)s",
        "https://www.youtube.com/watch?v=" + item["id"],
    ], timeout=50)
    cells = raw.strip().split("\t")
    if len(cells) != 7:
        raise RuntimeError("Video details were incomplete")
    date = datetime.strptime(cells[5], "%Y%m%d").replace(tzinfo=timezone.utc)
    views = int(cells[4])
    duration = int(float(cells[6]))
    return {"id": cells[0], "title": cells[1], "channel": cells[2],
            "channel_id": cells[3] if cells[3] != "NA" else item["channel_id"],
            "views": views, "published": date.date().isoformat(), "duration": duration,
            "url": "https://www.youtube.com/watch?v=" + cells[0]}


def blocked(item, blocked_creators):
    for creator in blocked_creators:
        creator_id = creator["channel_id"].casefold()
        creator_name = creator["name"].casefold()
        if creator_id and creator_id == item.get("channel_id", "").casefold():
            return True
        if creator_name and creator_name == item.get("channel", "").casefold():
            return True
    return False


def choose_videos(subject, blocked_creators):
    wanted = subject["count"]
    found = flat_search(subject["query"], min(30, max(18, wanted * 5)))
    found = [v for v in found if not blocked(v, blocked_creators)]
    # Inspect popular results and several new ones, then rank with a mild age adjustment.
    popular = sorted(found, key=lambda v: v["views"], reverse=True)[:max(10, wanted * 3)]
    first = found[:max(6, wanted * 2)]
    candidates = list({v["id"]: v for v in first + popular}.values())[:22]
    accepted = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(full_metadata, item): item for item in candidates}
        for future in concurrent.futures.as_completed(futures):
            try:
                video = future.result()
            except Exception:
                continue
            age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(video["published"]).date()).days
            if 0 <= age <= 7 and 60 <= video["duration"] <= 10800 and not blocked(video, blocked_creators):
                video["score"] = round(video["views"] / ((age + 1) ** 0.55))
                accepted.append(video)
    accepted.sort(key=lambda v: (v["score"], v["views"]), reverse=True)
    varied = []
    repeats = []
    used_creators = set()
    for video in accepted:
        creator = (video["channel_id"] or video["channel"]).casefold()
        if creator in used_creators:
            repeats.append(video)
        else:
            used_creators.add(creator)
            varied.append(video)
    return (varied + repeats)[:wanted]


def transcript(video_id):
    url = "https://api.freetranscriptapi.com/v1/transcript?video_url=" + quote(
        "https://www.youtube.com/watch?v=" + video_id, safe="")
    try:
        response = requests.get(url, timeout=35)
        response.raise_for_status()
        data = response.json()
        pieces = data.get("transcript", [])
        spoken = " ".join(str(part.get("text", "")) for part in pieces if isinstance(part, dict))
        spoken = re.sub(r"\[(?:Music|Applause|Laughter)\]", "", spoken, flags=re.I)
        if len(spoken) >= 500:
            return spoken
    except Exception:
        pass
    response = requests.get("https://youtube-transcript.ai/transcript/" + video_id + ".txt", timeout=35)
    response.raise_for_status()
    text = response.text
    if "## Transcript" not in text:
        raise RuntimeError("No speech text available")
    text = html.unescape(text.split("## Transcript", 1)[1])
    text = re.sub(r"\[\d+:\d+\]", "", text)
    if len(text) < 500:
        raise RuntimeError("No speech text available")
    return text


def ask_model(system, user, max_tokens=1400, timeout=180):
    if not MODEL:
        raise RuntimeError("Set MORNING_MODEL to the name of your text model")
    headers = {}
    if os.environ.get("MORNING_MODEL_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["MORNING_MODEL_KEY"]
    for limit in (max_tokens, max_tokens * 2):
        response = requests.post(LOCAL_AI, json={
            "model": MODEL, "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}],
            "temperature": 0.1, "max_tokens": limit,
        }, headers=headers, timeout=timeout)
        response.raise_for_status()
        answer = response.json()["choices"][0]
        result = (answer["message"].get("content") or "").strip()
        result = re.sub(r"<think>.*?</think>", "", result, flags=re.S).strip()
        if len(result) >= 30:
            return result
        if answer.get("finish_reason") != "length":
            break
    raise RuntimeError("The model gave no usable answer")


def video_notes(video, speech):
    # Read every part of long videos. A two-hour review must not lose its middle.
    chunks = []
    remaining = speech
    while len(remaining) > 30000:
        cut = remaining.rfind(" ", 25000, 30000)
        if cut < 25000:
            cut = 30000
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    system = ("You make evidence notes from a YouTube transcript. Report only what the speaker actually says. "
              "Prefer exact products, events, numbers, reasons, and opinions. Mark a speaker's prediction as a prediction. "
              "Never invent facts. Avoid broad statements such as 'things are changing'. Output 4 to 7 short bullets. "
              "If the speech is unrelated to the title or too thin, say so. Do not add an introduction.")
    parts = []
    for i, chunk in enumerate(chunks, 1):
        parts.append(ask_model(system, "VIDEO: " + video["title"] + "\nCREATOR: " + video["channel"] +
                               "\nPART: " + str(i) + " of " + str(len(chunks)) +
                               "\nTRANSCRIPT:\n" + chunk, max_tokens=1400))
    if len(parts) == 1:
        return parts[0]
    return ask_model(system, "VIDEO: " + video["title"] + "\nCombine these notes from every part "
                     "of the video. Keep the most concrete points and any change of view.\n" +
                     "\n".join(parts), max_tokens=1200)


def subject_digest(subject, notes):
    system = ("Write a short morning briefing about what these videos SAY. Use 2 to 4 bullets, each with a concrete "
              "product, event, claim, figure, or reason. Combine repeated points. State disagreements or differing opinions "
              "when present. Do not present an unverified video claim as an established fact. No vague filler, hype, "
              "intro, or stock conclusion. Plain English. Stay under 130 words. Do not quote at length.")
    source = "\n\n".join("VIDEO " + str(i + 1) + ": " + video["title"] + " — " + video["channel"] +
                           "\n" + note for i, (video, note) in enumerate(notes))
    return ask_model(system, "SUBJECT: " + subject["name"] + "\n" + source, max_tokens=1000)


def build_selection(config, progress=stamp):
    selection = []
    for subject in config["subjects"]:
        progress("Finding recent, highly watched videos: " + subject["name"])
        videos = choose_videos(subject, config["blocked"])
        selection.append({"subject": subject["name"], "requested": subject["count"], "videos": videos})
    return selection


def build_digest(config, selection, progress=stamp):
    sections = []
    for group in selection:
        videos = group["videos"]
        if not videos:
            sections.append({"subject": group["subject"], "text": "No recent videos with readable speech were found.",
                             "videos": [], "requested": group["requested"]})
            continue
        notes = []
        failures = []
        for video in videos:
            progress("Reading speech: " + video["title"][:55])
            try:
                speech = transcript(video["id"])
                note = video_notes(video, speech)
                notes.append((video, note))
            except Exception as exc:
                video["speech_error"] = str(exc)[:160]
                failures.append(video["speech_error"])
        if not notes:
            if failures:
                raise RuntimeError(group["subject"] + ": could not read or condense the selected videos (" +
                                   failures[0] + ")")
            content = "No videos were available to read."
        else:
            progress("Combining what was said: " + group["subject"])
            content = subject_digest({"name": group["subject"]}, notes)
        sections.append({"subject": group["subject"], "text": content,
                         "videos": [video for video, _ in notes], "requested": group["requested"]})
    return sections


def telegram_text(sections, timezone_name):
    day = datetime.now(ZoneInfo(timezone_name)).strftime("%d %B %Y")
    lines = ["Leave YouTube · " + day]
    for section in sections:
        lines.append("\n" + section["subject"] + " (" + str(len(section["videos"])) +
                     "/" + str(section["requested"]) + " videos)")
        lines.append(section["text"])
    site_url = os.environ.get("LEAVE_YOUTUBE_SITE_URL", "")
    if site_url:
        lines.append("\nChosen videos and blocked creators: " + site_url)
    return "\n".join(lines)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TEST_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Telegram bot token is not set")
    if not OWNER_CHAT:
        raise RuntimeError("Telegram chat is not set")
    # Telegram's limit is 4096 characters. Split at section boundaries where possible.
    pieces = []
    while message:
        if len(message) <= 3900:
            pieces.append(message)
            break
        cut = message.rfind("\n\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        pieces.append(message[:cut])
        message = message[cut:].lstrip()
    for piece in pieces:
        response = requests.post("https://api.telegram.org/bot" + token + "/sendMessage",
                                 json={"chat_id": OWNER_CHAT, "text": piece,
                                       "disable_web_page_preview": True}, timeout=25)
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram refused the message")


def run(kind, automatic=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another preview or report is already running") from None
        config = settings()
        selection = build_selection(config)
        result = {"at": datetime.now(timezone.utc).isoformat(), "selection": selection,
                  "kind": kind, "sent": False}
        if kind != "selection":
            sections = build_digest(config, selection)
            result["sections"] = sections
            result["message"] = telegram_text(sections, config["timezone"])
            if kind == "send":
                if not any(section["videos"] for section in sections):
                    raise RuntimeError("No videos had readable speech; Telegram was not sent")
                stamp("Sending the report to Telegram")
                send_telegram(result["message"])
                result["sent"] = True
                atomic_json(DELIVERY_FILE, {"sent_date":
                            datetime.now(ZoneInfo(config["timezone"])).date().isoformat(),
                            "sent_at": datetime.now(timezone.utc).isoformat(),
                            "automatic": automatic})
        atomic_json(STATE_FILE, result)
        return result


def start_job(kind):
    if not job_lock.acquire(blocking=False):
        raise RuntimeError("A preview or report is already running")
    stamp("Starting", running=True, kind=kind, error=None)

    def worker():
        try:
            run(kind)
            stamp("Finished", running=False)
        except Exception as exc:
            stamp("Could not finish", running=False, error=str(exc)[:300])
        finally:
            job_lock.release()

    threading.Thread(target=worker, daemon=True).start()


@app.get("/")
def home():
    if not authenticated():
        return render_template("open.html", base=BASE), 403
    return render_template("index.html", base=BASE)


@app.get("/open")
def open_private():
    key = request.args.get("key", "")
    if not key or not secrets.compare_digest(key, access_key()):
        abort(403)
    response = make_response(redirect(BASE + "/", code=302))
    response.set_cookie("morning_access", key, max_age=60 * 60 * 24 * 365,
                        secure=bool(BASE) or request.is_secure,
                        httponly=True, samesite="Strict", path=BASE or "/")
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post("/api/open")
def open_from_private_link():
    key = str((request.get_json(silent=True) or {}).get("key", ""))
    if not key or not secrets.compare_digest(key, access_key()):
        abort(403)
    response = jsonify({"opened": True})
    response.set_cookie("morning_access", key, max_age=60 * 60 * 24 * 365,
                        secure=bool(BASE) or request.is_secure, httponly=True,
                        samesite="Strict", path=BASE or "/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/about")
def about():
    return render_template("public.html", base=BASE,
                           source_available=SOURCE_FILE.exists(),
                           repo_url=os.environ.get("LEAVE_YOUTUBE_REPO_URL", ""))


@app.get("/source.zip")
def source_download():
    if not SOURCE_FILE.exists():
        abort(404)
    return send_file(SOURCE_FILE, as_attachment=True, download_name="leave-youtube-source.zip")


@app.get("/api/status")
def status():
    require_auth()
    saved = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else None
    return jsonify({"settings": settings(), "job": job_state, "last": saved})


@app.post("/api/settings")
def save_settings():
    require_action()
    try:
        clean = validate_settings(request.get_json(force=True))
    except (ValueError, AttributeError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    atomic_json(CONFIG_FILE, clean)
    return jsonify({"settings": clean})


@app.post("/api/run/<kind>")
def run_now(kind):
    require_action()
    if kind not in {"selection", "preview", "send"}:
        abort(404)
    try:
        start_job(kind)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"started": True})


def daily_due(config, state):
    if not config["enabled"] or not config["subjects"]:
        return False
    now = datetime.now(ZoneInfo(config["timezone"]))
    if now.strftime("%H:%M") < config["time"]:
        return False
    return not state or state.get("sent_date") != now.date().isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve", "daily", "selection", "preview", "send"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19133)
    args = parser.parse_args()
    settings()
    access_key()
    if args.mode == "serve":
        app.run(host=args.host, port=args.port, threaded=True)
        return
    if args.mode == "daily":
        state = json.loads(DELIVERY_FILE.read_text(encoding="utf-8")) if DELIVERY_FILE.exists() else None
        if not daily_due(settings(), state):
            return
        kind = "send"
    else:
        kind = args.mode
    result = run(kind, automatic=args.mode == "daily")
    print(json.dumps({"kind": kind, "subjects": len(result["selection"]),
                      "videos": sum(len(x["videos"]) for x in result["selection"]),
                      "sent": result["sent"]}))


if __name__ == "__main__":
    main()
