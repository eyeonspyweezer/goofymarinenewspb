# update_feed.py
# Runs on GitHub Actions. Reads recent Discord messages and rewrites vrc-feed.txt.
# Format per line: time|user|avatar|image|text|extra
#   time   : newest message of the line, "HH:MM MM/DD/YYYY" (UTC)
#   image  : one or more picture links separated by a space (can be empty)
#   text   : real new lines are written as the two characters \n   ("|" is removed)
#   extra  : poll / reaction info, plus EDITED and/or EVERYONE when they apply
#
# Also writes two files for the VRChat world into the folder  world/ :
#   world/vrc-feed-world.txt : time|user|avatarCell|pictureCells|text|extra   (cells are numbers in the atlas)
#   world/vrc-feed-atlas.jpg : ONE picture holding every avatar and picture (VRChat can only load fixed links)
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

# VRChat atlas: 8 x 8 cells of 256 px = one 2048 x 2048 picture
WORLD_DIR = Path("world")
CELL = 256
GRID = 8
PICS_PER_SLOT = 2         # each announcement gets 1 avatar cell + up to 2 picture cells

RAW = f"https://raw.githubusercontent.com/{REPO}/refs/heads/main"
API = "https://discord.com/api/v10"
HEADERS = {"Authorization": f"Bot {os.environ['DISCORD_TOKEN']}"}

ROLE_ONLY = re.compile(r"^\s*(?:<@&\d+>|@everyone|@here)\s*$")

AV_DIR = Path("avatars")
TH_DIR = Path("thumbnails")
AV_INDEX = AV_DIR / "index.json"
TH_INDEX = TH_DIR / "index.json"
CHANNELS_DIR = Path("channels")
ANNOUNCEMENT_CHANNELS_FILE = Path("announcement_channels.txt")
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
    words = []
    for line in BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if word and not word.startswith("#"):
            words.append(word)

    # Match whole words/phrases only. Spaces inside phrases may be repeated.
    if not words:
        return None
    patterns = []
    for word in sorted(set(words), key=len, reverse=True):
        parts = [re.escape(part) for part in word.split()]
        patterns.append(r"\\s+".join(parts))
    return re.compile(r"(?<![\\w])(?:" + "|".join(patterns) + r")(?![\\w])", re.I)


BLOCK = load_blocklist()


def has_blocked_terms(text):
    return bool(BLOCK and BLOCK.search(str(text or "")))


def censor(text):
    text = str(text or "")
    if not BLOCK:
        return text
    return BLOCK.sub(lambda m: "*" * len(m.group(0)), text)


def channel_ids():
    # The main channel is always included.
    ids = [CHANNEL_ID]

    # Add channels listed in announcement_channels.txt (one ID per line).
    # Blank lines and comments beginning with # are ignored.
    if ANNOUNCEMENT_CHANNELS_FILE.exists():
        for line in ANNOUNCEMENT_CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            cid = line.split("#", 1)[0].strip()
            if cid.isdigit() and cid not in ids:
                ids.append(cid)

    # Keep support for the older channels/<channel id>.txt method.
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
        # If a message contains blocked language, do not carry its images
        # into the VRChat atlas either. Text is retained with matched words masked.
        if not has_blocked_terms(text):
            for u in pick_images(m):
                if u not in images:
                    images.append(u)
    if not bodies:
        return None, None

    single = len(g) == 1
    extras = extra_of(last) if single else []
    reactions = sum(r.get("count", 0) for r in last.get("reactions", [])) if single else 0
    edited = any(m.get("edited_timestamp") for m, _ in g)
    everyone = any(m.get("mention_everyone") for m, _ in g)
    if edited:
        extras.append("EDITED")
    if everyone:
        extras.append("EVERYONE")
    joined = "\\n".join(bodies)
    line = "|".join([ts, name, avatar_url, " ".join(images), joined, "|".join(extras)])

    # the same message for the VRChat world (cells instead of links)
    wx = []
    if single and last.get("poll"):
        wx += [ascii_only(e) for e in extra_of(last)]
    if reactions:
        wx.append(f"REACTIONS:{reactions}")
    if edited:
        wx.append("EDITED")
    if everyone:
        wx.append("EVERYONE")
    rec = {
        "ts": ts, "name": name, "body": joined, "extra": "|".join(wx),
        "avatar": AV_DIR / f"{user['id']}.png",
        "pictures": [TH_DIR / u.rsplit("/", 1)[1] for u in images],
    }
    return line, rec


