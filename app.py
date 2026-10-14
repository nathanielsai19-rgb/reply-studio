"""Reply Studio — paste media or a post text, get short human-sounding reply comments.
Also makes GIFs from a text prompt (Pollinations free image gen -> Pillow crossfade/zoom).

Pipeline (all on free tiers):
  video: ffmpeg frames -> qwen3.8 vision -> whisper audio -> text model replies
  image: qwen3.8 vision -> text model replies
  text:  text model replies
  gif:   pollinations.ai images -> PIL animated GIF

Serves on 0.0.0.0 so your phone on the same WiFi can use it too.
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from io import BytesIO

import requests
from flask import Flask, jsonify, render_template, request
from PIL import Image
from werkzeug.utils import secure_filename

GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO = "https://api.groq.com/openai/v1/audio/transcriptions"

VISION_MODEL = "qwen/qwen3.8-27b"
TEXT_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
WHISPER_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]

MAX_FRAMES = 30          # vision requests stay snappy on the free tier
FRAME_SECONDS = 8        # one frame every N seconds of video
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".ts", ".avi", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

HORDE_GENERATE = "https://stablehorde.net/api/v2/generate/async"
HORDE_STATUS = "https://stablehorde.net/api/v2/generate/status/{job_id}"
HORDE_TIMEOUT = 420   # seconds to wait for a free anonymous worker
GIF_SIZE = 512           # frames per gif, keeps file small enough for X
GIF_TTL_SECONDS = 2 * 24 * 3600

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")
GIF_DIR = os.path.join(BASE, "static", "gifs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(GIF_DIR, exist_ok=True)

# API key: GROQ_API_KEY env var, else local groq_key.txt (never committed to git)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip() or \
    open(os.path.join(BASE, "groq_key.txt"), encoding="utf-8").read().strip()

HEADERS = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
PORT = int(os.environ.get("PORT", 5190))   # Render sets PORT; local default 5190


# ---------- helpers ----------

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-400:] or f"{cmd[0]} failed")
    return p.stdout


def groq_chat(messages, model, temperature=0.2, max_tokens=2048):
    r = requests.post(
        GROQ_CHAT,
        headers=HEADERS,
        json={"model": model, "messages": messages,
              "temperature": temperature, "max_completion_tokens": max_tokens},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def mime_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/jpeg"


def lan_ip():
    """Best-effort LAN address so the phone can reach this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


TAILSCALE_CLI = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"


def tailscale_ip():
    """The machine's 100.x.y.z Tailscale address, or None if Tailscale is off."""
    try:
        out = run([TAILSCALE_CLI, "ip", "-4"])
        return out.strip().splitlines()[0] or None
    except Exception:
        return None


def describe_images(paths, is_video):
    """Send images in batches of 3 (qwen3.8 limit) to the Groq vision model."""
    if not is_video:
        prompt = ("Describe this image: what/who is shown, the setting, any on-screen text "
                  "(quote it exactly), and the mood. Be concrete and grounded in what is visible. "
                  "Ignore watermarks.")
    else:
        prompt = ("These are frames from a video, in chronological order, several seconds apart. "
                  "For each frame describe: what/who is shown, the setting, any on-screen text "
                  "(quote it exactly), and the mood. Format: 'Frame N: ...'. Ignore watermarks.")

    parts = []
    for i in range(0, len(paths), 3):
        batch = paths[i:i + 3]
        content = [{"type": "text", "text": prompt}]
        for p in batch:
            b64 = base64.b64encode(open(p, "rb").read()).decode()
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime_for(p)};base64,{b64}"}})
        ok = False
        for _ in range(2):
            try:
                parts.append(groq_chat([{"role": "user", "content": content}], VISION_MODEL))
                ok = True
                break
            except requests.HTTPError:
                time.sleep(10)
            except Exception:
                break
        if not ok:
            parts.append("(vision batch failed)")
    return "\n\n".join(parts)


