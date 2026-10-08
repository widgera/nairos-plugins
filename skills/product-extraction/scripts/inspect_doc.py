# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pdfplumber>=0.11",
#   "pypdfium2>=4.30",
#   "pillow>=10",
#   "python-calamine>=0.3",
#   "python-docx>=1.1",
#   "python-pptx>=1.0",
# ]
# ///
"""Look at a document before extracting anything.

  uv run inspect_doc.py <file> --work <work_dir>

Writes into <work_dir>:
  summary.json          what the file is, per page/sheet facts, a suggested reading method
  overview.png          (PDF/images) thumbnails of every page on one sheet — look at it first
  pages/p001.png …      (PDF) each page rendered for reading with vision
  text/p001.txt …       (PDF/DOCX/PPTX) the text layer, legacy Georgian already converted
  sheets/<sheet>.txt    (XLSX/XLS/ODS/CSV) the first rows with row numbers and column letters

Prints a short human summary. Nothing here calls an AI service.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import geo_legacy  # noqa: E402

PDF_EXT = {".pdf"}
SHEET_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb", ".ods", ".csv", ".tsv"}
DOCX_EXT = {".docx"}
PPTX_EXT = {".pptx"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".heic"}

RENDER_LONG_SIDE = 1600   # px; enough to read 7-pt table text
THUMB_LONG_SIDE = 360


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def text_quality(text: str) -> dict:
    """Cheap signals that a page's text layer is missing or broken."""
    t = text or ""
    letters = sum(ch.isalpha() for ch in t)
    replacement = t.count("�") + len(re.findall(r"\(cid:\d+\)", t))
    words = re.findall(r"\w+", t)
    short = sum(1 for w in words if len(w) == 1 and w.isalpha())
    doubled = len(re.findall(r"(\w)\1(\w)\2(\w)\3", t))  # "ÜÜrrüünn" bold-duplication artefact
    return {
        "chars": len(t),
        "letters": letters,
        "replacement_glyphs": replacement,
        "single_letter_word_ratio": round(short / len(words), 2) if words else 0.0,
        "doubled_glyph_runs": doubled,
    }


def suggest_method(q: dict, n_images: int, n_tables: int) -> tuple[str, str]:
    if q["chars"] < 40 and n_images == 0 and n_tables == 0:
        return "skip?", "almost empty page — glance at the overview to confirm"
    if q["chars"] < 80:
        return "vision", "little or no text layer (scan, outlined text or image-only page)"
    if q["replacement_glyphs"] > 3 or q["single_letter_word_ratio"] > 0.25:
        return "vision", "text layer looks broken (missing glyphs / split words)"
    if n_tables and q["chars"] < 200:
        return "vision", "a table is drawn but its text layer is thin"
    return "text", "text layer looks usable — still check numbers against the page image"


