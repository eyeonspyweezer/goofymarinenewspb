# render_maps.py  (put in the TOP folder of the opvrcinteractivemap repo)
# Draws island boxes on TargetedMap.PNG for every button combo + every group.
# Output: maps/<mask>.jpg  and  groups/<id>.jpg
# mask = 6 digits for M U O P R K (1 = shown, 0 = hidden)

import csv, itertools
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance

Image.MAX_IMAGE_PIXELS = None

MAP_FILE = "TargetedMap.PNG"
OUT_W = 1600          # output width in pixels
FLIP_Y = False        # if boxes look upside down, change to True
COORD_W = None        # leave None. If boxes are the wrong SIZE/PLACE, tell me the log line.
COORD_H = None

base = Image.open(MAP_FILE).convert("RGB")
W, H = base.size
COORD_W = COORD_W or W
COORD_H = COORD_H or H
s = OUT_W / COORD_W
small = base.resize((OUT_W, round(H * OUT_W / W)), Image.LANCZOS)

islands = list(csv.DictReader(open("island-colors.tsv", encoding="utf-8"), delimiter="\t"))
groups = list(csv.DictReader(open("island-groups.tsv", encoding="utf-8"), delimiter="\t"))

print(f"image {W}x{H}; biggest box edge x={max(int(r['BoxX'])+int(r['BoxW']) for r in islands)} "
      f"y={max(int(r['BoxY'])+int(r['BoxH']) for r in islands)}")

TOKEN_CAT = {"#005aff": "M", "#000000": "P", "#b40000": "R", "#800080": "K"}
COLORS = {
    "M": (0, 90, 255), "P": (20, 20, 20), "R": (180, 0, 0),
    "K": (128, 0, 128), "U": (160, 160, 160),
}
ORDER = "MRKPOU"      # which color wins if an island is in 2 categories
LETTERS = "MUOPRK"    # button order


def cats(r):
    if r["Unknown"] == "1":
        return {"U"}
    if r["Unowned"] == "1":
        return {"O"}
    return {TOKEN_CAT[t] for t in r["Color"].lower().split() if t in TOKEN_CAT}


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def box(r):
    x, y, w, h = (int(r[k]) for k in ("BoxX", "BoxY", "BoxW", "BoxH"))
    if FLIP_Y:
        y = COORD_H - y - h
    return [x * s, y * s, (x + w) * s, (y + h) * s]


def render(items, dim=False):
    img = small.copy()
    if dim:
        img = ImageEnhance.Brightness(img).enhance(0.4)
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for b, col in items:
        d.rectangle(b, fill=col + (150,), outline=(255, 255, 255, 255), width=2)
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def save(img, path):
    Path(path).parent.mkdir(exist_ok=True)
    img.save(path, "JPEG", quality=80)


# 1) every button combo
for bits in itertools.product("01", repeat=6):
    mask = "".join(bits)
    active = {c for c, b in zip(LETTERS, bits) if b == "1"}
    items = []
    for r in islands:
        hit = cats(r) & active
        if not hit:
            continue
        c = next(x for x in ORDER if x in hit)
        col = hex_rgb(r["Color"].split()[0]) if c == "O" else COLORS[c]
        items.append((box(r), col))
    save(render(items), f"maps/{mask}.jpg")

# 2) one image per group that owns islands
for g in groups:
    gid = g["ID"]
    items = [(box(r), (0, 220, 255)) for r in islands
             if gid in (r["Group1"], r["Group2"], r["Group3"])]
    if items:
        save(render(items, dim=True), f"groups/{gid}.jpg")

# 3) one zoomed picture per island (for /island)
def island_img(r):
    x, y, w, h = (int(r[k]) for k in ("BoxX", "BoxY", "BoxW", "BoxH"))
    if FLIP_Y:
        y = COORD_H - y - h
    kx, ky = W / COORD_W, H / COORD_H
    pad = max(w, h) * 0.8 + 150
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(COORD_W, x + w + pad), min(COORD_H, y + h + pad)
    crop = base.crop((int(x0 * kx), int(y0 * ky), int(x1 * kx), int(y1 * ky)))
    sc = 800 / max(crop.size)
    crop = crop.resize((max(1, int(crop.width * sc)), max(1, int(crop.height * sc))), Image.LANCZOS)
    ImageDraw.Draw(crop).rectangle(
        [(x - x0) * kx * sc, (y - y0) * ky * sc, (x + w - x0) * kx * sc, (y + h - y0) * ky * sc],
        outline=(0, 220, 255), width=3,
    )
    return crop


for r in islands:
    try:
        save(island_img(r), f"islands/{r['ID']}.jpg")
    except Exception as e:
        print("island image failed:", r["ID"], e)

print("done")
