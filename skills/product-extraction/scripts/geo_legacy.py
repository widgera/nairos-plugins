# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Legacy Georgian fonts (AcadNusx, LitNusx, AcadMtavr …) → Unicode Georgian.

Before Unicode Georgian was common, Georgian text was typed with Latin letters and
shown in a font that draws Georgian glyphs. Extracting such text gives Latin
gibberish like "qarTuli" instead of "ქართული". The mapping is the standard
Georgian phonetic keyboard, a fixed one-to-one table, so conversion is exact.
Verify it once against a real supplier file before trusting it at scale.

CLI:
  uv run geo_legacy.py detect "some text"
  uv run geo_legacy.py convert "qarTuli ena"
"""
from __future__ import annotations

import re
import sys

LATIN_TO_GEORGIAN = {
    "a": "ა", "b": "ბ", "g": "გ", "d": "დ", "e": "ე", "v": "ვ", "z": "ზ",
    "T": "თ", "i": "ი", "k": "კ", "l": "ლ", "m": "მ", "n": "ნ", "o": "ო",
    "p": "პ", "J": "ჟ", "r": "რ", "s": "ს", "t": "ტ", "u": "უ", "f": "ფ",
    "q": "ქ", "R": "ღ", "y": "ყ", "S": "შ", "C": "ჩ", "c": "ც", "Z": "ძ",
    "w": "წ", "W": "ჭ", "x": "ხ", "j": "ჯ", "h": "ჰ",
}

# Font names that store Georgian as Latin letters. "Georgia" (a normal Latin
# serif font) must NOT match, hence the explicit list instead of a "geo" substring.
LEGACY_FONT_RE = re.compile(r"(acad\s*nusx|acadnusx|lit\s*nusx|litnusx|acad\s*mtavr|acadmtavr|"
                            r"bpg_?(?:nino|glaho)?_?(?:nusx|mtavr)|grigolia|nusxuri)", re.I)

# Words with a capital T/J/R/S/C/Z/W after a lowercase letter ("qarTuli",
# "saqarTvelo", "Sesabamisi" counts via S only when mid-word) are rare in English,
# Russian transliteration and brand names, and common in legacy Georgian.
_MIDCAP = re.compile(r"[a-z][TJRSCZW]")
_LATIN_WORD = re.compile(r"[A-Za-z]{4,}")


def is_legacy_font(font_name: str | None) -> bool:
    return bool(font_name) and bool(LEGACY_FONT_RE.search(font_name))


# Letter patterns frequent in legacy Georgian and rare in English/Turkish/translit:
# "q" not followed by "u" (ქ), plural "-ebi", and Georgian consonant clusters.
_MARKERS = re.compile(r"q(?!u)|ebi\b|ebis\b|[TSZCWw]v|[gk]v[aei]|wy|xv|dR|aR")


def legacy_score(text: str) -> float:
    """Share of Latin words (≥4 letters) that look like legacy-encoded Georgian."""
    words = _LATIN_WORD.findall(text or "")
    if len(words) < 3:
        return 0.0
    hits = sum(1 for w in words if _MIDCAP.search(w) or _MARKERS.search(w))
    return hits / len(words)


def looks_legacy(text: str, threshold: float = 0.15) -> bool:
    return legacy_score(text) >= threshold


def convert(text: str) -> str:
    """Convert legacy-encoded text. Digits, punctuation and unmapped capitals pass through."""
    return "".join(LATIN_TO_GEORGIAN.get(ch, ch) for ch in text)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"detect", "convert"}:
        print(__doc__)
        return 2
    text = " ".join(argv[2:])
    if argv[1] == "detect":
        s = legacy_score(text)
        print(f"score={s:.2f} legacy={'yes' if s >= 0.15 else 'no'}")
    else:
        print(convert(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
