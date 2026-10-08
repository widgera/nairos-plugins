# Fields the Nairos catalog accepts

`scripts/finalize.py` checks every record against `assets/canonical-product.schema.json`
(generated from the products app's own validator). Only `external_id` and `title` are required —
leave out what the document doesn't give.

| Field | What goes in |
|---|---|
| `external_id` | The product's code as printed (SKU / article / "Product code" / კოდი / Код). If there is none, leave it out — the checker uses the barcode or a stable hash. |
| `title` | Brand + series/collection + name + the one spec that tells it apart, same pattern for the whole file. Keep the document's script; never transliterate Georgian. |
| `sku`, `barcode`, `gtin` | As printed. Barcodes digit by digit — the checker verifies the check digit. |
| `description` | Only if the document has one. Don't write marketing text. |
| `brand`, `category` | Plain words, same wording across the file. |
| `category_path`, `tags`, `badges` | Arrays of strings: `["Flooring", "Laminate"]`, collection names as tags, printed badges only. |
| `price`, `compare_at_amount` | As printed (`129`, `"1 299,00 ₾"`); the checker turns it into `{"amount", "currency"}`. Current price → `price`, crossed-out/old → `compare_at_amount`, wholesale/dealer prices → attributes. |
| `currency` | `GEL` (₾, ლარი), `USD`, `EUR`, `TRY` (TL, ₺), `RUB` (₽, руб). |
| `stock_count`, `in_stock` | Integer stock; true/false availability. |
| `store_availability` | Per-branch stock: list of `{"storeId","branchName","city","address","inStock","phone","hours"}`. |
| `primary_image_url`, `image_urls`, `source_url` | Real URLs only; images embedded in a PDF have none. |
| `valid_from`, `valid_through` | ISO dates when a price list states its validity. |

**Everything else descriptive goes into `attributes`**: a list of `{"key": ..., "value": ...}` with
a string, number or true/false value. Put the unit in the key and a bare number in the value
(`{"key": "Thickness, mm", "value": 10}`) so the catalog can filter on it. Use one key name per
fact across the whole file, in the language chosen for titles. A list of features is one key with
values joined by `; `. Matching accessories (e.g. skirting) are an attribute, not separate products,
unless the user asks.

## Georgian labels

| Label | Field |
|---|---|
| კოდი, არტიკული | external_id / sku |
| დასახელება | title |
| ფასი / ძველი ფასი / ფასდაკლება | price / compare_at_amount / discount |
| ნაშთი, მარაგი, რაოდენობა | stock_count |
| შტრიხკოდი | barcode |
| ბრენდი, მწარმოებელი | brand |
| კატეგორია | category |
| ზომა, წონა, ფერი, მასალა, გარანტია, ქვეყანა | attributes |
| დღგ-ს ჩათვლით | VAT included |

Old Georgian fonts (AcadNusx, LitNusx) give Latin gibberish like `saqarTvelo`; `inspect_doc.py`
converts PDF text automatically. In spreadsheets list such columns in `legacy_georgian_columns`, or
convert one string with `uv run scripts/geo_legacy.py convert "qarTuli"`.
