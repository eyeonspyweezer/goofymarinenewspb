# update_feed.py
# Runs on GitHub Actions. Reads recent Discord messages and rewrites vrc-feed.txt.
# Format per line: time|user|avatar|image|text|extra
#   time   : newest message of the line, "HH:MM MM/DD/YYYY" (UTC)
#   image  : one or more picture links separated by a space (can be empty)
#   text   : real new lines are written as the two characters \n   ("|" is removed)
#   extra  : poll / reaction info, plus EDITED and/or EVERYONE when they apply
#
# Also maintains discord-archive.txt with all accessible messages from configured channels.
# Also writes two files for the VRChat world into the folder  world/ :
#   world/vrc-feed-world.txt : time|user|avatarCell|pictureCells|text|extra   (cells are numbers in the atlas)
#   world/vrc-feed-atlas.jpg : ONE picture holding every avatar and picture (VRChat can only load fixed links)
#
# Extra channels: add one empty file per channel in the folder  channels/<channel id>.txt
# Censor list  : downloaded at run time from dsojevic/profanity-list (MIT)

import os, re, json, hashlib, unicodedata, time
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
GIF_FRAME_CELL = 128       # static GIF spritesheet: 4 x 4 frames
GIF_MAX_FRAMES = 16
GIF_GRID = 4

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
ARCHIVE_FILE = Path("discord-archive.txt")
ARCHIVE_STATE_FILE = Path("discord-archive-state.json")
ARCHIVE_CHANNEL_IDS = [CHANNEL_ID]  # Keep the text archive limited to the single main channel.
ARCHIVE_HIDDEN_AUTHORS = {"farquaad"}
BLOCKLIST_URL = "https://raw.githubusercontent.com/dsojevic/profanity-list/main/en.txt"

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
    # Third-party English list; fetched fresh on each workflow run.
    # Source: dsojevic/profanity-list (MIT license).
    response = requests.get(BLOCKLIST_URL, timeout=30)
    response.raise_for_status()

    words = []
    for line in response.text.splitlines():
        word = line.strip()
        if word and not word.startswith("#"):
            words.append(word)

    if not words:
        raise RuntimeError("The external profanity list was empty; refusing to publish an unfiltered feed.")

    # Match whole words/phrases only. Spaces inside phrases may be repeated.
    patterns = []
    for word in sorted(set(words), key=len, reverse=True):
        parts = [re.escape(part) for part in word.split()]
        patterns.append(r"\s+".join(parts))
    return re.compile(r"(?<![\w])(?:" + "|".join(patterns) + r")(?![\w])", re.I)


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