def inspect_pdf(path: Path, work: Path) -> dict:
    import pdfplumber
    import pypdfium2 as pdfium

    (work / "pages").mkdir(parents=True, exist_ok=True)
    (work / "text").mkdir(parents=True, exist_ok=True)
    pages = []
    page_sizes = []
    legacy_any = False
    with pdfplumber.open(str(path)) as pdf:
        meta = {k: pdf.metadata.get(k) for k in ("Producer", "Creator", "Title") if pdf.metadata.get(k)}
        for i, page in enumerate(pdf.pages, start=1):
            fonts = sorted({c.get("fontname", "") for c in page.chars})
            text = page.extract_text() or ""
            legacy_font = any(geo_legacy.is_legacy_font(f) for f in fonts)
            converted = False
            if legacy_font:
                # Convert only the characters drawn in a legacy font, so English and
                # Russian text on the same page stay untouched.
                from pdfplumber.utils import extract_text as _extract

                chars = [dict(c, text=geo_legacy.convert(c["text"])) if geo_legacy.is_legacy_font(c.get("fontname"))
                         else c for c in page.chars]
                text = _extract(chars)
                converted = legacy_any = True
            elif geo_legacy.looks_legacy(text):
                # No telltale font name but the text reads like legacy Georgian: keep the
                # original next to the converted copy and let the reader decide.
                (work / "text" / f"p{i:03d}.orig.txt").write_text(text, encoding="utf-8")
                text = geo_legacy.convert(text)
                converted = legacy_any = True
            try:
                n_tables = len(page.find_tables())
            except Exception:
                n_tables = 0
            q = text_quality(text)
            method, why = suggest_method(q, len(page.images), n_tables)
            img_boxes = [(float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"])) for im in page.images]
            page_sizes.append((float(page.width), float(page.height), img_boxes))
            (work / "text" / f"p{i:03d}.txt").write_text(text, encoding="utf-8")
            pages.append({
                "page": i,
                "chars": q["chars"],
                "images": len(page.images),
                "tables": n_tables,
                "fonts": [f.split("+")[-1] for f in fonts][:8],
                "legacy_georgian_converted": converted,
                "quality": q,
                "suggested": method,
                "why": why,
                "first_line": (text.strip().splitlines() or [""])[0][:100],
            })

    doc = pdfium.PdfDocument(str(path))
    thumbs = []
    for i in range(len(doc)):
        pg = doc[i]
        w, h = pg.get_size()
        img = pg.render(scale=RENDER_LONG_SIDE / max(w, h)).to_pil()
        img.save(work / "pages" / f"p{i + 1:03d}.png")
        pages[i]["image_candidates"] = image_candidates(img, page_sizes[i], i + 1, work)
        t = img.copy()
        t.thumbnail((THUMB_LONG_SIDE, THUMB_LONG_SIDE))
        thumbs.append(t)
    make_overview(thumbs, work / "overview.png")
    return {"kind": "pdf", "pages": pages, "page_count": len(pages), "metadata": meta,
            "legacy_georgian": legacy_any}


def image_candidates(img, size_info, page_no: int, work: Path) -> list[dict]:
    """Pictures embedded in the page, as boxes in pixels of pages/pNNN.png.

    The boxes are the pictures' full extent; designer PDFs often show only part of a
    picture (a clipping mask), so a box can be larger than what you see. The page is
    saved again as pNNN-boxes.png with the boxes drawn and numbered, so you can pick
    the right one for each product (source.image = "p008-2") or give your own box.
    """
    from PIL import ImageDraw

    pw, ph, boxes = size_info
    sx, sy = img.width / pw, img.height / ph
    out, seen = [], set()
    for x0, y0, x1, y1 in boxes:
        b = (max(0, round(x0 * sx)), max(0, round(y0 * sy)), min(img.width, round(x1 * sx)), min(img.height, round(y1 * sy)))
        w, h = b[2] - b[0], b[3] - b[1]
        if w < 40 or h < 40 or w * h > 0.85 * img.width * img.height or b in seen:
            continue  # icons, specks and full-page backgrounds are never product photos
        seen.add(b)
        out.append({"id": f"p{page_no:03d}-{len(out) + 1}", "box": list(b)})
    if out:
        ann = img.convert("RGB").copy()
        d = ImageDraw.Draw(ann)
        for c in out:
            d.rectangle(c["box"], outline=(220, 0, 40), width=4)
            d.rectangle([c["box"][0], c["box"][1], c["box"][0] + 92, c["box"][1] + 24], fill=(220, 0, 40))
            d.text((c["box"][0] + 6, c["box"][1] + 6), c["id"], fill="white")
        ann.save(work / "pages" / f"p{page_no:03d}-boxes.png")
    return out


def make_overview(thumbs, out: Path) -> None:
    """All pages on one or more contact sheets, numbered, so the structure is visible at a glance."""
    from PIL import Image, ImageDraw

    if not thumbs:
        return
    per_sheet = 20
    for s in range(0, len(thumbs), per_sheet):
        chunk = thumbs[s:s + per_sheet]
        cols = 5
        rows = math.ceil(len(chunk) / cols)
        cw = THUMB_LONG_SIDE + 16
        ch = THUMB_LONG_SIDE + 34
        sheet = Image.new("RGB", (cols * cw, rows * ch), "white")
        d = ImageDraw.Draw(sheet)
        for k, t in enumerate(chunk):
            x, y = (k % cols) * cw + 8, (k // cols) * ch + 26
            sheet.paste(t, (x, y))
            d.rectangle([x - 1, y - 1, x + t.width, y + t.height], outline="#999")
            d.text((x, y - 20), f"p{s + k + 1}", fill="#c00")
        name = out if s == 0 else out.with_name(f"{out.stem}-{s // per_sheet + 1}{out.suffix}")
        sheet.save(name)


def read_sheet_rows(path: Path) -> dict[str, list[list[str]]]:
    if path.suffix.lower() in {".csv", ".tsv"}:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1251", "cp1252", "latin-1"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.Sniffer().sniff(txt[:20000], delimiters=",;\t|")
        return {path.stem: [list(r) for r in csv.reader(io.StringIO(txt), dialect)]}
    from python_calamine import CalamineWorkbook

    wb = CalamineWorkbook.from_path(str(path))
    out = {}
    for name in wb.sheet_names:
        rows = wb.get_sheet_by_name(name).to_python(skip_empty_area=False)
        out[name] = [["" if v is None else str(v) for v in r] for r in rows]
    return out


def inspect_sheets(path: Path, work: Path, sample_rows: int = 40) -> dict:
    (work / "sheets").mkdir(parents=True, exist_ok=True)
    sheets = []
    for name, rows in read_sheet_rows(path).items():
        nonempty = [i for i, r in enumerate(rows) if any(c.strip() for c in r)]
        width = max((len(r) for r in rows), default=0)
        sci = sum(1 for r in rows for c in r if re.fullmatch(r"\d\.\d+E\+\d+", c.strip(), re.I))
        sample_txt = " ".join(" ".join(r) for r in rows[:200])
        legacy = geo_legacy.looks_legacy(sample_txt)
        lines = []
        for i, r in enumerate(rows[:sample_rows]):
            cells = [f"{col_letter(j)}={c.strip()[:40]}" for j, c in enumerate(r) if c.strip()]
            lines.append(f"row {i + 1}: " + " | ".join(cells))
        # also show a slice from the middle and the end so layout changes are visible
        for label, start in (("middle", len(rows) // 2), ("end", max(0, len(rows) - 10))):
            if start > sample_rows:
                lines.append(f"--- {label} ---")
                for i in range(start, min(len(rows), start + 10)):
                    cells = [f"{col_letter(j)}={c.strip()[:40]}" for j, c in enumerate(rows[i]) if c.strip()]
                    lines.append(f"row {i + 1}: " + " | ".join(cells))
        safe = re.sub(r"[^\w\-]+", "_", name)[:60] or "sheet"
        (work / "sheets" / f"{safe}.txt").write_text("\n".join(lines), encoding="utf-8")
        sheets.append({
            "sheet": name, "file": f"sheets/{safe}.txt", "rows": len(rows), "non_empty_rows": len(nonempty),
            "columns": width, "scientific_notation_cells": sci, "legacy_georgian_suspected": legacy,
        })
    return {"kind": "spreadsheet", "sheets": sheets}


def inspect_docx(path: Path, work: Path) -> dict:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for ti, t in enumerate(d.tables, start=1):
        parts.append(f"\n[table {ti}]")
        for r in t.rows:
            parts.append(" | ".join(c.text.strip() for c in r.cells))
    fonts = {run.font.name for p in d.paragraphs for run in p.runs if run.font.name}
    return write_flat_text(work, "\n".join(parts), fonts, "docx", tables=len(d.tables))


def inspect_pptx(path: Path, work: Path) -> dict:
    from pptx import Presentation

    prs = Presentation(str(path))
    parts, fonts, tables = [], set(), 0
    for si, slide in enumerate(prs.slides, start=1):
        parts.append(f"\n[slide {si}]")
        for shp in slide.shapes:
            if shp.has_text_frame:
                for p in shp.text_frame.paragraphs:
                    for r in p.runs:
                        if r.font.name:
                            fonts.add(r.font.name)
                parts.append(shp.text_frame.text)
            if getattr(shp, "has_table", False) and shp.has_table:
                tables += 1
                for r in shp.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in r.cells))
    return write_flat_text(work, "\n".join(parts), fonts, "pptx", tables=tables)


def write_flat_text(work: Path, text: str, fonts: set, kind: str, tables: int) -> dict:
    (work / "text").mkdir(parents=True, exist_ok=True)
    legacy = any(geo_legacy.is_legacy_font(f) for f in fonts) or geo_legacy.looks_legacy(text)
    if legacy:
        text = geo_legacy.convert(text)
    (work / "text" / "all.txt").write_text(text, encoding="utf-8")
    return {"kind": kind, "text_file": "text/all.txt", "chars": len(text), "tables": tables,
            "fonts": sorted(fonts)[:10], "legacy_georgian_converted": legacy}


def inspect_image(path: Path, work: Path) -> dict:
    from PIL import Image, ImageOps

    (work / "pages").mkdir(parents=True, exist_ok=True)
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    w, h = img.size
    scale = min(1.0, 2000 / max(w, h))
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)))
    img.save(work / "pages" / "p001.png")
    return {"kind": "image", "pages": [{"page": 1, "suggested": "vision", "why": "photo or scan",
                                        "image_candidates": [{"id": "p001-1", "box": [0, 0, img.width, img.height]}]}],
            "original_size": [w, h]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--work", required=True)
    a = ap.parse_args()
    path = Path(a.file).expanduser()
    work = Path(a.work).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        info = inspect_pdf(path, work)
    elif ext in SHEET_EXT:
        info = inspect_sheets(path, work)
    elif ext in DOCX_EXT:
        info = inspect_docx(path, work)
    elif ext in PPTX_EXT:
        info = inspect_pptx(path, work)
    elif ext in IMAGE_EXT:
        info = inspect_image(path, work)
    else:
        print(f"unsupported file type: {ext} (convert .doc/.xls-legacy/.ppt to the modern format first)")
        return 2
    info["file"] = str(path)
    (work / "summary.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"{path.name}: {info['kind']}")
    if info["kind"] == "pdf":
        by = {}
        for p in info["pages"]:
            by.setdefault(p["suggested"], []).append(p["page"])
        for m, ps in by.items():
            print(f"  {m}: pages {ps}")
        with_pics = [p["page"] for p in info["pages"] if p.get("image_candidates")]
        if with_pics:
            print(f"  pictures found on pages {with_pics} — see pages/pNNN-boxes.png")
        if info["legacy_georgian"]:
            print("  legacy Georgian font detected and converted in text/ — spot-check one page")
        print(f"  look first at: {work / 'overview.png'}")
    elif info["kind"] == "spreadsheet":
        for s in info["sheets"]:
            flags = []
            if s["scientific_notation_cells"]:
                flags.append(f"{s['scientific_notation_cells']} cells in scientific notation (barcodes may be lost)")
            if s["legacy_georgian_suspected"]:
                flags.append("legacy Georgian suspected")
            print(f"  sheet '{s['sheet']}': {s['non_empty_rows']} non-empty rows × {s['columns']} cols"
                  f" → {work / s['file']}" + (f"  [{'; '.join(flags)}]" if flags else ""))
    else:
        print(f"  details in {work / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