def extract_frames(path, workdir, duration):
    n = min(MAX_FRAMES, max(1, int(duration // FRAME_SECONDS)))
    fps = n / duration if duration > 0 else 1
    out = os.path.join(workdir, "f%02d.jpg")
    run(["ffmpeg", "-y", "-v", "error", "-i", path,
         "-vf", f"fps={fps},scale=854:480:force_original_aspect_ratio=decrease", "-q:v", "4", out])
    return sorted(glob.glob(os.path.join(workdir, "f*.jpg")))


def has_audio(path):
    info = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", path])
    return "audio" in info


def transcribe(path, workdir):
    wav = os.path.join(workdir, "audio.wav")
    run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav])
    with open(wav, "rb") as f:
        data = f.read()
    for model in WHISPER_MODELS:
        try:
            r = requests.post(
                GROQ_AUDIO,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("audio.wav", data, "audio/wav")},
                data={"model": model, "response_format": "json", "temperature": "0"},
                timeout=300,
            )
            r.raise_for_status()
            return r.json().get("text", "")
        except Exception:
            continue
    return "(transcription failed)"


def generate_replies(understanding, preferred=None):
    content = understanding[:6000]
    prompt = (
        "You are about to reply on X (Twitter) as a real person who just saw this post. "
        "Below is the post's content (visual description, narration transcript, "
        "and/or its text).\n\n"
        "POST CONTENT:\n" + content + "\n\n"
        "Write 3 replies, each from a different person.\n\n"
        "How to sound real:\n"
        "- Grab a SPECIFIC detail from the post — a word, a number, a name, a moment — and "
        "react to that. A reply that could sit under any post is banned.\n"
        "- Text it, don't write it. Uneven case, fragments, dropped words: 'nah this is "
        "crazy', 'bro really did that', 'he knew'. Thumbs typing fast.\n"
        "- Land the point and stop. Cut trailing explanations and closing sentences.\n"
        "- React, don't review. Don't restate the post, don't explain why it's funny "
        "or impressive.\n"
        "- Humor is dry. Not every reply is a joke; 2-word replies are valid.\n"
        "- Never: emojis, hashtags, em-dashes, or these dead phrases — honestly, truly, "
        "the way, imagine, living rent free, power move, fancy way of saying, "
        "we are so back, real ones know.\n\n"
        "Reply shapes should differ — some one-liners, some two lines, different energy "
        "each.\n\n"
        "Reference points for feel (for a post of a cat that knocked a TV over):\n"
        "'cats really do run the house'\n"
        "'rip tv'\n"
        "'he knocked it over then just sat there like nothing happened'\n\n"
        "Output ONLY a JSON array: [{\"vibe\": \"one or two words\", \"text\": \"...\"}]"
    )
    data, model = gen_json(prompt, preferred)
    # refinement pass: make every reply actually react to THIS content
    refine = (
        "You are tightening replies to a post on X (Twitter). Here is what the post contains:\n\n"
        "POST CONTENT:\n" + content[:3000] + "\n\n"
        "These draft replies exist:\n" + json.dumps(data, ensure_ascii=False) + "\n\n"
        "Fix them:\n"
        "- Every reply must react to a SPECIFIC detail from the post — a word, a number, "
        "a name, a moment. If a reply is generic and could sit under any post, rewrite it "
        "to grab onto something concrete from the content above.\n"
        "- Keep voice rules: text it don't write it, uneven case and fragments ok, no "
        "emojis, no hashtags, no em-dashes, no dead phrases (honestly, truly, the way, "
        "imagine, power move, living rent free, we are so back, unreal, cinema), land the "
        "point and stop, dry humor fine, each reply a different energy, some one-liners.\n"
        "- Keep each reply's vibe label, updating it if the rewrite changes the energy.\n"
        "- If a reply is already specific and human, leave it exactly as it is.\n\n"
        "Output ONLY a JSON array: [{\"vibe\": \"...\", \"text\": \"...\"}]"
    )
    try:
        refined, _ = gen_json(refine, preferred)
        if refined:
            data = refined
    except Exception:
        pass
    return data, model


