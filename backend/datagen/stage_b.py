"""Stage B: eleven more tables around the unchanged Stage A core (15 tables in all).

Stage A's four tables stay byte-for-byte the same, so every Stage A answer stays valid. The new
tables are *derived from* Stage A where they'd be connected in a real business, so they agree
with each other:
- inventory goes down when Stage A sales happen and up when purchase orders are received;
- purchase orders are raised by a reorder rule when a store's stock runs low;
- online orders get shipments; returns, late deliveries and some orders get support tickets;
- the dashboard summary is computed from transactions, but with a *different* revenue
  definition (before cancellations and refunds): a deliberate look-alike of net sales.

Deliberate traps for the planner, beyond Stage A's:
- `inventory_snapshots.units_on_hand` is point-in-time: summing it across days is wrong.
- `sales_daily_summary.dashboard_revenue` vs governed `net_sales`; "orders" (customers) vs
  "purchase orders" (suppliers); `sales_targets` look like sales but are plans.
- Leavers in `staff` (headcount depends on the date); open purchase orders and tickets with no
  end date; April budget actuals missing (month not closed yet); CSAT answered by ~55%.

Deterministic: one random.Random(SEED_B), and Stage A is regenerated from its own seed.
"""

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from datagen.catalog import PRODUCTS, PROMO_CATEGORY, PROMO_CODE, STORES, Product, Store
from datagen.generate import generate

SEED_B = 20260202
START, END = date(2026, 1, 1), date(2026, 4, 30)
DATA_END = datetime(2026, 4, 30, 23, 59, 59)

STORE = {s.store_id: s for s in STORES}
PRODUCT = {p.product_id: p for p in PRODUCTS}


def days(start: date = START, end: date = END):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


@dataclass
class Table:
    header: list[str]
    rows: list[list[object]]


# ----------------------------------------------------------------------------- reference lists

# (id, name, country, category, lead time days, payment terms days, preferred)
SUPPLIERS = [
    ("SU01", "Lumen Works", "Poland", "Lighting", 12, 30, True),
    ("SU02", "Bright Atelier", "Italy", "Lighting", 18, 45, False),
    ("SU03", "Ferro Kitchenware", "Portugal", "Kitchen", 10, 30, True),
    ("SU04", "Nordic Pantry Goods", "Sweden", "Kitchen", 14, 60, False),
    ("SU05", "Porto Linen Co.", "Portugal", "Textiles", 9, 30, True),
    ("SU06", "Anatolia Weavers", "Turkey", "Textiles", 21, 60, False),
    ("SU07", "Oakhaus Furniture", "Germany", "Furniture", 20, 45, True),
    ("SU08", "Baltic Timber Design", "Lithuania", "Furniture", 28, 60, False),
    ("SU09", "Clay & Glass Studio", "Spain", "Decor", 11, 30, True),
    ("SU10", "Frame Republic", "Netherlands", "Decor", 8, 30, False),
]

PROMOTIONS = [
    # code, name, category, discount, start, end, channel
    ("AUTUMN15", "Autumn refresh", "Decor", 0.15, date(2025, 10, 1), date(2025, 10, 31), "All"),
    (
        "BLACKFRI25",
        "Black Friday",
        "Furniture",
        0.25,
        date(2025, 11, 28),
        date(2025, 12, 1),
        "Online",
    ),
    (
        PROMO_CODE,
        "Spring textiles",
        PROMO_CATEGORY,
        0.10,
        date(2026, 4, 1),
        date(2026, 4, 30),
        "All",
    ),
    ("GARDEN10", "Garden decor", "Decor", 0.10, date(2026, 5, 15), date(2026, 6, 15), "All"),
    (
        "SUMMER20",
        "Summer furniture",
        "Furniture",
        0.20,
        date(2026, 6, 15),
        date(2026, 7, 15),
        "All",
    ),
]

