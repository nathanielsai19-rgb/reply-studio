# Reply Studio

Short reply comments for your X account + an AI GIF maker. Works on PC and phone (same WiFi).

Three tabs:
- **Media** — drop a video or image (or paste a screenshot), get 3 short human-sounding replies
- **Text** — paste a post's text, get 3 short replies
- **GIF** — describe a GIF and it makes one: *Morph* (3 AI images crossfade) or *Zoom* (smooth loop on 1 image)

Replies are copy-only — you post them yourself. Never automated posting.

## How it works

| Input | Pipeline |
|---|---|
| Video | ffmpeg extracts ~1 frame per 8s (max 30) → `qwen/qwen3.8-27b` vision describes the frames → whisper-large-v3-turbo transcribes the audio → a text model writes 3 reply comments (funny / hype / thoughtful) |
| Image | Downscaled to ≤1280px → same vision model → same reply step |
| Text | Post text goes straight to the reply step |
| GIF | Stable Horde (free, anonymous, no signup) generates 1-3 images → Pillow stitches them into a looping GIF |

Free APIs: Groq (vision/audio/text) and Stable Horde (images). No payment anywhere.

## Run

Double-click `run.bat` (or `python app.py`) — the browser opens at http://127.0.0.1:5190

**Phone, same WiFi:** open `http://<pc-lan-ip>:5190` — the URL is shown in the page footer.

**Phone, any network (Tailscale):** install Tailscale on the phone, log in with the same account as the PC, then open `http://<pc-tailscale-ip>:5190` — also shown in the footer while Tailscale is running. Tailscale keeps the app private (only your devices), unlike a public tunnel.

If the page won't load from the phone either way, Windows Firewall is blocking the port: run this once in an admin PowerShell:

```
netsh advfirewall firewall add rule name="Reply Studio 5190" dir=in action=allow protocol=TCP localport=5190
```

(Or click "Allow" on the Windows Firewall popup when the app starts.)

## Notes

- Code lives in a private GitHub repo (`nathanielsai19-rgb/reply-studio`). The Groq key is **not** in git — it loads from `groq_key.txt` (gitignored) or the `GROQ_API_KEY` env var.
- Requires `ffmpeg`/`ffprobe` on PATH and `pip install flask requests pillow`.
- Videos take ~2 minutes; images ~30 seconds; GIFs 1-5 minutes (anonymous queue on Stable Horde).
- Old generated GIFs are auto-deleted after 2 days.
- **Ops VM (always-on):** `deploy/install.sh` deploys it to the Hetzner VM so the app works even when this PC is off. The VM binds to its Tailscale IP only (`REPLY_HOST` in the service file), so the app is never reachable from the public internet.
