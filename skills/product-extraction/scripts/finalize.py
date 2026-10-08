# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema>=4.21", "pillow>=10"]
# ///
"""Check, normalise and package draft records into the Nairos import file.

  uv run finalize.py <work>/draft/*.jsonl --out <out_dir> [--currency GEL] [--expected 120]
                     [--source-text <work>/text]

Input lines (written by you or by sheet_apply.py):
  {"data": {catalog fields…}, "source": {"file", "page"|"sheet"+"row", "text"?, "method"?}, "flags": [...]}

Writes to <out_dir>:
  products.ndjson     the file to upload: Nairos console → space → Products → Catalog → product table → Import
  review.csv          one line per product with its flags — open it in Excel to eyeball the result
  rejected.jsonl      records that could not be fixed, with the reason
  report.md           counts, flags, duplicates, reconciliation — show this to the user
  preview.html        open in a browser: every product as a card, with its source page and JSON
  images/<code>.jpg   product photos cut from the document (when source.image / image_box is set)
  images-check.png    all cut photos on one sheet, labelled by code — look at it once

Rules (why they exist):
  * title and external_id are required by the catalog; a record without a usable title is
    rejected here rather than silently dead-lettered later.
  * price / compare_at_amount must be {"amount": "...", "currency": "XXX"} in the catalog.
    Plain numbers and strings like "1 299,00 ₾" are converted; a price with no currency
    anywhere is moved to attributes (never deleted) and flagged.
  * keys the catalog does not know are moved into attributes, so nothing is lost.
  * external_id is stable across file revisions: SKU, else barcode/GTIN, else a hash of
    brand + title. Two different products that end up with the same id get distinct ids
    and a flag; exact repeats are merged.
  * values are checked against the source text when we have it; a value that cannot be
    found is kept and flagged, never removed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).parent
SCHEMA_PATH = HERE.parent / "assets" / "canonical-product.schema.json"

CURRENCY_SIGNS = {
    "₾": "GEL", "ლარი": "GEL", "gel": "GEL", "lari": "GEL",
    "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "₺": "TRY", "tl": "TRY", "try": "TRY",
    "₽": "RUB", "руб": "RUB", "rub": "RUB", "£": "GBP", "gbp": "GBP", "₼": "AZN", "azn": "AZN",
    "֏": "AMD", "amd": "AMD", "₴": "UAH", "uah": "UAH",
}
MONEY_FIELDS = ("price", "compare_at_amount")
LIST_FIELDS = {"category_path", "tags", "badges", "image_urls", "video_urls"}
INTERNAL_KEYS = {"_number_format"}


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    return re.sub(r"\s+", " ", s).strip()


def parse_amount(raw, fmt: dict | None = None) -> tuple[Decimal | None, str | None]:
    """'1 299,00 ₾' → (Decimal('1299.00'), 'GEL'). Returns (None, None) if no number."""
    if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
        return Decimal(str(raw)), None
    s = str(raw).strip()
    cur = None
    low = s.casefold()
    for sign, code in sorted(CURRENCY_SIGNS.items(), key=lambda kv: -len(kv[0])):
        if sign in low:
            cur = code
            break
    num = re.sub(r"[^\d,.\-\s']", "", s).strip()
    num = re.sub(r"[\s']", "", num)
    if not re.search(r"\d", num):
        return None, cur
    if fmt and fmt.get("decimal") == ",":
        num = num.replace(".", "").replace(",", ".")
    elif fmt and fmt.get("decimal") == ".":
        num = num.replace(",", "")
    else:
        # Guess: the right-most separator followed by exactly 1–2 digits is the decimal point.
        m = re.search(r"[.,](\d{1,2})$", num)
        if m:
            head = num[: m.start()].replace(",", "").replace(".", "")
            num = f"{head}.{m.group(1)}"
        else:
            num = num.replace(",", "").replace(".", "")
    try:
        return Decimal(num), cur
    except InvalidOperation:
        return None, cur


def gtin_ok(code: str) -> bool:
    if not re.fullmatch(r"\d{8}|\d{12}|\d{13}|\d{14}", code):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def slug(s: str) -> str:
    return re.sub(r"[^\w\-.]+", "-", norm_text(s)).strip("-")[:200]


class Finalizer:
    def __init__(self, schema: dict, default_currency: str | None, text_dir: Path | None):
        self.schema = schema
        self.props = set(schema["properties"])
        self.default_currency = default_currency.upper() if default_currency else None
        self.text_dir = text_dir
        self._page_text: dict = {}
        from jsonschema import Draft202012Validator

        self.validator = Draft202012Validator(schema)

    def page_text(self, source: dict) -> str | None:
        if source.get("text"):
            return source["text"]
        if self.text_dir and source.get("page"):
            key = int(source["page"])
            if key not in self._page_text:
                p = self.text_dir / f"p{key:03d}.txt"
                self._page_text[key] = p.read_text(encoding="utf-8") if p.exists() else None
            return self._page_text[key]
        return None

    def normalise(self, rec: dict) -> tuple[dict | None, list[str], str | None]:
        data = dict(rec.get("data") or {})
        flags = list(rec.get("flags") or [])
        source = rec.get("source") or {}
        fmt = data.pop("_number_format", None)
        for k in INTERNAL_KEYS:
            data.pop(k, None)

        # strings: trim, drop empties
        for k, v in list(data.items()):
            if isinstance(v, str):
                v = re.sub(r"\s+", " ", v).strip()
                if v == "":
                    data.pop(k)
                else:
                    data[k] = v
            elif v is None or v == [] or v == {}:
                data.pop(k)

        # attributes: accept dict or list; values must be scalar
        attrs = data.get("attributes") or []
        if isinstance(attrs, dict):
            attrs = [{"key": k, "value": v} for k, v in attrs.items()]
        clean_attrs, seen = [], set()
        for a in attrs if isinstance(attrs, list) else []:
            if not isinstance(a, dict) or not str(a.get("key", "")).strip():
                continue
            val = a.get("value")
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            if val is None or str(val).strip() == "":
                continue
            key = str(a["key"]).strip()
            if (key, str(val)) in seen:
                continue
            seen.add((key, str(val)))
            clean_attrs.append({"key": key, "value": val})

        # unknown keys → attributes (nothing is lost)
        for k in list(data):
            if k not in self.props and k != "attributes":
                v = data.pop(k)
                clean_attrs.append({"key": k, "value": v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)})
                flags.append(f"moved_to_attributes:{k}")

        # money
        currency = data.get("currency")
        if isinstance(currency, str):
            c = CURRENCY_SIGNS.get(currency.strip().casefold(), currency.strip().upper())
            data["currency"] = c
            currency = c
        for f in MONEY_FIELDS:
            if f not in data:
                continue
            v = data[f]
            if isinstance(v, dict) and "amount" in v:
                amount, cur = parse_amount(v["amount"], fmt)
                cur = (v.get("currency") or cur)
            else:
                amount, cur = parse_amount(v, fmt)
            cur = (cur or currency or self.default_currency)
            if amount is None:
                clean_attrs.append({"key": f"{f}_raw", "value": str(v)})
                data.pop(f)
                flags.append(f"unparsable_{f}")
                continue
            if not cur:
                clean_attrs.append({"key": f"{f}_no_currency", "value": str(amount)})
                data.pop(f)
                flags.append(f"{f}_without_currency")
                continue
            cur = CURRENCY_SIGNS.get(str(cur).casefold(), str(cur).upper())
            if amount < 0:
                flags.append(f"negative_{f}")
            data[f] = {"amount": f"{amount:.2f}", "currency": cur}
            data.setdefault("currency", cur)

        # stock
        if "stock_count" in data:
            try:
                data["stock_count"] = int(Decimal(str(data["stock_count"]).replace(",", ".").replace(" ", "")))
            except (InvalidOperation, ValueError):
                clean_attrs.append({"key": "stock_raw", "value": str(data.pop("stock_count"))})
                flags.append("unparsable_stock_count")
        if "in_stock" in data and not isinstance(data["in_stock"], bool):
            s = norm_text(data["in_stock"])
            if s in {"yes", "true", "1", "კი", "да", "in stock", "მარაგშია", "есть", "var"}:
                data["in_stock"] = True
            elif s in {"no", "false", "0", "არა", "нет", "out of stock", "yok"}:
                data["in_stock"] = False
            else:
                clean_attrs.append({"key": "in_stock_raw", "value": str(data.pop("in_stock"))})
                flags.append("unparsable_in_stock")
        if "stock_count" in data and "in_stock" not in data:
            data["in_stock"] = data["stock_count"] > 0

        # lists
        for f in LIST_FIELDS:
            if f in data and isinstance(data[f], str):
                data[f] = [x.strip() for x in re.split(r"\s*[;|>]\s*", data[f]) if x.strip()]
            if f in data:
                data[f] = [str(x).strip() for x in data[f] if str(x).strip()]

        # image object
        if isinstance(data.get("primary_image_url"), str):
            data["primary_image_url"] = {"url": data["primary_image_url"]}

        # barcodes
        for f in ("barcode", "gtin"):
            if f in data:
                code = re.sub(r"\D", "", str(data[f]))
                if re.fullmatch(r"\d\.\d+e\+\d+", str(data[f]).strip(), re.I):
                    flags.append(f"{f}_scientific_notation_digits_lost")
                elif code and not gtin_ok(code):
                    flags.append(f"{f}_check_digit_failed")
                data[f] = code or str(data[f])

        # rating
        if "rating_avg" in data:
            amt, _ = parse_amount(data["rating_avg"])
            if amt is None:
                data.pop("rating_avg")
            else:
                data["rating_avg"] = float(amt)

        if clean_attrs:
            data["attributes"] = clean_attrs
        else:
            data.pop("attributes", None)

        # title
        title = data.get("title")
        if not title:
            return None, flags, "no title"
        if len(title) > 255:
            data["title"] = title[:252].rstrip() + "…"
            flags.append("title_truncated")

        # external id
        ext = data.get("external_id") or data.get("sku") or data.get("barcode") or data.get("gtin")
        if not ext:
            ext = "auto-" + hashlib.sha1(norm_text(f"{data.get('brand', '')}|{title}").encode()).hexdigest()[:12]
            flags.append("external_id_generated")
        data["external_id"] = str(ext).strip()[:255]

        # grounding against the source text
        text = None if source.get("method") == "vision" else self.page_text(source)
        if text:
            hay = norm_text(text)
            hay_digits = re.sub(r"\D", "", hay)
            for f in ("sku", "barcode", "gtin"):
                if f in data and norm_text(data[f]) not in hay and re.sub(r"\D", "", str(data[f])) not in hay_digits:
                    flags.append(f"not_in_source:{f}")
            # Judge the title by its own name words: category/brand/tag words are often the
            # extractor's chosen wording ("Laminate flooring") rather than printed text.
            context = norm_text(" ".join([str(data.get("category", "")), str(data.get("brand", "")),
                                          " ".join(map(str, data.get("category_path", []))), " ".join(map(str, data.get("tags", [])))]))
            words = [w for w in re.findall(r"\w{3,}", norm_text(data["title"])) if w not in context and not w.isdigit()]
            if words:
                found = sum(1 for w in words if w in hay)
                if found / len(words) < 0.5:
                    flags.append("title_mostly_not_in_source")
            for f in MONEY_FIELDS:
                if f in data:
                    amt = Decimal(data[f]["amount"])
                    forms = {f"{amt:.2f}", f"{amt:.2f}".replace(".", ","), str(amt.normalize()), f"{amt:,.2f}"}
                    if amt == amt.to_integral():
                        forms.add(str(int(amt)))
                    # "1 299,00", "1.299,00" and "1,299.00" all lose their thousands separator here
                    hay_num = re.sub(r"(?<=\d)[\s\u00a0'.,](?=\d{3}(?!\d))", "", hay)
                    if not any(fm.casefold() in hay or fm.casefold() in hay_num for fm in forms):
                        flags.append(f"not_in_source:{f}")
        elif source.get("method") == "vision":
            flags.append("read_from_image")
        return data, sorted(set(flags)), None

    def validate(self, data: dict) -> list[str]:
        return [f"{'/'.join(map(str, e.path)) or '(record)'}: {e.message}" for e in self.validator.iter_errors(data)]


def _b64_jpeg(img, long_side: int, quality: int = 75) -> str:
    import base64
    import io

    im = img.convert("RGB").copy()
    im.thumbnail((long_side, long_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _crop_box(work: Path, source: dict):
    """The product photo's box in pixels of pages/pNNN.png, from an image id or an explicit box."""
    if source.get("image_box"):
        return [int(v) for v in source["image_box"]]
    if source.get("image"):
        summ = work / "summary.json"
        if summ.exists():
            info = json.loads(summ.read_text(encoding="utf-8"))
            for pg in info.get("pages", []):
                for c in pg.get("image_candidates", []) or []:
                    if c["id"] == source["image"]:
                        return c["box"]
    return None


