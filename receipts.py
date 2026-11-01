"""Receipt reader: OCR (Tesseract) or PDF text -> line items -> per-unit prices.

Everything here is plain rules and lookup tables (no AI / LLM calls). The only
"smart" parts are Tesseract for turning photos into text and the editable
grocery_catalog.json for naming items. OCR output is cached next to the
receipts, and a same-named .txt file overrides the OCR text so mistakes can be
fixed by hand.
"""
import datetime as dt
import hashlib
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(HERE, 'grocery_catalog.json')
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.heic', '.heif'}
CACHE_VERSION = 'v3'
VARIANT_SEP = '\n=====OCR-VARIANT=====\n'

# ── units ────────────────────────────────────────────────────────────────────
# (kind, factor to the base unit). Bases: weight -> lb, volume -> fl oz, count -> each.
UNITS = {
    'lb': ('weight', 1.0), 'lbs': ('weight', 1.0), 'pound': ('weight', 1.0), 'pounds': ('weight', 1.0),
    'kg': ('weight', 2.20462), 'g': ('weight', 0.00220462), 'gram': ('weight', 0.00220462),
    'gal': ('volume', 128.0), 'gallon': ('volume', 128.0), 'qt': ('volume', 32.0), 'quart': ('volume', 32.0),
    'pt': ('volume', 16.0), 'pint': ('volume', 16.0), 'l': ('volume', 33.814), 'lt': ('volume', 33.814),
    'ltr': ('volume', 33.814), 'liter': ('volume', 33.814), 'litre': ('volume', 33.814), 'ml': ('volume', 0.033814),
    'fl oz': ('volume', 1.0), 'floz': ('volume', 1.0),
    'ct': ('count', 1.0), 'count': ('count', 1.0), 'pk': ('count', 1.0), 'pack': ('count', 1.0), 'pc': ('count', 1.0),
    'pcs': ('count', 1.0), 'ea': ('count', 1.0), 'each': ('count', 1.0), 'dz': ('count', 12.0), 'doz': ('count', 12.0),
    'dozen': ('count', 12.0),
}
OZ = 'oz'  # ambiguous: weight (1/16 lb) or volume (1 fl oz) — decided per item
DISPLAY = {'weight': ('lb', 'per lb'), 'volume': ('gal', 'per gal'), 'count': ('each', 'per each')}
_UNIT_RE = r'fl\.?\s?oz|floz|oz|lbs?|pounds?|kg|grams?|g|gal(?:lon)?|qt|quart|pt|pint|liter|litre|ltr|lt|l|ml|ct|count|pk|pack|pcs?|each|ea|dz|doz|dozen'
SIZE_RE = re.compile(r'(?<![\w.])(\d+(?:[.,]\d+)?)\s*(' + _UNIT_RE + r')(?![a-z])', re.I)
DOZEN_RE = re.compile(r'\b(dozen|doz)\b', re.I)
MULTI_RE = re.compile(r'(?<![\w.])(\d{1,3})\s*[x/]\s*(\d+(?:[.,]\d+)?)\s*(' + _UNIT_RE + r')(?![a-z])', re.I)
PACK_THEN_VOL_RE = re.compile(r'(?<![\w.])(\d{1,3})\s*(?:pk|ct|pack|count)\s*(\d+(?:[.,]\d+)?)\s*(fl\.?\s?oz|floz|ml|l)(?![a-z])', re.I)

_NUM = r'(\d+(?:[.,]\d+)?)'
_PRICE = r'\$?\s?(\d+[.,]\d{2})'
WEIGHT_AT_RE = re.compile(_NUM + r'\s*(lbs?|oz|kg|g)\b\.?\s*(?:@|x)\s*' + _PRICE + r'\s*(?:/|per\s*)?\s*(lbs?|oz|kg|g)?', re.I)
QTY_AT_RE = re.compile(r'(?<![\d.])(\d{1,3})\s*(?:@|x)\s*' + _PRICE, re.I)
PRICE_END_RE = re.compile(r'(?<![\d.,/])(?:(-)\$?|\$?\s?)(\d{0,4})[.,](\d{2})(-?)(?:\s*[=~_*-]?\s*([A-Za-z]{1,2})[.,]?)?(?:\s+(?=\S*[A-Za-z])[^@/\\\s]{1,5})?\s*$')
UNIT_WORDS = {'lb', 'oz', 'kg', 'g', 'ea', 'ct', 'pk', 'ml', 'qt', 'pt', 'gal', 'l', 'lt', 'dz'}

DATE_PATTERNS = [
    (re.compile(r'\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b'), 'ymd'),
    (re.compile(r'\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2}|\d{2})\b'), 'mdy'),
    (re.compile(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})\b', re.I), 'mon d y'),
    (re.compile(r'\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+(20\d{2})\b', re.I), 'd mon y'),
]
MONTHS = {m: i + 1 for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}


# ── catalog ──────────────────────────────────────────────────────────────────
def stem(w):
    if len(w) > 4 and w.endswith('ies'):
        return w[:-3] + 'y'
    if len(w) > 4 and w.endswith(('ches', 'shes', 'sses', 'xes', 'oes')):
        return w[:-2]
    if len(w) > 3 and w.endswith('s') and not w.endswith('ss'):
        return w[:-1]
    return w


def words(s):
    return [stem(w) for w in re.findall(r"[a-z0-9%&']+", s.lower().replace("'s", 's')) if w]


_catalog_cache = {'mtime': None, 'data': None}


def load_catalog():
    mtime = os.path.getmtime(CATALOG_FILE) if os.path.exists(CATALOG_FILE) else None
    if _catalog_cache['mtime'] == mtime and _catalog_cache['data']:
        return _catalog_cache['data']
    raw = {}
    if mtime is not None:
        try:
            with open(CATALOG_FILE, encoding='utf-8') as f:
                raw = json.load(f)
        except ValueError:
            raw = {}
    entries = []
    for group, items in (raw.get('items') or {}).items():
        for name, spec in items.items():
            if isinstance(spec, list):
                spec = {'aliases': spec}
            for alias in spec.get('aliases', []):
                entries.append({'name': name, 'group': group, 'alias': words(alias),
                                'exclude': [words(x) for x in spec.get('exclude', [])], 'kind': spec.get('kind')})
    entries.sort(key=lambda e: (-len(e['alias']), -sum(len(w) for w in e['alias'])))
    data = {
        'entries': entries,
        'stores': {name: [p.lower() for p in pats] for name, pats in (raw.get('stores') or {}).items()},
        'abbr': {k.lower(): v for k, v in (raw.get('abbreviations') or {}).items()},
        'ignore_start': sorted([w.lower() for w in raw.get('ignore_start', [])], key=len, reverse=True),
        'ignore_anywhere': [w.lower() for w in raw.get('ignore_anywhere', [])],
        'discount': [w.lower() for w in raw.get('discount_words', [])],
        'volume_words': {stem(w) for w in raw.get('volume_words', [])},
        'plu': {str(k): v for k, v in (raw.get('plu') or {}).items()},
        'dining_stores': [w.lower() for w in raw.get('dining_stores', [])],
        'dining_words': [w.lower() for w in raw.get('dining_words', [])],
        'by_name': {e['name']: e for e in entries},
        'vocab': sorted({w for e in entries for w in e['alias'] if len(w) >= 4}),
    }
    _catalog_cache.update(mtime=mtime, data=data)
    return data


def match_item(tokens, cat):
    tset = set(tokens)
    for e in cat['entries']:
        if all(w in tset for w in e['alias']) and not any(all(w in tset for w in ex) for ex in e['exclude']):
            return e
    return None


