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
import tempfile
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
LOCAL_AI = os.environ.get("MORNING_MODEL_URL", "")
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
        "followed": [],
        "angle": "",
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
    raw_followed = value.get("followed", [])
    if not isinstance(raw_followed, list) or len(raw_followed) > 30:
        raise ValueError("Use up to 30 creators to follow")
    followed = []
    seen_followed = set()
    for item in raw_followed:
        name = str(item.get("name", "")).strip()[:100]
        channel_id = str(item.get("channel_id", "")).strip()[:100]
        try:
            count = int(item.get("count", 2))
        except (TypeError, ValueError):
            raise ValueError("Creator video count must be a whole number") from None
        if not name or not 1 <= count <= 10:
            raise ValueError("Each creator needs a name and 1 to 10 videos")
        key = (channel_id or name).casefold()
        if key not in seen_followed:
            seen_followed.add(key)
            followed.append({"name": name, "channel_id": channel_id, "count": count})
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
        "followed": followed,
        "angle": str(value.get("angle", "")).strip()[:400],
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
    recent_query = query + " after:" + (datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()
    raw = command_output([
        "yt-dlp", "--no-update", "--no-warnings", "--socket-timeout", "12",
        "--playlist-end", str(how_many), "--flat-playlist", "--print",
        "%(id)s\t%(title)s\t%(channel)s\t%(channel_id)s\t%(view_count)s",
        "ytsearch" + str(how_many) + ":" + recent_query,
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


def matches_creator(item, creator):
    if not creator:
        return True
    if creator.get("channel_id"):
        return creator["channel_id"].casefold() == item.get("channel_id", "").casefold()
    def clean(name):
        words = re.findall(r"[a-z0-9]+", name.casefold())
        return " ".join(words[1:] if words[:1] == ["the"] else words)
    return clean(creator["name"]) == clean(item.get("channel", ""))


def choose_videos(subject, blocked_creators, extra=0):
    wanted = subject["count"]
    found = flat_search(subject["query"], min(30, max(18, wanted * 5)))
    found = [v for v in found if not blocked(v, blocked_creators)
             and matches_creator(v, subject.get("creator"))]
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
            if (0 <= age <= 7 and 60 <= video["duration"] <= 10800
                    and not blocked(video, blocked_creators)
                    and matches_creator(video, subject.get("creator"))):
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
    return (varied + repeats)[:wanted + extra]


def transcript(video_id):
    cache = DATA_DIR / "transcripts" / (video_id + ".txt")
    if cache.exists() and len(cache.read_text(encoding="utf-8")) >= 500:
        return cache.read_text(encoding="utf-8")
    # YouTube's own caption file avoids depending on third-party speech services.
    try:
        with tempfile.TemporaryDirectory(prefix="leave-captions-") as directory:
            args = ["yt-dlp", "--no-update", "--no-warnings", "--skip-download",
                    "--write-subs", "--write-auto-subs", "--sub-lang", "en",
                    "--sub-format", "json3", "-o", directory + "/%(id)s.%(ext)s",
                    "https://www.youtube.com/watch?v=" + video_id]
            result = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
            captions = list(Path(directory).glob("*.json3"))
            if result.returncode == 0 and captions:
                data = json.loads(captions[0].read_text(encoding="utf-8"))
                spoken = "".join("".join(seg.get("utf8", "") for seg in event.get("segs", [])) + " "
                                 for event in data.get("events", []) if not event.get("aAppend"))
                spoken = re.sub(r"\[(?:Music|Applause|Laughter)\]", "", spoken, flags=re.I)
                spoken = re.sub(r"\s+", " ", spoken).strip()
                if len(spoken) >= 500:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(spoken, encoding="utf-8")
                    return spoken
    except Exception:
        pass
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
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(spoken, encoding="utf-8")
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
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text


def ask_model(system, user, max_tokens=1400, timeout=180):
    if not LOCAL_AI or not MODEL:
        raise RuntimeError("Set both MORNING_MODEL_URL and MORNING_MODEL to use a text model")
    headers = {}
    key = os.environ.get("MORNING_MODEL_KEY", "")
    if LOCAL_AI.startswith("https://api.openai.com/"):
        key = key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("Set your own OPENAI_API_KEY to make reports")
    if key:
        headers["Authorization"] = "Bearer " + key
    for limit in (max_tokens, max_tokens * 2):
        payload = {
            "model": MODEL, "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}],
            "temperature": 0.1, "max_tokens": limit,
        }
        if os.environ.get("MORNING_DISABLE_REASONING") == "1":
            payload["reasoning_effort"] = "none"
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        response = requests.post(LOCAL_AI, json=payload, headers=headers, timeout=timeout)
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
              "If the speech is unrelated to the title or too thin, output exactly UNUSABLE. "
              "Use plain text, no Markdown. Do not add an introduction.")
    parts = []
    for i, chunk in enumerate(chunks, 1):
        parts.append(ask_model(system, "VIDEO: " + video["title"] + "\nCREATOR: " + video["channel"] +
                               "\nPART: " + str(i) + " of " + str(len(chunks)) +
                               "\nTRANSCRIPT:\n" + chunk, max_tokens=1400))
    if any(part.strip() == "UNUSABLE" for part in parts):
        return "UNUSABLE"
    if len(parts) == 1:
        return parts[0]
    return ask_model(system, "VIDEO: " + video["title"] + "\nCombine these notes from every part "
                     "of the video. Keep the most concrete points and any change of view.\n" +
                     "\n".join(parts), max_tokens=1200)


def subject_digest(subject, notes, angle=""):
    system = ("Write two very short lines from these video notes. Format exactly:\n"
              "Point: [at most 22 words; name the creator and a concrete claim]\n"
              "Why it matters: [at most 18 words; explain a direct practical consequence or decision supported by the notes]\n"
              "The second line must answer why a person should care about the first point, not repeat it. "
              "Use the owner's angle when supplied: prefer a supported trade-off, missing evidence, or who benefits. "
              "Do not state a hidden motive as fact. If the notes do not support a motive, leave it out. "
              "If the notes give no consequence, say 'The videos give no clear practical consequence.' "
              "Attribute claims to speakers. An implication is an implication, not a proven fact. "
              "Use plain words. No vague filler, bullets, Markdown, or facts absent from the notes.")
    source = "\n\n".join("VIDEO " + str(i + 1) + ": " + video["title"] + " — " + video["channel"] +
                           "\n" + note for i, (video, note) in enumerate(notes))
    answer = ask_model(system, "SUBJECT: " + subject["name"] + "\nOWNER'S ANGLE: " +
                       (angle or "None supplied") + "\n" + source, max_tokens=250)
    point = re.search(r"(?im)^\s*Point:\s*(.+)$", answer)
    why = re.search(r"(?im)^\s*Why it matters:\s*(.+)$", answer)
    if not point or not why:
        raise RuntimeError("The model did not give a point and why it matters")
    def short_line(value, limit):
        value = re.split(r"(?<=[.!?])\s+(?=[A-Z])", value.strip(), maxsplit=1)[0]
        words = value.split()
        return " ".join(words[:limit]).rstrip(",;:") + ("…" if len(words) > limit else "")
    claim = short_line(point.group(1), 24)
    if not claim.endswith((".", "!", "?", "…")):
        claim += "."
    return claim + " Why it matters: " + short_line(why.group(1), 20)


def free_notes(video, speech):
    """Select short, verbatim claims from speech without an AI account."""
    clean = re.sub(r"\[[^]]{1,30}\]", " ", speech)
    clean = re.sub(r"\s+", " ", clean).strip()
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", clean)
    title_words = {word.casefold() for word in re.findall(r"[A-Za-z]{4,}", video["title"])}
    common = {"this", "that", "with", "from", "they", "have", "what", "your", "about", "video", "made", "watch"}
    title_words -= common
    scored = []
    for sentence in sentences:
        words = re.findall(r"[A-Za-z0-9]+", sentence)
        if not 9 <= len(words) <= 42 or sentence.endswith("?"):
            continue
        lower = sentence.casefold()
        if any(filler in lower for filler in ("subscribe", "sponsor", "link in the description", "welcome back", "like this video", "thanks for watching")):
            continue
        overlap = sum(word in lower for word in title_words)
        number = bool(re.search(r"\d", sentence))
        reason = any(word in lower for word in ("because", "means that", "compared", "instead", "however", "but ", "according to", "percent", "%"))
        if not (overlap or number or reason):
            continue
        score = overlap * 3 + number * 3 + reason * 2 + min(len(words), 28) / 14
        scored.append((score, sentence.strip()))
    if not scored:
        return "UNUSABLE"
    selected = []
    for _, sentence in sorted(scored, reverse=True):
        tokens = set(re.findall(r"[a-z]{4,}", sentence.casefold()))
        if any(len(tokens & old) / max(1, len(tokens | old)) > .45 for old in (item[1] for item in selected)):
            continue
        selected.append((sentence, tokens))
        if len(selected) == 1:
            break
    return [item[0] for item in selected]


def free_digest(notes):
    video, excerpts = notes[0]
    excerpt = excerpts[0]
    words = excerpt.split()
    claim = " ".join(words[:27]) + ("…" if len(words) > 27 else "")
    result = video["channel"] + ' said: “' + claim + '”'
    consequence = re.search(r"\b(?:means that|which means|as a result|leads to|will|could|allows|prevents|makes it harder|makes it easier)\b[^.!?]*", excerpt, re.I)
    if consequence:
        words = consequence.group(0).split()
        result += ' Why it matters: “' + " ".join(words[:18]) + ("…" if len(words) > 18 else "") + '”'
    return result


def build_selection(config, progress=stamp):
    selection = []
    subjects = list(config["subjects"])
    for creator in config.get("followed", []):
        subjects.append({"name": creator["name"], "query": creator["name"],
                         "count": creator["count"], "creator": creator})
    for subject in subjects:
        progress("Finding recent, highly watched videos: " + subject["name"])
        candidates = choose_videos(subject, config["blocked"], extra=5)
        selection.append({"subject": subject["name"], "requested": subject["count"],
                          "videos": candidates[:subject["count"]],
                          "alternates": candidates[subject["count"]:]})
    return selection


def build_digest(config, selection, progress=stamp):
    model_mode = bool(LOCAL_AI and MODEL)
    sections = []
    for group in selection:
        videos = group["videos"] + group.get("alternates", [])
        if not videos:
            sections.append({"subject": group["subject"], "text": "No recent videos with readable speech were found.",
                             "videos": [], "requested": group["requested"]})
            continue
        notes = []
        failures = []
        for video in videos:
            if len(notes) >= group["requested"]:
                break
            progress("Reading speech: " + video["title"][:55])
            try:
                speech = transcript(video["id"])
                note = video_notes(video, speech) if model_mode else free_notes(video, speech)
                if note == "UNUSABLE":
                    video["speech_error"] = "Speech did not contain useful points about this subject"
                    continue
                notes.append((video, note))
            except Exception as exc:
                video["speech_error"] = str(exc)[:160]
                failures.append(video["speech_error"])
        if not notes:
            content = "No recent videos with readable speech were found."
        else:
            progress("Combining what was said: " + group["subject"])
            if model_mode:
                try:
                    content = subject_digest({"name": group["subject"]}, notes, config.get("angle", ""))
                except Exception:
                    video, note = notes[0]
                    first = next((line.lstrip("-• ").strip() for line in note.splitlines()
                                  if line.strip()), "No clear point was found.")
                    content = video["channel"] + " said: " + " ".join(first.split()[:28])
            else:
                content = free_digest(notes)
        group["videos"] = [video for video, _ in notes]
        group.pop("alternates", None)
        sections.append({"subject": group["subject"], "text": content,
                         "videos": [video for video, _ in notes], "requested": group["requested"]})
    return sections


def telegram_text(sections, timezone_name):
    day = datetime.now(ZoneInfo(timezone_name)).strftime("%d %B %Y")
    lines = ["Leave YouTube · " + day]
    for section in sections:
        lines.append(section["subject"] + ": " + section["text"])
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


def record_delivery(config, automatic=False):
    atomic_json(DELIVERY_FILE, {"sent_date":
                datetime.now(ZoneInfo(config["timezone"])).date().isoformat(),
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "automatic": automatic})


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
                  "kind": kind, "sent": False, "settings": config}
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
                record_delivery(config, automatic)
        atomic_json(STATE_FILE, result)
        return result


def start_job(kind):
    if not job_lock.acquire(blocking=False):
        if kind == "send" and job_state.get("running") and job_state.get("kind") == "preview":
            stamp("Will send as soon as the preview finishes", send_after_preview=True)
            return
        raise RuntimeError("A preview or report is already running")
    stamp("Starting", running=True, kind=kind, error=None, send_after_preview=False)

    def worker():
        try:
            result = run(kind)
            if kind == "preview" and job_state.get("send_after_preview"):
                if not any(section["videos"] for section in result["sections"]):
                    raise RuntimeError("No videos had readable speech; Telegram was not sent")
                stamp("Sending the preview to Telegram", kind="send")
                send_telegram(result["message"])
                result["sent"] = True
                result["kind"] = "send"
                atomic_json(STATE_FILE, result)
                record_delivery(settings())
            stamp("Finished", running=False)
        except Exception as exc:
            stamp("Could not finish", running=False, error=str(exc)[:300])
        finally:
            job_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def send_recent_preview():
    if not STATE_FILE.exists() or not job_lock.acquire(blocking=False):
        return False
    try:
        with LOCK_FILE.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if saved.get("kind") != "preview" or saved.get("sent"):
                return False
            if not any(section.get("videos") for section in saved.get("sections", [])):
                return False
            age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["at"])
            config = settings()
            if age > timedelta(hours=2) or (saved.get("settings") and saved["settings"] != config):
                return False
            stamp("Sending the preview to Telegram", running=True, kind="send", error=None)
            try:
                send_telegram(saved["message"])
                record_delivery(config)
                saved["sent"] = True
                saved["kind"] = "send"
                atomic_json(STATE_FILE, saved)
                stamp("Finished", running=False)
            except Exception as exc:
                stamp("Could not send", running=False, error=str(exc)[:300])
                raise
            return True
    finally:
        job_lock.release()


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


@app.get("/portrait.jpg")
def portrait():
    return send_file(ROOT / "assets" / "creator-portrait.jpg", mimetype="image/jpeg")


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
        if kind == "send" and send_recent_preview():
            return jsonify({"sent": True})
        start_job(kind)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"started": True})


def daily_due(config, state):
    if not config["enabled"] or not (config["subjects"] or config.get("followed")):
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