def build_preview(out: Path, order, accepted, meta, rejected, expected, flagged, drafts) -> int:
    """Write preview.html (self-contained) and cut product photos into out/images/."""
    from datetime import datetime

    from PIL import Image, ImageDraw

    (out / "images").mkdir(exist_ok=True)
    page_cache: dict = {}
    pages_b64: dict = {}
    products = []
    crops = []
    MAX_PAGES = 80  # keeps the preview small enough to open on any laptop
    for ext in order:
        m = meta[ext]
        src, work = m["source"], m["work"]
        item = {"env": {"entityType": "product", "recordId": ext, "data": accepted[ext]}, "flags": m["flags"],
                "source": {k: v for k, v in src.items() if k in ("file", "page", "sheet", "row", "text", "method")}}
        page = src.get("page")
        if page:
            key = f"{work}:{int(page)}"
            png = work / "pages" / f"p{int(page):03d}.png"
            if png.exists():
                if key not in page_cache:
                    page_cache[key] = Image.open(png)
                if key not in pages_b64 and len(pages_b64) < MAX_PAGES:
                    pages_b64[key] = _b64_jpeg(page_cache[key], 1100, 70)
                item["pageKey"] = key
                box = _crop_box(work, src)
                if box:
                    im = page_cache[key]
                    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(im.width, box[2]), min(im.height, box[3])
                    if x1 - x0 > 10 and y1 - y0 > 10:
                        crop = im.crop((x0, y0, x1, y1)).convert("RGB")
                        name = re.sub(r"[^\w\-.]+", "-", ext).strip("-") or "product"
                        crop.save(out / "images" / f"{name}.jpg", quality=85)
                        item["img"] = _b64_jpeg(crop, 520, 80)
                        crops.append((ext, crop))
        products.append(item)

    if crops:
        cell = 170
        cols = 6
        rows = (len(crops) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell, rows * (cell + 22)), "white")
        d = ImageDraw.Draw(sheet)
        for k, (ext, crop) in enumerate(crops):
            t = crop.copy()
            t.thumbnail((cell - 10, cell - 10))
            x, y = (k % cols) * cell + 5, (k // cols) * (cell + 22) + 5
            sheet.paste(t, (x, y))
            d.text((x, y + cell - 4), ext[:26], fill="black")
        sheet.save(out / "images-check.png")

    files = sorted({str(meta[e]["source"].get("file", "")) for e in order if meta[e]["source"].get("file")}) or [p.name for p in drafts]
    data = {
        "summary": {"files": files, "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "products": len(order),
                    "expected": expected, "flagged": flagged, "rejected": len(rejected)},
        "products": products,
        "pages": pages_b64,
        "rejected": [{"where": r["where"], "reason": r["reason"]} for r in rejected],
    }
    tpl = (HERE.parent / "assets" / "preview_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (out / "preview.html").write_text(tpl.replace("__DATA__", payload), encoding="utf-8")
    return len(crops)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("drafts", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--currency", help="currency to assume for prices written without one (ask the user)")
    ap.add_argument("--expected", type=int, help="how many products the source should contain, if known")
    ap.add_argument("--source-text", help="folder with p001.txt … from inspect_doc.py, for grounding")
    a = ap.parse_args()

    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    fin = Finalizer(load_schema(), a.currency, Path(a.source_text).expanduser() if a.source_text else None)

    drafts = []
    for p in a.drafts:
        for n, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                try:
                    drafts.append((p, n, json.loads(line)))
                except json.JSONDecodeError as e:
                    drafts.append((p, n, {"_bad_json": str(e)}))

    accepted: dict[str, dict] = {}
    order: list[str] = []
    meta: dict[str, dict] = {}
    rejected = []
    dup_merged = 0
    dup_renamed = 0
    flag_counts: Counter = Counter()
    for p, n, rec in drafts:
        if "_bad_json" in rec:
            rejected.append({"where": f"{p}:{n}", "reason": "bad JSON: " + rec["_bad_json"]})
            continue
        data, flags, reason = fin.normalise(rec)
        if reason or data is None:
            rejected.append({"where": f"{p}:{n}", "reason": reason or "empty", "record": rec})
            continue
        errors = fin.validate(data)
        if errors:
            rejected.append({"where": f"{p}:{n}", "reason": "; ".join(errors)[:500], "record": data})
            continue
        ext = data["external_id"]
        if ext in accepted:
            if accepted[ext] == data:
                dup_merged += 1
                continue
            k = 2
            while f"{ext}-{k}" in accepted:
                k += 1
            data["external_id"] = f"{ext}-{k}"
            flags.append(f"duplicate_id_renamed_from:{ext}")
            meta[ext]["flags"].append("has_duplicate_id")
            dup_renamed += 1
            ext = data["external_id"]
        accepted[ext] = data
        order.append(ext)
        meta[ext] = {"flags": flags, "source": rec.get("source") or {}, "work": Path(p).resolve().parent.parent}

    with (out / "products.ndjson").open("w", encoding="utf-8") as f:
        for ext in order:
            f.write(json.dumps({"entityType": "product", "recordId": ext, "data": accepted[ext]}, ensure_ascii=False) + "\n")
    with (out / "rejected.jsonl").open("w", encoding="utf-8") as f:
        for r in rejected:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (out / "review.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["external_id", "title", "price", "currency", "sku", "barcode", "brand", "category", "attributes", "flags", "source"])
        for ext in order:
            d, m = accepted[ext], meta[ext]
            for fl in m["flags"]:
                flag_counts[fl.split(":")[0]] += 1
            src = m["source"]
            where = f"{src.get('file', '')} " + (f"p{src['page']}" if src.get("page") else f"{src.get('sheet', '')} r{src.get('row', '')}")
            w.writerow([ext, d["title"], (d.get("price") or {}).get("amount", ""), d.get("currency", ""), d.get("sku", ""),
                        d.get("barcode", ""), d.get("brand", ""), d.get("category", ""),
                        "; ".join(f"{x['key']}={x['value']}" for x in d.get("attributes", [])),
                        " ".join(m["flags"]), where.strip()])

    flagged = sum(1 for e in order if meta[e]["flags"])
    lines = [
        "# Product extraction report", "",
        f"- Draft records read: **{len(drafts)}**",
        f"- Products in the import file: **{len(order)}**",
        f"- Rejected (not in the file): **{len(rejected)}**",
        f"- Exact duplicates merged: {dup_merged}",
        f"- Different products sharing an id (renamed with -2, -3…): {dup_renamed}",
        f"- Products with at least one flag: {flagged}",
    ]
    if a.expected is not None:
        diff = len(order) - a.expected
        lines.append(f"- Expected from the source: {a.expected} → {'OK' if diff == 0 else f'MISMATCH ({diff:+d})'}")
    if flag_counts:
        lines += ["", "## Flags", ""] + [f"- `{k}`: {v}" for k, v in flag_counts.most_common()]
    if rejected:
        reasons = Counter(r["reason"].split(":")[0][:80] for r in rejected)
        lines += ["", "## Rejected", ""] + [f"- {k}: {v}" for k, v in reasons.most_common()]
    lines += ["", "## Files", "",
              f"- Upload: `{out / 'products.ndjson'}`",
              f"- Review sheet: `{out / 'review.csv'}`",
              "", "Importing the same product again replaces it completely in the catalog, including edits made in the console."]
    crops = build_preview(out, order, accepted, meta, rejected, a.expected, flagged, [Path(p) for p in a.drafts])
    lines += ["", f"Preview (open in a browser): `{out / 'preview.html'}`"]
    if crops:
        lines += [f"Product photos: {crops} saved in `{out / 'images'}` — check `{out / 'images-check.png'}`"]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if order else 1


if __name__ == "__main__":
    sys.exit(main())