# ---------- VRChat world files ----------

def cover(img, size):
    img = img.convert("RGB")
    sc = max(size / img.width, size / img.height)
    img = img.resize((max(1, round(img.width * sc)), max(1, round(img.height * sc))), Image.LANCZOS)
    left, top = (img.width - size) // 2, (img.height - size) // 2
    return img.crop((left, top, left + size, top + size))


def build_world(records, output_dir=WORLD_DIR):
    """records: newest first. Slot i uses cells 3i (avatar), 3i+1 and 3i+2 (pictures)."""
    per = 1 + PICS_PER_SLOT
    atlas = Image.new("RGB", (CELL * GRID, CELL * GRID), (32, 34, 37))

    def put(path, cell):
        try:
            im = cover(Image.open(path), CELL)
        except Exception as e:
            print("atlas picture failed:", path, e)
            return False
        atlas.paste(im, ((cell % GRID) * CELL, (cell // GRID) * CELL))
        return True

    lines = []
    for i, r in enumerate(records[: (GRID * GRID) // per]):
        a = i * per
        avatar = str(a) if put(r["avatar"], a) else "-1"
        cells = []
        for k, pth in enumerate(r["pictures"][:PICS_PER_SLOT]):
            if put(pth, a + 1 + k):
                cells.append(str(a + 1 + k))
        lines.append("|".join([r["ts"], r["name"], avatar, " ".join(cells), r["body"], r["extra"]]))

    output_dir.mkdir(parents=True, exist_ok=True)
    atlas.save(output_dir / "vrc-feed-atlas.jpg", "JPEG", quality=82)
    (output_dir / "vrc-feed-world.txt").write_text("\n".join(lines), encoding="utf-8")


# ---------- main ----------

def main():
    me = api("/users/@me")["id"]
    all_groups, seen = [], set()
    per_channel_groups = {}

    for cid in channel_ids():
        try:
            msgs = api(f"/channels/{cid}/messages?limit=25")
        except Exception as e:
            print("channel failed:", cid, e)
            continue
        msgs.reverse()  # oldest -> newest

        items = []
        combined_items = []
        channel_seen = set()
        for m in msgs:
            if m["author"]["id"] == me:
                continue
            raw = (m.get("content") or "").strip()
            if ROLE_ONLY.fullmatch(raw) and not m.get("attachments"):
                continue
            text = content_of(m)
            key = text.strip().lower()
            if key not in channel_seen:
                channel_seen.add(key)
                items.append((m, text))
            if key not in seen:
                seen.add(key)
                combined_items.append((m, text))

        per_channel_groups[cid] = combine(items)
        all_groups.extend(combine(combined_items))

    # Main channel alone goes in world/. Every other channel gets its own folder.
    for cid, groups in per_channel_groups.items():
        groups.sort(key=lambda g: g[-1][0]["timestamp"])
        records = []
        for g in groups[-MAX_ITEMS:]:
            _, rec = group_line(g)
            if rec:
                records.append(rec)
        if records:
            target = WORLD_DIR if cid == CHANNEL_ID else Path("channelids") / cid
            build_world(list(reversed(records)), target)
            print(f"Wrote channel {cid}: {len(records)} announcement(s).")

    # Keep the original root feed as a combined feed for compatibility.
    all_groups.sort(key=lambda g: g[-1][0]["timestamp"])
    lines, records = [], []
    for g in all_groups[-MAX_ITEMS:]:
        line, rec = group_line(g)
        if line:
            lines.append(line)
            records.append(rec)

    if lines:
        Path(FEED_FILE).write_text("\n".join(reversed(lines)), encoding="utf-8")
        print(f"Wrote combined root feed: {len(lines)} line(s).")
    else:
        print("No announcements found.")

    AV_DIR.mkdir(exist_ok=True)
    AV_INDEX.write_text(json.dumps(avatar_index, indent=1, sort_keys=True))
    if thumb_index:
        TH_DIR.mkdir(exist_ok=True)
        TH_INDEX.write_text(json.dumps(thumb_index, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
