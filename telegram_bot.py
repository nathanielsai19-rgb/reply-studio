"""Reply Studio Telegram bot.

One GitHub Actions run = one ~5h45m long-poll loop. Cron fires every 6 hours so
the bot is up around the clock with no server and no PC. Only the owner's chat
is served (fail-closed, like the verse watchdog).

Usage (in your private chat with the bot):
  any text       -> 3 reply comments for that post text
  photo / video  -> 3 replies on the media (a caption is treated as the post text)
  /post [topic]  -> 3 original posts (topic optional — it picks if empty)
  /gif <desc>    -> animated GIF from the description, morph mode (~1-4 min)
  /gifz <desc>   -> zoom-loop GIF (1 image, faster)
  /help          -> this list
"""
import os
import random
import shutil
import tempfile
import time

import requests

from app import (describe_images, extract_frames, generate_posts, generate_replies,
                 has_audio, horde_images, make_gif, run, transcribe)

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"
OWNER = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
OFFSET_FILE = "offset.txt"
RUN_SECONDS = 345 * 60      # 5h45m — headroom under the 6h Actions job cap
GIF_SUFFIX = ", vibrant colors, sharp details, no text"

HELP = ("Reply Studio\n\n"
        "paste a post text -> 3 short replies\n"
        "send a photo or video (with optional caption) -> 3 replies on it\n"
        "/post [topic] -> 3 original posts\n"
        "/gif <description> -> animated gif (morph, ~1-4 min)\n"
        "/gifz <description> -> zoom-loop gif (faster)\n"
        "Replies arrive in under a minute.")


def tg(method, **kw):
    r = requests.post(f"{TG}/{method}", timeout=120, **kw)
    r.raise_for_status()
    return r.json()


def send_text(chat, text):
    tg("sendMessage", json={"chat_id": chat, "text": text[:4000]})


def send_gif(chat, path):
    with open(path, "rb") as f:
        tg("sendAnimation", data={"chat_id": chat}, files={"animation": f})


def format_replies(replies):
    return "\n\n".join(r["text"] for r in replies)


def download_file(file_id, dest):
    info = tg("getFile", params={"file_id": file_id})
    url = f"https://api.telegram.org/file/bot{os.environ['TELEGRAM_TOKEN']}/{info['result']['file_path']}"
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)


# ---------- handlers (all in a temp dir per message) ----------

def handle_text(msg, text):
    if text.startswith("/"):
        if text.startswith("/post"):
            topic = text[len("/post"):].strip()
            posts, _ = generate_posts(topic)
            send_text(msg["chat"]["id"], "Posts:\n\n" + format_replies(posts))
        elif text.startswith("/gifz"):
            desc = text[len("/gifz"):].strip()
            handle_gif(msg, desc, "zoom")
        elif text.startswith("/gif"):
            desc = text[len("/gif"):].strip()
            handle_gif(msg, desc, "morph")
        else:
            send_text(msg["chat"]["id"], HELP)
        return
    replies, _, _ = generate_replies("TEXT OF THE POST:\n" + text[:6000])
    send_text(msg["chat"]["id"], format_replies(replies))


def handle_gif(msg, desc, mode):
    chat = msg["chat"]["id"]
    if not desc:
        send_text(chat, "describe the gif, e.g. /gif a cat breakdancing")
        return
    send_text(chat, f"making your gif ({mode}) — this can take a few minutes")
    work = tempfile.mkdtemp(prefix="rs_")
    try:
        images = horde_images(desc + GIF_SUFFIX, 1 if mode == "zoom" else 3)
        out = os.path.join(work, "gif.gif")
        make_gif(images, out, mode)
        send_gif(chat, out)
    except Exception as e:
        send_text(chat, f"gif failed: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def handle_media(msg, is_video):
    chat = msg["chat"]["id"]
    send_text(chat, "reading it — replies in a minute or two")
    work = tempfile.mkdtemp(prefix="rs_")
    try:
        media = msg["video"] if is_video else msg["photo"][-1]
        src = os.path.join(work, "vid.mp4" if is_video else "img.jpg")
        download_file(media["file_id"], src)
        if is_video:
            duration = float(run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration", "-of", "csv=p=0", src]).strip() or 0)
            visual = describe_images(extract_frames(src, work, duration), is_video=True)
            audio = transcribe(src, work) if has_audio(src) else "(no audio track)"
            understanding = f"VISUAL DESCRIPTION:\n{visual}\n\nNARRATION/AUDIO TRANSCRIPT:\n{audio}"
        else:
            understanding = "VISUAL DESCRIPTION:\n" + describe_images([src], is_video=False)
        caption = (msg.get("caption") or "").strip()
        if caption:
            understanding = f"TEXT OF THE POST:\n{caption[:3000]}\n\n" + understanding
        replies, _, _ = generate_replies(understanding)
        send_text(chat, format_replies(replies))
    except Exception as e:
        send_text(chat, f"couldn't read that one: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def handle_update(upd):
    msg = upd.get("message")
    if not msg:
        return
    if OWNER and msg["chat"]["id"] != OWNER:
        return                                   # not the owner — ignore
    try:
        if msg.get("text"):
            handle_text(msg, msg["text"])
        elif msg.get("photo"):
            handle_media(msg, is_video=False)
        elif msg.get("video"):
            handle_media(msg, is_video=True)
        elif msg.get("document") and (msg["document"].get("mime_type") or "").startswith("image/"):
            handle_media(msg, is_video=False)
    except Exception as e:
        try:
            send_text(msg["chat"]["id"], f"something broke: {e}")
        except Exception:
            pass


# ---------- main loop ----------

def read_offset():
    try:
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def main():
    offset = read_offset()
    deadline = time.time() + RUN_SECONDS
    while time.time() < deadline:
        try:
            params = {"timeout": 50}
            if offset:
                params["offset"] = offset
            data = tg("getUpdates", params=params)
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                handle_update(upd)
                write_offset(offset)
        except Exception:
            time.sleep(5)
    if offset:
        write_offset(offset)


if __name__ == "__main__":
    main()
