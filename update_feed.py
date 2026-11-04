# update_feed.py
# Runs on GitHub Actions. Reads recent Discord messages and rewrites vrc-feed.txt.
# Format per line: time|user|avatar|image|text|extra
#   time   : newest message of the line, "HH:MM MM/DD/YYYY" (UTC)
#   image  : one or more picture links separated by a space (can be empty)
#   text   : real new lines are written as the two characters \n   ("|" is removed)
#   extra  : poll / reaction info, plus EDITED and/or EVERYONE when they apply
#
# Extra channels: add one empty file per channel in the folder  channels/<channel id>.txt
# Censor list  : put one word per line in  blocklist.txt  (optional)

import os, re, json, hashlib, unicodedata
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

REPO = "eyeonspyweezer/goofymarinenewspb"
CHANNEL_ID = "1501032245614346300"          # main channel (always read)
FEED_FILE = "vrc-feed.txt"

MAX_ITEMS = 15            # most announcements kept, newest first
COMBINE_SECONDS = 300     # same person, same channel, within 5 min, no reactions -> one message
THUMB_MAX = 512           # pictures are shrunk to at most this many pixels

RAW = f"https://raw.githubusercontent.com/{REPO}/refs/heads/main"
API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"}

ROLE_ONLY = re.compile(r"^\s*(?:<@&\d+>|@everyone|@here)\s*$")

AV_DIR = Path("avatars")
TH_DIR = Path("thumbnails")
AV_INDEX = AV_DIR / "index.json"
TH_INDEX = TH_DIR / "index.json"
CHANNELS_DIR = Path("channels")
BLOCKLIST_FILE = Path("blocklist.txt")

avatar_index = json.loads(AV_INDEX.read_text()) if AV_INDEX.exists() else {}
thumb_index = json.loads(TH_INDEX.read_text()) if TH_INDEX.exists() else {}


# ---------- small helpers ----------

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


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_blocklist():
    if not BLOCKLIST_FILE.exists():
        return None
    words = [w.strip() for w in BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines()
             if w.strip() and not w.strip().startswith("#")]
    if not words:
        return None
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I)


BLOCK = load_blocklist()


def censor(text):
    if not BLOCK:
        return text
    return BLOCK.sub(lambda m: "*" * len(m.group(0)), text)


def channel_ids():
    ids = [CHANNEL_ID]
    if CHANNELS_DIR.exists():
        for f in sorted(CHANNELS_DIR.glob("*.txt")):
            if f.stem.isdigit() and f.stem not in ids:
                ids.append(f.stem)
    return ids


def humanize(m, text):
    """Make Discord markup readable: spoilers hidden, mentions and emoji as plain words."""
    text = re.sub(r"\|\|.*?\|\|", "[spoiler]", text, flags=re.S)
    names = {u["id"]: (u.get("global_name") or u["username"]) for u in m.get("mentions", [])}
    text = re.sub(r"<@!?(\d+)>", lambda x: "@" + names.get(x.group(1), "user"), text)
    text = re.sub(r"<@&\d+>", "@role", text)
    text = re.sub(r"<#\d+>", "#channel", text)
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    text = re.sub(r"<t:\d+(?::\w)?>", "[time]", text)
    return text


# ---------- pictures ----------

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
    """Save a small copy of the picture. Same link, or the same picture from a different
    link, is only ever stored once."""
    key = url.split("?")[0]
    name = thumb_index.get(key)
    if name and (TH_DIR / name).exists():
        return f"{RAW}/thumbnails/{name}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        img.seek(0)                       # GIFs: first frame only
        img = img.convert("RGBA")
        img.thumbnail((THUMB_MAX, THUMB_MAX))
        has_alpha = img.getchannel("A").getextrema()[0] < 255
        buf = BytesIO()
        if has_alpha:
            img.save(buf, format="PNG", optimize=True)
            ext = "png"
        else:
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            ext = "jpg"
        data = buf.getvalue()
        name = hashlib.sha256(data).hexdigest()[:32] + "." + ext
        TH_DIR.mkdir(exist_ok=True)
        path = TH_DIR / name
        if not path.exists():
            path.write_bytes(data)
        thumb_index[key] = name
    except Exception as e:
        print("thumbnail failed:", url, e)
        return ""
    return f"{RAW}/thumbnails/{name}"


