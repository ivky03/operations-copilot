"""Write the reference dataset as CSV files.

Usage (from backend/):
    uv run python -m datagen                 # writes ../data/reference/tables/*.csv
    uv run python -m datagen --out /tmp/x    # write somewhere else
"""

import argparse
import csv
from pathlib import Path

from datagen.catalog import PRODUCTS, STORES
from datagen.generate import generate

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "data" / "reference" / "tables"


def _fmt(value: object) -> str:
    """Stable text formatting so files are byte-identical across runs and machines."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.2f}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _write(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows([[_fmt(v) for v in row] for row in rows])


def write_all(out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    lines, returns = generate()

    _write(
        out / "stores.csv",
        ["store_id", "store_name", "country", "region", "channel", "opened_date"],
        [[s.store_id, s.store_name, s.country, s.region, s.channel, s.opened_date] for s in STORES],
    )
    _write(
        out / "products.csv",
        [
            "product_id",
            "sku",
            "product_name",
            "category",
            "list_price",
            "status",
            "discontinued_date",
        ],
        [
            [
                p.product_id,
                p.sku,
                p.product_name,
                p.category,
                p.list_price,
                p.status,
                p.discontinued_date,
            ]
            for p in PRODUCTS
        ],
    )
    _write(
        out / "transactions.csv",
        [
            "line_id",
            "invoice_no",
            "line_no",
            "invoice_date",
            "store_id",
            "product_id",
            "customer_id",
            "quantity",
            "unit_price",
            "promo_code",
            "is_cancellation",
        ],
        [
            [
                x.line_id,
                x.invoice_no,
                x.line_no,
                x.invoice_date,
                x.store_id,
                x.product_id,
                x.customer_id,
                x.quantity,
                x.unit_price,
                x.promo_code,
                x.is_cancellation,
            ]
            for x in lines
        ],
    )
    _write(
        out / "returns.csv",
        [
            "return_id",
            "invoice_no",
            "line_no",
            "return_date",
            "store_id",
            "product_id",
            "quantity",
            "refund_amount",
            "reason",
        ],
        [
            [
                r.return_id,
                r.invoice_no,
                r.line_no,
                r.return_date,
                r.store_id,
                r.product_id,
                r.quantity,
                r.refund_amount,
                r.reason,
            ]
            for r in returns
        ],
    )
    return {
        "stores": len(STORES),
        "products": len(PRODUCTS),
        "transactions": len(lines),
        "returns": len(returns),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    counts = write_all(args.out)
    for table, n in counts.items():
        print(f"{table:<13} {n:>4} rows -> {args.out / (table + '.csv')}")


if __name__ == "__main__":
    main()
