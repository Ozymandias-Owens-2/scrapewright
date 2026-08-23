"""Write a batch of results to a file: .csv, .xlsx, or .jsonl by extension.

Works for the typed product path and for schema-agnostic records alike — the
column set is fixed for products and derived from the data for records.

CSV and JSONL use only the stdlib. XLSX needs ``openpyxl`` (install with
``pip install scrapewright[excel]``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .models import Product, Record

COLUMNS = [
    "title", "brand", "price", "currency", "available",
    "sku", "url", "description", "images", "sizes", "source_platform",
]


def _flatten(value: Any) -> str:
    """One cell's worth of text: lists become ' | '-joined, None becomes ''."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    return str(value)


def _product_row(p: Product) -> dict:
    d = p.model_dump(exclude={"raw"})
    return {c: _flatten(d.get(c)) for c in COLUMNS}


def _record_columns(records: list[Record]) -> list[str]:
    """Union of the fields that actually came back, in first-seen order."""
    columns: list[str] = []
    for r in records:
        for key in r.data:
            if key not in columns:
                columns.append(key)
    return columns + ["url", "source_platform"]


def write_products(products: list[Product], path: str | Path) -> Path:
    rows = [_product_row(p) for p in products]
    payload = [p.model_dump(exclude={"raw"}) for p in products]
    return _dispatch(rows, COLUMNS, payload, path)


def write_records(records: list[Record], path: str | Path) -> Path:
    columns = _record_columns(records)
    rows = []
    for r in records:
        row = {k: _flatten(v) for k, v in r.data.items()}
        row["url"] = r.url
        row["source_platform"] = r.source_platform or ""
        rows.append({c: row.get(c, "") for c in columns})
    payload = [{**r.data, "url": r.url, "source_platform": r.source_platform}
               for r in records]
    return _dispatch(rows, columns, payload, path)


def write_any(items: list, path: str | Path) -> Path:
    """Dispatch on what the caller happens to be holding."""
    if items and isinstance(items[0], Record):
        return write_records(items, path)
    return write_products(items, path)


def _dispatch(rows: list[dict], columns: list[str], payload: list[dict],
              path: str | Path) -> Path:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _write_csv(rows, columns, path)
    elif suffix == ".xlsx":
        _write_xlsx(rows, columns, path)
    elif suffix in (".jsonl", ".ndjson"):
        _write_jsonl(payload, path)
    else:
        raise ValueError(f"Unsupported export format '{suffix}' — use .csv, .xlsx, or .jsonl")
    return path


def _write_csv(rows: list[dict], columns: list[str], path: Path) -> None:
    # utf-8-sig so Excel opens Cyrillic/accented text correctly by default.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(payload: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for item in payload:
            f.write(json.dumps(item, default=str, ensure_ascii=False) + "\n")


def _write_xlsx(rows: list[dict], columns: list[str], path: Path) -> None:
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise RuntimeError(
            "XLSX export needs the 'openpyxl' package. "
            "Install it with:  pip install scrapewright[excel]"
        ) from e

    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(columns)
    for row in rows:
        ws.append([row.get(c, "") for c in columns])
    # Freeze the header and widen the columns people actually read.
    ws.freeze_panes = "A2"
    for idx, name in enumerate(columns, start=1):
        letter = ws.cell(row=1, column=idx).column_letter
        ws.column_dimensions[letter].width = 60 if name in ("url", "description", "images") else 24
    wb.save(path)