def generate_posts(topic, preferred=None):
    """Original X posts (not replies). topic may be empty — the model picks."""
    if topic:
        brief = f"The post should be about: {topic[:1500]}"
    else:
        brief = ("Pick a subject yourself — something real and specific that would "
                 "actually land with people right now.")
    prompt = (
        "You are writing an original post for X (Twitter). It must be VERY good — the "
        "kind that makes people stop scrolling and want to reply.\n\n"
        + brief + "\n\n"
        "Write 3 different posts.\n\n"
        "Requirements:\n"
        "- Short. Most are 1-3 lines. No threads.\n"
        "- A real person typed this fast. Uneven case ok, fragments ok. No emojis, "
        "no hashtags, no em-dashes.\n"
        "- Strong first line: a hook, a surprising take, a specific detail, or a question "
        "people will actually answer.\n"
        "- Original take. If it sounds like an engagement-farming account wrote it, rewrite.\n"
        "- Banned formats: 'unpopular opinion:', 'hot take:', 'POV:', 'thread:', "
        "'am I the only one', 'reminder that', 'nobody is talking about'.\n"
        "- Banned phrases: honestly, truly, the way, imagine, living rent free, "
        "power move, we are so back, real ones know, let that sink in.\n"
        "- Specific beats vague. Numbers, names, concrete moments.\n"
        "- No motivation-poster energy, no life coaching unless it's genuinely funny.\n"
        "- No engagement bait ('like if you agree', 'rt to save a life', 'this will get "
        "me canceled').\n"
        "- Dry humor and honest opinions welcome.\n"
        "- Three different angles: one funny, one honest/relatable, one sharp observation.\n\n"
        "Output ONLY a JSON array: [{\"vibe\": \"one or two words\", \"text\": \"...\"}]"
    )
    return gen_json(prompt, preferred)


def gen_json(prompt, preferred=None):
    """Run the model-loop and parse a JSON array of {\"vibe\", \"text\"} objects."""
    models = TEXT_MODELS
    if preferred and preferred in TEXT_MODELS:
        models = [preferred] + [m for m in TEXT_MODELS if m != preferred]
    for model in models:
        for _ in range(2):
            try:
                raw = groq_chat([{"role": "user", "content": prompt}], model,
                                temperature=0.95, max_tokens=800)
                raw = raw.replace("```json", "").replace("```", "")
                start, end = raw.find("["), raw.rfind("]")
                if start == -1 or end == -1:
                    continue
                data = json.loads(raw[start:end + 1])
                if isinstance(data, list) and data:
                    return data, model
            except requests.HTTPError:
                time.sleep(10)
            except Exception:
                break
    raise RuntimeError("generation failed — try again")


# ---------- gif helpers ----------

def horde_image(prompt, variation=0, count=1):
    """One Stable Horde generation. Anonymous (no key): free, but queues behind
    registered users and is rate-limited. Returns a PIL image."""
    vp = prompt
    if count > 1:
        vp = (f"{prompt}, variation {variation + 1} of {count}: "
              "different pose and angle, same subject and style")
    body = {
        "prompt": vp,
        "n": 1,
        "params": {
            "width": GIF_SIZE,
            "height": GIF_SIZE,
            "steps": 28,
            "cfg_scale": 7.0,
            "sampler_name": "k_euler",
            "seed": str(random.randint(1, 10**9)),
        },
    }
    job_id = None
    for attempt in range(4):
        try:
            r = requests.post(HORDE_GENERATE, json=body, timeout=60,
                              headers={"apikey": "0000000000"})
            if r.status_code == 429:      # anonymous rate limit — back off
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            job_id = r.json()["id"]
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))
    if job_id is None:
        raise RuntimeError("image generator is rate-limited right now — wait a minute and try again")

    deadline = time.time() + HORDE_TIMEOUT
    while time.time() < deadline:
        s = requests.get(HORDE_STATUS.format(job_id=job_id), timeout=30)
        s.raise_for_status()
        st = s.json()
        if st.get("done"):
            for g in st.get("generations", []):
                img = g["img"]
                # newer horde returns a signed URL, older returned base64
                if img.startswith("http"):
                    data = requests.get(img, timeout=120).content
                else:
                    data = base64.b64decode(img)
                return Image.open(BytesIO(data)).convert("RGB")
            raise RuntimeError("horde finished with no image")
        if st.get("faulted"):
            raise RuntimeError("image generation failed on the horde workers")
        time.sleep(6)
    raise RuntimeError("timed out waiting for a free worker — try again in a bit")


