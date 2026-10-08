# /// script
# requires-python = ">=3.10"
# dependencies = ["python-calamine>=0.3"]
# ///
"""Apply a column mapping to EVERY row of a spreadsheet — no AI per row.

  uv run sheet_apply.py <mapping.json> --out <work>/draft/<name>.jsonl

You (Claude) write mapping.json after reading the sheet sample from inspect_doc.py:
{
  "file": "/path/price-list.xlsx",
  "sheet": "Sheet1",                    # omit for CSV
  "header_row": 3,                      # 1-based row holding the column names
  "data_start_row": 4,                  # first product row (1-based)
  "data_end_row": null,                 # last row to read, null = to the end
  "columns": {                          # catalog field -> column letter (or exact header text)
    "sku": "A", "title": "B", "price": "E", "barcode": "C", "brand": "D", "stock_count": "G"
  },
  "title_template": null,               # e.g. "{brand} {B} {F}" to build a title from several columns
  "attributes": {"Color": "F", "Power, W": "H"},   # extra characteristics -> attributes
  "constants": {"currency": "GEL", "brand": "Bosch", "category": "Kettles"},
  "category_rows": true,                # rows with one filled cell and no price are section headings
  "price_columns": ["price", "compare_at_amount"],
  "number_format": {"decimal": ",", "thousands": " "},   # how prices are written in THIS file
  "legacy_georgian_columns": ["B"],     # columns typed in AcadNusx-style fonts
  "stop_when_blank_rows": 5             # stop after this many blank rows in a row
}

Every output line is a draft record for finalize.py:
  {"data": {...}, "source": {"file", "sheet", "row", "text"}, "flags": [...]}
A count line is printed so you can reconcile rows: data rows = emitted + headings + skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import geo_legacy  # noqa: E402
from inspect_doc import read_sheet_rows  # noqa: E402

LIST_FIELDS = {"category_path", "tags", "badges", "image_urls", "video_urls"}


def letter_index(letter: str) -> int:
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def resolve_columns(spec: dict, header: list[str]) -> dict[str, int]:
    out = {}
    norm = [h.strip().lower() for h in header]
    for field, ref in spec.items():
        if isinstance(ref, int):
            out[field] = ref
        elif re.fullmatch(r"[A-Za-z]{1,3}", ref or ""):
            out[field] = letter_index(ref)
        elif ref and ref.strip().lower() in norm:
            out[field] = norm.index(ref.strip().lower())
        else:
            raise SystemExit(f"column for '{field}' not found: {ref!r} (header row: {header})")
    return out


def cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if 0 <= idx < len(row) and row[idx] is not None else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mapping")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    m = json.loads(Path(a.mapping).read_text(encoding="utf-8"))
    path = Path(m["file"]).expanduser()
    sheets = read_sheet_rows(path)
    sheet = m.get("sheet") or next(iter(sheets))
    rows = sheets[sheet]
    header = rows[m["header_row"] - 1] if m.get("header_row") else []
    cols = resolve_columns(m.get("columns", {}), header)
    attrs = resolve_columns(m.get("attributes", {}), header)
    legacy_cols = {letter_index(c) for c in m.get("legacy_georgian_columns", [])}
    consts = m.get("constants", {})
    price_fields = set(m.get("price_columns", ["price", "compare_at_amount"]))
    template = m.get("title_template")
    start = m.get("data_start_row", (m.get("header_row") or 0) + 1)
    end = m.get("data_end_row") or len(rows)
    stop_blank = m.get("stop_when_blank_rows", 0)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts = {"rows_read": 0, "blank": 0, "headings": 0, "emitted": 0, "no_title": 0}
    category = consts.get("category")
    blank_run = 0
    with out.open("w", encoding="utf-8") as f:
        for r in range(start - 1, min(end, len(rows))):
            row = list(rows[r])
            for j in legacy_cols:
                if j < len(row) and row[j]:
                    row[j] = geo_legacy.convert(row[j])
            filled = [c for c in row if c and c.strip()]
            if not filled:
                counts["blank"] += 1
                blank_run += 1
                if stop_blank and blank_run >= stop_blank:
                    break
                continue
            blank_run = 0
            counts["rows_read"] += 1
            has_price = any(cell(row, cols[p]) for p in price_fields if p in cols)
            if m.get("category_rows") and len(filled) == 1 and not has_price:
                category = filled[0].strip()
                counts["headings"] += 1
                continue
            data: dict = {k: v for k, v in consts.items()}
            if category:
                data["category"] = category
            for field, idx in cols.items():
                v = cell(row, idx)
                if not v:
                    continue
                data[field] = [x.strip() for x in re.split(r"[;|>/]", v) if x.strip()] if field in LIST_FIELDS else v
            if template:
                letters = {h: cell(row, letter_index(h)) for h in re.findall(r"\{([A-Z]{1,3})\}", template)}
                title = template.format(**{**{k: str(data.get(k, "")) for k in re.findall(r"\{([a-z_]+)\}", template)}, **letters})
                data["title"] = re.sub(r"\s+", " ", title).strip()
            attributes = [{"key": k, "value": cell(row, i)} for k, i in attrs.items() if cell(row, i)]
            if attributes:
                data["attributes"] = attributes
            flags = []
            if m.get("number_format"):
                data["_number_format"] = m["number_format"]
            if not data.get("title"):
                counts["no_title"] += 1
                flags.append("no_title_in_row")
            src_text = " | ".join(c.strip() for c in row if c and c.strip())
            rec = {"data": data, "source": {"file": path.name, "sheet": sheet, "row": r + 1, "text": src_text},
                   "flags": flags}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            counts["emitted"] += 1
    print(json.dumps({"sheet": sheet, **counts, "out": str(out)}, ensure_ascii=False))
    print(f"check: rows_read {counts['rows_read']} = emitted {counts['emitted']} + headings {counts['headings']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
