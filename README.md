# Leave YouTube — Lose the adverts, keep the info

Choose subjects, choose how many recent popular videos to read, follow creators you like, and block creators you do not. Get a short Telegram report instead of watching for hours.

## Download the program

**[Download Leave YouTube here](https://eochu.app/leave-youtube/source.zip).** It downloads one ZIP file. Open it, then read **[START_HERE.md](START_HERE.md)** inside. The starter walks you through setting up Telegram and opening your private settings page. You do not need GitHub's Branch button.

This version runs on a Mac or Linux computer with Python 3.10 or newer. It is not a phone app, and the Windows starter is not ready yet. The computer must stay on to send the morning report.

## Which Telegram chat?

Eochu's live reports come from [@cuntz2_bot](https://t.me/cuntz2_bot). Open that chat and send **“add YouTube The United Stand”** to include a creator or **“add subject fashion”** to add a subject. Reply to a **new** YouTube report with **“kill that YouTube”** when it names one creator; that creator will stay out of future reports. If several creators are in the report, or the report is older, send **“kill YouTube The United Stand”** using the creator's name. Telegram confirms each change.

If you download this program for yourself, the starter helps you create **your own** Telegram bot. Eochu's chat does not control your copy.

## Give this to your own AI

Send it this [project link](https://github.com/eochu-ollathair/leave-youtube) and say: **“Read AGENTS.md and get my own copy working. Help me choose videos, make a real preview, and connect my Telegram. Show me what actually worked.”** The [assistant instructions](AGENTS.md) give it the exact checks. It should use your own accounts and keep your private details off GitHub.

The free report quotes useful speech from videos and needs no AI account. If you connect your own text AI, it can combine repeated points and disagreements, explain why a point matters, and use the sceptical angle you write. A suspected motive stays a possibility unless there is evidence. The maker's own AI is not available to other users.

## For people who want to run it by hand

The starter runs `app.py serve` and checks once a minute whether the morning report is due. The private page controls subjects, creators, blocked channels, delivery time and preview. Video search uses `yt-dlp`; speech comes from available captions or fallback speech services. Videos with no readable speech are left out. After a video is sent, it disappears from the page and is never chosen for another report. Reports describe what speakers claimed; they do not independently prove those claims.

You can also run `python3 app.py daily` from a scheduled task if you prefer. The program saves the last delivery date and sends once per local day after the chosen time. Use the same Telegram settings each time. The starter keeps your bot code and chat number in the private `data/telegram.json` file, which is excluded from GitHub. A manual setup may instead set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in its environment. The optional text AI uses `MORNING_MODEL_URL`, `MORNING_MODEL`, and if needed `MORNING_MODEL_KEY`.

To put your own settings page on a public website, put it behind HTTPS and forward a private path to this app. Set `LEAVE_YOUTUBE_BASE` to that path. Keep `data/` and its private opening key out of public files.