def horde_images(prompt, count):
    """count images, requested one at a time (anonymous queue is rate-limited)."""
    images = []
    for i in range(count):
        images.append(horde_image(prompt, i, count))
        if i < count - 1:
            time.sleep(15)   # stagger so the free workers don't flag us
    return images


def zoom_frame(img, s):
    w, h = img.size
    big = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    x = (big.width - w) // 2
    y = (big.height - h) // 2
    return big.crop((x, y, x + w, y + h))


def make_gif(images, out_path, mode):
    """mode 'zoom': one image, smooth zoom in/out ping-pong loop.
    mode 'morph': 3 images of the same prompt, crossfaded into a loop."""
    w, h = images[0].size
    images = [im.resize((w, h)) for im in images]
    frames = []
    if mode == "zoom" or len(images) == 1:
        steps = 12
        for i in range(steps):
            frames.append(zoom_frame(images[0], 1.0 + 0.12 * i / (steps - 1)))
        for i in range(steps - 2, 0, -1):
            frames.append(zoom_frame(images[0], 1.0 + 0.12 * i / (steps - 1)))
    else:
        per = 8
        for i in range(len(images)):
            a, b = images[i], images[(i + 1) % len(images)]
            for k in range(per):
                frames.append(Image.blend(a, b, k / per))
    frames[0].save(out_path, save_all=True, append_images=frames[1:],
                   duration=110, loop=0, optimize=False, quality=85)


def clean_old_gifs():
    now = time.time()
    for name in os.listdir(GIF_DIR):
        p = os.path.join(GIF_DIR, name)
        try:
            if now - os.path.getmtime(p) > GIF_TTL_SECONDS:
                os.remove(p)
        except OSError:
            pass


# ---------- pin gate (only active when REQUIRE_PIN=1, i.e. on the public host) ----------

PIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reply Studio</title><style>
body{background:#0e1116;color:#e8ecf3;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.box{text-align:center;padding:0 20px}
h1{font-size:1.4rem;margin-bottom:22px}
input{background:#161b24;border:1px solid #262d3a;border-radius:12px;color:#e8ecf3;
padding:14px;font-size:1.2rem;text-align:center;letter-spacing:.3em;width:180px;outline:none}
button{margin-top:14px;padding:13px 34px;border:none;border-radius:12px;font-size:1rem;
font-weight:600;background:linear-gradient(90deg,#6c8cff,#9d6cff);color:#fff;cursor:pointer}
#err{color:#ff6b6b;font-size:.85rem;margin-top:12px;display:none}
</style></head><body><div class="box">
<h1>Enter PIN</h1>
<input id="pin" inputmode="numeric" autocomplete="off">
<div><button onclick="tryPin()">Unlock</button></div>
<div id="err">Wrong PIN</div>
</div><script>
async function tryPin(){
  const r=await fetch('/api/pin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pin:document.getElementById('pin').value})});
  if(r.ok){location.reload();}else{document.getElementById('err').style.display='block';}
}
document.getElementById('pin').addEventListener('keydown',e=>{if(e.key==='Enter')tryPin();});
</script></body></html>"""


def pin_expected():
    """sha256 of the PIN — the cookie holds this hash, never the PIN itself."""
    pin = os.environ.get("REPLY_PIN", "").strip() or \
        open(os.path.join(BASE, "pin.txt"), encoding="utf-8").read().strip()
    return hashlib.sha256(pin.encode()).hexdigest() if pin else None


@app.before_request
def pin_gate():
    if os.environ.get("REQUIRE_PIN") != "1":
        return
    if request.path.startswith("/static/") or request.path == "/api/pin":
        return
    expected = pin_expected()
    if expected and not hmac.compare_digest(request.cookies.get("rs_pin", ""), expected):
        if request.path.startswith("/api/"):
            return jsonify({"error": "enter the PIN first"}), 401
        return PIN_PAGE


@app.route("/api/pin", methods=["POST"])
def pin_check():
    data = request.get_json(silent=True) or {}
    expected = pin_expected()
    if expected and hmac.compare_digest(
            hashlib.sha256((data.get("pin") or "").encode()).hexdigest(), expected):
        resp = jsonify({"ok": True})
        resp.set_cookie("rs_pin", expected, max_age=30 * 24 * 3600, httponly=True)
        return resp
    return jsonify({"error": "wrong pin"}), 401


# ---------- routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info")
def info():
    return jsonify({"lan_ip": lan_ip(), "tailscale_ip": tailscale_ip(), "port": PORT})


@app.route("/api/text", methods=["POST"])
def text_replies():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "paste the post text first"}), 400
    try:
        replies, model = generate_replies("TEXT OF THE POST:\n" + text[:6000],
                                          preferred=data.get("model"))
        return jsonify({"ok": True, "replies": replies, "model": model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/post", methods=["POST"])
def make_post():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    try:
        posts, model = generate_posts(topic, preferred=data.get("model"))
        return jsonify({"ok": True, "replies": posts, "model": model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/gif", methods=["POST"])
def gif():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    mode = data.get("mode") or "morph"
    if mode not in ("morph", "zoom"):
        mode = "morph"
    if not prompt:
        return jsonify({"error": "describe the gif you want first"}), 400

    # keep the user's words dominant, just steer the style slightly
    full_prompt = prompt + ", vibrant colors, sharp details, no text"
    try:
        clean_old_gifs()
        images = horde_images(full_prompt, 1 if mode == "zoom" else 3)

        name = uuid.uuid4().hex + ".gif"
        out = os.path.join(GIF_DIR, name)
        make_gif(images, out, mode)
        return jsonify({"ok": True, "url": "/static/gifs/" + name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate", methods=["POST"])
def generate():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file received"}), 400
    post_text = (request.form.get("text") or "").strip()

    workdir = os.path.join(UPLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(workdir, exist_ok=True)
    src = os.path.join(workdir, secure_filename(f.filename) or "upload")
    f.save(src)

    try:
        ext = os.path.splitext(src)[1].lower()
        if ext in IMAGE_EXTS:
            kind = "image"
            jpg = os.path.join(workdir, "img.jpg")
            run(["ffmpeg", "-y", "-v", "error", "-i", src,
                 "-vf", "scale='min(1280,iw)':-2", "-q:v", "4", jpg])
            understanding = "VISUAL DESCRIPTION:\n" + describe_images([jpg], is_video=False)
        elif ext in VIDEO_EXTS:
            kind = "video"
            duration = float(run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration", "-of", "csv=p=0", src]).strip() or 0)
            frames = extract_frames(src, workdir, duration)
            visual = describe_images(frames, is_video=True)
            transcript = transcribe(src, workdir) if has_audio(src) else "(no audio track)"
            understanding = (f"VISUAL DESCRIPTION:\n{visual}\n\n"
                             f"NARRATION/AUDIO TRANSCRIPT:\n{transcript}")
        else:
            return jsonify({"error": f"unsupported file type: {ext or 'unknown'}"}), 400

        replies, model = generate_replies(
            (f"TEXT OF THE POST:\n{post_text[:3000]}\n\n" if post_text else "") + understanding,
            preferred=request.form.get("model"))
        return jsonify({"ok": True, "kind": kind, "understanding": understanding,
                        "replies": replies, "model": model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    ip = lan_ip()
    if os.name == "nt" and "--no-browser" not in sys.argv:   # headless boxes have no browser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"\nReply Studio running:")
    print(f"  PC:     http://127.0.0.1:{PORT}")
    print(f"  Phone:  http://{ip}:{PORT}   (same WiFi)\n")
    # REPLY_HOST env var: set to a Tailscale IP on the VM so the app stays off the public internet
    app.run(host=os.environ.get("REPLY_HOST", "0.0.0.0"), port=PORT, debug=False)