def pick_images(m):
    urls = []
    for e in m.get("embeds", []):
        u = (e.get("thumbnail") or {}).get("url") or (e.get("image") or {}).get("url")
        if u and u not in urls:
            urls.append(u)
    for a in m.get("attachments", []):
        if (a.get("content_type") or "").startswith("image") and a["url"] not in urls:
            urls.append(a["url"])
    out = []
    for u in urls:
        img = ensure_thumbnail(u)
        if img and img not in out:
            out.append(img)
    return out


# ---------- message text ----------

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


def body_of(m, text):
    text = censor(ascii_only(humanize(m, text)))
    text = text.replace("\r", "").strip()
    return re.sub(r"\n{3,}", "\n\n", text).replace("\n", "\\n").replace("|", "")


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
            t = censor(clean(a["poll_media"].get("text", ""))).replace("|", "")
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
            parts.append(f"{censor(name).replace('|', '')}:{r['count']}")
        extras.append("(" + ",".join(parts) + ")")

    return extras


# ---------- combining ----------

def plain(m):
    """Can this message be merged with its neighbours?"""
    return not m.get("reactions") and not m.get("edited_timestamp") and not m.get("poll")


def combine(items):
    """items: [(message, text)] oldest -> newest, one channel. Returns a list of groups."""
    groups = []
    for it in items:
        m = it[0]
        if groups:
            g = groups[-1]
            last = g[-1][0]
            gap = (parse_time(m["timestamp"]) - parse_time(last["timestamp"])).total_seconds()
            if (plain(m) and all(plain(x[0]) for x in g)
                    and m["author"]["id"] == last["author"]["id"]
                    and gap <= COMBINE_SECONDS):
                g.append(it)
                continue
        groups.append([it])
    return groups


def group_line(g):
    last = g[-1][0]
    user = last["author"]
    ensure_avatar(user)
    name = censor(ascii_only(user.get("global_name") or user["username"])).replace("|", "")
    avatar_url = f"{RAW}/avatars/{user['id']}.png"
    ts = parse_time(last["timestamp"]).strftime("%H:%M %m/%d/%Y")

    bodies, images = [], []
    for m, text in g:
        b = body_of(m, text)
        if b and b != "[no text]":
            bodies.append(b)
        for u in pick_images(m):
            if u not in images:
                images.append(u)
    if not bodies:
        return None

    extras = extra_of(last) if len(g) == 1 else []
    if any(m.get("edited_timestamp") for m, _ in g):
        extras.append("EDITED")
    if any(m.get("mention_everyone") for m, _ in g):
        extras.append("EVERYONE")
    joined = "\\n".join(bodies)
    return "|".join([ts, name, avatar_url, " ".join(images), joined, "|".join(extras)])


# ---------- main ----------

def main():
    me = api("/users/@me")["id"]
    all_groups, seen = [], set()

    for cid in channel_ids():
        try:
            msgs = api(f"/channels/{cid}/messages?limit=25")
        except Exception as e:
            print("channel failed:", cid, e)
            continue
        msgs.reverse()  # oldest -> newest

        items = []
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
        all_groups.extend(combine(items))

    all_groups.sort(key=lambda g: g[-1][0]["timestamp"])
    lines = []
    for g in all_groups[-MAX_ITEMS:]:
        line = group_line(g)
        if line:
            lines.append(line)

    if not lines:
        print("Nothing to write; leaving feed as is.")
        return

    Path(FEED_FILE).write_text("\n".join(reversed(lines)), encoding="utf-8")
    AV_DIR.mkdir(exist_ok=True)
    AV_INDEX.write_text(json.dumps(avatar_index, indent=1, sort_keys=True))
    if thumb_index:
        TH_DIR.mkdir(exist_ok=True)
        TH_INDEX.write_text(json.dumps(thumb_index, indent=1, sort_keys=True))
    print(f"Wrote {len(lines)} line(s).")


if __name__ == "__main__":
    main()