FIRST_NAMES = [
    "Ana",
    "Ben",
    "Carla",
    "Dev",
    "Elif",
    "Finn",
    "Greta",
    "Hugo",
    "Ines",
    "Jonas",
    "Kira",
    "Liam",
    "Maya",
    "Nico",
    "Olga",
    "Pablo",
    "Quinn",
    "Rosa",
    "Sven",
    "Tara",
    "Umar",
    "Vera",
    "Wes",
    "Xenia",
    "Yusuf",
    "Zoe",
    "Aiden",
    "Bea",
    "Cian",
    "Dora",
    "Emil",
    "Fleur",
    "Gael",
    "Hana",
    "Ivo",
    "Jade",
    "Karl",
    "Lena",
    "Marco",
    "Nora",
    "Oskar",
    "Pia",
    "Rami",
    "Sofia",
    "Theo",
    "Uma",
    "Viktor",
    "Wanda",
]

DEPARTMENTS = {
    "Stores": ["Staff", "Rent", "Utilities"],
    "Online": ["Staff", "Platform fees", "Marketing"],
    "Fulfilment": ["Staff", "Carriers", "Packaging"],
    "Customer Service": ["Staff", "Software"],
    "Marketing": ["Media", "Agencies"],
    "Head Office": ["Staff", "Rent", "IT"],
}

CARRIER = {
    "United Kingdom": "Parcelwise UK",
    "Ireland": "Parcelwise UK",
    "Germany": "Rhein Logistik",
    "Netherlands": "Rhein Logistik",
    "France": "Hexa Express",
    "Spain": "Hexa Express",
}
PROMISED_DAYS = {  # calendar days from dispatch, from the shipping and delivery standards
    "United Kingdom": 4,
    "Ireland": 4,
    "Germany": 5,
    "Netherlands": 5,
    "France": 6,
    "Spain": 6,
}
FURNITURE_PROMISED_DAYS = 10
DISPATCH_CUTOFF = time(14, 0)


def _supplier_for(product: Product) -> tuple:
    return next(s for s in SUPPLIERS if s[3] == product.category and s[6])


# ----------------------------------------------------------------------------- generation


