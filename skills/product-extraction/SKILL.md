---
name: product-extraction
description: Turn product documents — supplier catalogs, price lists, spec sheets, scans, phone photos of shelf labels, Excel/CSV, Word or PowerPoint, in Georgian, Russian, English or Turkish — into one checked file ready to import into a Nairos catalog (products.ndjson). Use this whenever someone wants products, prices, SKUs, barcodes or specs pulled out of a document, or wants a catalog or price list loaded, imported or added to Nairos — even if they never say "extract" or "Nairos".
---

# Product extraction → Nairos import file

You read the document; the scripts in `scripts/` (next to this file, run with `uv run`) do the
mechanical parts and check the result. If `uv` is missing, ask the user to install it
(`curl -LsSf https://astral.sh/uv/install.sh | sh`) and stop.

**Only write what the document says.** No price in the document → no price in the file. Don't
compute, estimate or describe things that aren't printed (colours judged from a photo, pieces per
pack worked out by hand). Clients sell from these records; a missing value is fine, a wrong one isn't.

## Steps

Work in `./nairos-extract/<file-name>/` in the user's folder.

1. **Inspect** — `uv run scripts/inspect_doc.py "<file>" --work ./nairos-extract/<name>`.
   Look at `overview.png` first (all pages as thumbnails) to see the structure. For each page it
   writes `pages/pNNN.png` and `text/pNNN.txt` (old Georgian fonts already converted); for
   spreadsheets, `sheets/<sheet>.txt` with row numbers and column letters. A text layer can look
   fine and still miss product names — if text and image disagree, trust the image.

2. **Extract**
   - **Spreadsheets**: don't copy rows yourself. Write a `mapping.json` (format in the header of
     `scripts/sheet_apply.py`) and run `uv run scripts/sheet_apply.py mapping.json --out draft/sheet.jsonl`.
   - **Everything else**: read text and page images in small batches (a page, or up to ~60 table
     rows) and append each batch to `draft/<name>.jsonl`, one product per line:
     `{"data": {...}, "source": {"file": "...", "page": 13, "method": "text" | "vision"}, "flags": []}`.
     Use `"vision"` when you read the value from the image. Fields are in `references/fields.md`.
   - **Product photo** (PDFs and photos): if the page shows a picture of the product, add it to
     `source` so the preview and `images/<code>.jpg` get it. `pages/pNNN-boxes.png` shows the
     pictures found on the page, numbered — use `"image": "p008-2"` when a box frames the product
     well, otherwise give your own box in pixels of `pages/pNNN.png`: `"image_box": [x0, y0, x1, y1]`.
     Boxes from designer PDFs are often too big (they ignore clipping), so check.

   Reading traps that matter:
   - a cell merged across rows applies to each of those rows;
   - specs printed once for a collection (spec table, feature icons, warranty) go on every product in it;
   - one product spread over several pages (chart, detail page, swatch) becomes one record, keyed by its code;
   - price-list heading rows are categories, not products;
   - if a table has no codes and its rows don't visibly line up with the products, don't assign its
     values by position — leave them out and say so.

3. **Check** — count the products in the document's own list first, then:
   `uv run scripts/finalize.py ./nairos-extract/<name>/draft/*.jsonl --out ./nairos-extract/output --expected <count> --source-text ./nairos-extract/<name>/text [--currency GEL]`.
   It writes `products.ndjson` (the upload), `review.csv`, `rejected.jsonl`, `report.md`,
   `preview.html` and, when photos were marked, `images/` plus `images-check.png` — look at that
   sheet once and fix any box that shows the wrong thing. Fix and
   rerun until the count matches and every flag is fixed or explained: re-read the page image for
   `not_in_source` flags, re-read barcodes that fail the check digit, ask for the currency if prices
   have none.

4. **Hand over** — tell the user how many products are in the file (and how many the document
   lists), what you assumed (language, currency, category), what is flagged, and where the file is.
   Point them to `preview.html` (open it in a browser: every product as a card, with its source
   page and its line in the import file) and offer to open it (`open` on macOS, `xdg-open` on Linux).
   Upload: Nairos console → space → Products → Catalog → open the product table → Import →
   `products.ndjson`. Importing a product again replaces it completely, including edits made in the
   console. Photos are saved in `images/` by product code but aren't in the import file (the catalog
   needs image URLs).

## Many files

For three or more documents, don't read them all in this conversation — long jobs lose products.
1. List the files and settle the shared choices once (title language, currency, category wording);
   ask the user if they're around.
2. Give each file to the `document-extractor` agent (Agent tool), 3–4 at a time in parallel. In each
   prompt pass: the document path, its work folder `./nairos-extract/<name>/`, this skill's folder
   (the base directory shown when this skill loaded) and the shared choices.
3. A file whose work folder already has `DONE.json` is finished — skip it, so a stopped job resumes.
4. When all are done, run `finalize.py` once over every `./nairos-extract/*/draft/*.jsonl` (no
   `--expected`; it reads each file's `DONE.json`). The same product code in different files is
   merged into one product (catalog specs + price list price); disagreements are flagged as
   `conflict:<field>` — check them against both sources.
5. Hand over with the per-file table from `report.md`.

One or two files: do them yourself as above.

When the document is bilingual and the user hasn't said which language to use for titles and
attribute names, ask; if they're not around, use the document's main language and say so.
