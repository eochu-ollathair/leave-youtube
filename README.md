# Leave YouTube — Lose the adverts, not the info

Leave YouTube is an open source, self hosted YouTube summarizer that sends a daily video digest to Telegram. Choose subjects, choose how many recent popular videos to consider for each one, and block creators you dislike. The app reads available video speech, combines repeated points, and sends a concrete morning report. The settings page shows the selected videos and can make a report preview.

This is a small app you run for yourself. It does not host accounts for multiple people. Your settings, access key, and report history stay in a local `data/` folder, which is excluded from GitHub.

## What you need

- Python 3.10 or newer
- A Telegram bot token and your Telegram chat number
- A text model that accepts the common chat completion request format
- Internet access for video search, speech text, and Telegram delivery

## Start it

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='your-bot-token'
export TELEGRAM_CHAT_ID='your-chat-number'
export MORNING_MODEL_URL='http://127.0.0.1:11438/v1/chat/completions'
export MORNING_MODEL='your-model-name'
python app.py serve --host 127.0.0.1 --port 19133
```

Open `http://127.0.0.1:19133/#key=YOUR_KEY`. The first run creates the key in `data/access-key`. Use that link once; the browser then stays signed in. Keep the key private. If your model needs an access key, set `MORNING_MODEL_KEY` too.

The starting subjects are Phones, Politics, AI and Fashion. Change or remove them on the page. Set your time and time zone there; it starts with Ireland time. Turn on morning delivery when you are ready. The page has buttons to see chosen videos, make a preview, and send a report immediately.

## Send every morning

Run `python app.py daily` every ten minutes with your machine's scheduler. It sends once per local calendar day after the chosen time. It does nothing if morning delivery is switched off, no subjects are saved, or that day's report was already sent. Keep the same Telegram and model settings in the scheduled process.

## If you publish the settings page on a website

Put it behind HTTPS. Forward a private path such as `/leave-youtube` to this app, removing that path before the request reaches it, and set `LEAVE_YOUTUBE_BASE=/leave-youtube`. Set `LEAVE_YOUTUBE_SITE_URL` to the page address if you want it at the end of the Telegram report. Do not expose `data/`, `.env`, or the opening key.

The app uses YouTube search through `yt-dlp`, then checks each video's publication date, length and view count. It currently considers videos from the last seven days. It tries two public speech text services; either may refuse a video or limit requests. A video with no readable speech is not summarized. Video IDs are sent to those services, the speech text goes to the model you configure, and the finished report goes to Telegram. The report describes what video speakers said; it does not independently prove their claims.

## Settings

| Setting | What it does |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot that sends the report |
| `TELEGRAM_CHAT_ID` | Your Telegram chat number |
| `MORNING_MODEL_URL` | Address of your text model's chat completion endpoint |
| `MORNING_MODEL` | Model name to request |
| `MORNING_MODEL_KEY` | Optional model access key |
| `MORNING_DATA_DIR` | Optional folder for private settings and history |
| `MORNING_ACCESS_KEY` | Optional fixed key for opening the settings page |
| `LEAVE_YOUTUBE_BASE` | Website path when hosted behind a path forwarding service |
| `LEAVE_YOUTUBE_SITE_URL` | Optional settings page link in Telegram |

The optional `MORNING_CREDENTIALS_FILE` setting is for an existing private credential file. Normal installations can use the automatically created `data/access-key`.
