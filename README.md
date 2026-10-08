# Nairos plugins for Claude Code

## nairos-product-extractor

Turns your product documents — supplier catalogs, price lists, spec sheets, scans, photos of shelf
labels, Excel/CSV, Word or PowerPoint, in Georgian, Russian, English or Turkish — into one checked
file you can import into your Nairos catalog.

Your own Claude reads the documents. Bundled scripts render pages, read spreadsheets row by row,
convert old Georgian fonts (AcadNusx) and check every product before it goes into the file. No other
AI service is called and nothing is sent to Nairos; files stay on your computer apart from what
Claude reads in your conversation.

### Requirements

- [Claude Code](https://claude.com/claude-code)
- [uv](https://docs.astral.sh/uv/) — runs the scripts and installs their libraries on first use:
  `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS/Linux) or
  `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (Windows)

### Install

In Claude Code:

```
/plugin marketplace add widgera/nairos-plugins
/plugin install nairos-product-extractor@nairos
```

Or from a terminal:

```
claude plugin marketplace add widgera/nairos-plugins
claude plugin install nairos-product-extractor@nairos
```

Update later with `claude plugin update nairos-product-extractor@nairos`.

### Use

Open Claude Code in the folder with your document and ask in your own words, for example:

> Our supplier sent this catalog: catalog.pdf. Make a product file I can import into Nairos.

Claude works in `./nairos-extract/` and finishes with `nairos-extract/output/`:

| File | What it is |
|---|---|
| `products.ndjson` | the file to import: Nairos console → your space → Products → Catalog → open the product table → Import |
| `preview.html` | open in a browser: every product as a card, with its source page and its line in the import file |
| `review.csv` | one row per product with anything flagged for checking — opens in Excel |
| `report.md` | counts (found vs. listed in the document), flags, rejected rows |
| `images/<code>.jpg` | product photos cut from the document, named by product code |

Many files at once: put them in one folder and ask for one import file. Claude hands each document
to its own helper, a few at a time, and merges the results — the same product code in a catalog and a
price list becomes one product (specs from one, price from the other), and disagreements between files
are flagged. If a long job stops, ask again in the same folder: finished files are skipped.
`report.md` and `preview.html` show the counts per file.

Good to know:
- Only what the document says goes into the file — no price in the document, no price in the file.
- Importing a product again replaces it completely, including edits made in the console.
- Photos are not inside the import file (the catalog needs image web addresses); upload them separately.
