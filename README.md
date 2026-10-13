# Reply Studio

Short reply comments for your X account + an AI GIF maker + original posts. Web app on the PC, Telegram bot in the cloud (phone works even with the PC off).

Web app tabs: **Media** (video/image replies), **Text** (post-text replies), **Post** (original posts), **GIF** (AI GIF maker, morph/zoom). Casual/Dry voice picker. Copy-only — you post yourself; never automated posting.

Telegram bot: send text/photo/video → 3 replies; `/post [topic]` → original posts; `/gif <desc>` / `/gifz <desc>` → GIFs. Runs on GitHub Actions (6h runs, cron every 6h), owner-chat only.

## How it works

| Input | Pipeline |
|---|---|
| Video | ffmpeg extracts ~1 frame per 8s (max 30) → `qwen/qwen3.8-27b` vision describes the frames → whisper-large-v3-turbo transcribes the audio → a text model writes 3 reply comments |
| Image | Downscaled → same vision model → same reply step |
| Text | Post text goes straight to the reply step |
| Post | Original posts from a topic (or the model picks), 3 angles |
| GIF | Stable Horde (free, anonymous, no signup) generates 1-3 images → Pillow stitches them into a looping GIF |

Free APIs: Groq (vision/audio/text) and Stable Horde (images). No payment anywhere.

## Run (PC web app)

Double-click `run.bat` — it starts the app invisibly and opens http://127.0.0.1:5190
Auto-start: a shortcut in the shell:startup folder launches it at logon (`--no-browser`).

**Phone, same WiFi:** `http://<pc-lan-ip>:5190` — the URL is shown in the page footer.
**Phone, any network:** Tailscale → `http://<pc-tailscale-ip>:5190` (also in the footer).
If the page won't load from the phone, Windows Firewall is blocking the port — run once in an admin PowerShell:

```
netsh advfirewall firewall add rule name="Reply Studio 5190" dir=in action=allow protocol=TCP localport=5190
```

**Phone, PC off:** the Telegram bot (see below).

## Telegram bot (cloud)

1. Create a bot: in Telegram chat with @BotFather → `/newbot` → name it, pick a username. BotFather gives a token.
2. In the GitHub repo: Settings → Secrets and variables → Actions → add:
   - `TELEGRAM_TOKEN` = the BotFather token
   - `TELEGRAM_CHAT_ID` = your numeric chat id
   - `GROQ_API_KEY` = your Groq key (contents of `groq_key.txt`)
3. Actions → reply-bot → Run workflow (or wait for the next 6h cron). Message your bot in Telegram.

Replies arrive in under a minute; GIFs take 1-4 min. The bot only answers your chat.

## Notes

- Repo is public (code only — all keys live in GitHub secrets / local gitignored files). Keeps Actions minutes free/unlimited, same setup as the verse-bot repo.
- Requires `ffmpeg`/`ffprobe` on PATH for the PC app (`pip install flask requests pillow`); the Actions runner and Docker image include ffmpeg.
- Old generated GIFs are auto-deleted after 2 days.
- `deploy/` holds a VM systemd setup (unused — no Hetzner account; parked until there's a server).