class StageB:
    def __init__(self, seed: int = SEED_B) -> None:
        self.rng = random.Random(seed)
        self.lines, self.returns = generate()
        self.sales = [x for x in self.lines if not x.is_cancellation]
        self.cancelled_orders = {x.invoice_no[1:] for x in self.lines if x.is_cancellation}

    # -- suppliers, costs, inventory and purchase orders (one simulation)

    def suppliers(self) -> Table:
        return Table(
            [
                "supplier_id",
                "supplier_name",
                "country",
                "category",
                "lead_time_days",
                "payment_terms_days",
                "is_preferred",
            ],
            [list(s) for s in SUPPLIERS],
        )

    def _unit_costs(self) -> dict[str, float]:
        return {
            p.product_id: round(p.list_price * self.rng.uniform(0.36, 0.48), 2) for p in PRODUCTS
        }

    def inventory_and_purchasing(self) -> tuple[Table, Table]:
        rng = self.rng
        cost = self._unit_costs()
        movements: dict[tuple[str, str, date], int] = defaultdict(int)
        for x in self.lines:  # sales take stock; a cancelled order puts it back when cancelled
            movements[(x.store_id, x.product_id, x.invoice_date.date())] -= x.quantity
        for r in self.returns:  # returned goods are restocked
            movements[(r.store_id, r.product_id, r.return_date.date())] += r.quantity

        stock = {(s.store_id, p.product_id): rng.randint(3, 12) for s in STORES for p in PRODUCTS}
        arrivals: dict[tuple[str, str, date], int] = defaultdict(int)
        open_po: dict[tuple[str, str], dict] = {}
        po_rows: list[list[object]] = []
        snapshots: list[list[object]] = []
        po_seq = 0

        for d in days():
            for s in STORES:
                if s.opened_date > d:
                    continue
                for p in PRODUCTS:
                    key = (s.store_id, p.product_id)
                    stock[key] += arrivals.pop((s.store_id, p.product_id, d), 0)
                    stock[key] = max(
                        0, stock[key] + movements.get((s.store_id, p.product_id, d), 0)
                    )
                    po = open_po.get(key)
                    if po and po["closes"] <= d:
                        del open_po[key]
                        po = None
                    active = p.discontinued_date is None or d < p.discontinued_date
                    if po is None and active and stock[key] <= 2:
                        po_seq += 1
                        po = self._purchase_order(po_seq, s, p, d, cost[p.product_id])
                        po_rows.append(po["row"])
                        if po["received"]:
                            arrivals[(s.store_id, p.product_id, po["received"])] += po[
                                "qty_received"
                            ]
                        open_po[key] = po
                    on_order = (
                        po["qty"] if po and (po["received"] is None or po["received"] > d) else 0
                    )
                    snapshots.append(
                        [d, s.store_id, p.product_id, stock[key], on_order, stock[key] == 0]
                    )

        purchase_orders = Table(
            [
                "po_line_id",
                "po_number",
                "supplier_id",
                "store_id",
                "product_id",
                "ordered_date",
                "expected_date",
                "received_date",
                "quantity_ordered",
                "quantity_received",
                "unit_cost",
                "status",
                "days_late",
            ],
            po_rows,
        )
        inventory = Table(
            [
                "snapshot_date",
                "store_id",
                "product_id",
                "units_on_hand",
                "units_on_order",
                "is_stockout",
            ],
            snapshots,
        )
        return inventory, purchase_orders

    def _purchase_order(
        self, seq: int, store: Store, product: Product, d: date, unit_cost: float
    ) -> dict:
        rng = self.rng
        supplier = _supplier_for(product)
        qty = 4 if product.category == "Furniture" else 12
        expected = d + timedelta(days=supplier[4])
        roll = rng.random()
        if roll < 0.05:  # cancelled by the supplier: nothing arrives
            received, qty_received, status = None, 0, "cancelled"
            closes = expected + timedelta(days=7)
        else:
            delay = rng.choice([-2, -1]) if roll < 0.15 else 0
            if roll > 0.75:
                delay = rng.randint(1, 3) if roll < 0.92 else rng.randint(4, 10)
            received = expected + timedelta(days=delay)
            qty_received = qty - rng.randint(1, qty // 3) if rng.random() < 0.08 else qty
            status = "received" if qty_received == qty else "partially_received"
            closes = received + timedelta(days=1)
            if received > END:  # still on its way when the data ends
                received, qty_received, status = None, 0, "open"
        days_late = (received - expected).days if received else None
        po_number = f"PO-{26000 + seq}"
        row = [
            f"{po_number}-1",
            po_number,
            supplier[0],
            store.store_id,
            product.product_id,
            d,
            expected,
            received,
            qty,
            qty_received,
            unit_cost,
            status,
            days_late,
        ]
        return {
            "row": row,
            "qty": qty,
            "qty_received": qty_received,
            "received": received,
            "closes": closes,
        }

    # -- promotions

    def promotions(self) -> Table:
        return Table(
            [
                "promo_code",
                "promo_name",
                "category",
                "discount_pct",
                "start_date",
                "end_date",
                "channel",
            ],
            [
                [c, n, cat, round(disc * 100, 2), s, e, ch]
                for c, n, cat, disc, s, e, ch in PROMOTIONS
            ],
        )

    # -- staff and shifts

    def staff(self) -> Table:
        rng = self.rng
        rows: list[list[object]] = []
        names = iter(FIRST_NAMES)
        seq = 0

        def add(role, store_id, contract, start, end=None):
            nonlocal seq
            seq += 1
            name = f"{next(names)} {chr(65 + rng.randint(0, 25))}."
            hours = 37.5 if contract == "full_time" else rng.choice([16.0, 20.0, 24.0])
            rows.append([f"E{seq:03d}", name, role, store_id, contract, hours, start, end])

        for s in STORES:
            hired = max(s.opened_date - timedelta(days=21), date(2017, 6, 1))
            if s.channel == "Retail":
                add("Store Manager", s.store_id, "full_time", hired)
                for _ in range(rng.randint(3, 4)):
                    contract = rng.choice(["full_time", "part_time"])
                    add(
                        "Sales Associate",
                        s.store_id,
                        contract,
                        hired + timedelta(days=rng.randint(0, 900)),
                    )
            else:
                for _ in range(2 if s.store_id != "S08" else 1):
                    add(
                        "Fulfilment Associate",
                        s.store_id,
                        "full_time",
                        hired + timedelta(days=rng.randint(0, 400)),
                    )
        for _ in range(5):
            add(
                "Customer Service Agent",
                None,
                rng.choice(["full_time", "part_time"]),
                date(2021, 1, 4) + timedelta(days=rng.randint(0, 1500)),
            )
        # Clamp start dates to the past, then add the deliberate joiners and leavers.
        for r in rows:
            if r[6] > date(2025, 12, 31):
                r[6] = date(2025, 12, 31) if r[3] != "S08" else r[6]
        leavers = [r for r in rows if r[2] == "Sales Associate"]
        for r, end in zip(
            rng.sample(leavers, 3),
            [date(2026, 2, 13), date(2026, 3, 6), date(2026, 3, 27)],
            strict=True,
        ):
            r[7] = end
        add("Sales Associate", "S04", "part_time", date(2026, 3, 16))  # Berlin hires in March
        add("Customer Service Agent", None, "full_time", date(2026, 3, 2))  # for the FR launch
        for r in rows:  # "is_active": still employed when the data ends (30 Apr 2026)
            r.append(r[7] is None)
        return Table(
            [
                "employee_id",
                "display_name",
                "role",
                "store_id",
                "contract_type",
                "weekly_contract_hours",
                "start_date",
                "end_date",
                "is_active",
            ],
            rows,
        )

    def shifts(self, staff: Table) -> Table:
        rng = self.rng
        rows: list[list[object]] = []
        seq = 0
        for e in staff.rows:
            emp_id, _, role, store_id, contract, weekly, start, end, _active = e
            per_week = 5 if contract == "full_time" else (2 if weekly <= 16 else 3)
            pattern = sorted(rng.sample(range(7 if store_id else 5), per_week))
            hours = round(weekly / per_week * 2) / 2
            first = max(start, START)
            if store_id and STORE[store_id].opened_date > first:
                first = STORE[store_id].opened_date
            last = min(end or END, END)
            for d in days(first, last):
                if d.weekday() not in pattern:
                    continue
                seq += 1
                roll = rng.random()
                if roll < 0.035:
                    reason = "sick"
                elif roll < 0.045:
                    reason = "no_show"
                elif roll < 0.07:
                    reason = "holiday"
                else:
                    reason = None
                worked = (
                    0.0
                    if reason
                    else hours + (rng.choice([0.5, 1.0]) if rng.random() < 0.1 else 0.0)
                )
                rows.append([f"SH-{seq:05d}", emp_id, store_id, d, hours, worked, reason])
        return Table(
            [
                "shift_id",
                "employee_id",
                "store_id",
                "shift_date",
                "scheduled_hours",
                "worked_hours",
                "absence_reason",
            ],
            rows,
        )

    # -- shipments and support tickets

    def shipments(self) -> Table:
        rng = self.rng
        orders: dict[str, list] = defaultdict(list)
        for x in self.sales:
            if STORE[x.store_id].channel == "Online" and x.invoice_no not in self.cancelled_orders:
                orders[x.invoice_no].append(x)
        rows = []
        for n, invoice in enumerate(sorted(orders, key=lambda i: orders[i][0].invoice_date), 1):
            first = orders[invoice][0]
            store = STORE[first.store_id]
            placed = first.invoice_date
            shipped = placed.date() + timedelta(days=0 if placed.time() < DISPATCH_CUTOFF else 1)
            furniture = any(PRODUCT[x.product_id].category == "Furniture" for x in orders[invoice])
            promised_days = FURNITURE_PROMISED_DAYS if furniture else PROMISED_DAYS[store.country]
            promised = shipped + timedelta(days=promised_days)
            transit = promised_days - rng.randint(0, 2)
            if rng.random() < 0.12:  # late
                transit = promised_days + rng.randint(1, 4)
            delivered = shipped + timedelta(days=transit)
            if delivered > END:
                delivered = None
            rows.append(
                [
                    f"SHP-{n:04d}",
                    invoice,
                    store.store_id,
                    CARRIER[store.country],
                    shipped,
                    promised,
                    delivered,
                    None if delivered is None else delivered <= promised,
                    None if delivered is None else (delivered - shipped).days,
                    "in_transit" if delivered is None else "delivered",
                ]
            )
        return Table(
            [
                "shipment_id",
                "invoice_no",
                "store_id",
                "carrier",
                "shipped_date",
                "promised_date",
                "delivered_date",
                "is_on_time",
                "delivery_days",
                "status",
            ],
            rows,
        )

    def support_tickets(self, shipments: Table) -> Table:
        rng = self.rng
        raw: list[tuple] = []  # (opened_at, channel, category, store_id, invoice_no, base csat)
        for r in self.returns:
            when = datetime.combine(r.return_date.date(), time(10)) - timedelta(
                days=rng.randint(1, 3)
            )
            raw.append((when, "email", "Returns", r.store_id, r.invoice_no, 3.6))
        for s in shipments.rows:
            if s[7] is False:  # late delivery (None: still in transit)
                when = datetime.combine(s[5], time(9)) + timedelta(days=1, hours=rng.randint(0, 8))
                if when <= DATA_END:
                    raw.append((when, rng.choice(["chat", "phone"]), "Delivery", s[2], s[1], 2.6))
        for invoice in sorted(self.cancelled_orders):
            if rng.random() < 0.5:
                x = next(y for y in self.sales if y.invoice_no == invoice)
                when = x.invoice_date + timedelta(hours=rng.randint(1, 20))
                raw.append((when, "email", "Billing", x.store_id, invoice, 3.4))
        for d in days():  # questions not tied to an order
            for _ in range(rng.choice([1, 2, 2, 3])):
                when = datetime.combine(d, time(rng.randint(8, 19), rng.choice([0, 15, 30, 45])))
                category = rng.choice(
                    ["Product question", "Product question", "Store visit", "Other"]
                )
                store = rng.choice(STORES)
                if store.opened_date > d:
                    store = STORES[0]
                raw.append(
                    (
                        when,
                        rng.choice(["chat", "email", "phone"]),
                        category,
                        store.store_id if category == "Store visit" else None,
                        None,
                        4.1,
                    )
                )

        rows = []
        for n, (opened, channel, category, store_id, invoice, base) in enumerate(
            sorted(raw, key=lambda t: t[0]), 1
        ):
            first_response = {
                "chat": rng.randint(1, 12),
                "phone": rng.randint(0, 4),
                "email": rng.randint(45, 1500),
            }[channel]
            resolution = round(
                rng.uniform(0.2, 6.0) if channel != "email" else rng.uniform(2, 72), 1
            )
            closed = opened + timedelta(hours=resolution)
            priority = (
                "high"
                if category in ("Delivery", "Billing")
                else rng.choice(["normal", "normal", "low"])
            )
            if closed > DATA_END or rng.random() < 0.01:
                closed_at, resolution_hours, status, csat = None, None, "open", None
            else:
                closed_at, resolution_hours, status = (
                    closed.replace(microsecond=0, second=0),
                    resolution,
                    "closed",
                )
                csat = None
                if rng.random() < 0.55:
                    csat = max(1, min(5, round(rng.gauss(base, 0.9))))
            rows.append(
                [
                    f"T-{n:05d}",
                    opened,
                    closed_at,
                    channel,
                    category,
                    store_id,
                    invoice,
                    priority,
                    status,
                    first_response,
                    resolution_hours,
                    csat,
                ]
            )
        return Table(
            [
                "ticket_id",
                "opened_at",
                "closed_at",
                "channel",
                "category",
                "store_id",
                "invoice_no",
                "priority",
                "status",
                "first_response_minutes",
                "resolution_hours",
                "csat_score",
            ],
            rows,
        )

    # -- the dashboard look-alike

    def sales_daily_summary(self) -> Table:
        agg: dict[tuple[date, str], list] = defaultdict(lambda: [set(), 0.0, 0])
        for x in self.sales:  # every order placed, including ones cancelled later
            a = agg[(x.invoice_date.date(), x.store_id)]
            a[0].add(x.invoice_no)
            a[1] += x.quantity * x.unit_price
            a[2] += x.quantity
        return Table(
            ["summary_date", "store_id", "orders_placed", "dashboard_revenue", "units_ordered"],
            [[d, s, len(a[0]), round(a[1], 2), a[2]] for (d, s), a in sorted(agg.items())],
        )

    # -- finance: targets and budgets

    def _net_sales_by_store_month(self) -> dict[tuple[str, int], float]:
        net: dict[tuple[str, int], float] = defaultdict(float)
        for x in self.lines:
            net[(x.store_id, x.invoice_date.month)] += x.quantity * x.unit_price
        for r in self.returns:
            net[(r.store_id, r.return_date.month)] -= r.refund_amount
        return net

    def sales_targets(self) -> Table:
        rng = self.rng
        actual = self._net_sales_by_store_month()
        rows = []
        for month in range(1, 5):
            first = date(2026, month, 1)
            for s in STORES:
                if s.opened_date > first + timedelta(days=31):
                    continue
                base = max(actual.get((s.store_id, month), 0.0), 400.0)
                target = round(base * rng.uniform(0.85, 1.25) / 100) * 100
                rows.append([first, s.store_id, float(target)])
        return Table(["target_month", "store_id", "net_sales_target"], rows)

    def budgets(self) -> Table:
        rng = self.rng
        rows = []
        n = 0
        for month in range(1, 5):
            for dept, categories in DEPARTMENTS.items():
                for cat in categories:
                    n += 1
                    budget = float(rng.randrange(2000, 60000, 500))
                    actual = None if month == 4 else round(budget * rng.uniform(0.82, 1.18), 2)
                    rows.append([f"B-{n:03d}", date(2026, month, 1), dept, cat, budget, actual])
        return Table(
            [
                "budget_line_id",
                "budget_month",
                "department",
                "cost_category",
                "budget_eur",
                "actual_eur",
            ],
            rows,
        )

    def tables(self) -> dict[str, Table]:
        inventory, purchase_orders = self.inventory_and_purchasing()
        staff = self.staff()
        shipments = self.shipments()
        return {
            "suppliers": self.suppliers(),
            "purchase_orders": purchase_orders,
            "inventory_snapshots": inventory,
            "promotions": self.promotions(),
            "staff": staff,
            "shifts": self.shifts(staff),
            "shipments": shipments,
            "support_tickets": self.support_tickets(shipments),
            "sales_daily_summary": self.sales_daily_summary(),
            "sales_targets": self.sales_targets(),
            "budgets": self.budgets(),
        }


def generate_stage_b(seed: int = SEED_B) -> dict[str, Table]:
    return StageB(seed).tables()
