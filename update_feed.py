# update_feed.py
# Runs on GitHub Actions. Reads recent Discord messages and rewrites vrc-feed.txt.
# Format per line: time|user|avatar|image|text|extra

import os, re, json, hashlib, unicodedata
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

REPO = "eyeonspyweezer/goofymarinenewspb"
CHANNEL_ID = "1501032245614346300"
FEED_FILE = "vrc-feed.txt"

RAW = f"https://raw.githubusercontent.com/{REPO}/refs/heads/main"
API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"}

ROLE_ONLY = re.compile(r"^\s*(?:<@&\d+>|@everyone|@here)\s*$")

AV_DIR = Path("avatars")
TH_DIR = Path("thumbnails")
AV_INDEX = AV_DIR / "index.json"
avatar_index = json.loads(AV_INDEX.read_text()) if AV_INDEX.exists() else {}


def api(path):
    r = requests.get(API + path, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def ascii_only(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.encode("ascii", "ignore").decode("ascii")


def clean(text):
    text = unicodedata.normalize("NFC", str(text or ""))
    return re.sub(r"[\x00-\x1F\x7F]", "", text)


def ensure_avatar(user):
    uid = user["id"]
    h = user.get("avatar") or "default"
    path = AV_DIR / f"{uid}.png"
    if path.exists() and avatar_index.get(uid) == h:
        return
    if user.get("avatar"):
        url = f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=128"
    else:
        url = f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6}.png"
    r = requests.get(url, timeout=30)
    if r.ok:
        AV_DIR.mkdir(exist_ok=True)
        path.write_bytes(r.content)
        avatar_index[uid] = h


def ensure_thumbnail(url):
    key = hashlib.sha256(url.split("?")[0].encode()).hexdigest()
    path = TH_DIR / f"{key}.png"
    if not path.exists():
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            TH_DIR.mkdir(exist_ok=True)
            Image.open(BytesIO(r.content)).convert("RGBA").save(path, format="PNG")
        except Exception as e:
            print("thumbnail failed:", url, e)
            return ""
    return f"{RAW}/thumbnails/{key}.png"


def pick_image(m):
    urls = []
    for e in m.get("embeds", []):
        u = (e.get("thumbnail") or {}).get("url") or (e.get("image") or {}).get("url")
        if u:
            urls.append(u)
    for a in m.get("attachments", []):
        if (a.get("content_type") or "").startswith("image"):
            urls.append(a["url"])
    for u in reversed(urls):  # last one wins, like before
        img = ensure_thumbnail(u)
        if img:
            return img
    return ""


def content_of(m):
    text = m.get("content") or ""
    poll = m.get("poll")
    if not text and poll:
        text = poll["question"].get("text") or "Poll"
    if not text and m.get("embeds"):
        e = m["embeds"][0]
        text = e.get("title") or e.get("description") or ""
    if not text and m.get("attachments"):
        text = m["attachments"][0]["filename"]
    return text or "[no text]"


def extra_of(m):
    extras = []

    poll = m.get("poll")
    if poll:
        counts = {
            c["id"]: c["count"]
            for c in (poll.get("results") or {}).get("answer_counts", [])
        }
        parts = []
        for a in poll.get("answers", []):
            t = clean(a["poll_media"].get("text", "")).replace("|", "")
            parts.append(f"{t}:{counts.get(a['answer_id'], 0)}")
        extras.append("(" + ",".join(parts) + ")")

        remaining = 0
        if poll.get("expiry"):
            exp = datetime.fromisoformat(poll["expiry"].replace("Z", "+00:00"))
            remaining = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
        extras.append("POLL ENDED" if remaining <= 0 else f"{remaining}s left")

    if m.get("reactions"):
        parts = []
        for r in m["reactions"]:
            e = r["emoji"]
            name = f"<:{e['name']}:{e['id']}>" if e.get("id") else (e.get("name") or "")
            parts.append(f"{name.replace('|', '')}:{r['count']}")
        extras.append("(" + ",".join(parts) + ")")

    return "|".join(extras)


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def trim(items):
    # keep newest 3, plus up to 2 older ones posted within 5 min of the newest
    if len(items) <= 3:
        return items
    newest = parse_time(items[-1][0]["timestamp"])
    extras = []
    for m, t in reversed(items[:-3]):
        if (newest - parse_time(m["timestamp"])).total_seconds() <= 300:
            extras.insert(0, (m, t))
        if len(extras) >= 2:
            break
    return extras + items[-3:]


def main():
    me = api("/users/@me")["id"]
    msgs = api(f"/channels/{CHANNEL_ID}/messages?limit=25")
    msgs.reverse()  # oldest -> newest

    items, seen = [], set()
    for m in msgs:
        if m["author"]["id"] == me:
            continue
        raw = (m.get("content") or "").strip()
        if ROLE_ONLY.fullmatch(raw) and not m.get("attachments"):
            continue
        text = content_of(m)
        key = text.strip().lower()
        if key in seen:  # skip repeated text
            continue
        seen.add(key)
        items.append((m, text))

    lines = []
    for m, text in trim(items):
        body = ascii_only(text).replace("\n", " ").replace("|", "")
        if body.strip() == "[no text]":
            continue
        user = m["author"]
        ensure_avatar(user)
        name = (user.get("global_name") or user["username"]).replace("|", "")
        avatar_url = f"{RAW}/avatars/{user['id']}.png"
        ts = parse_time(m["timestamp"]).strftime("%H:%M %m/%d/%Y")
        lines.append(f"{ts}|{name}|{avatar_url}|{pick_image(m)}|{body}|{extra_of(m)}")

    if not lines:
        print("Nothing to write; leaving feed as is.")
        return

    Path(FEED_FILE).write_text("\n".join(lines), encoding="utf-8")
    AV_DIR.mkdir(exist_ok=True)
    AV_INDEX.write_text(json.dumps(avatar_index, indent=1, sort_keys=True))
    print(f"Wrote {len(lines)} line(s).")


if __name__ == "__main__":
    main()
