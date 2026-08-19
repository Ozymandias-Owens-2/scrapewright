"""Write a batch of products to a file: .csv, .xlsx, or .jsonl by extension.

CSV and JSONL use only the stdlib. XLSX needs ``openpyxl`` (install with
``pip install scrapewright[excel]``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import Product

COLUMNS = [
    "title", "brand", "price", "currency", "available",
    "sku", "url", "description", "images", "sizes", "source_platform",
]


def _row(p: Product) -> dict:
    d = p.model_dump(exclude={"raw"})
    d["images"] = " | ".join(d.get("images") or [])
    d["sizes"] = " | ".join(d.get("sizes") or [])
    d["price"] = str(d["price"]) if d.get("price") is not None else ""
    return {c: d.get(c, "") for c in COLUMNS}


def write_products(products: list[Product], path: str | Path) -> Path:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _write_csv(products, path)
    elif suffix == ".xlsx":
        _write_xlsx(products, path)
    elif suffix in (".jsonl", ".ndjson"):
        _write_jsonl(products, path)
    else:
        raise ValueError(f"Unsupported export format '{suffix}' — use .csv, .xlsx, or .jsonl")
    return path


def _write_csv(products: list[Product], path: Path) -> None:
    # utf-8-sig so Excel opens Cyrillic/accented text correctly by default.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for p in products:
            writer.writerow(_row(p))


def _write_jsonl(products: list[Product], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for p in products:
            f.write(json.dumps(p.model_dump(exclude={"raw"}), default=str,
                               ensure_ascii=False) + "\n")


def _write_xlsx(products: list[Product], path: Path) -> None:
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise RuntimeError(
            "XLSX export needs the 'openpyxl' package. "
            "Install it with:  pip install scrapewright[excel]"
        ) from e

    wb = Workbook()
    ws = wb.active
    ws.title = "products"
    ws.append(COLUMNS)
    for p in products:
        row = _row(p)
        ws.append([row[c] for c in COLUMNS])
    # Freeze the header and give obvious columns a sane width.
    ws.freeze_panes = "A2"
    widths = {"A": 40, "B": 18, "C": 10, "G": 50, "H": 60}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    wb.save(path)