# ── text extraction ──────────────────────────────────────────────────────────
def _has_rapid():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _has_tesseract():
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_status():
    """Which text readers are available. RapidOCR (a local OCR model, no network, no LLM) reads receipt photos best;
    Tesseract is the classic fallback and adds extra readings for the vote."""
    engines = [n for n, ok in (('rapidocr', _has_rapid()), ('tesseract', _has_tesseract())) if ok]
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        engines = []
    if engines:
        return {'ok': True, 'engines': engines}
    return {'ok': False, 'engines': [], 'reason': 'No OCR engine installed. Run: pip install rapidocr-onnxruntime pillow '
            '(recommended), or brew install tesseract && pip install pytesseract pillow.'}


def open_image(path):
    from PIL import Image, ImageOps
    if os.path.splitext(path)[1].lower() in ('.heic', '.heif'):
        import pillow_heif
        pillow_heif.register_heif_opener()
    return ImageOps.exif_transpose(Image.open(path))


def _otsu(hist):
    total = sum(hist)
    sum_all = sum(i * c for i, c in enumerate(hist))
    best, wb, sb, thr = -1.0, 0, 0, 0
    for i, c in enumerate(hist):
        wb += c
        if not wb:
            continue
        wf = total - wb
        if not wf:
            break
        sb += i * c
        between = wb * wf * (sb / wb - (sum_all - sb) / wf) ** 2
        if between > best:
            best, thr = between, i
    return thr


def _paper_mask(small):
    """Boolean grid: True where the pixel looks like paper (bright AND colourless; tables, counters and cloth are
    darker and/or coloured). The cut-off is chosen per photo with Otsu, so it adapts to any background."""
    w, h = small.size
    px = small.load()
    score = [[0] * w for _ in range(h)]
    hist = [0] * 256
    for y in range(h):
        row = score[y]
        for x in range(w):
            r, g, b = px[x, y]
            v = int(max(0, min(255, (r + g + b) / 3 - 1.5 * (max(r, g, b) - min(r, g, b)))))
            row[x] = v
            hist[v] += 1
    thr = _otsu(hist)
    return [[score[y][x] > thr for x in range(w)] for y in range(h)], thr


def _components(mask):
    h, w = len(mask), len(mask[0])
    seen = [[False] * w for _ in range(h)]
    comps = []
    for sy in range(h):
        for sx in range(w):
            if not mask[sy][sx] or seen[sy][sx]:
                continue
            stack, pts = [(sx, sy)], []
            seen[sy][sx] = True
            while stack:
                x, y = stack.pop()
                pts.append((x, y))
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            comps.append(pts)
    return comps


def _hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _min_rect(hull):
    """Smallest rotated rectangle around a convex hull -> (cx, cy, width, height, angle_deg), width = short side."""
    import math
    best = None
    for i in range(len(hull)):
        (x0, y0), (x1, y1) = hull[i], hull[(i + 1) % len(hull)]
        ang = math.atan2(y1 - y0, x1 - x0)
        c, s = math.cos(-ang), math.sin(-ang)
        xs = [(x * c - y * s) for x, y in hull]
        ys = [(x * s + y * c) for x, y in hull]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if best is None or w * h < best[0]:
            mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
            cx, cy = mx * math.cos(ang) - my * math.sin(ang), mx * math.sin(ang) + my * math.cos(ang)
            best = (w * h, cx, cy, w, h, math.degrees(ang))
    _, cx, cy, w, h, ang = best
    if w > h:                       # make the receipt upright: long side vertical
        w, h, ang = h, w, ang + 90
    return cx, cy, w, h, ang


def _profile_score(m):
    """How rectangle-aligned a mask is: tall columns + wide rows (max when the paper's edges are axis-aligned)."""
    from PIL import Image
    cols = list(m.resize((m.width, 1), Image.BOX).getdata())
    rows = list(m.resize((1, m.height), Image.BOX).getdata())
    return sum(c * c for c in cols) + sum(r * r for r in rows)


def _span(values, frac):
    """Longest run around the peak where values stay above frac * max -> (start, end) inclusive."""
    peak = max(values)
    if peak <= 0:
        return None
    mid = values.index(peak)
    lo = hi = mid
    while lo > 0 and values[lo - 1] >= frac * peak:
        lo -= 1
    while hi < len(values) - 1 and values[hi + 1] >= frac * peak:
        hi += 1
    return lo, hi


