"""Generate transactions and returns for the reference dataset.

Deterministic: every random choice comes from one random.Random(seed), so the same seed always
produces byte-identical CSV files. Tests rely on that.

Deliberate edge cases:
- Cancellations are separate lines: invoice number prefixed "C", negative quantity.
- One order placed on 28 Feb is cancelled on 1 Mar (crosses a month boundary).
- One order is placed at 23:47 on 31 Mar (month-boundary timestamp).
- About 15% of orders have no customer_id (guest checkout).
- FR Online opens on 2 Mar 2026; Glass Vase is discontinued after 28 Feb.
- Textiles carry a SPRING10 discount on some April lines.
"""

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from datagen.catalog import (
    PRODUCTS,
    PROMO_CATEGORY,
    PROMO_CODE,
    PROMO_DISCOUNT,
    PROMO_MONTH,
    STORES,
    Product,
    Store,
)

SEED = 20260101
YEAR = 2026
INVOICES_PER_MONTH = {1: 38, 2: 36, 3: 46, 4: 50}
DATA_END = datetime(2026, 4, 30, 23, 59, 59)
GUEST_SHARE = 0.15
CANCELLED_SHARE = 0.07
RETURN_COUNT = 24
RETURN_REASONS = ["Damaged in transit", "Changed mind", "Wrong item", "Not as described"]

# Online channels sell more often than individual physical stores.
STORE_WEIGHT = {"Online": 3, "Retail": 1}


@dataclass
class Line:
    invoice_no: str
    line_no: int
    invoice_date: datetime
    store_id: str
    product_id: str
    customer_id: str | None
    quantity: int
    unit_price: float
    promo_code: str | None
    is_cancellation: bool

    @property
    def line_id(self) -> str:
        return f"{self.invoice_no}-{self.line_no}"


@dataclass
class Return:
    return_id: str
    invoice_no: str
    line_no: int
    return_date: datetime
    store_id: str
    product_id: str
    quantity: int
    refund_amount: float
    reason: str | None


def _open_stores(day: date) -> list[Store]:
    return [s for s in STORES if s.opened_date <= day]


def _sellable_products(day: date) -> list[Product]:
    return [p for p in PRODUCTS if p.discontinued_date is None or day <= p.discontinued_date]


def _random_timestamp(rng: random.Random, month: int) -> datetime:
    next_month = date(YEAR, month + 1, 1) if month < 12 else date(YEAR + 1, 1, 1)
    days_in_month = (next_month - date(YEAR, month, 1)).days
    day = rng.randint(1, days_in_month)
    return datetime(YEAR, month, day, rng.randint(8, 21), rng.randint(0, 59))


def _order_lines(rng: random.Random, when: datetime, customer_pool: list[str]) -> dict:
    """One order: store, customer and 1-4 product lines (invoice number assigned later)."""
    stores = _open_stores(when.date())
    store = rng.choices(stores, weights=[STORE_WEIGHT[s.channel] for s in stores])[0]
    customer = None if rng.random() < GUEST_SHARE else rng.choice(customer_pool)
    products = rng.sample(_sellable_products(when.date()), k=rng.randint(1, 4))
    lines = []
    for p in products:
        qty = rng.choice([1, 1, 2]) if p.category == "Furniture" else rng.randint(1, 4)
        price, promo = p.list_price, None
        if when.month == PROMO_MONTH and p.category == PROMO_CATEGORY and rng.random() < 0.6:
            price, promo = round(p.list_price * (1 - PROMO_DISCOUNT), 2), PROMO_CODE
        lines.append((p.product_id, qty, price, promo))
    return {"when": when, "store_id": store.store_id, "customer_id": customer, "lines": lines}


def generate(seed: int = SEED) -> tuple[list[Line], list[Return]]:
    rng = random.Random(seed)
    customer_pool = [f"C{n}" for n in range(1001, 1181)]

    orders = [
        _order_lines(rng, _random_timestamp(rng, month), customer_pool)
        for month, count in INVOICES_PER_MONTH.items()
        for _ in range(count)
    ]
    # Forced edge cases (fixed timestamps; contents still drawn from the seeded generator).
    boundary_order = _order_lines(rng, datetime(2026, 3, 31, 23, 47), customer_pool)
    cross_month_cancel = _order_lines(rng, datetime(2026, 2, 28, 19, 15), customer_pool)
    orders += [boundary_order, cross_month_cancel]

    # Invoice numbers follow chronological order, as a real till or web shop would assign them.
    orders.sort(key=lambda o: o["when"])
    sales: list[Line] = []
    invoices: dict[str, dict] = {}
    for n, order in enumerate(orders, start=100001):
        invoice_no = f"INV-{n}"
        invoices[invoice_no] = order
        for line_no, (product_id, qty, price, promo) in enumerate(order["lines"], start=1):
            sales.append(
                Line(
                    invoice_no,
                    line_no,
                    order["when"],
                    order["store_id"],
                    product_id,
                    order["customer_id"],
                    qty,
                    price,
                    promo,
                    False,
                )
            )

    # Cancellations: whole orders, cancelled 1-20 hours after purchase.
    cross_month_invoice = next(k for k, v in invoices.items() if v is cross_month_cancel)
    candidates = [
        k for k in invoices if k != cross_month_invoice and invoices[k] is not boundary_order
    ]
    cancelled = sorted(rng.sample(candidates, k=round(len(invoices) * CANCELLED_SHARE)))
    cancelled.append(cross_month_invoice)
    cancellations: list[Line] = []
    for invoice_no in cancelled:
        order = invoices[invoice_no]
        if invoice_no == cross_month_invoice:
            cancelled_at = datetime(2026, 3, 1, 9, 30)  # deliberate: crosses into March
        else:
            cancelled_at = order["when"] + timedelta(hours=rng.randint(1, 20))
        cancelled_at = min(cancelled_at, DATA_END)
        for line in (s for s in sales if s.invoice_no == invoice_no):
            cancellations.append(
                Line(
                    f"C{invoice_no}",
                    line.line_no,
                    cancelled_at,
                    line.store_id,
                    line.product_id,
                    line.customer_id,
                    -line.quantity,
                    line.unit_price,
                    line.promo_code,
                    True,
                )
            )

    # Returns: part or all of a line from a non-cancelled order, 2-35 days later,
    # kept only if the return happened before the data cutoff.
    cancelled_set = set(cancelled)
    returnable = [s for s in sales if s.invoice_no not in cancelled_set]
    returns: list[Return] = []
    for line in rng.sample(returnable, k=len(returnable)):
        if len(returns) == RETURN_COUNT:
            break
        returned_at = line.invoice_date + timedelta(
            days=rng.randint(2, 35), hours=rng.randint(0, 8)
        )
        if returned_at > DATA_END:
            continue
        qty = rng.randint(1, line.quantity)
        # Edge case: two returns have no recorded reason.
        reason = None if len(returns) in (5, 17) else rng.choice(RETURN_REASONS)
        returns.append(
            Return(
                "",
                line.invoice_no,
                line.line_no,
                returned_at,
                line.store_id,
                line.product_id,
                qty,
                round(qty * line.unit_price, 2),
                reason,
            )
        )
    returns.sort(key=lambda r: r.return_date)
    for i, r in enumerate(returns, start=1):
        r.return_id = f"R-{i:04d}"

    lines = sorted(sales + cancellations, key=lambda x: (x.invoice_date, x.invoice_no, x.line_no))
    return lines, returns