def gif_spritesheet(img):
    """Turn an animated GIF into one static 4x4 contact sheet for the VRChat atlas."""
    frame_total = getattr(img, "n_frames", 1)
    if frame_total <= 1:
        return None

    # Sample evenly across the animation, keeping the sheet small and predictable.
    count = min(frame_total, GIF_MAX_FRAMES)
    indexes = sorted(set(round(i * (frame_total - 1) / max(1, count - 1)) for i in range(count)))
    sheet = Image.new("RGBA", (GIF_FRAME_CELL * GIF_GRID, GIF_FRAME_CELL * GIF_GRID), (0, 0, 0, 0))

    for slot, frame_index in enumerate(indexes[:GIF_MAX_FRAMES]):
        img.seek(frame_index)
        frame = img.convert("RGBA")
        frame.thumbnail((GIF_FRAME_CELL, GIF_FRAME_CELL), Image.LANCZOS)
        x = (slot % GIF_GRID) * GIF_FRAME_CELL + (GIF_FRAME_CELL - frame.width) // 2
        y = (slot // GIF_GRID) * GIF_FRAME_CELL + (GIF_FRAME_CELL - frame.height) // 2
        sheet.alpha_composite(frame, (x, y))

    return sheet


def ensure_thumbnail(url):
    """Save a small copy of the picture. Same link, or the same picture from a different
    link, is only ever stored once."""
    key = url.split("?")[0]
    # Force GIF URLs through the spritesheet version instead of reusing old first-frame thumbnails.
    if key.lower().endswith(".gif"):
        key += "#spritesheet-v1"
    name = thumb_index.get(key)
    if name and (TH_DIR / name).exists():
        return f"{RAW}/thumbnails/{name}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        sheet = gif_spritesheet(img) if (img.format or "").upper() == "GIF" else None
        if sheet is not None:
            img = sheet
        else:
            img.seek(0)
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
        image_url = (e.get("image") or {}).get("url")
        thumbnail_url = (e.get("thumbnail") or {}).get("url")
        # Prefer the actual GIF over Discord's still thumbnail when both are present.
        u = image_url if image_url and image_url.split("?")[0].lower().endswith(".gif") else (thumbnail_url or image_url)
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
    flagged_group = any(has_blocked_terms(text) for _, text in g)
    rec = {
        "ts": ts, "name": name, "body": joined, "extra": "|".join(wx),
        # Hide the author's avatar too when the message contains blocked language.
        "avatar": Path("__blocked_avatar__") if flagged_group else AV_DIR / f"{user['id']}.png",
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


# ---------- complete Discord text archive ----------

def archive_message_text(message):
    """Render a retrieved Discord message without feed combining or censoring."""
    author = message.get("author") or {}
    display = author.get("global_name") or author.get("username") or "Unknown"
    # Hide Farquaad-authored messages from the public TXT archive.
    author_names = {
        str(author.get("global_name") or "").strip().casefold(),
        str(author.get("username") or "").strip().casefold(),
    }
    if author_names & ARCHIVE_HIDDEN_AUTHORS:
        return None
    stamp = message.get("timestamp") or ""
    message_id = str(message.get("id") or "")
    lines = [f"[{stamp}] {display} (message {message_id})"]
    content = (message.get("content") or "").strip()
    if content:
        lines.append(content)
    for attachment in message.get("attachments") or []:
        filename = attachment.get("filename") or "attachment"
        url = attachment.get("url") or attachment.get("proxy_url") or ""
        lines.append(f"Attachment: {filename}" + (f" — {url}" if url else ""))
    for embed in message.get("embeds") or []:
        title = (embed.get("title") or "").strip()
        description = (embed.get("description") or "").strip()
        url = (embed.get("url") or "").strip()
        if title or description or url:
            lines.append("Embed: " + " | ".join(x for x in (title, description, url) if x))
    poll = message.get("poll") or {}
    question = (poll.get("question") or {}).get("text") or ""
    if question:
        lines.append("Poll: " + question)
        for answer in poll.get("answers") or []:
            answer_text = (answer.get("poll_media") or {}).get("text") or ""
            if answer_text:
                lines.append("  - " + answer_text)
    reactions = []
    for reaction in message.get("reactions") or []:
        emoji = reaction.get("emoji") or {}
        label = emoji.get("name") or "emoji"
        reactions.append(f"{label}: {reaction.get('count', 0)}")
    if reactions:
        lines.append("Reactions: " + ", ".join(reactions))
    lines.append(f"Channel ID: {message.get('channel_id') or ''}")
    return "\n".join(lines)


def archive_api_messages(channel_id, params):
    """Fetch a page with basic Discord rate-limit handling."""
    while True:
        response = requests.get(
            f"{API}/channels/{channel_id}/messages",
            headers=HEADERS,
            params=params,
            timeout=45,
        )
        if response.status_code == 429:
            try:
                delay = float(response.json().get("retry_after", 2))
            except (ValueError, TypeError):
                delay = 2
            time.sleep(max(1, delay) + 0.25)
            continue
        response.raise_for_status()
        return response.json()


def fetch_archive_messages(channel_id, last_id=None):
    """First run backfills the full accessible history; later runs fetch new messages."""
    collected = []
    if last_id:
        page = archive_api_messages(channel_id, {"after": last_id, "limit": 100})
        collected.extend(page)
        if len(page) == 100:
            ascending = int(page[0]["id"]) < int(page[-1]["id"])
            cursor = (
                max(int(m["id"]) for m in page)
                if ascending
                else min(int(m["id"]) for m in page)
            )
            while True:
                if ascending:
                    page = archive_api_messages(
                        channel_id, {"after": str(cursor), "limit": 100}
                    )
                else:
                    page = archive_api_messages(
                        channel_id, {"before": str(cursor), "limit": 100}
                    )
                if not page:
                    break
                collected.extend(page)
                next_cursor = (
                    max(int(m["id"]) for m in page)
                    if ascending
                    else min(int(m["id"]) for m in page)
                )
                if next_cursor == cursor or len(page) < 100:
                    break
                cursor = next_cursor
        collected = [m for m in collected if int(m["id"]) > int(last_id)]
    else:
        before = None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            page = archive_api_messages(channel_id, params)
            if not page:
                break
            collected.extend(page)
            next_before = str(min(int(m["id"]) for m in page))
            if next_before == before or len(page) < 100:
                break
            before = next_before
    unique = {str(m["id"]): m for m in collected}
    return sorted(unique.values(), key=lambda m: int(m["id"]))


def update_complete_archive(channel_list):
    """Maintain one chronological-per-channel TXT archive, incrementally."""
    try:
        state = json.loads(ARCHIVE_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    existing = ARCHIVE_FILE.read_text(encoding="utf-8") if ARCHIVE_FILE.exists() else ""
    additions = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    for channel_id in channel_list:
        last_id = state.get(channel_id)
        try:
            messages = fetch_archive_messages(channel_id, last_id)
            if not messages:
                print(f"Archive channel {channel_id}: no new messages.")
                continue
            channel_name = next(
                (m.get("channel_id") for m in messages if m.get("channel_id")),
                channel_id,
            )
            visible_messages = [
                rendered for m in messages
                if (rendered := archive_message_text(m)) is not None
            ]
            if visible_messages:
                additions.append(f"\n\n===== CHANNEL {channel_name} | collected {now} =====\n")
                additions.append("\n\n".join(visible_messages))
            state[channel_id] = max((str(m["id"]) for m in messages), key=int)
            hidden_count = len(messages) - len(visible_messages)
            print(
                f"Archive channel {channel_id}: added {len(visible_messages)} message(s); "
                f"hidden {hidden_count} Farquaad message(s)."
            )
        except Exception as error:
            print(f"Archive channel {channel_id} failed; cursor unchanged: {error}")

    if additions:
        ARCHIVE_FILE.write_text(existing + "".join(additions) + "\n", encoding="utf-8")
    elif not ARCHIVE_FILE.exists():
        ARCHIVE_FILE.write_text(
            "# Discord archive\n# Includes messages the bot can currently retrieve from configured channels.\n",
            encoding="utf-8",
        )
    ARCHIVE_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    me = api("/users/@me")["id"]
    all_groups, seen = [], set()
    per_channel_groups = {}

    update_complete_archive(ARCHIVE_CHANNEL_IDS)

    for cid in channel_ids():
        # Keep an entry for every configured channel, even if Discord returns
        # no messages or the bot cannot access that channel.
        per_channel_groups[cid] = []
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
        target = WORLD_DIR if cid == CHANNEL_ID else Path("channelids") / cid
        # Always create both files so every configured channel has stable URLs.
        # Empty channels receive an empty text feed and a blank atlas image.
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
