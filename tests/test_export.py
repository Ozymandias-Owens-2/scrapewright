import csv
import json

import pytest

from scrapewright.export import write_products
from scrapewright.models import Product

PRODUCTS = [
    Product(url="https://x/a", title="Alpha Coat", brand="Maison", price="1290",
            currency="EUR", images=["https://x/1.jpg", "https://x/2.jpg"],
            sizes=["S", "M"], source_platform="json-ld"),
    Product(url="https://x/b", title="Beta Boots — «чёрные»", price="380,50",
            source_platform="selector"),
]


def test_csv_export(tmp_path):
    path = write_products(PRODUCTS, tmp_path / "out.csv")
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["title"] == "Alpha Coat"
    assert rows[0]["images"] == "https://x/1.jpg | https://x/2.jpg"
    assert rows[0]["price"] == "1290"
    assert rows[1]["title"] == "Beta Boots — «чёрные»"   # cyrillic survives
    assert rows[1]["price"] == "380.50"


def test_jsonl_export(tmp_path):
    path = write_products(PRODUCTS, tmp_path / "out.jsonl")
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["brand"] == "Maison"
    assert "raw" not in lines[0]


def test_xlsx_export(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = write_products(PRODUCTS, tmp_path / "out.xlsx")
    ws = openpyxl.load_workbook(path).active
    header = [c.value for c in ws[1]]
    assert header[0] == "title"
    assert ws.cell(row=2, column=1).value == "Alpha Coat"
    assert ws.max_row == 3


def test_unknown_extension_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_products(PRODUCTS, tmp_path / "out.parquet")
