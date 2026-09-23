# Leave YouTube — Lose the adverts, not the info

Leave YouTube is an open source program that sends a daily YouTube video digest to Telegram. Choose subjects, choose how many recent popular videos to consider for each one, add creators whose videos you want included, and block creators you dislike. It reads available video speech and sends a few short, concrete lines. The settings page shows the selected videos and can make a report preview.

This is a small app you run for yourself. It does not host accounts for multiple people. The free default picks useful passages directly from the videos' speech and names the creator. It needs no AI account or AI key. If you connect a text model you run or pay for yourself, it can combine repeated points and disagreements, explain why a point matters, and apply the angle you write on the settings page. That angle can ask who benefits or what is missing; possible motives must stay labelled as possibilities. The free default can show a spoken consequence when one is explicit, but cannot reliably infer hidden motives. There is no route to the original maker's local model or AI account. Your settings, access key, and report history stay in a local `data/` folder, which is excluded from GitHub.

## What you need

- Python 3.10 or newer
- A Telegram bot token and your Telegram chat number
- Internet access for video search, speech text, and Telegram delivery

## Start it

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='your-bot-token'
export TELEGRAM_CHAT_ID='your-chat-number'
python app.py serve --host 127.0.0.1 --port 19133
```

The first run creates a private opening key in `data/access-key`. Read it with `cat data/access-key`, then open `http://127.0.0.1:19133/#key=YOUR_KEY`, replacing `YOUR_KEY` with that value. The browser then stays signed in. Keep the key private.

The starting subjects are Phones, Politics, AI and Fashion. Change or remove them on the page. Add a specific creator, such as The United Stand, if you want their recent videos in a separate report section. Write your angle in the box provided if you use a text model. Set your time and time zone there; it starts with Ireland time. Turn on morning delivery when you are ready. The page has buttons to see chosen videos, make a preview, and send a report immediately.

To get a more condensed comparison, set both `MORNING_MODEL_URL` and `MORNING_MODEL` to a model available to you. Set `MORNING_MODEL_KEY` only if that model needs a key. A model running on your own computer can work without an AI key. Those settings apply only to your copy of the app.

## Send every morning

Run `python app.py daily` every ten minutes with your machine's scheduler. It sends once per local calendar day after the chosen time. It does nothing if morning delivery is switched off, no subjects are saved, or that day's report was already sent. Keep the same Telegram settings and any optional model settings in the scheduled process.

## If you publish the settings page on a website

Put it behind HTTPS. Forward a private path such as `/leave-youtube` to this app, removing that path before the request reaches it, and set `LEAVE_YOUTUBE_BASE=/leave-youtube`. Do not expose `data/`, `.env`, or the opening key.

The app uses YouTube search through `yt-dlp`, then checks each video's publication date, length and view count. It currently considers videos from the last seven days. It reads YouTube captions directly when available and keeps a local copy so later reports can reuse them. Two public speech text services are fallbacks; they may refuse a video or limit requests. A video with no readable speech is not summarized. Speech text is processed on your computer in the free default; it goes to a text model only if you configure one. The finished report goes to Telegram. The report describes what video speakers said; it does not independently prove their claims.

## Settings

| Setting | What it does |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot that sends the report |
| `TELEGRAM_CHAT_ID` | Your Telegram chat number |
| `MORNING_MODEL_URL` | Optional address of a text model for shorter combined reports |
| `MORNING_MODEL` | Optional model name; use with `MORNING_MODEL_URL` |
| `MORNING_MODEL_KEY` | Optional model access key |
| `MORNING_DISABLE_REASONING` | Set to `1` for models that use their answer allowance for hidden thinking instead of the report |
| `MORNING_DATA_DIR` | Optional folder for private settings and history |
| `MORNING_ACCESS_KEY` | Optional fixed key for opening the settings page |
| `LEAVE_YOUTUBE_BASE` | Website path when hosted behind a path forwarding service |

The optional `MORNING_CREDENTIALS_FILE` setting is for an existing private credential file. Normal installations can use the automatically created `data/access-key`.