def crop_to_paper(img):
    """Cut a photographed receipt out of whatever it was photographed on (granite, wood, cloth, a bright wall…).
    The paper is found as the biggest bright-and-colourless shape, straightened by finding the angle at which its
    edges line up with the frame, then trimmed to its edges. Anything still showing at crumpled corners is
    blanked. If no clear paper shape exists (flat scan, white-on-white) the photo is returned unchanged."""
    from PIL import Image, ImageFilter
    rgb = img.convert('RGB')
    k = 240 / max(rgb.size)
    if k >= 1:
        return img
    small = rgb.resize((max(1, int(rgb.width * k)), max(1, int(rgb.height * k))), Image.BILINEAR)
    mask, _ = _paper_mask(small)
    w, h = small.size
    m_img = Image.new('L', (w, h))
    m_img.putdata([255 if v else 0 for row in mask for v in row])
    m_img = m_img.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))   # close gaps left by printed text
    comps = _components([[m_img.getpixel((x, y)) > 127 for x in range(w)] for y in range(h)])
    if not comps:
        return img
    pts = max(comps, key=len)
    if len(pts) < 0.04 * w * h:
        return img
    comp = Image.new('L', (w, h), 0)
    for x, y in pts:
        comp.putpixel((x, y), 255)

    # 1) angle that lines the paper up with the frame
    best_a, best_v, a = 0.0, -1.0, -45.0
    while a <= 45.0:
        v = _profile_score(comp.rotate(a, expand=True, fillcolor=0))
        if v > best_v:
            best_a, best_v = a, v
        a += 0.5
    rc = comp.rotate(best_a, expand=True, fillcolor=0)
    # 2) trim to the paper's rows / columns (a wall or cloth touching one edge is thinner than the paper, so it drops out)
    cols = list(rc.resize((rc.width, 1), Image.BOX).getdata())
    rows = list(rc.resize((1, rc.height), Image.BOX).getdata())
    cs, rs = _span(cols, 0.5), _span(rows, 0.5)
    if not cs or not rs:
        return img
    (x0, x1), (y0, y1) = cs, rs
    fill = sum(1 for yy in range(y0, y1 + 1) for xx in range(x0, x1 + 1) if rc.getpixel((xx, yy)) > 127) / max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
    if fill < 0.6 or (x1 - x0 + 1) * (y1 - y0 + 1) > 0.97 * rc.width * rc.height:
        return img
    work = rgb      # cut from the full-resolution photo so print stays sharp
    big = work.rotate(best_a, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    fx, fy = big.width / rc.width, big.height / rc.height
    pad_x, pad_y = 0.02 * (x1 - x0 + 1), 0.006 * (y1 - y0 + 1)        # a little extra: crumpled edges curl inward
    out = big.crop((max(0, int((x0 - pad_x) * fx)), max(0, int((y0 - pad_y) * fy)), min(big.width, int((x1 + 1 + pad_x) * fx)), min(big.height, int((y1 + 1 + pad_y) * fy))))
    if out.width < 200 or out.height < 200:
        return img
    if out.width > out.height * 1.1:      # long side horizontal: the photo is sideways
        out = out.rotate(90, expand=True)
    # 3) blank background still visible at the corners of crumpled / curled receipts
    sm2 = out.resize((max(1, int(out.width * 200 / max(out.size))), max(1, int(out.height * 200 / max(out.size)))), Image.BILINEAR)
    w2, h2 = sm2.size
    px2 = sm2.load()
    # background = clearly coloured pixels (granite, wood, cloth). Shadows and creases on paper stay neutral.
    chroma = [[max(px2[x, y]) - min(px2[x, y]) for x in range(w2)] for y in range(h2)]
    centre = sorted(chroma[y][x] for y in range(h2 // 4, 3 * h2 // 4) for x in range(w2 // 4, 3 * w2 // 4))
    cut_off = max(34, (centre[len(centre) // 2] if centre else 0) + 18)      # relative to this paper's own tint
    band = max(3, int(0.08 * w2))                                           # background only lingers near the edge
    bg = [[chroma[y][x] >= cut_off and (min(x, w2 - 1 - x, y, h2 - 1 - y) < band) for x in range(w2)] for y in range(h2)]
    seen = [[False] * w2 for _ in range(h2)]
    edge = Image.new('L', (w2, h2), 0)
    for sy in range(h2):
        for sx in range(w2):
            if (sx in (0, w2 - 1) or sy in (0, h2 - 1)) and bg[sy][sx] and not seen[sy][sx]:
                stack, comp2 = [(sx, sy)], []
                seen[sy][sx] = True
                while stack:
                    x, y = stack.pop()
                    comp2.append((x, y))
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < w2 and 0 <= ny < h2 and bg[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
                if len(comp2) > 25:        # real background patches are blobs, text specks are not
                    for x, y in comp2:
                        edge.putpixel((x, y), 255)
    edge = edge.filter(ImageFilter.MaxFilter(3)).resize(out.size, Image.BILINEAR).point(lambda v: 255 if v > 80 else 0)
    out.paste((255, 255, 255), mask=edge)
    return out


def photo_date(path):
    """Date the photo was taken (EXIF), used only when the receipt text has no readable date."""
    try:
        exif = open_image(path).getexif()
        raw = exif.get(36867) or exif.get(306) or (exif.get_ifd(0x8769).get(36867) if hasattr(exif, 'get_ifd') else None)
        if raw:
            return dt.datetime.strptime(str(raw)[:10], '%Y:%m:%d').date().isoformat()
    except Exception:
        pass
    try:
        return dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()
    except OSError:
        return None


def flatten_light(img, gain=4):
    """Dark ink on white regardless of shadows / uneven light (subtract a blurred copy of the page)."""
    from PIL import ImageChops, ImageFilter, ImageOps
    bg = img.filter(ImageFilter.GaussianBlur(max(10, img.width // 50)))
    return ImageOps.invert(ImageChops.subtract(bg, img).point(lambda v: min(255, v * gain)))


def deskew(img):
    """Straighten a tilted photo: pick the angle (-8..8 deg) whose text rows are the most sharply separated."""
    from PIL import Image, ImageOps
    t = ImageOps.invert(flatten_light(img))
    t = t.resize((400, max(1, int(t.height * 400 / t.width)))).point(lambda v: 255 if v > 60 else 0)
    best, a = (0.0, -1.0), -8.0
    while a <= 8.0:
        col = list(t.rotate(a, fillcolor=0).resize((1, t.height), Image.BOX).getdata())
        mean = sum(col) / len(col)
        var = sum((v - mean) ** 2 for v in col)
        if var > best[1]:
            best = (a, var)
        a += 0.5
    if abs(best[0]) < 0.6:
        return img
    return img.rotate(best[0], expand=True, resample=Image.BICUBIC, fillcolor=255)


WEIGHT_TAG = re.compile(r'\A#w=(\d+)\n')


def split_weight(text):
    m = WEIGHT_TAG.match(text)
    return (int(m.group(1)), text[m.end():]) if m else (1, text)


def rank_variants(texts):
    """Order OCR readings best-first. The subtotal is read reliably across readings, so the reading whose items
    add up closest to it leads; recognised items break ties."""
    parsed = [parse_receipt_text(split_weight(t)[1]) for t in texts]
    subs = [p['subtotal'] or p['total'] for p in parsed if p['subtotal'] or p['total']]
    ref = max(set(subs), key=subs.count) if subs else None

    def score(p):
        got = sum(it['paid'] for it in p['items'])
        match = sum(1.0 if it['matched'] else 0.4 for it in p['items'])
        fit = 10 * (1 - min(1.0, abs(got - ref) / ref / 0.25)) if ref else 0.0
        return match + fit
    order = sorted(range(len(texts)), key=lambda i: score(parsed[i]), reverse=True)
    return [texts[i] for i in order]


def cropped_path(path, cache_dir):
    """Path of the background-free JPEG of a photo (made once, kept in the cache), or None if no paper was found."""
    if os.path.splitext(path)[1].lower() not in IMAGE_EXT:
        return None
    key = os.path.join(cache_dir, file_hash(path)[:20] + '-crop')
    if os.path.exists(key + '.jpg'):
        return key + '.jpg'
    if os.path.exists(key + '.none'):
        return None
    os.makedirs(cache_dir, exist_ok=True)
    try:
        full = open_image(path)
        paper = crop_to_paper(full)
        if paper.size == full.size and paper is full:
            open(key + '.none', 'w').close()
            return None
        paper.convert('RGB').save(key + '.jpg', 'JPEG', quality=90)
        return key + '.jpg'
    except Exception:
        return None


_rapid = {'engine': None}
_rapid_lock = __import__('threading').Lock()


def rapid_lines(img):
    """Read a receipt image with RapidOCR and rebuild text lines. Boxes are grouped by their height along the
    receipt's own tilt, so a slightly slanted photo still gives one line per printed row."""
    import math
    import numpy as np
    with _rapid_lock:
        if _rapid['engine'] is None:
            from rapidocr_onnxruntime import RapidOCR
            _rapid['engine'] = RapidOCR()
        res, _ = _rapid['engine'](np.array(img.convert('RGB')))
    if not res:
        return ''
    boxes = []
    for box, txt, _conf in res:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        ang = math.atan2(box[1][1] - box[0][1], box[1][0] - box[0][0])
        boxes.append({'x': sum(xs) / 4, 'y': sum(ys) / 4, 'h': max(ys) - min(ys), 'w': max(xs) - min(xs), 'ang': ang, 't': txt})
    wide = sorted(b['ang'] for b in boxes if b['w'] > 2 * b['h']) or [0.0]
    tilt = wide[len(wide) // 2]
    sn, cs = math.sin(tilt), math.cos(tilt)
    for b in boxes:
        b['u'] = b['x'] * cs + b['y'] * sn
        b['v'] = -b['x'] * sn + b['y'] * cs
    hs = sorted(b['h'] for b in boxes)
    gap = 0.6 * hs[len(hs) // 2]
    boxes.sort(key=lambda b: b['v'])
    lines, cur = [], []
    for b in boxes:
        if cur and b['v'] - sum(c['v'] for c in cur) / len(cur) > gap:
            lines.append(cur)
            cur = []
        cur.append(b)
    if cur:
        lines.append(cur)
    return '\n'.join('  '.join(c['t'] for c in sorted(ln, key=lambda c: c['u'])) for ln in lines)


def ocr_image(path, cache_dir):
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
    crop = cropped_path(path, cache_dir)
    cropped = crop is not None
    crop_img = open_image(crop) if cropped else None
    base = (crop_img if cropped else open_image(path)).convert('L')
    prepared = {}

    def sized(width):
        if width not in prepared:
            b = base.resize((width, max(1, int(base.height * width / base.width))), Image.LANCZOS)
            b = deskew(b) if cropped else b
            prepared[width] = b
        return prepared[width]

    def make(width, kind):
        b = sized(width)
        if kind == 'plain':
            img = ImageOps.autocontrast(b, cutoff=1).filter(ImageFilter.SHARPEN)
        else:
            img = flatten_light(b, 3 if kind == 'flat3' else 4)
        if cropped:       # blank the paper edge / table that survives the crop
            m = max(6, int(img.width * 0.012))
            d = ImageDraw.Draw(img)
            for box in ((0, 0, img.width, m), (0, img.height - m, img.width, img.height), (0, 0, m, img.height), (img.width - m, 0, img.width, img.height)):
                d.rectangle(box, fill=255)
        return img
    # no single setting reads every photo well, so read it many ways and let the readings vote (see parse_variants)
    plan = [(1500, 'flat3', 4), (1500, 'flat3', 6), (1500, 'flat4', 4), (1500, 'plain', 4), (1500, 'plain', 6),
            (2200, 'plain', 6), (2200, 'plain', 4), (2200, 'flat3', 4), (3000, 'plain', 6), (3000, 'flat3', 6)]
    texts, cache_imgs = [], {}
    if _has_rapid():                                  # primary reader; three treatments of the same crop
        from PIL import ImageEnhance
        color = crop_img if cropped else open_image(path)
        for variant in (color, ImageEnhance.Contrast(color.convert('RGB')).enhance(1.6), ImageOps.autocontrast(color.convert('L'), cutoff=1).convert('RGB')):
            try:
                t = rapid_lines(variant)
            except Exception:
                continue
            if t.strip() and all(t != x[5:] for x in texts):
                texts.append('#w=3\n' + t)
        if not _has_tesseract():
            return rank_variants(texts)
        plan = plan[:4]                                # a few classic readings add diversity
    elif not _has_tesseract():
        return rank_variants(texts)
    import pytesseract
    for width, kind, psm in plan:
        img = cache_imgs.setdefault((width, kind), make(width, kind))
        text = pytesseract.image_to_string(img, config=f'--oem 1 --psm {psm}')
        if text.strip() and text not in texts:
            texts.append('#w=1\n' + text)
    return rank_variants(texts)


def pdf_text(path):
    from pypdf import PdfReader
    return '\n'.join((p.extract_text() or '') for p in PdfReader(path).pages)


def file_hash(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_variants(path, cache_dir):
    """Return ([text, ...], source, warning). The first text is the best reading; photos may have several."""
    ext = os.path.splitext(path)[1].lower()
    sidecar = os.path.splitext(path)[0] + '.txt'
    if ext == '.txt' or os.path.exists(sidecar):
        with open(path if ext == '.txt' else sidecar, encoding='utf-8', errors='replace') as f:
            return [f.read()], 'text-file', None
    if ext == '.pdf':
        try:
            text = pdf_text(path)
        except Exception as e:
            return [''], 'pdf', f'could not read PDF ({e})'
        if len(text.strip()) < 30:
            return [''], 'pdf', 'PDF has no text layer (scanned) — save it as an image or add a .txt next to it'
        return [text], 'pdf', None
    cache = os.path.join(cache_dir, f'{file_hash(path)[:20]}-{CACHE_VERSION}.txt')
    if os.path.exists(cache):
        with open(cache, encoding='utf-8') as f:
            return f.read().split(VARIANT_SEP), 'cache', None
    try:
        texts = ocr_image(path, cache_dir)
    except Exception as e:
        return [''], 'ocr', f'could not read image ({e})'
    os.makedirs(cache_dir, exist_ok=True)
    with open(cache, 'w', encoding='utf-8') as f:
        f.write(VARIANT_SEP.join(texts))
    return texts, 'ocr', None


def read_text(path, cache_dir):
    """Return (best text, source, warning)."""
    texts, source, warn = read_variants(path, cache_dir)
    return (split_weight(texts[0])[1] if texts else ''), source, warn


# ── parsing ──────────────────────────────────────────────────────────────────
def num(s):
    return float(s.replace(',', '.'))


NOT_A_NAME = re.compile(r'\b(ave|avenue|st|street|rd|road|blvd|dr|drive|way|suite|ste|store|tel|phone|fax|www|receipt|welcome|thank|cashier|'
                        r'order|invoice|date|time|open|sale|transaction|customer|served|member|trans|till|reg|terminal|copy)\b|[#@:]|\.com', re.I)


def _word_hit(pattern, text):
    pattern = pattern.strip()
    return bool(re.search(r'(?<![a-z0-9])' + re.escape(pattern) + r'(?![a-z0-9])', text))


def find_store(text, filename, cat):
    """Vendor name: a known store in the header, else the receipt's own title line, else a known store anywhere
    (whole words only, so 'Haldiram' is never read as 'Aldi'), else the file name."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = '\n'.join(lines[:12]).lower()
    squeezed = re.sub(r'[^a-z0-9]', '', head)            # OCR often splits a name: 'Fel ipes Market'
    for name, pats in cat['stores'].items():
        if any(_word_hit(p, head) or (len(re.sub(r'[^a-z0-9]', '', p)) >= 6 and re.sub(r'[^a-z0-9]', '', p) in squeezed) for p in pats):
            return name
    for ln in lines[:8]:
        letters = re.sub(r'[^A-Za-z]', '', ln)
        if len(letters) >= 4 and len(letters) / max(1, len(ln.replace(' ', ''))) >= 0.75 and not NOT_A_NAME.search(ln) \
                and not PRICE_END_RE.search(ln) and not re.search(r'\d{3}', ln):
            return ' '.join(w.capitalize() if w.islower() or w.isupper() else w for w in re.sub(r"[^A-Za-z0-9&' ]", ' ', ln).split()[:4])
    low = text.lower()
    for name, pats in cat['stores'].items():
        if any(_word_hit(p, low) for p in pats):
            return name
    fn = os.path.basename(filename).lower()
    for name, pats in cat['stores'].items():
        if any(_word_hit(p, fn) for p in pats):
            return name
    return 'Unknown store'


def valid_date(y, m, d):
    try:
        out = dt.date(y, m, d)
    except ValueError:
        return None
    return out if dt.date(2000, 1, 1) <= out <= dt.date.today() + dt.timedelta(days=2) else None


def find_date(text, filename):
    for source in ('\n'.join(fix_ocr(ln) for ln in text.splitlines()), os.path.basename(filename)):
        for rx, kind in DATE_PATTERNS:
            for m in rx.finditer(source):
                g = m.groups()
                if kind == 'ymd':
                    d = valid_date(int(g[0]), int(g[1]), int(g[2]))
                elif kind == 'mdy':
                    y = int(g[2]) + (2000 if int(g[2]) < 100 else 0)
                    d = valid_date(y, int(g[0]), int(g[1])) or valid_date(y, int(g[1]), int(g[0]))
                elif kind == 'mon d y':
                    d = valid_date(int(g[2]), MONTHS[g[0][:3].lower()], int(g[1]))
                else:
                    d = valid_date(int(g[2]), MONTHS[g[1][:3].lower()], int(g[0]))
                if d:
                    return d.isoformat()
    return None


def split_price(line):
    """-> (text_without_price, price|None) where price is signed float."""
    m = PRICE_END_RE.search(line)
    if not m:
        return line, None
    flag = m.group(5)
    if flag and flag.lower() in UNIT_WORDS:  # '1.25 lb' is a weight, not a price
        return line, None
    price = num(f'{m.group(2) or 0}.{m.group(3)}')
    if m.group(1) == '-' or m.group(4) == '-':
        price = -price
    return line[:m.start()].rstrip(' .:*$'), price


def parse_size(text, kind_hint, volume_ctx):
    """Find a package size in a description -> (base_qty, kind, label) or None."""
    low = text.lower()
    m = MULTI_RE.search(low)
    if m:
        n, size, unit = int(m.group(1)), num(m.group(2)), m.group(3)
        unit = re.sub(r'[.\s]', '', unit).lower()
        got = unit_to_base(size, unit, kind_hint, volume_ctx)
        if got and got[0] != 'count':
            return got[1] * n, got[0], f'{n} × {fmt_num(size)} {unit}'
    m = PACK_THEN_VOL_RE.search(low)
    if m:
        n, size, unit = int(m.group(1)), num(m.group(2)), m.group(3)
        got = unit_to_base(size, re.sub(r'[.\s]', '', unit).lower(), 'volume', True)
        if got:
            return got[1] * n, 'volume', f'{n} × {fmt_num(size)} {re.sub(r"[.\s]", "", unit).lower()}'
    found = []
    for m in SIZE_RE.finditer(low):
        size, unit = num(m.group(1)), re.sub(r'[.\s]', '', m.group(2)).lower()
        got = unit_to_base(size, unit, kind_hint, volume_ctx)
        if got and size > 0:
            found.append((got[0], got[1], f'{fmt_num(size)} {unit}'))
    if DOZEN_RE.search(low) and not found:
        found.append(('count', 12.0, '1 dozen'))
    if not found:
        if re.search(r'\bhalf\s*gal(?:lon)?\b|\bhalf\s*gal\b', low):
            found.append(('volume', 64.0, '½ gal'))
        elif re.search(r'\bgal(?:lon)?\b', low):
            found.append(('volume', 128.0, '1 gal'))
    if not found and re.search(r'\b(each|ea)\b', low):
        found.append(('count', 1.0, 'each'))
    if not found:
        return None
    measured = [f for f in found if f[0] != 'count']
    if measured:
        pick = measured[0]
    else:
        pick = found[0]
    return pick[1], pick[0], pick[2]


def unit_to_base(size, unit, kind_hint, volume_ctx):
    unit = unit.lower()
    if unit in ('floz', 'fl oz', 'fl.oz'):
        return 'volume', size
    if unit == OZ:
        if kind_hint == 'volume' or (kind_hint is None and volume_ctx):
            return 'volume', size
        return 'weight', size / 16.0
    if unit in UNITS:
        kind, factor = UNITS[unit]
        return kind, size * factor
    return None


def fmt_num(x):
    return f'{x:.3f}'.rstrip('0').rstrip('.')


def clean_name(desc, cat):
    """Strip codes, sizes, flags; expand abbreviations. -> cleaned lowercase string."""
    s = desc.lower()
    s = re.sub(r'\b\d{5,}\b', ' ', s)                                  # UPC / PLU / item numbers
    s = MULTI_RE.sub(' ', s)
    s = SIZE_RE.sub(' ', s)
    s = re.sub(r'(?<![\w.])\d+(?:[.,]\d+)?\s*(?:@|x)\s*\S*', ' ', s)
    s = re.sub(r'[^a-z0-9%&\' ]', ' ', s)
    toks = [t for t in s.split() if not re.search(r'\d', t) or re.fullmatch(r'\d+%', t)]
    if toks and len(toks[0]) == 1:                                       # Costco-style leading flag letter
        toks = toks[1:]
    if toks and len(toks[-1]) == 1:                                      # trailing tax flag
        toks = toks[:-1]
    out = []
    for t in toks:
        out.extend(cat['abbr'].get(t, t).split())
    return ' '.join(out)


def ignored_line(line, cat):
    low = line.lower().strip()
    if any(p in low for p in cat['ignore_anywhere']):
        return True
    plain = re.sub(r'[^a-z0-9#& ]', ' ', low).strip()
    for p in cat['ignore_start']:
        if plain == p or plain.startswith(p + ' '):
            rest = plain[len(p):].split()
            if p in ('total', 'subtotal', 'sub total', 'tax', 'sales tax', 'balance', 'change', 'cash', 'credit', 'debit',
                     'tender', 'payment', 'savings'):
                return len(rest) <= 4 and not any(w in ('yogurt', 'greek', 'cereal') for w in rest)
            return True
    return False


def fix_ocr(line):
    """Repair the usual OCR slips inside prices ('Q.29', '5.4/7', '1 .99') without touching words."""
    line = re.sub(r'(?<![A-Za-z0-9])[OQDo]\.(\d{2})\b', r'0.\1', line)
    line = re.sub(r'(\d)[.,](\d)[/|!](\d)\b', r'\1.\2\3', line)
    line = re.sub(r'(\d)[.,][/|](\d)\b', r'\1.7\2', line)               # '4./9' -> '4.79'
    line = re.sub(r'(?<=\b\d{4} )(\d{2})(?=[^\d]*$)', r'.\1', line)                         # 'CARROTS 4562 41 F' -> '.41'
    if re.search(r'@|\b[Il1]b\b|\blb\b', line) and not re.search(r'\d{1,2}/\d{1,2}/\d{2}', line):
        line = re.sub(r'(?<=/)\s*(\d{2})\b(?![.\d])', r' .\1', line)               # '1 lb / 89' -> '1 lb / .89'
    line = re.sub(r'(?<=[A-Za-z])(?=\d{1,3}(?:\.\d+)?\s?(?:lb|oz|ct|pk|kg|ml)\b)', ' ', line)      # 'HAPPI2LB' -> 'HAPPI 2LB'
    line = re.sub(r'\b(\d{1,2})[-/.](\d)[.,](\d)[-/.](20\d{2})\b', r'\1-\2\3-\4', line)          # '09-2.1-2026' -> '09-21-2026'
    line = re.sub(r'\b(\d{1,2})[-/.](\d{2})(20\d{2})\b', r'\1-\2-\3', line)               # '09-212026' -> '09-21-2026'
    line = re.sub(r'(?<=\d)(?:1b|ib)\b', 'lb', line)                       # '11b' -> '1lb'
    line = re.sub(r'(\d{6,})(?=[A-Za-z])', r'\1 ', line)                    # '8904063230119Alo0' -> UPC then name
    line = re.sub(r'(?<=[\d/ ])1b\b', 'lb', line)                            # '1.5 1b' -> '1.5 lb'
    line = re.sub(r'(?<=\d) ?(?:[Il1\\|]b|ib|lo)(?=\s*[@e©9])', ' lb', line)          # '1.87 1b@' / '3.33 1\\b @'
    return line


DEC_RE = re.compile(r'(?<![\d.])(\d*[.,]\d{2})(?![\d])')


GARBLED_LB = re.compile(r'\b[ilIL1|\\]{1,2}[bBh]\w{0,3}\b')   # 'lb', '1b', 'ilb', 'libs', 'Libby' …


def naked_measure(line):
    """A weight/qty line that carries no line total, e.g. '1.87 lb @ 1 lb / 1.29' or '2 @ 1/ .89'
    (units are often garbled by OCR). -> {'w':weight|None,'r':rate|None,'lb':bool} or None."""
    if re.fullmatch(r'\W{0,3}\d{1,2}\W{0,3}\$?\s?\d*[.,]\d{2}\W{0,3}', line) and len(line) < 16:    # '3 @ $0.79' with the @ lost
        tok = DEC_RE.findall(line)[-1]
        return {'w': None, 'r': num(('0' if tok.startswith(('.', ',')) else '') + tok), 'lb': False}
    if re.search(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}:\d{2}', line):
        return None                                                  # dates and times are not weight lines
    stripped = GARBLED_LB.sub(' ', line)
    if any(len(w) >= 4 for w in re.findall(r'[A-Za-z]+', stripped)):
        return None
    if not re.search(r'[@/\\|©]|[Il1]b|\blb\b|\bib\b', line):
        return None
    toks = DEC_RE.findall(line)
    val = lambda t: num(('0' if t.startswith(('.', ',')) else '') + t)
    wm = re.match(r'^[^\d]{0,4}(\d)[.,]?\s?(\d{2})', line)         # '187' / '140' with the decimal point lost
    if len(toks) >= 2:
        w, r = val(toks[0]), val(toks[-1])
    elif toks and (lambda mk: mk and line.find(toks[0]) < mk.start())(re.search(r'@|©|\b[Il1]b|\blb', line)) and not re.search(r'(?<![\d.])(\d{2})\.?(?:\s+\S{1,4})?\s*$', line):
        w, r = val(toks[0]), None                                   # '1.38 lb @ ..29': the number before the unit is the weight
    elif toks:
        bare = re.search(r'(?<![\d.])(\d{2})\.?(?:\s+\S{1,4})?\s*$', line)     # '... lb/ 69.' = rate .69 with the dot lost
        if bare and line.find(toks[0]) < bare.start(1):
            w, r = val(toks[0]), num('0.' + bare.group(1))
        else:
            r = val(toks[0])
            w = num(f'{wm.group(1)}.{wm.group(2)}') if wm and wm.end() <= line.find(toks[0]) else None
    else:
        r, w = None, num(f'{wm.group(1)}.{wm.group(2)}') if wm else None
    if w is None and r is None:
        return None
    return {'w': w, 'r': r, 'lb': bool(GARBLED_LB.search(line))}


def resolve_measure(m, paid):
    """Match a naked measure line to an item total. -> measure dict for finish_item or None.
    strong=True when weight x rate reproduces the printed total; otherwise the more plausible of
    'weight as read' / 'weight implied by the rate' is used."""
    w, r = m['w'], m['r']
    if w and r and abs(w * r - paid) <= 0.04 * paid + 0.02:
        return {'type': 'weight', 'qty': w, 'unit': 'lb', 'rate': r, 'strong': True}
    if r and not w and not m.get('lb'):          # '2 @ 1/ .89': a count, valid only when paid / rate is a whole number
        q = paid / r
        if abs(q - round(q)) <= 0.03 and 2 <= round(q) <= 24:
            return {'type': 'count', 'qty': int(round(q)), 'rate': r, 'strong': True}
        return None
    cands = []
    if w:
        cands.append((w, paid / w, abs(paid / w - r) / r if r else 0.0))          # weight as printed
    if r:
        cands.append((paid / r, r, abs(paid / r - w) / w if w else 0.0))          # weight implied by the $/lb
    ok = [c for c in cands if 0.05 < c[0] <= 15 and 0.35 <= c[1] <= 40]
    if not ok:
        return None
    wt, rate, err = min(ok, key=lambda c: c[2])
    if len(ok) > 1 and err > 0.6:
        return None                                  # both numbers disagree with the total: don't guess
    return {'type': 'weight', 'qty': round(wt, 2), 'unit': 'lb', 'rate': round(rate, 2), 'strong': False}


def parse_receipt_text(text, filename=''):
    cat = load_catalog()
    store = find_store(text, filename, cat)
    date = find_date(text, filename)
    warnings = []
    subtotal = total = None
    pending = None   # description line waiting for its price/weight line
    last = None
    recs = []        # ('item', dict) | ('measure', dict) in receipt order

    for lineno, raw in enumerate(text.splitlines()):
        line = fix_ocr(re.sub(r'\s+', ' ', raw).strip())
        line = re.sub(r'^(?:[^\w\s]\S?\s+|[^\s\d%]{1,2}\s+){1,2}(?=[A-Za-z]{4,})', '', line)   # stray marks left of a name
        if len(line) < 3:
            continue
        low = line.lower()
        body, price = split_price(line)
        if price is not None and re.match(r'^\W*\w{0,4}\W*(sub\s*total|total|balance due|balance to pay|amount due|grand total)\b', low) and 'saving' not in low:
            if re.search(r'sub\s*total', low[:14]):
                subtotal = price
            elif total is None or price > total:
                total = price
            continue
        if ignored_line(line, cat):
            pending = None
            continue

        # a "2 @ 3.99" or "1.2 lb @ 2.99" line: the trailing number is the unit rate, not a line total
        m2 = re.fullmatch(r'\s*(\d{1,3})\s*[@x]\s*', body) if price is not None else None
        m3 = re.fullmatch(r'\s*' + _NUM + r'\s*(lbs?|oz|kg|g)\s*[@x]\s*', body, re.I) if price is not None and not m2 else None
        wm = WEIGHT_AT_RE.search(body)
        qm = None if wm else QTY_AT_RE.search(body)
        measure = None
        rest = body
        if m2 or m3:           # weight / quantity line with a unit rate but no total: it belongs to a neighbouring item
            recs.append(('measure', {'w': num(m3.group(1)) if m3 else None, 'r': price, 'lb': bool(m3), 'line': lineno}))
            continue
        if wm:
            measure = {'type': 'weight', 'qty': num(wm.group(1)), 'unit': wm.group(2).lower(), 'rate': num(wm.group(3))}
            rest = (body[:wm.start()] + ' ' + body[wm.end():]).strip()
        elif qm:
            measure = {'type': 'count', 'qty': int(qm.group(1)), 'rate': num(qm.group(2))}
            rest = (body[:qm.start()] + ' ' + body[qm.end():]).strip()
        nm = naked_measure(line) if measure is None else None
        if nm:                     # weight line without a total; resolved against item totals once all lines are read
            recs.append(('measure', dict(nm, line=lineno)))
            continue
        letters = len(re.findall(r'[A-Za-z]', rest))

        # discounts / coupons reduce the previous item
        if price is not None and price < 0 and any(w in low for w in cat['discount']) and last is not None:
            last['paid'] = round(last['paid'] + price, 2)
            last['discount'] = round(last.get('discount', 0) + price, 2)
            continue

        if letters < 3:   # continuation line: "1.23 lb @ 2.99/lb  3.68" or "2 @ 1.50"
            target = pending or (last if last is not None and last.get('measure') is None else None)
            if target is not None and measure is not None:
                target['measure'] = measure
                if price is not None and price > 0 and target.get('paid') is None:
                    target['paid'] = price
                if target is pending:
                    last, pending = pending, None
            continue

        if price is None and measure is None and last is not None and last.get('paid') and not last.get('extra_size') \
                and SIZE_RE.search(rest) and len(re.findall(r'[A-Za-z]{3,}', SIZE_RE.sub(' ', rest))) <= 2 and len(rest.split()) <= 4:
            last['extra_size'] = rest              # e.g. '2lb,Gopi' printed under the item name
            continue
        if price is None and measure is None:
            pending = {'raw': rest, 'line': lineno, 'paid': None, 'measure': None}
            recs.append(('item', pending))   # may get its total from a neighbouring weight line (w x rate)
            continue
        if price is not None and price <= 0:
            continue
        item = {'raw': rest, 'line': lineno, 'paid': price, 'measure': measure}
        recs.append(('item', item))   # no printed total + a weight/qty line: the total is worked out from the rate
        last, pending = item, None

    attach_measures(recs)
    items = [it for kind, it in recs if kind == 'item']
    for it in items:
        finish_item(it, cat)
    items = [it for it in items if it.get('paid') and it['paid'] > 0 and it.get('base_qty') and keep_item(it)]

    kept_sum = sum(it['paid'] for it in items)
    ref = subtotal if subtotal else total
    if ref and ref > 0:
        gap = abs(kept_sum - ref) / ref
        if gap > 0.12:
            warnings.append(f'Items add up to ${kept_sum:,.2f} but the receipt shows ${ref:,.2f} — some lines may be missing or misread.')
    if not items:
        warnings.append('No priced items were found in this receipt.')
    return {'store': store, 'date': date, 'items': items, 'subtotal': subtotal, 'total': total, 'warnings': warnings}


def modal(values):
    return max(set(values), key=values.count) if values else None


def consensus_items(results, ref):
    """Combine many OCR readings of one receipt. Every distinct price seen is a candidate line and each reading that
    contains it is a vote (RapidOCR readings count more). When the receipt prints its own subtotal, choose the
    combination of prices that adds up to it exactly and is best supported by the votes; a misread price only
    gets in if the readings really back it. Without a subtotal, keep what most readings agree on. Names, sizes and
    weights are then voted per price too, so two different items with the same price stay two items."""
    total_w = sum(r['_w'] for r in results)
    tagged = []
    for vi, r in enumerate(results):
        for it in r['items']:
            tagged.append(dict(it, _v=vi))
    by_val = {}
    for t in tagged:
        by_val.setdefault(round(t['paid'], 2), []).append(t)
    cands = {}                                    # value -> {votes, mult, names: [(votes, mult, rep)]}
    for val, its in by_val.items():
        variants = {i['_v'] for i in its}
        per = [sum(1 for i in its if i['_v'] == v) for v in variants]
        # a garbled name takes the recognised name read for the same price in another reading
        known = {}
        for i in its:
            if i['matched']:
                known[i['name']] = known.get(i['name'], 0) + results[i['_v']]['_w']
        known_toks = {i['name']: set(i['cleaned'].split()) for i in its if i['matched']}

        def label(i):
            if i['matched']:
                return i['name']
            mine = set(i['cleaned'].split())          # fold a garble into a recognised name only if they look alike
            for nm in sorted(known, key=known.get, reverse=True):
                if any(len(a) >= 4 and len(b) >= 4 and edits(a, b, 2) for a in mine for b in known_toks[nm]) or mine & known_toks[nm]:
                    return nm
            return 'other:' + i['cleaned'][:12]
        by_name = {}
        for i in its:
            by_name.setdefault(label(i), []).append(i)
        names = []
        for nm, group in by_name.items():
            vs = {i['_v'] for i in group}
            cnt = Counter_mode([sum(1 for i in group if i['_v'] == v) for v in vs])
            names.append((sum(results[v]['_w'] for v in vs), cnt, pick_rep(group, results)))
        names.sort(key=lambda x: -x[0])
        cands[val] = {'votes': sum(results[v]['_w'] for v in variants), 'mult': Counter_mode(per), 'names': names}
    # a price whose '$' was read as a digit ('85.99') also offers '5.99' (only used if it makes the total add up)
    for val in list(cands):
        m = re.fullmatch(r'(\d)(\d{1,2}\.\d{2})', f'{val:.2f}')
        if m and val >= 10 and float(m.group(2)) >= 0.5:
            alt = round(float(m.group(2)), 2)
            if alt not in cands:
                c = cands[val]
                cands[alt] = {'votes': c['votes'] * 0.9, 'mult': c['mult'], 'alt': True,
                              'names': [(v, k, dict(rep, paid=alt, _alt=True)) for v, k, rep in c['names']]}
    if ref and len(cands) > 1 and any(v != round(ref, 2) for v in cands):
        cands.pop(round(ref, 2), None)            # the total itself is not an item

    def build(picks):
        out = []
        for val, count in picks:
            slots = []
            for _votes, mult, rep in cands[val]['names']:
                slots += [rep] * mult
            slots = (slots + [cands[val]['names'][0][2]] * count)[:count]
            for rep in slots:
                it = dict(rep)
                if it.pop('_alt', False):
                    reprice(it, val)
                it.pop('_v', None)
                out.append(it)
        return out

    half = 0.5 * total_w
    if ref:
        target = int(round(ref * 100))
        dp = {0: (0.0, [])}                                       # cents -> (support score, picks)
        for val, c in cands.items():
            cents, score = int(round(val * 100)), c['votes'] - half     # lines most readings back score > 0
            ndp = dict(dp)
            for have, (sc, picks) in dp.items():
                for k in range(1, c['mult'] + 1):
                    nc = have + cents * k
                    if nc > target + 2:
                        break
                    cand_sc = sc + score * k
                    if nc not in ndp or cand_sc > ndp[nc][0]:
                        ndp[nc] = (cand_sc, picks + [(val, k)])
            dp = ndp
        hit = max(((sc, c) for c, (sc, _) in dp.items() if abs(c - target) <= 1 and c > 0), default=None)
        if hit:
            return build(dp[hit[1]][1]), True
    need = max(2.0, 0.4 * total_w)
    return build([(v, c['mult']) for v, c in cands.items() if c['votes'] >= need and not c.get('alt')]), False


def pick_rep(group, results):
    """Representative reading of one line. Weight / size are voted (readings disagree about digits), then the cleanest
    text with that weight wins."""
    tally = {}
    for i in group:
        key = i['qty_label']
        tally[key] = tally.get(key, 0) + results[i['_v']]['_w'] * (2 if i['kind'] != 'count' or i['size_known'] else 1)
    top = max(tally, key=tally.get)
    return best_rep([i for i in group if i['qty_label'] == top])


def Counter_mode(xs):
    return max(set(xs), key=xs.count) if xs else 1


def best_rep(items):
    """The reading of a line carrying the most information: recognised name, weight/size, clean text."""
    def rank(i):
        return (i['matched'], i['kind'] != 'count' or i['size_known'], bool(i.get('unit_price')), -len(re.findall(r'[^\w\s]', i['desc'])))
    return max(items, key=rank)


def is_dining(text, store, cat):
    """Restaurant / cafe receipts (eating out) are kept out of the grocery price analysis. A known restaurant chain in
    the header, or at least two restaurant-only words (tip, gratuity, server, table, guests, dine in...)."""
    low = text.lower()
    head = '\n'.join(low.splitlines()[:12])
    if any(_word_hit(w, head) or _word_hit(w, (store or '').lower()) for w in cat['dining_stores']):
        return True
    return sum(1 for w in cat['dining_words'] if re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', low)) >= 2


def parse_variants(texts, filename=''):
    """Parse every OCR reading, then vote line by line and check the result against the receipt's own subtotal."""
    results = []
    for t in texts:
        w, body = split_weight(t)
        if body.strip():
            r = parse_receipt_text(body, filename)
            r['_w'] = w
            results.append(r)
    if not results:
        return parse_receipt_text('', filename)
    base = results[0]

    def wmodal(key, pool=None):          # readings vote with their weight (RapidOCR counts more than Tesseract)
        tally = {}
        for r in results:
            v = r[key]
            if v and v != 'Unknown store':
                tally[v] = tally.get(v, 0) + r['_w']
        return max(tally, key=tally.get) if tally else None
    base['store'] = wmodal('store') or base['store']
    base['date'] = wmodal('date')                            # OCR misreads a digit now and then: majority wins
    ref = wmodal('subtotal') or wmodal('total')
    base['subtotal'] = ref
    base['dining'] = is_dining('\n'.join(split_weight(t)[1] for t in texts[:3]), base['store'], load_catalog())
    items, exact = consensus_items(results, ref)
    base['items'] = items
    base['warnings'] = []
    got = sum(i['paid'] for i in items)
    if ref and abs(got - ref) / ref > 0.012:
        base['warnings'].append(f'Items add up to ${got:,.2f} but the receipt shows ${ref:,.2f} — some lines may be missing or misread.')
    if not items:
        base['warnings'].append('No priced items were found in this receipt.')
    return base


def reprice(it, paid):
    it['paid'] = round(paid, 2)
    if it.get('base_qty'):
        per = {'weight': 1.0, 'volume': 128.0, 'count': 1.0}[it['kind']]
        it['unit_price'] = round(it['paid'] / (it['base_qty'] / per), 4)


def attach_measures(recs):
    """Give each weight line to the item it belongs to. Receipts print it either before or after the item name;
    the direction that makes weight x rate equal the printed total most often wins for the whole receipt."""
    def fit(mi, ii):
        it, m = recs[ii][1], recs[mi][1]
        if it.get('measure'):
            return None
        if not it.get('paid'):          # no readable total: derive it from weight x rate
            if m['w'] and m['r'] and 0.1 < m['w'] * m['r'] < 200:
                return {'type': 'weight', 'qty': m['w'], 'unit': 'lb', 'rate': m['r'], 'strong': False, 'paid': round(m['w'] * m['r'], 2)}
            return None
        return resolve_measure(m, it['paid'])
    votes = {1: 0, -1: 0}
    for mi, (kind, _) in enumerate(recs):
        if kind != 'measure':
            continue
        for d in (1, -1):
            ni = mi + d
            while 0 <= ni < len(recs) and recs[ni][0] == 'measure':
                ni += d
            if 0 <= ni < len(recs):
                r = fit(mi, ni)
                if r and r['strong']:
                    votes[d] += 1
    order = (1, -1) if votes[1] >= votes[-1] else (-1, 1)
    for mi, (kind, _) in enumerate(recs):
        if kind != 'measure':
            continue
        for d in order:
            ni = mi + d
            while 0 <= ni < len(recs) and recs[ni][0] == 'measure':
                ni += d
            if 0 <= ni < len(recs):
                r = fit(mi, ni)
                if r and (r['strong'] or d == order[0]):
                    if r.get('paid') and not recs[ni][1].get('paid'):
                        recs[ni][1]['paid'] = r['paid']
                    recs[ni][1]['measure'] = r
                    break


def keep_item(it):
    """Drop OCR debris that isn't a plausible product line."""
    if it['matched']:
        return True
    letters = re.sub(r'[^a-z ]', '', it['cleaned'])
    return any(len(w) >= 4 for w in letters.split()) and '@' not in it['desc']


def finish_item(it, cat):
    desc = it['raw']
    cleaned = clean_name(desc, cat)
    toks = fuzzy_tokens(words(cleaned), cat)
    cleaned = ' '.join(toks)
    it['cleaned'] = cleaned
    entry = match_item(toks, cat)
    if entry is None:                      # produce stickers carry a 4-digit PLU code that identifies the item
        code = re.search(r'\b(\d{4})\b', desc)
        entry = cat['by_name'].get(cat['plu'].get(code.group(1), '')) if code else None
    kind_hint = entry['kind'] if entry else None
    volume_ctx = any(t in cat['volume_words'] for t in toks)
    it['name'] = entry['name'] if entry else auto_name(cleaned)
    it['group'] = entry['group'] if entry else 'Other (not in catalog)'
    it['matched'] = bool(entry)
    it['organic'] = 'organic' in toks
    m = it.pop('measure', None)
    paid = it.get('paid')
    size = parse_size(desc + ' ' + it.get('extra_size', ''), kind_hint, volume_ctx)

    base_qty = kind = label = None
    if m and m['type'] == 'weight':
        unit = m['unit'].replace('lbs', 'lb')
        got = unit_to_base(m['qty'], unit, 'weight', False)
        kind, base_qty, label = 'weight', got[1] if got else None, f"{fmt_num(m['qty'])} {unit}"
        if paid is None and base_qty:
            rate_unit = unit
            paid = round(m['rate'] * m['qty'], 2) if rate_unit == unit else None
    elif size:
        base_qty, kind, label = size[0], size[1], size[2]
        if m and m['type'] == 'count' and m['qty'] > 1 and (paid is None or abs(paid - m['qty'] * m['rate']) <= max(0.05, 0.05 * paid)):
            base_qty *= m['qty']
            label = f"{m['qty']} × {label}"
            if paid is None:
                paid = round(m['qty'] * m['rate'], 2)
    else:
        n = m['qty'] if m and m['type'] == 'count' else 1
        if m and paid and abs(paid - n * m['rate']) > max(0.05, 0.05 * paid):
            n = 1   # printed price isn't qty x rate, so the quantity line is informational
        base_qty, kind, label = float(n), 'count', f'{n} item' + ('s' if n != 1 else '')
        it['size_known'] = False
        if paid is None and m:
            paid = round(n * m['rate'], 2)
    it['size_known'] = it.get('size_known', True)
    it['paid'] = paid
    it['kind'] = kind
    it['base_qty'] = base_qty
    it['qty_label'] = label
    if paid and base_qty:
        per = {'weight': 1.0, 'volume': 128.0, 'count': 1.0}[kind]   # report $/lb, $/gal, $/each
        it['unit_price'] = round(paid / (base_qty / per), 4)
        it['unit_qty'] = round(base_qty / per, 4)
    it['desc'] = re.sub(r'\s+', ' ', desc).strip()


def edits(a, b, limit):
    """True when a and b differ by at most `limit` single-character edits (insert/delete/replace)."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= limit


def fuzzy_tokens(toks, cat):
    """Repair OCR typos and truncations against catalog words: 'broccolt' -> 'broccoli', 'toma' -> 'tomato'."""
    vocab, vset, out = cat['vocab'], set(cat['vocab']), []
    for t in toks:
        if len(t) < 4 or t in vset or t in cat['abbr']:
            out.append(t)
            continue
        pref = [v for v in vocab if v.startswith(t)] if len(t) >= 4 else []
        if len(pref) == 1:
            out.append(pref[0])
            continue
        lim = 2 if len(t) >= 7 else 1 if len(t) >= 5 else 0
        near = [v for v in vocab if lim and edits(t, v, lim)] if lim else []
        out.append(near[0] if len(near) >= 1 and len({*near}) == 1 else t)
    return out


def auto_name(cleaned):
    toks = [t for t in cleaned.split() if len(t) > 1]
    toks = [t for t in toks if t not in ('organic', 'fresh', 'large', 'small', 'medium', 'lg', 'sm', 'great', 'value', 'kirkland')]
    if not toks:
        return 'Unlabeled item'
    return ' '.join(w.capitalize() for w in toks[:3])


# ── folder scan ──────────────────────────────────────────────────────────────
def receipt_id(rel):
    return hashlib.sha1(rel.encode('utf-8')).hexdigest()[:12]


def scan_receipts(folder):
    """Read every receipt in the folder. -> {receipts, warnings, ocr}"""
    cache_dir = os.path.join(folder, '.cache')
    files = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for n in sorted(names):
            if n.startswith('.'):
                continue
            ext = os.path.splitext(n)[1].lower()
            full = os.path.join(root, n)
            if ext in IMAGE_EXT or ext == '.pdf':
                files.append(full)
            elif ext == '.txt':
                stem_path = os.path.splitext(full)[0]
                if not any(os.path.exists(stem_path + e) for e in IMAGE_EXT | {'.pdf'}):
                    files.append(full)
    status = ocr_status()
    out, warnings = [], []

    def work(path):
        rel = os.path.relpath(path, folder)
        ext = os.path.splitext(path)[1].lower()
        rid = receipt_id(rel)
        base = {'id': rid, 'file': rel, 'kind': 'image' if ext in IMAGE_EXT else 'pdf' if ext == '.pdf' else 'text'}
        sidecar = os.path.exists(os.path.splitext(path)[0] + '.txt')
        if ext in IMAGE_EXT and not status['ok'] and not sidecar:
            return dict(base, items=[], warnings=['OCR unavailable — ' + status['reason']], store=None, date=None)
        texts, source, warn = read_variants(path, cache_dir)
        if warn:
            return dict(base, items=[], warnings=[warn], store=None, date=None)
        parsed = parse_variants(texts, path)
        if not parsed['date'] and ext in IMAGE_EXT:
            parsed['date'] = photo_date(path)      # no readable date on the receipt: use when the photo was taken
            parsed['date_from_photo'] = bool(parsed['date'])
        return dict(base, source=source, **parsed)

    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(work, files))
    for r in out:
        for w in r['warnings']:
            warnings.append(f"{r['file']}: {w}")
    return {'receipts': out, 'warnings': warnings, 'ocr': status, 'count': len(out)}


def raw_text(path, folder):
    text, _, _ = read_text(path, os.path.join(folder, '.cache'))
    return text


def jpeg_bytes(path):
    img = open_image(path).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=88)
    return buf.getvalue()
