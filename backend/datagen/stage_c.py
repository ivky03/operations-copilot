"""Stage C: 35 more tables around Stages A and B, for 50 tables in all.

Stage C is the scale stage. It exists to make *choosing the right table* hard, the way it is in a
real small company's warehouse: many tables, several domains, and look-alikes that answer a
different question from the one asked. Each table here is defined once (manifest and rows
together), so the CSV and its manifest can't drift apart.

Domains added: customers and loyalty, marketing and web, catalogue and pricing, store
operations, warehousing, people, and finance.

Deliberate look-alikes, beyond Stage B's:
- revenue: `web_sessions_daily.online_revenue` (tracked in the browser, before cancellations),
  `campaign_spend_daily.attributed_revenue` (credited to campaigns, overlapping), and
  `gl_entries` sales revenue (accounting, by posting date) vs governed `net_sales`;
- stock: `warehouse_stock` (central warehouses, weekly) vs `inventory_snapshots` (stores, daily);
- visitors vs orders: `store_footfall_daily.visitors` vs transactions' order count;
- satisfaction: `nps_responses` (customers, 0-10) vs `staff_survey` (employees, 1-5) vs
  support tickets' CSAT (1-5);
- spend: `campaign_spend_daily.spend` (media spend) vs `expenses` marketing claims vs
  `gl_entries` marketing account;
- cost: `product_costs` (Finance, monthly landed cost) vs purchase orders' unit cost;
- headcount: `vacancies` (open roles) vs `staff` (people employed).

Rows that join to Stages A and B use their real keys (stores, products, suppliers, staff,
customers, invoices, promotions), read from the committed CSVs, which are themselves
deterministic. One random.Random(SEED_C) drives everything else.
"""

import csv
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
SEED_C = 20260303
START, END = date(2026, 1, 1), date(2026, 4, 30)
MONTHS = [date(2026, m, 1) for m in (1, 2, 3, 4)]

# Which Stage C tables join Stage B's 15 to make the 30-table step of the scale experiment.
# Chosen to bring the main look-alikes in early, so difficulty grows step by step.
SIZE_30 = [
    "customers",
    "loyalty_members",
    "loyalty_points",
    "marketing_campaigns",
    "campaign_spend_daily",
    "web_sessions_daily",
    "product_reviews",
    "price_changes",
    "product_costs",
    "store_footfall_daily",
    "warehouses",
    "warehouse_stock",
    "stock_transfers",
    "payroll",
    "gl_entries",
]


def days(start: date = START, end: date = END):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _read(stage: str, table: str) -> list[dict]:
    with (DATA / stage / "tables" / f"{table}.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------------------- manifest helpers


def col(name, type_, role, description, **extra) -> dict:
    return {"name": name, "type": type_, "role": role, "description": description, **extra}


def dim(name, description, values=None, **extra) -> dict:
    """A text dimension with a value index (and its allowed values, when fixed)."""
    out = col(name, "string", "dimension", description, value_index=True, **extra)
    if values:
        out["allowed_values"] = list(values)
    return out


def metric(name, title, description, aggregation, expression, unit=None, filters=None) -> dict:
    out = {
        "name": name,
        "title": title,
        "description": description,
        "aggregation": aggregation,
        "expression": expression,
    }
    if unit:
        out["unit"] = unit
    if filters:
        out["filters"] = filters
    return out


def link(column, references) -> dict:
    return {"column": column, "references": references}


@dataclass
class Table:
    manifest: dict
    rows: list[list[object]]
    readers: list[str]

    @property
    def header(self) -> list[str]:
        return [c["name"] for c in self.manifest["columns"]]


def table(
    source,
    title,
    description,
    grain,
    primary_key,
    columns,
    rows,
    readers=("operations", "finance"),
    time_column=None,
    relationships=(),
    metrics=(),
    derived=(),
) -> Table:
    manifest = {
        "source": source,
        "title": title,
        "description": " ".join(description.split()),
        "grain": grain,
        "primary_key": list(primary_key),
    }
    if time_column:
        manifest["time_column"] = time_column
    manifest["columns"] = columns
    if relationships:
        manifest["relationships"] = list(relationships)
    if metrics:
        manifest["metrics"] = list(metrics)
    if derived:
        manifest["derived_metrics"] = list(derived)
    return Table(manifest, rows, list(readers))


EUR, MIN, HRS = "EUR", "minutes", "hours"


# ----------------------------------------------------------------------------- the generator


class StageC:
    def __init__(self, seed: int = SEED_C):
        self.rng = random.Random(seed)
        self.stores = _read("reference", "stores")
        self.products = _read("reference", "products")
        self.transactions = _read("reference", "transactions")
        self.returns = _read("reference", "returns")
        self.suppliers = _read("stage_b", "suppliers")
        self.staff = _read("stage_b", "staff")
        self.purchase_orders = _read("stage_b", "purchase_orders")
        self.promotions = _read("stage_b", "promotions")
        self.retail = [s for s in self.stores if s["channel"] == "Retail"]
        self.online = [s for s in self.stores if s["channel"] == "Online"]
        self.opened = {s["store_id"]: date.fromisoformat(s["opened_date"]) for s in self.stores}
        self.price = {p["product_id"]: float(p["list_price"]) for p in self.products}
        self.category = {p["product_id"]: p["category"] for p in self.products}
        # Facts other tables must agree with: every order as the checkout saw it (before any
        # cancellation), and sales by store and month. A cancellation is a separate invoice
        # (CINV-...) with negative quantities.
        self.checkout: dict[str, dict] = {}
        self.sales_month: dict[tuple[str, date], dict[str, float]] = {}
        for t in self.transactions:
            when = datetime.fromisoformat(t["invoice_date"])
            value = int(t["quantity"]) * float(t["unit_price"])
            key = (t["store_id"], when.date().replace(day=1))
            totals = self.sales_month.setdefault(
                key, {"gross": 0.0, "cancelled": 0.0, "refunds": 0.0}
            )
            if t["is_cancellation"] == "true":
                totals["cancelled"] += -value  # stored as a positive amount
                continue
            totals["gross"] += value
            order = self.checkout.setdefault(
                t["invoice_no"], {"store_id": t["store_id"], "day": when.date(), "value": 0.0}
            )
            order["value"] += value
        for r in self.returns:
            key = (r["store_id"], datetime.fromisoformat(r["return_date"]).date().replace(day=1))
            totals = self.sales_month.setdefault(
                key, {"gross": 0.0, "cancelled": 0.0, "refunds": 0.0}
            )
            totals["refunds"] += float(r["refund_amount"])
        self.segment_sizes: dict[
            str, int
        ] = {}  # opted-in customers per segment, set by customers()

    def open_on(self, store_id: str, day: date) -> bool:
        return self.opened[store_id] <= day

    def money(self, low: float, high: float) -> float:
        return round(self.rng.uniform(low, high), 2)

    # ------------------------------------------------------------------ customers and loyalty

    def customer_segments(self) -> Table:
        rows = [
            ["NEW", "New customers", "First order in the last 90 days.", 1, 0.0],
            ["REG", "Regulars", "Two to five orders in the last 12 months.", 2, 0.0],
            ["VIP", "VIP", "Six or more orders in the last 12 months.", 6, 5.0],
            ["LAP", "Lapsed", "No order in the last 12 months.", 0, 0.0],
            ["B2B", "Trade accounts", "Interior designers and businesses on trade terms.", 1, 12.0],
        ]
        return table(
            "customer_segments",
            "Customer segments",
            """Marketing's customer segments: what each means and any standing discount.
            A small reference table; customers name their segment by code.""",
            "One row per segment.",
            ["segment_code"],
            [
                col("segment_code", "string", "id", "Segment code."),
                dim("segment_name", "Segment name."),
                col("definition", "string", "attribute", "How a customer qualifies."),
                col("min_orders", "integer", "attribute", "Orders needed to qualify."),
                col(
                    "standing_discount_pct", "decimal", "attribute", "Automatic discount, percent."
                ),
            ],
            rows,
        )

    def customers(self) -> Table:
        seen = sorted({t["customer_id"] for t in self.transactions if t["customer_id"]})
        extra = [f"C{n}" for n in range(2000, 2140)]  # signed up, never ordered this year
        countries = {s["store_id"]: s["country"] for s in self.stores}
        first_store = {}
        orders = {}
        for t in self.transactions:
            if t["customer_id"] and t["is_cancellation"] != "true":
                first_store.setdefault(t["customer_id"], t["store_id"])
                orders.setdefault(t["customer_id"], set()).add(t["invoice_no"])
        rows = []
        for cid in seen + extra:
            store = first_store.get(cid) or self.rng.choice(self.stores)["store_id"]
            n = len(orders.get(cid, ()))
            segment = "VIP" if n >= 6 else "REG" if n >= 2 else "NEW" if n == 1 else "LAP"
            if self.rng.random() < 0.04:
                segment = "B2B"
            signup = date(2026, 1, 1) - timedelta(days=self.rng.randint(0, 2200))
            if cid in extra and self.rng.random() < 0.3:
                signup = START + timedelta(days=self.rng.randint(0, 119))
            # Lifetime value covers every year since sign-up, not just 2026: a look-alike.
            ltv = round(n * self.rng.uniform(40, 160) + self.rng.uniform(0, 900), 2)
            opted_in = self.rng.random() < 0.62
            if opted_in:
                self.segment_sizes[segment] = self.segment_sizes.get(segment, 0) + 1
            rows.append(
                [
                    cid,
                    countries[store],
                    signup,
                    "online" if store in {s["store_id"] for s in self.online} else "store",
                    opted_in,
                    segment,
                    store,
                    ltv,
                ]
            )
        return table(
            "customers",
            "Customers",
            """Registered customers: where they signed up, their marketing consent and
            segment. Lifetime value is the CRM's estimate over the customer's whole history
            since sign-up, not 2026 sales; for sales use net sales from transactions.""",
            "One row per registered customer.",
            ["customer_id"],
            [
                col("customer_id", "string", "id", "Customer identifier, as on transactions."),
                dim("country", "Country of the customer's home store."),
                col("signup_date", "date", "time", "When the customer registered."),
                dim(
                    "signup_channel", "Where they registered: store or online.", ["store", "online"]
                ),
                col(
                    "marketing_opt_in",
                    "boolean",
                    "dimension",
                    "Whether the customer agreed to marketing email.",
                ),
                col("segment_code", "string", "id", "Marketing segment."),
                col("home_store_id", "string", "id", "Store where the customer first bought."),
                col(
                    "lifetime_value",
                    "decimal",
                    "measure",
                    "CRM estimate of total spend since sign-up (all years), EUR.",
                    unit=EUR,
                ),
            ],
            rows,
            time_column="signup_date",
            relationships=[
                link("segment_code", "customer_segments.segment_code"),
                link("home_store_id", "stores.store_id"),
            ],
            metrics=[
                metric(
                    "customer_count", "Customers", "Registered customers.", "count", "customer_id"
                ),
                metric(
                    "opted_in_customer_count",
                    "Customers opted in to marketing",
                    "Registered customers who accept marketing email.",
                    "count",
                    "customer_id",
                    filters=[{"column": "marketing_opt_in", "op": "=", "value": True}],
                ),
                metric(
                    "average_lifetime_value",
                    "Average customer lifetime value",
                    "Average CRM lifetime value estimate (all years since sign-up).",
                    "avg",
                    "lifetime_value",
                    EUR,
                ),
            ],
        )

    def loyalty(self) -> tuple[Table, Table]:
        customers = sorted({t["customer_id"] for t in self.transactions if t["customer_id"]})
        members, by_customer = [], {}
        for i, cid in enumerate(c for c in customers if self.rng.random() < 0.7):
            mid = f"M{5001 + i}"
            by_customer[cid] = mid
            members.append([mid, cid, None, START - timedelta(days=self.rng.randint(0, 1500))])
        entries, balance = [], {m[0]: self.rng.randint(0, 400) for m in members}
        n = 0
        for t in self.transactions:
            mid = by_customer.get(t["customer_id"])
            if not mid or t["is_cancellation"] == "true":
                continue
            n += 1
            points = int(int(t["quantity"]) * float(t["unit_price"]))  # 1 point per euro
            day = datetime.fromisoformat(t["invoice_date"]).date()
            entries.append(
                [f"LP-{n:05d}", mid, day, t["store_id"], t["invoice_no"], points, "earn"]
            )
            balance[mid] += points
            if balance[mid] >= 500 and self.rng.random() < 0.35:
                n += 1
                entries.append(
                    [f"LP-{n:05d}", mid, day, t["store_id"], t["invoice_no"], -500, "redeem"]
                )
                balance[mid] -= 500
        for mid in balance:
            if self.rng.random() < 0.15:
                n += 1
                bonus = self.rng.choice([50, 100, 250])
                day = START + timedelta(days=self.rng.randint(0, 119))
                entries.append([f"LP-{n:05d}", mid, day, None, None, bonus, "bonus"])
                balance[mid] += bonus
        entries.sort(key=lambda e: (e[2], e[0]))
        for m in members:
            b = balance[m[0]]
            m[2] = "Gold" if b >= 1500 else "Silver" if b >= 600 else "Bronze"
            m.extend([b, "active" if b > 0 else "lapsed"])
        members_t = table(
            "loyalty_members",
            "Loyalty programme members",
            """Members of the Brindlewood Rewards programme, their tier and current points
            balance (as of 30 April 2026). Tier follows the balance: Gold 1,500+, Silver
            600+, otherwise Bronze.""",
            "One row per member.",
            ["member_id"],
            [
                col("member_id", "string", "id", "Membership number."),
                col("customer_id", "string", "id", "The member's customer record."),
                dim("tier", "Bronze, Silver or Gold.", ["Bronze", "Silver", "Gold"]),
                col("joined_date", "date", "time", "When the customer joined the programme."),
                col("points_balance", "integer", "measure", "Points held on 30 April 2026."),
                dim("status", "active, or lapsed (no points).", ["active", "lapsed"]),
            ],
            [[m[0], m[1], m[2], m[3], m[4], m[5]] for m in members],
            time_column="joined_date",
            relationships=[link("customer_id", "customers.customer_id")],
            metrics=[
                metric(
                    "member_count", "Loyalty members", "Programme members.", "count", "member_id"
                ),
                metric(
                    "points_outstanding",
                    "Points outstanding",
                    "Points members hold and could still redeem.",
                    "sum",
                    "points_balance",
                ),
            ],
        )
        points_t = table(
            "loyalty_points",
            "Loyalty points ledger",
            """Every points movement: earned on purchases (one point per euro), redeemed
            (500 points for a voucher), and bonus points from campaigns. Redemptions are
            negative.""",
            "One row per points movement.",
            ["entry_id"],
            [
                col("entry_id", "string", "id", "Ledger entry."),
                col("member_id", "string", "id", "Member."),
                col("entry_date", "date", "time", "Date of the movement."),
                col("store_id", "string", "id", "Store, for purchases.", nullable=True),
                col(
                    "invoice_no",
                    "string",
                    "attribute",
                    "Order that earned or used the points.",
                    nullable=True,
                ),
                col("points", "integer", "measure", "Points added (positive) or used (negative)."),
                dim("entry_type", "earn, redeem or bonus.", ["earn", "redeem", "bonus"]),
            ],
            entries,
            time_column="entry_date",
            relationships=[
                link("member_id", "loyalty_members.member_id"),
                link("store_id", "stores.store_id"),
            ],
            metrics=[
                metric(
                    "points_earned",
                    "Points earned",
                    "Points earned on purchases and bonuses.",
                    "sum",
                    "points",
                    filters=[{"column": "entry_type", "op": "in", "value": ["earn", "bonus"]}],
                ),
                metric(
                    "points_redeemed",
                    "Points redeemed",
                    "Points used for vouchers (shown as a negative number).",
                    "sum",
                    "points",
                    filters=[{"column": "entry_type", "op": "=", "value": "redeem"}],
                ),
                metric(
                    "redemption_count",
                    "Redemptions",
                    "Number of voucher redemptions.",
                    "count",
                    "entry_id",
                    filters=[{"column": "entry_type", "op": "=", "value": "redeem"}],
                ),
            ],
        )
        return members_t, points_t

    def gift_cards(self) -> Table:
        rows = []
        for n in range(1, 241):
            store = self.rng.choice(self.stores)["store_id"]
            day = START + timedelta(days=self.rng.randint(0, 119))
            while not self.open_on(store, day):
                day += timedelta(days=7)
            value = float(self.rng.choice([25, 50, 50, 75, 100, 150, 200]))
            used = self.rng.random()
            balance = 0.0 if used < 0.35 else round(value * self.rng.uniform(0.1, 1.0), 2)
            status = "redeemed" if balance == 0 else "active"
            rows.append([f"GC-{n:04d}", day, store, value, balance, status])
        return table(
            "gift_cards",
            "Gift cards",
            """Gift cards sold in 2026. Selling a gift card is not a sale of goods: it is
            money held for the customer until the card is used, so gift cards are never part
            of net sales.""",
            "One row per gift card.",
            ["card_id"],
            [
                col("card_id", "string", "id", "Card number."),
                col("issued_date", "date", "time", "When the card was sold."),
                col("store_id", "string", "id", "Store or web shop that sold it."),
                col("initial_value", "decimal", "measure", "Value loaded at sale, EUR.", unit=EUR),
                col(
                    "balance", "decimal", "measure", "Unused value on 30 April 2026, EUR.", unit=EUR
                ),
                dim("status", "active or redeemed (balance used up).", ["active", "redeemed"]),
            ],
            rows,
            time_column="issued_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric("gift_cards_sold", "Gift cards sold", "Cards sold.", "count", "card_id"),
                metric(
                    "gift_card_value_sold",
                    "Gift card value sold",
                    "Value loaded onto cards sold (not revenue).",
                    "sum",
                    "initial_value",
                    EUR,
                ),
                metric(
                    "gift_card_liability",
                    "Gift card liability",
                    "Unused balance still owed to card holders.",
                    "sum",
                    "balance",
                    EUR,
                ),
            ],
        )

    def nps_responses(self) -> Table:
        topics = ["Product quality", "Staff", "Delivery", "Price", "Store layout", "Website"]
        rows, n = [], 0
        for t in self.transactions:
            if t["line_no"] != "1" or t["is_cancellation"] == "true" or self.rng.random() > 0.45:
                continue
            n += 1
            day = datetime.fromisoformat(t["invoice_date"]).date() + timedelta(
                days=self.rng.randint(1, 6)
            )
            if day > END:
                continue
            score = min(10, max(0, int(self.rng.gauss(8.0, 2.0))))
            online = t["store_id"] in {s["store_id"] for s in self.online}
            rows.append(
                [
                    f"NPS-{n:04d}",
                    day,
                    t["store_id"],
                    t["customer_id"] or None,
                    score,
                    "email" if online else self.rng.choice(["email", "in_store"]),
                    self.rng.choice(topics),
                ]
            )
        return table(
            "nps_responses",
            "Customer NPS survey responses",
            """Net Promoter Score survey answers from customers after a purchase: "How likely
            are you to recommend us?" from 0 to 10. Promoters score 9-10, detractors 0-6. NPS
            is the percentage of promoters minus the percentage of detractors. Not the support
            CSAT rating, and not the staff survey.""",
            "One row per survey response.",
            ["response_id"],
            [
                col("response_id", "string", "id", "Response."),
                col("response_date", "date", "time", "When the customer answered."),
                col("store_id", "string", "id", "Store or web shop of the purchase."),
                col("customer_id", "string", "id", "Customer, if known.", nullable=True),
                col("score", "integer", "measure", "Likelihood to recommend, 0-10."),
                dim("survey_channel", "email or in_store.", ["email", "in_store"]),
                dim("main_topic", "What the customer commented on most.", topics),
            ],
            rows,
            time_column="response_date",
            relationships=[
                link("store_id", "stores.store_id"),
                link("customer_id", "customers.customer_id"),
            ],
            metrics=[
                metric(
                    "nps_response_count",
                    "NPS responses",
                    "Survey responses.",
                    "count",
                    "response_id",
                ),
                metric(
                    "average_nps_score",
                    "Average NPS score",
                    "Average 0-10 score (not NPS itself).",
                    "avg",
                    "score",
                ),
                metric(
                    "promoter_count",
                    "Promoters",
                    "Responses scoring 9 or 10.",
                    "count",
                    "response_id",
                    filters=[{"column": "score", "op": ">=", "value": 9}],
                ),
                metric(
                    "detractor_count",
                    "Detractors",
                    "Responses scoring 0 to 6.",
                    "count",
                    "response_id",
                    filters=[{"column": "score", "op": "<=", "value": 6}],
                ),
            ],
            derived=[
                {
                    "name": "net_promoter_score",
                    "title": "Net Promoter Score",
                    "description": "Percentage of promoters minus percentage of detractors (as a fraction).",
                    "formula": "(promoter_count - detractor_count) / nps_response_count",
                }
            ],
        )

    # ------------------------------------------------------------------ marketing and web

    def marketing(self) -> tuple[Table, Table, Table]:
        campaigns = [
            (
                "CMP-01",
                "New year refresh",
                "email",
                date(2026, 1, 2),
                date(2026, 1, 31),
                400.0,
                "Retention",
                None,
                None,
            ),
            (
                "CMP-02",
                "Winter lighting",
                "social",
                date(2026, 1, 10),
                date(2026, 2, 15),
                650.0,
                "Awareness",
                "Lighting",
                None,
            ),
            (
                "CMP-03",
                "Brand search",
                "search",
                date(2026, 1, 1),
                date(2026, 4, 30),
                1200.0,
                "Acquisition",
                None,
                None,
            ),
            (
                "CMP-04",
                "Kitchen essentials",
                "display",
                date(2026, 2, 1),
                date(2026, 2, 28),
                350.0,
                "Acquisition",
                "Kitchen",
                None,
            ),
            (
                "CMP-05",
                "Spring textiles",
                "email",
                date(2026, 4, 1),
                date(2026, 4, 30),
                300.0,
                "Promotion",
                "Textiles",
                "SPRING10",
            ),
            (
                "CMP-06",
                "Spring textiles social",
                "social",
                date(2026, 4, 1),
                date(2026, 4, 30),
                550.0,
                "Promotion",
                "Textiles",
                "SPRING10",
            ),
            (
                "CMP-07",
                "France launch",
                "search",
                date(2026, 3, 2),
                date(2026, 4, 30),
                900.0,
                "Acquisition",
                None,
                None,
            ),
            (
                "CMP-08",
                "Furniture showcase",
                "display",
                date(2026, 3, 1),
                date(2026, 3, 31),
                450.0,
                "Awareness",
                "Furniture",
                None,
            ),
        ]
        camp_rows = [list(c) for c in campaigns]
        # Ad platforms credit a real online order to any campaign the customer clicked
        # recently, so one order is often credited to several campaigns at once.
        online_ids = {st["store_id"] for st in self.online}
        orders_on: dict[date, list[float]] = {}
        for order in self.checkout.values():
            if order["store_id"] in online_ids:
                orders_on.setdefault(order["day"], []).append(order["value"])
        spend = []
        for c in campaigns:
            cid, _, channel, start, end, budget = c[:6]
            n_days = (end - start).days + 1
            for d in days(start, end):
                s = round(budget / n_days * self.rng.uniform(0.7, 1.25), 2)
                impressions = int(s * self.rng.uniform(80, 220))
                clicks = int(impressions * self.rng.uniform(0.005, 0.03))
                credited = [v for v in orders_on.get(d, []) if self.rng.random() < 0.35]
                revenue = round(sum(credited) * 1.2, 2)  # incl. VAT, before cancellations
                spend.append([d, cid, impressions, clicks, s, len(credited), revenue])
        sends, n = [], 0
        for c in campaigns:
            if c[2] != "email":
                continue
            for week in range(4):
                n += 1
                day = c[3] + timedelta(days=7 * week)
                for seg in ("NEW", "REG", "VIP", "LAP"):
                    recipients = self.segment_sizes.get(seg, 0)
                    if recipients == 0:
                        continue
                    delivered = int(recipients * self.rng.uniform(0.95, 0.99))
                    opens = int(delivered * self.rng.uniform(0.18, 0.45))
                    clicks = int(opens * self.rng.uniform(0.05, 0.2))
                    unsub = int(delivered * self.rng.uniform(0.001, 0.006))
                    sends.append(
                        [
                            f"ES-{n:02d}-{seg}",
                            c[0],
                            day,
                            seg,
                            recipients,
                            delivered,
                            opens,
                            clicks,
                            unsub,
                        ]
                    )
        channels = ["email", "social", "search", "display"]
        campaigns_t = table(
            "marketing_campaigns",
            "Marketing campaigns",
            """Marketing campaigns: channel, dates, budget and objective. A campaign may push a
            promotion code; the promotion itself (discount and dates) is in promotions.""",
            "One row per campaign.",
            ["campaign_id"],
            [
                col("campaign_id", "string", "id", "Campaign."),
                dim("campaign_name", "Campaign name."),
                dim("channel", "Marketing channel.", channels),
                col("start_date", "date", "time", "First day."),
                col("end_date", "date", "dimension", "Last day."),
                col("budget", "decimal", "measure", "Planned media budget, EUR.", unit=EUR),
                dim(
                    "objective",
                    "Awareness, Acquisition, Retention or Promotion.",
                    ["Awareness", "Acquisition", "Retention", "Promotion"],
                ),
                dim("category", "Product category featured, if any.", nullable=True),
                col("promo_code", "string", "id", "Promotion code pushed, if any.", nullable=True),
            ],
            camp_rows,
            time_column="start_date",
            relationships=[link("promo_code", "promotions.promo_code")],
            metrics=[
                metric(
                    "campaign_count", "Campaigns", "Number of campaigns.", "count", "campaign_id"
                ),
                metric(
                    "campaign_budget",
                    "Campaign budget",
                    "Planned media budget.",
                    "sum",
                    "budget",
                    EUR,
                ),
            ],
        )
        spend_t = table(
            "campaign_spend_daily",
            "Campaign spend and results by day",
            """Daily media spend and ad-platform results per campaign. Attributed revenue is
            what the ad platforms credit to the campaign: campaigns overlap and it is measured
            before cancellations and returns, so it is NOT net sales and adding it up across
            campaigns over-counts.""",
            "One row per campaign per day.",
            ["spend_date", "campaign_id"],
            [
                col("spend_date", "date", "time", "Day."),
                col("campaign_id", "string", "id", "Campaign."),
                col("impressions", "integer", "measure", "Times an ad was shown."),
                col("clicks", "integer", "measure", "Clicks on ads."),
                col("spend", "decimal", "measure", "Media spend that day, EUR.", unit=EUR),
                col(
                    "attributed_orders",
                    "integer",
                    "measure",
                    "Orders the ad platform credits to the campaign.",
                ),
                col(
                    "attributed_revenue",
                    "decimal",
                    "measure",
                    "Revenue the ad platform credits to the campaign, EUR (not net sales).",
                    unit=EUR,
                ),
            ],
            spend,
            time_column="spend_date",
            relationships=[link("campaign_id", "marketing_campaigns.campaign_id")],
            metrics=[
                metric("media_spend", "Media spend", "Money spent on ads.", "sum", "spend", EUR),
                metric("impressions", "Impressions", "Ads shown.", "sum", "impressions"),
                metric("ad_clicks", "Ad clicks", "Clicks on ads.", "sum", "clicks"),
                metric(
                    "attributed_revenue",
                    "Attributed revenue",
                    "Revenue credited to campaigns by ad platforms (overlapping; not net sales).",
                    "sum",
                    "attributed_revenue",
                    EUR,
                ),
            ],
            derived=[
                {
                    "name": "click_through_rate",
                    "title": "Click-through rate",
                    "description": "Clicks divided by impressions.",
                    "formula": "ad_clicks / impressions",
                }
            ],
        )
        email_t = table(
            "email_sends",
            "Email campaign sends",
            """Marketing emails sent, by campaign and customer segment: how many were
            delivered, opened and clicked, and how many people unsubscribed.""",
            "One row per send per segment.",
            ["send_id"],
            [
                col("send_id", "string", "id", "Send."),
                col("campaign_id", "string", "id", "Campaign."),
                col("send_date", "date", "time", "When it was sent."),
                col("segment_code", "string", "id", "Customer segment sent to."),
                col("recipients", "integer", "measure", "Addresses sent to."),
                col("delivered", "integer", "measure", "Emails delivered."),
                col("opens", "integer", "measure", "Unique opens."),
                col("clicks", "integer", "measure", "Unique clicks."),
                col("unsubscribes", "integer", "measure", "People who unsubscribed."),
            ],
            sends,
            time_column="send_date",
            relationships=[
                link("campaign_id", "marketing_campaigns.campaign_id"),
                link("segment_code", "customer_segments.segment_code"),
            ],
            metrics=[
                metric(
                    "emails_delivered", "Emails delivered", "Emails delivered.", "sum", "delivered"
                ),
                metric("email_opens", "Email opens", "Unique opens.", "sum", "opens"),
                metric("email_clicks", "Email clicks", "Unique clicks.", "sum", "clicks"),
                metric(
                    "unsubscribes",
                    "Unsubscribes",
                    "People who unsubscribed.",
                    "sum",
                    "unsubscribes",
                ),
            ],
            derived=[
                {
                    "name": "email_open_rate",
                    "title": "Email open rate",
                    "description": "Opens divided by emails delivered.",
                    "formula": "email_opens / emails_delivered",
                }
            ],
        )
        return campaigns_t, spend_t, email_t

    def web(self) -> tuple[Table, Table, Table]:
        devices = ["desktop", "mobile", "tablet"]
        sources = ["organic", "paid", "email", "direct"]
        device_share = {"desktop": 0.38, "mobile": 0.52, "tablet": 0.10}
        source_share = {"organic": 0.4, "paid": 0.25, "email": 0.1, "direct": 0.25}
        sessions_rows, views_rows, searches_rows = [], [], []
        terms = [
            "lamp",
            "desk lamp",
            "oak table",
            "rug",
            "linen",
            "kettle",
            "mug",
            "mirror",
            "candle",
            "bookshelf",
            "sofa",
            "curtains",
            "gift card",
            "delivery",
            "returns",
            "vase",
        ]
        catalogue = {"sofa", "curtains"}  # searched for, not sold
        cells = [(dev, src) for dev in devices for src in sources]
        weights = [device_share[dev] * source_share[src] for dev, src in cells]
        checkout_on: dict[tuple[str, date], list[float]] = {}
        for order in self.checkout.values():
            checkout_on.setdefault((order["store_id"], order["day"]), []).append(order["value"])
        for d in days():
            for s in self.online:
                sid = s["store_id"]
                if not self.open_on(sid, d):
                    continue
                # Every real order placed that day, as the browser saw it at checkout (incl.
                # VAT, before any cancellation), lands in one device/source cell.
                tracked: dict[tuple[str, str], list[float]] = {}
                for value in checkout_on.get((sid, d), []):
                    cell = self.rng.choices(cells, weights)[0]
                    tracked.setdefault(cell, []).append(value * 1.2)
                base = 40 if sid != "S08" else 25
                for dev, src in cells:
                    share = device_share[dev] * source_share[src]
                    sessions = max(1, round(base * share * self.rng.uniform(0.6, 1.4)))
                    orders = tracked.get((dev, src), [])
                    sessions = max(sessions, 3 * len(orders))
                    users = max(1, round(sessions * self.rng.uniform(0.75, 0.95)))
                    bounces = round(sessions * self.rng.uniform(0.3, 0.6))
                    revenue = round(sum(orders), 2)
                    sessions_rows.append(
                        [d, sid, dev, src, sessions, users, bounces, len(orders), revenue]
                    )
                    for p in self.products:
                        views = sum(self.rng.random() < 0.12 for _ in range(sessions))
                        if views == 0:
                            continue
                        carts = sum(self.rng.random() < 0.08 for _ in range(views))
                        views_rows.append([d, sid, p["product_id"], dev, src, views, carts])
                for term in self.rng.sample(terms, 8):
                    n = self.rng.randint(3, 60)
                    found = term not in catalogue
                    searches_rows.append(
                        [
                            d,
                            sid,
                            term,
                            n,
                            found,
                            int(n * self.rng.uniform(0.2, 0.7)) if found else 0,
                        ]
                    )
        sessions_t = table(
            "web_sessions_daily",
            "Web shop sessions by day",
            """Web analytics for the three web shops: visits (sessions), visitors, bounces,
            orders and revenue as tracked in the browser. Online revenue here includes VAT and
            is counted when the order is placed, before cancellations and returns, so it does
            not match net sales. Use it for traffic and conversion, not for revenue.""",
            "One row per web shop, day, device and traffic source.",
            ["session_date", "store_id", "device", "traffic_source"],
            [
                col("session_date", "date", "time", "Day."),
                col("store_id", "string", "id", "Web shop."),
                dim("device", "desktop, mobile or tablet.", devices),
                dim("traffic_source", "Where visitors came from.", sources),
                col("sessions", "integer", "measure", "Visits."),
                col(
                    "users",
                    "integer",
                    "measure",
                    "Distinct visitors that day (not additive across days).",
                ),
                col("bounces", "integer", "measure", "Visits that left after one page."),
                col("orders", "integer", "measure", "Orders placed in the browser."),
                col(
                    "online_revenue",
                    "decimal",
                    "measure",
                    "Browser-tracked order value incl. VAT, EUR (not net sales).",
                    unit=EUR,
                ),
            ],
            sessions_rows,
            time_column="session_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric("sessions", "Web sessions", "Visits to the web shops.", "sum", "sessions"),
                metric(
                    "web_orders",
                    "Web orders (analytics)",
                    "Orders tracked by web analytics.",
                    "sum",
                    "orders",
                ),
                metric("bounces", "Bounces", "Single-page visits.", "sum", "bounces"),
                metric(
                    "online_revenue",
                    "Online revenue (analytics)",
                    "Browser-tracked order value incl. VAT, before cancellations (not net sales).",
                    "sum",
                    "online_revenue",
                    EUR,
                ),
            ],
            derived=[
                {
                    "name": "conversion_rate",
                    "title": "Web conversion rate",
                    "description": "Web orders divided by sessions.",
                    "formula": "web_orders / sessions",
                },
                {
                    "name": "bounce_rate",
                    "title": "Bounce rate",
                    "description": "Bounces divided by sessions.",
                    "formula": "bounces / sessions",
                },
            ],
        )
        views_t = table(
            "product_page_views",
            "Product page views",
            """How often each product page was viewed in the web shops, and how often the
            product was added to the basket, by day, device and traffic source.""",
            "One row per web shop, day, product, device and traffic source.",
            ["view_date", "store_id", "product_id", "device", "traffic_source"],
            [
                col("view_date", "date", "time", "Day."),
                col("store_id", "string", "id", "Web shop."),
                col("product_id", "string", "id", "Product."),
                dim("device", "desktop, mobile or tablet.", devices),
                dim("traffic_source", "Where visitors came from.", sources),
                col("page_views", "integer", "measure", "Views of the product page."),
                col(
                    "add_to_cart",
                    "integer",
                    "measure",
                    "Times the product was added to the basket.",
                ),
            ],
            views_rows,
            time_column="view_date",
            relationships=[
                link("store_id", "stores.store_id"),
                link("product_id", "products.product_id"),
            ],
            metrics=[
                metric(
                    "page_views",
                    "Product page views",
                    "Views of product pages.",
                    "sum",
                    "page_views",
                ),
                metric(
                    "add_to_cart",
                    "Add to basket",
                    "Times products were added to the basket.",
                    "sum",
                    "add_to_cart",
                ),
            ],
        )
        searches_t = table(
            "site_searches",
            "Web shop searches",
            """What visitors typed into the web shop search box, how often, and whether any
            product was found. Searches with no results show demand for things we don't sell.""",
            "One row per web shop, day and search term.",
            ["search_date", "store_id", "search_term"],
            [
                col("search_date", "date", "time", "Day."),
                col("store_id", "string", "id", "Web shop."),
                dim("search_term", "What was searched for."),
                col("searches", "integer", "measure", "Number of searches."),
                col(
                    "results_found", "boolean", "dimension", "Whether the search found any product."
                ),
                col("result_clicks", "integer", "measure", "Clicks on a search result."),
            ],
            searches_rows,
            time_column="search_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric(
                    "searches", "Site searches", "Searches in the web shops.", "sum", "searches"
                ),
                metric(
                    "zero_result_searches",
                    "Searches with no results",
                    "Searches that found no product.",
                    "sum",
                    "searches",
                    filters=[{"column": "results_found", "op": "=", "value": False}],
                ),
            ],
        )
        return sessions_t, views_t, searches_t

    # ------------------------------------------------------------------ catalogue and pricing

    def catalogue(self) -> tuple[Table, Table, Table, Table, Table]:
        managers = {
            "Lighting": "Priya N.",
            "Kitchen": "Tomás R.",
            "Textiles": "Ingrid S.",
            "Furniture": "Karl W.",
            "Decor": "Amélie D.",
        }
        targets = {
            "Lighting": 52.0,
            "Kitchen": 48.0,
            "Textiles": 55.0,
            "Furniture": 42.0,
            "Decor": 58.0,
        }
        window = {"Lighting": 30, "Kitchen": 30, "Textiles": 30, "Furniture": 30, "Decor": 30}
        cats = [
            [c, managers[c], "Home" if c != "Furniture" else "Furniture", targets[c], window[c]]
            for c in managers
        ]
        cats_t = table(
            "product_categories",
            "Product categories",
            """The five product categories: who manages each, the department, the gross margin
            target and the returns window in days.""",
            "One row per category.",
            ["category"],
            [
                dim("category", "Product category."),
                dim("category_manager", "Buyer responsible for the category."),
                dim("department", "Home or Furniture.", ["Home", "Furniture"]),
                col("margin_target_pct", "decimal", "attribute", "Gross margin target, percent."),
                col(
                    "returns_window_days",
                    "integer",
                    "attribute",
                    "Days customers have to return items.",
                ),
            ],
            cats,
            relationships=[],
        )
        changes, n = [], 0
        for p in self.products:
            if self.rng.random() < 0.45:
                n += 1
                old = self.price[p["product_id"]]
                reason = self.rng.choice(["cost increase", "competitor price", "clearance"])
                factor = {"cost increase": 1.08, "competitor price": 0.95, "clearance": 0.7}[reason]
                new = round(old * factor, 2)
                day = START + timedelta(days=self.rng.randint(5, 110))
                changes.append(
                    [
                        f"PC-{n:03d}",
                        p["product_id"],
                        day,
                        old,
                        new,
                        reason,
                        self.rng.choice(list(managers.values())),
                    ]
                )
        changes_t = table(
            "price_changes",
            "Price changes",
            """Changes to products' shelf prices: old and new price, why, and who approved
            it. Prices actually charged on each sale (after promotions) are on transactions.""",
            "One row per price change.",
            ["change_id"],
            [
                col("change_id", "string", "id", "Change."),
                col("product_id", "string", "id", "Product."),
                col("effective_date", "date", "time", "First day of the new price."),
                col("old_price", "decimal", "attribute", "Price before, EUR."),
                col("new_price", "decimal", "attribute", "Price after, EUR."),
                dim(
                    "reason",
                    "cost increase, competitor price or clearance.",
                    ["cost increase", "competitor price", "clearance"],
                ),
                dim("approved_by", "Category manager who approved it."),
            ],
            changes,
            time_column="effective_date",
            relationships=[link("product_id", "products.product_id")],
            metrics=[
                metric(
                    "price_change_count",
                    "Price changes",
                    "Number of price changes.",
                    "count",
                    "change_id",
                )
            ],
        )
        po_cost = {}
        for po in self.purchase_orders:
            po_cost.setdefault(po["product_id"], []).append(float(po["unit_cost"]))
        supplier_for = {}
        for s in self.suppliers:
            if s["is_preferred"] == "true":
                supplier_for[s["category"]] = s["supplier_id"]
        costs = []
        for m in MONTHS:
            for p in self.products:
                pid = p["product_id"]
                base = (
                    (sum(po_cost[pid]) / len(po_cost[pid]))
                    if pid in po_cost
                    else self.price[pid] * 0.45
                )
                unit = round(base * self.rng.uniform(0.97, 1.05), 2)
                freight = round(unit * self.rng.uniform(0.04, 0.12), 2)
                costs.append(
                    [m, pid, supplier_for[p["category"]], unit, freight, round(unit + freight, 2)]
                )
        costs_t = table(
            "product_costs",
            "Product landed costs by month",
            """Finance's landed cost per unit for each product and month: purchase cost plus
            freight. This is the cost used for margin reporting. Purchase orders show the
            invoice price of each order, without freight.""",
            "One row per product per month.",
            ["cost_month", "product_id"],
            [
                col("cost_month", "date", "time", "First day of the month."),
                col("product_id", "string", "id", "Product."),
                col("supplier_id", "string", "id", "Main supplier."),
                col("unit_cost", "decimal", "measure", "Purchase cost per unit, EUR.", unit=EUR),
                col("freight_cost", "decimal", "measure", "Freight per unit, EUR.", unit=EUR),
                col(
                    "landed_cost",
                    "decimal",
                    "measure",
                    "Purchase cost plus freight per unit, EUR.",
                    unit=EUR,
                ),
            ],
            costs,
            readers=["finance"],
            time_column="cost_month",
            relationships=[
                link("product_id", "products.product_id"),
                link("supplier_id", "suppliers.supplier_id"),
            ],
            metrics=[
                metric(
                    "average_landed_cost",
                    "Average landed cost",
                    "Average purchase plus freight cost per unit.",
                    "avg",
                    "landed_cost",
                    EUR,
                ),
                metric(
                    "average_freight_cost",
                    "Average freight cost",
                    "Average freight per unit.",
                    "avg",
                    "freight_cost",
                    EUR,
                ),
            ],
        )
        reviews, n = [], 0
        topics = ["Quality", "Value", "Delivery", "Looks", "Size"]
        for d in days():
            for _ in range(self.rng.randint(0, 4)):
                n += 1
                p = self.rng.choice(self.products)
                store = self.rng.choice([s for s in self.online if self.open_on(s["store_id"], d)])
                rating = min(
                    5,
                    max(
                        1, round(self.rng.gauss(4.1 if p["category"] != "Furniture" else 3.7, 1.0))
                    ),
                )
                reviews.append(
                    [
                        f"RV-{n:04d}",
                        p["product_id"],
                        d,
                        store["store_id"],
                        rating,
                        self.rng.random() < 0.8,
                        self.rng.randint(0, 30),
                        self.rng.choice(topics),
                    ]
                )
        reviews_t = table(
            "product_reviews",
            "Product reviews",
            """Star ratings and reviews customers left on the web shops (1 to 5 stars), and
            whether the reviewer bought the product.""",
            "One row per review.",
            ["review_id"],
            [
                col("review_id", "string", "id", "Review."),
                col("product_id", "string", "id", "Product."),
                col("review_date", "date", "time", "When it was posted."),
                col("store_id", "string", "id", "Web shop."),
                col("rating", "integer", "measure", "Stars, 1-5."),
                col("verified_purchase", "boolean", "dimension", "Whether the reviewer bought it."),
                col("helpful_votes", "integer", "measure", "Other customers who found it helpful."),
                dim("topic", "Main subject of the review.", topics),
            ],
            reviews,
            time_column="review_date",
            relationships=[
                link("product_id", "products.product_id"),
                link("store_id", "stores.store_id"),
            ],
            metrics=[
                metric("review_count", "Reviews", "Reviews posted.", "count", "review_id"),
                metric("average_rating", "Average rating", "Average stars (1-5).", "avg", "rating"),
            ],
        )
        colours = ["white", "black", "natural", "blue", "green", "grey", "brass"]
        materials = {
            "Lighting": ["metal", "paper", "brass"],
            "Kitchen": ["stoneware", "enamel", "wood", "glass", "cast iron"],
            "Textiles": ["linen", "wool", "cotton", "jute"],
            "Furniture": ["oak", "rattan", "pine", "walnut"],
            "Decor": ["glass", "ceramic", "wax", "paper"],
        }
        origins = [
            "Poland",
            "Italy",
            "Portugal",
            "Sweden",
            "Turkey",
            "Germany",
            "Lithuania",
            "Spain",
            "Netherlands",
        ]
        attrs = []
        for p in self.products:
            big = p["category"] == "Furniture"
            attrs.append(
                [
                    p["product_id"],
                    self.rng.choice(colours),
                    self.rng.choice(materials[p["category"]]),
                    round(self.rng.uniform(40, 160) if big else self.rng.uniform(8, 60), 1),
                    round(self.rng.uniform(30, 80) if big else self.rng.uniform(8, 50), 1),
                    round(self.rng.uniform(40, 180) if big else self.rng.uniform(5, 60), 1),
                    round(self.rng.uniform(6, 40) if big else self.rng.uniform(0.2, 4), 2),
                    self.rng.choice(origins),
                    self.rng.random() < 0.3,
                ]
            )
        attrs_t = table(
            "product_attributes",
            "Product attributes",
            """Physical details of each product: colour, main material, size, weight, country
            of origin, and whether it needs two-person delivery.""",
            "One row per product.",
            ["product_id"],
            [
                col("product_id", "string", "id", "Product."),
                dim("colour", "Main colour."),
                dim("material", "Main material."),
                col("width_cm", "decimal", "attribute", "Width, cm."),
                col("depth_cm", "decimal", "attribute", "Depth, cm."),
                col("height_cm", "decimal", "attribute", "Height, cm."),
                col("weight_kg", "decimal", "measure", "Weight, kg."),
                dim("country_of_origin", "Where it is made."),
                col("two_person_delivery", "boolean", "dimension", "Needs two people to deliver."),
            ],
            attrs,
            relationships=[link("product_id", "products.product_id")],
            metrics=[
                metric(
                    "average_weight_kg",
                    "Average weight",
                    "Average product weight, kg.",
                    "avg",
                    "weight_kg",
                )
            ],
        )
        return cats_t, changes_t, costs_t, reviews_t, attrs_t

    # ------------------------------------------------------------------ store operations

    def store_ops(self) -> list[Table]:
        weather = ["dry", "rain", "cold", "warm"]
        orders_per_day = {}
        for t in self.transactions:
            key = (t["store_id"], datetime.fromisoformat(t["invoice_date"]).date())
            orders_per_day.setdefault(key, set()).add(t["invoice_no"])
        footfall = []
        for d in days():
            for s in self.retail:
                sid = s["store_id"]
                if not self.open_on(sid, d):
                    continue
                base = {"S01": 70, "S03": 30, "S04": 40, "S06": 35, "S07": 30}.get(sid, 30)
                weekend = 1.35 if d.weekday() >= 5 else 1.0
                visitors = int(base * weekend * self.rng.uniform(0.8, 1.2))
                footfall.append(
                    [
                        d,
                        sid,
                        visitors,
                        self.rng.choice([11, 12, 13, 15, 16, 17]),
                        self.rng.choice(weather),
                    ]
                )
        footfall_t = table(
            "store_footfall_daily",
            "Store footfall by day",
            """People entering each physical store per day, counted by door sensors. Footfall
            counts visitors, not customers or orders: most visitors buy nothing. Web shops
            have no footfall (see web sessions).""",
            "One row per physical store per day.",
            ["footfall_date", "store_id"],
            [
                col("footfall_date", "date", "time", "Day."),
                col("store_id", "string", "id", "Physical store."),
                col("visitors", "integer", "measure", "People who entered."),
                col("peak_hour", "integer", "attribute", "Busiest hour (24-hour clock)."),
                dim("weather", "Local weather that day.", weather),
            ],
            footfall,
            time_column="footfall_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric(
                    "visitors",
                    "Store visitors",
                    "People entering physical stores (footfall).",
                    "sum",
                    "visitors",
                )
            ],
        )
        energy = []
        for m in MONTHS:
            for s in self.retail:
                if not self.open_on(s["store_id"], m):
                    continue
                elec = int(self.rng.uniform(9000, 22000) * (1.2 if m.month <= 2 else 1.0))
                gas = int(self.rng.uniform(2000, 9000) * (1.5 if m.month <= 2 else 0.8))
                cost = round(elec * 0.27 + gas * 0.09, 2)
                energy.append(
                    [m, s["store_id"], elec, gas, cost, round(self.rng.uniform(20, 85), 1)]
                )
        energy_t = table(
            "energy_usage",
            "Store energy use by month",
            "Electricity and gas used by each physical store per month, the cost, and the renewable share.",
            "One row per physical store per month.",
            ["usage_month", "store_id"],
            [
                col("usage_month", "date", "time", "First day of the month."),
                col("store_id", "string", "id", "Physical store."),
                col("electricity_kwh", "integer", "measure", "Electricity used, kWh."),
                col("gas_kwh", "integer", "measure", "Gas used, kWh."),
                col("energy_cost", "decimal", "measure", "Energy bill, EUR.", unit=EUR),
                col(
                    "renewable_pct",
                    "decimal",
                    "attribute",
                    "Share of electricity from renewables, percent.",
                ),
            ],
            energy,
            time_column="usage_month",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric(
                    "electricity_kwh",
                    "Electricity used",
                    "Electricity, kWh.",
                    "sum",
                    "electricity_kwh",
                ),
                metric(
                    "energy_cost", "Energy cost", "Energy bills, EUR.", "sum", "energy_cost", EUR
                ),
            ],
        )
        staff_by_store = {}
        for e in self.staff:
            staff_by_store.setdefault(e["store_id"], []).append(e["employee_id"])
        kinds = ["slip or fall", "theft", "damaged stock", "fire alarm", "aggressive customer"]
        incidents = []
        for n in range(1, 74):
            s = self.rng.choice(self.retail)
            sid = s["store_id"]
            d = START + timedelta(days=self.rng.randint(0, 119))
            if not self.open_on(sid, d):
                continue
            kind = self.rng.choice(kinds)
            injuries = (
                self.rng.choice([0, 0, 0, 1])
                if kind in {"slip or fall", "aggressive customer"}
                else 0
            )
            loss = (
                round(self.rng.uniform(20, 900), 2) if kind in {"theft", "damaged stock"} else 0.0
            )
            severity = "high" if injuries or loss > 600 else self.rng.choice(["low", "medium"])
            reporter = self.rng.choice(staff_by_store.get(sid, [None]))
            incidents.append([f"INC-{n:03d}", sid, d, kind, severity, injuries, loss, reporter])
        incidents_t = table(
            "store_incidents",
            "Store incidents",
            """Safety and security incidents in physical stores: what happened, how serious,
            injuries, and the value of goods lost.""",
            "One row per incident.",
            ["incident_id"],
            [
                col("incident_id", "string", "id", "Incident."),
                col("store_id", "string", "id", "Store."),
                col("incident_date", "date", "time", "When it happened."),
                dim("incident_type", "What happened.", kinds),
                dim("severity", "low, medium or high.", ["low", "medium", "high"]),
                col("injuries", "integer", "measure", "People hurt."),
                col("loss_value", "decimal", "measure", "Goods lost or damaged, EUR.", unit=EUR),
                col("reported_by", "string", "id", "Employee who reported it.", nullable=True),
            ],
            incidents,
            time_column="incident_date",
            relationships=[
                link("store_id", "stores.store_id"),
                link("reported_by", "staff.employee_id"),
            ],
            metrics=[
                metric("incident_count", "Incidents", "Store incidents.", "count", "incident_id"),
                metric("injuries", "Injuries", "People injured.", "sum", "injuries"),
                metric(
                    "incident_loss_value",
                    "Incident losses",
                    "Goods lost or damaged in incidents.",
                    "sum",
                    "loss_value",
                    EUR,
                ),
            ],
        )
        cats = ["HVAC", "lighting", "plumbing", "IT", "fixtures", "doors and locks"]
        contractors = ["Klimaservice GmbH", "BrightFix", "PipeRight", "Tekhelp", "ShopFit Europe"]
        maint = []
        for n in range(1, 131):
            s = self.rng.choice(self.retail)
            sid = s["store_id"]
            opened = START + timedelta(days=self.rng.randint(0, 119))
            if not self.open_on(sid, opened):
                continue
            priority = self.rng.choice(["urgent", "normal", "normal", "low"])
            close_days = {"urgent": 2, "normal": 9, "low": 20}[priority] + self.rng.randint(-1, 8)
            closed = opened + timedelta(days=max(0, close_days))
            if closed > END:
                closed = None
            cost = round(self.rng.uniform(80, 2400), 2) if closed else None
            maint.append(
                [
                    f"MR-{n:03d}",
                    sid,
                    opened,
                    closed,
                    self.rng.choice(cats),
                    priority,
                    self.rng.choice(contractors),
                    cost,
                    "closed" if closed else "open",
                ]
            )
        maint_t = table(
            "maintenance_requests",
            "Store maintenance requests",
            """Repairs requested by stores: category, priority, contractor, cost once done, and
            whether still open. Cost is empty until the work is finished.""",
            "One row per request.",
            ["request_id"],
            [
                col("request_id", "string", "id", "Request."),
                col("store_id", "string", "id", "Store."),
                col("opened_date", "date", "time", "When requested."),
                col(
                    "closed_date",
                    "date",
                    "dimension",
                    "When completed. Empty while open.",
                    nullable=True,
                ),
                dim("category", "Type of repair.", cats),
                dim("priority", "urgent, normal or low.", ["urgent", "normal", "low"]),
                dim("contractor", "Contractor assigned."),
                col(
                    "cost",
                    "decimal",
                    "measure",
                    "Final cost, EUR. Empty while open.",
                    unit=EUR,
                    nullable=True,
                ),
                dim("status", "open or closed.", ["open", "closed"]),
            ],
            maint,
            time_column="opened_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric(
                    "maintenance_request_count",
                    "Maintenance requests",
                    "Repairs requested.",
                    "count",
                    "request_id",
                ),
                metric(
                    "open_maintenance_count",
                    "Open maintenance requests",
                    "Repairs not yet done.",
                    "count",
                    "request_id",
                    filters=[{"column": "status", "op": "=", "value": "open"}],
                ),
                metric(
                    "maintenance_cost",
                    "Maintenance cost",
                    "Cost of completed repairs.",
                    "sum",
                    "cost",
                    EUR,
                ),
            ],
        )
        audit_types = ["health and safety", "visual merchandising", "cash handling"]
        audits, n = [], 0
        for m in MONTHS:
            for s in self.retail:
                if not self.open_on(s["store_id"], m):
                    continue
                for a in audit_types:
                    if self.rng.random() < 0.6:
                        n += 1
                        score = round(self.rng.uniform(62, 99), 1)
                        audits.append(
                            [
                                f"AU-{n:03d}",
                                s["store_id"],
                                m + timedelta(days=self.rng.randint(3, 25)),
                                a,
                                score,
                                score >= 80,
                                self.rng.choice(["Area manager", "External auditor"]),
                            ]
                        )
        audits_t = table(
            "store_audits",
            "Store audits",
            "Compliance audits of physical stores and their scores. 80% or more is a pass.",
            "One row per audit.",
            ["audit_id"],
            [
                col("audit_id", "string", "id", "Audit."),
                col("store_id", "string", "id", "Store."),
                col("audit_date", "date", "time", "When audited."),
                dim("audit_type", "What was audited.", audit_types),
                col("score_pct", "decimal", "measure", "Score, percent."),
                col("passed", "boolean", "dimension", "80% or more."),
                dim("auditor", "Who audited.", ["Area manager", "External auditor"]),
            ],
            audits,
            time_column="audit_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric("audit_count", "Audits", "Audits carried out.", "count", "audit_id"),
                metric(
                    "average_audit_score",
                    "Average audit score",
                    "Average audit score, percent.",
                    "avg",
                    "score_pct",
                ),
                metric(
                    "failed_audit_count",
                    "Failed audits",
                    "Audits scoring under 80%.",
                    "count",
                    "audit_id",
                    filters=[{"column": "passed", "op": "=", "value": False}],
                ),
            ],
        )
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        hours = []
        for s in self.retail:
            for wd in weekdays:
                # Handbook: 09:00-20:00 Mon-Sat, 11:00-17:00 Sun, unless local rules differ.
                # Local rules: German stores close on Sunday; London opens late on Thursday.
                sunday_closed = s["country"] == "Germany" and wd == "Sunday"
                late = s["store_id"] == "S01" and wd == "Thursday"
                opens = None if sunday_closed else ("11:00" if wd == "Sunday" else "09:00")
                closes = (
                    None
                    if sunday_closed
                    else ("17:00" if wd == "Sunday" else "21:00" if late else "20:00")
                )
                hours.append([s["store_id"], wd, opens, closes, late, sunday_closed])
        hours_t = table(
            "opening_hours",
            "Store opening hours",
            "Regular opening hours of each physical store by weekday. German stores close on Sundays; London Flagship opens until 21:00 on Thursdays.",
            "One row per store per weekday.",
            ["store_id", "weekday"],
            [
                col("store_id", "string", "id", "Store."),
                dim("weekday", "Day of the week.", weekdays),
                col(
                    "opens",
                    "string",
                    "attribute",
                    "Opening time (HH:MM). Empty when closed.",
                    nullable=True,
                ),
                col(
                    "closes",
                    "string",
                    "attribute",
                    "Closing time (HH:MM). Empty when closed.",
                    nullable=True,
                ),
                col("late_opening", "boolean", "dimension", "Open late (until 21:00)."),
                col("closed_all_day", "boolean", "dimension", "Closed that weekday."),
            ],
            hours,
            relationships=[link("store_id", "stores.store_id")],
        )
        return [footfall_t, energy_t, incidents_t, maint_t, audits_t, hours_t]

    # ------------------------------------------------------------------ warehousing and logistics

    def logistics(self) -> list[Table]:
        warehouses = [
            ["WH1", "Midlands DC", "United Kingdom", 4200, "Northline Logistics", "UK & Ireland"],
            ["WH2", "Rhine DC", "Germany", 5600, "Kontor 3PL", "Central Europe"],
            ["WH3", "Zaragoza DC", "Spain", 3100, "Iberia Fulfilment", "Southwest Europe"],
        ]
        wh_t = table(
            "warehouses",
            "Distribution centres",
            "The three distribution centres that supply stores and web shops, run by logistics partners.",
            "One row per warehouse.",
            ["warehouse_id"],
            [
                col("warehouse_id", "string", "id", "Warehouse."),
                dim("warehouse_name", "Name."),
                dim("country", "Country."),
                col("capacity_pallets", "integer", "measure", "Pallet spaces."),
                dim("operator", "Logistics partner running it."),
                dim(
                    "serves_region",
                    "Region supplied.",
                    ["UK & Ireland", "Central Europe", "Southwest Europe"],
                ),
            ],
            warehouses,
            metrics=[
                metric(
                    "warehouse_capacity",
                    "Warehouse capacity",
                    "Pallet spaces.",
                    "sum",
                    "capacity_pallets",
                )
            ],
        )
        stock, monday = [], date(2026, 1, 5)
        weeks = []
        while monday <= END:
            weeks.append(monday)
            monday += timedelta(days=7)
        level = {
            (w[0], p["product_id"]): self.rng.randint(40, 400)
            for w in warehouses
            for p in self.products
        }
        for wk in weeks:
            for w in warehouses:
                for p in self.products:
                    key = (w[0], p["product_id"])
                    level[key] = max(0, level[key] + self.rng.randint(-40, 45))
                    reserved = min(level[key], self.rng.randint(0, 30))
                    stock.append(
                        [
                            wk,
                            w[0],
                            p["product_id"],
                            level[key],
                            reserved,
                            self.rng.choice([0, 0, 0, 1, 2]),
                        ]
                    )
        stock_t = table(
            "warehouse_stock",
            "Warehouse stock by week",
            """Units held in the distribution centres, counted every Monday. This is central
            warehouse stock only: stock in stores and web shops is in inventory snapshots.
            Point in time: never add it up across weeks.""",
            "One row per warehouse, product and Monday.",
            ["snapshot_week", "warehouse_id", "product_id"],
            [
                col("snapshot_week", "date", "time", "Monday of the count."),
                col("warehouse_id", "string", "id", "Warehouse."),
                col("product_id", "string", "id", "Product."),
                col("units_available", "integer", "measure", "Units in the warehouse."),
                col("units_reserved", "integer", "measure", "Units already promised to stores."),
                col("units_damaged", "integer", "measure", "Units damaged and unsellable."),
            ],
            stock,
            time_column="snapshot_week",
            relationships=[
                link("warehouse_id", "warehouses.warehouse_id"),
                link("product_id", "products.product_id"),
            ],
            metrics=[
                metric(
                    "warehouse_units_available",
                    "Warehouse units available",
                    "Units in distribution centres (point in time).",
                    "sum",
                    "units_available",
                ),
                metric(
                    "warehouse_units_reserved",
                    "Warehouse units reserved",
                    "Units promised to stores (point in time).",
                    "sum",
                    "units_reserved",
                ),
            ],
        )
        region_wh = {"UK & Ireland": "WH1", "Central Europe": "WH2", "Southwest Europe": "WH3"}
        transfers = []
        for n in range(1, 181):
            s = self.rng.choice(self.stores)
            req = START + timedelta(days=self.rng.randint(0, 115))
            if not self.open_on(s["store_id"], req):
                continue
            shipped = req + timedelta(days=self.rng.randint(1, 3))
            received = shipped + timedelta(days=self.rng.randint(1, 4))
            status = "received"
            if received > END:
                received, status = None, "in transit"
            if shipped > END:
                shipped, status = None, "requested"
            transfers.append(
                [
                    f"TR-{n:04d}",
                    self.rng.choice(self.products)["product_id"],
                    region_wh[s["region"]],
                    s["store_id"],
                    req,
                    shipped,
                    received,
                    self.rng.randint(2, 24),
                    status,
                ]
            )
        transfers_t = table(
            "stock_transfers",
            "Stock transfers to stores",
            "Stock moved from a distribution centre to a store or web shop: requested, shipped and received dates.",
            "One row per transfer.",
            ["transfer_id"],
            [
                col("transfer_id", "string", "id", "Transfer."),
                col("product_id", "string", "id", "Product."),
                col("from_warehouse_id", "string", "id", "Sending warehouse."),
                col("to_store_id", "string", "id", "Receiving store."),
                col("requested_date", "date", "time", "When the store asked."),
                col(
                    "shipped_date",
                    "date",
                    "dimension",
                    "When it left the warehouse.",
                    nullable=True,
                ),
                col(
                    "received_date",
                    "date",
                    "dimension",
                    "When the store received it.",
                    nullable=True,
                ),
                col("quantity", "integer", "measure", "Units."),
                dim(
                    "status",
                    "requested, in transit or received.",
                    ["requested", "in transit", "received"],
                ),
            ],
            transfers,
            time_column="requested_date",
            relationships=[
                link("product_id", "products.product_id"),
                link("from_warehouse_id", "warehouses.warehouse_id"),
                link("to_store_id", "stores.store_id"),
            ],
            metrics=[
                metric(
                    "transfer_count",
                    "Stock transfers",
                    "Transfers requested.",
                    "count",
                    "transfer_id",
                ),
                metric(
                    "units_transferred",
                    "Units transferred",
                    "Units moved to stores.",
                    "sum",
                    "quantity",
                ),
            ],
        )
        carriers = [  # names as on shipments; days as the shipping and delivery standards promise
            ["Parcelwise UK", "standard", "United Kingdom; Ireland", 4.90, 4, 250.0],
            ["Rhein Logistik", "standard", "Germany; Netherlands", 4.40, 5, 500.0],
            ["Hexa Express", "standard", "France; Spain", 5.20, 6, 300.0],
            ["TwoMen Freight", "two-person", "All", 39.00, 10, 2500.0],
        ]
        carriers_t = table(
            "carriers",
            "Delivery carriers",
            "Delivery companies we contract: service level, countries, price per parcel, contractual delivery days and insurance cover.",
            "One row per carrier.",
            ["carrier_name"],
            [
                dim("carrier_name", "Carrier, as named on shipments."),
                dim("service_level", "standard or two-person.", ["standard", "two-person"]),
                col("countries_served", "string", "attribute", "Countries covered."),
                col("rate_per_parcel", "decimal", "attribute", "Contract price per parcel, EUR."),
                col("contract_days", "integer", "attribute", "Contractual delivery days."),
                col("insurance_limit", "decimal", "attribute", "Cover per parcel, EUR."),
            ],
            carriers,
        )
        inspections = []
        for r in self.returns:
            grade = self.rng.choice(["A", "A", "B", "C"])
            value = round(
                float(r["refund_amount"]) * (1.0 if grade == "C" else 0.3 if grade == "B" else 0.0),
                2,
            )
            day = datetime.fromisoformat(r["return_date"]).date() + timedelta(
                days=self.rng.randint(1, 5)
            )
            inspections.append(
                [
                    f"IN-{r['return_id'][2:]}",
                    r["return_id"],
                    min(day, END),
                    r["product_id"],
                    grade,
                    grade != "C",
                    value,
                ]
            )
        inspections_t = table(
            "return_inspections",
            "Returned goods inspections",
            """What happened to each returned item after inspection: grade A goes back on sale,
            grade B is sold at a discount, grade C is written off. The write-off value is the
            stock value lost, not the refund paid (refunds are in returns).""",
            "One row per inspected return.",
            ["inspection_id"],
            [
                col("inspection_id", "string", "id", "Inspection."),
                col("return_id", "string", "id", "The return inspected."),
                col("inspected_date", "date", "time", "When inspected."),
                col("product_id", "string", "id", "Product."),
                dim("grade", "A (resell), B (discount) or C (write off).", ["A", "B", "C"]),
                col("restocked", "boolean", "dimension", "Put back into stock."),
                col("write_off_value", "decimal", "measure", "Stock value lost, EUR.", unit=EUR),
            ],
            inspections,
            time_column="inspected_date",
            relationships=[
                link("return_id", "returns.return_id"),
                link("product_id", "products.product_id"),
            ],
            metrics=[
                metric(
                    "inspection_count",
                    "Inspections",
                    "Returned items inspected.",
                    "count",
                    "inspection_id",
                ),
                metric(
                    "write_off_value",
                    "Write-offs",
                    "Stock value written off after returns.",
                    "sum",
                    "write_off_value",
                    EUR,
                ),
            ],
        )
        return [wh_t, stock_t, transfers_t, carriers_t, inspections_t]

    # ------------------------------------------------------------------ people

    def people(self) -> list[Table]:
        courses = [
            ("Fire safety", "safety", 2.0),
            ("Manual handling", "safety", 1.5),
            ("Till system", "systems", 3.0),
            ("Product knowledge: furniture", "product", 2.5),
            ("Product knowledge: textiles", "product", 2.0),
            ("Leading a team", "leadership", 6.0),
            ("Data protection", "compliance", 1.0),
        ]
        records, n = [], 0
        for e in self.staff:
            for course, category, hrs in courses:
                if category == "leadership" and "Manager" not in e["role"]:
                    continue
                if self.rng.random() < 0.55:
                    n += 1
                    due = START + timedelta(days=self.rng.randint(0, 119))
                    done = due - timedelta(days=self.rng.randint(-20, 40))
                    completed = done if done <= END and self.rng.random() < 0.85 else None
                    status = "completed" if completed else ("overdue" if due < END else "assigned")
                    records.append(
                        [
                            f"TRN-{n:04d}",
                            e["employee_id"],
                            course,
                            category,
                            due,
                            completed,
                            hrs,
                            status,
                        ]
                    )
        training_t = table(
            "training_records",
            "Staff training records",
            "Mandatory and optional courses assigned to employees, due dates, completion and hours.",
            "One row per employee per course.",
            ["record_id"],
            [
                col("record_id", "string", "id", "Record."),
                col("employee_id", "string", "id", "Employee."),
                dim("course", "Course."),
                dim(
                    "course_category",
                    "safety, systems, product, leadership or compliance.",
                    ["safety", "systems", "product", "leadership", "compliance"],
                ),
                col("due_date", "date", "time", "When it must be done."),
                col(
                    "completed_date",
                    "date",
                    "dimension",
                    "When completed. Empty if not.",
                    nullable=True,
                ),
                col("course_hours", "decimal", "measure", "Length, hours.", unit=HRS),
                dim(
                    "status",
                    "completed, overdue or assigned.",
                    ["completed", "overdue", "assigned"],
                ),
            ],
            records,
            readers=["operations"],
            time_column="due_date",
            relationships=[link("employee_id", "staff.employee_id")],
            metrics=[
                metric(
                    "training_assigned",
                    "Training assigned",
                    "Courses assigned.",
                    "count",
                    "record_id",
                ),
                metric(
                    "training_completed",
                    "Training completed",
                    "Courses completed.",
                    "count",
                    "record_id",
                    filters=[{"column": "status", "op": "=", "value": "completed"}],
                ),
                metric(
                    "training_overdue",
                    "Training overdue",
                    "Courses past due and not done.",
                    "count",
                    "record_id",
                    filters=[{"column": "status", "op": "=", "value": "overdue"}],
                ),
            ],
        )
        payroll = []
        for m in MONTHS:
            month_end = (m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            for e in self.staff:
                start = date.fromisoformat(e["start_date"])
                end = date.fromisoformat(e["end_date"]) if e["end_date"] else None
                if start > month_end or (end and end < m):
                    continue
                hours = float(e["weekly_contract_hours"])
                rate = 21.0 if "Manager" in e["role"] else 14.5
                gross = round(hours * 4.33 * rate * self.rng.uniform(0.98, 1.02), 2)
                ot = round(self.rng.choice([0, 0, 0, 2, 4, 6, 8]) * 1.0, 2)
                ot_pay = round(ot * rate * 1.5, 2)
                payroll.append(
                    [
                        m,
                        e["employee_id"],
                        e["store_id"] or None,
                        gross,
                        ot,
                        ot_pay,
                        round((gross + ot_pay) * 0.21, 2),
                    ]
                )
        payroll_t = table(
            "payroll",
            "Payroll by month",
            """Monthly pay for each employee: gross pay, overtime hours and pay, and employer
            costs (social charges). Finance only. Hours actually worked per shift are in
            shifts.""",
            "One row per employee per month.",
            ["pay_month", "employee_id"],
            [
                col("pay_month", "date", "time", "First day of the month."),
                col("employee_id", "string", "id", "Employee."),
                col(
                    "store_id",
                    "string",
                    "id",
                    "Store the employee works at. Empty for head-office staff (customer service).",
                    nullable=True,
                ),
                col(
                    "gross_pay",
                    "decimal",
                    "measure",
                    "Gross pay excluding overtime, EUR.",
                    unit=EUR,
                ),
                col("overtime_hours", "decimal", "measure", "Overtime hours paid.", unit=HRS),
                col("overtime_pay", "decimal", "measure", "Overtime pay, EUR.", unit=EUR),
                col(
                    "employer_costs",
                    "decimal",
                    "measure",
                    "Employer social charges, EUR.",
                    unit=EUR,
                ),
            ],
            payroll,
            readers=["finance"],
            time_column="pay_month",
            relationships=[
                link("employee_id", "staff.employee_id"),
                link("store_id", "stores.store_id"),
            ],
            metrics=[
                metric(
                    "gross_pay",
                    "Gross pay",
                    "Gross pay excluding overtime.",
                    "sum",
                    "gross_pay",
                    EUR,
                ),
                metric("overtime_pay", "Overtime pay", "Overtime pay.", "sum", "overtime_pay", EUR),
                metric(
                    "overtime_hours",
                    "Overtime hours",
                    "Overtime hours paid.",
                    "sum",
                    "overtime_hours",
                ),
                metric(
                    "employer_costs",
                    "Employer costs",
                    "Employer social charges.",
                    "sum",
                    "employer_costs",
                    EUR,
                ),
            ],
            derived=[
                {
                    "name": "total_staff_cost",
                    "title": "Total staff cost",
                    "description": "Gross pay plus overtime pay plus employer costs.",
                    "formula": "gross_pay + overtime_pay + employer_costs",
                }
            ],
        )
        roles = ["Sales Associate", "Stockroom Assistant", "Store Manager", "Visual Merchandiser"]
        vacancies = []
        for n in range(1, 27):
            s = self.rng.choice(self.stores)
            opened = START + timedelta(days=self.rng.randint(0, 110))
            if not self.open_on(s["store_id"], opened) and s["store_id"] != "S08":
                continue
            filled = opened + timedelta(days=self.rng.randint(14, 60))
            status = "filled" if filled <= END else "open"
            if self.rng.random() < 0.08:
                status, filled = "withdrawn", None
            if status == "open":
                filled = None
            role = self.rng.choice(roles)
            band = "B3" if role == "Store Manager" else "B1"
            vacancies.append(
                [
                    f"VAC-{n:03d}",
                    s["store_id"],
                    role,
                    opened,
                    filled,
                    self.rng.randint(3, 60),
                    status,
                    band,
                ]
            )
        vac_t = table(
            "vacancies",
            "Job vacancies",
            """Open roles being recruited for, applicants and when filled. A vacancy is a role
            not yet filled; people actually employed are in staff.""",
            "One row per vacancy.",
            ["vacancy_id"],
            [
                col("vacancy_id", "string", "id", "Vacancy."),
                col("store_id", "string", "id", "Store."),
                dim("job_role", "Role being recruited.", roles),
                col("opened_date", "date", "time", "When advertised."),
                col(
                    "filled_date",
                    "date",
                    "dimension",
                    "When accepted by a candidate.",
                    nullable=True,
                ),
                col("applicants", "integer", "measure", "Applications received."),
                dim("status", "open, filled or withdrawn.", ["open", "filled", "withdrawn"]),
                dim("salary_band", "Pay band.", ["B1", "B3"]),
            ],
            vacancies,
            readers=["operations"],
            time_column="opened_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric("vacancy_count", "Vacancies", "Vacancies opened.", "count", "vacancy_id"),
                metric(
                    "open_vacancy_count",
                    "Open vacancies",
                    "Vacancies still being recruited.",
                    "count",
                    "vacancy_id",
                    filters=[{"column": "status", "op": "=", "value": "open"}],
                ),
                metric("applicants", "Applicants", "Applications received.", "sum", "applicants"),
            ],
        )
        topics = ["Workload", "Manager support", "Pay", "Training", "Team"]
        survey, n = [], 0
        for m in (date(2026, 1, 1), date(2026, 4, 1)):
            for e in self.staff:
                if self.rng.random() < 0.7 and date.fromisoformat(e["start_date"]) <= m:
                    n += 1
                    survey.append(
                        [
                            f"SS-{n:03d}",
                            m,
                            e["store_id"] or None,
                            min(5, max(1, round(self.rng.gauss(3.7, 0.9)))),
                            self.rng.random() < 0.72,
                            self.rng.choice(topics),
                        ]
                    )
        survey_t = table(
            "staff_survey",
            "Staff engagement survey",
            """Anonymous employee engagement survey, run in January and April: engagement from
            1 to 5 and whether they'd recommend Brindlewood as an employer. About employees,
            not customers.""",
            "One row per anonymous response.",
            ["response_id"],
            [
                col("response_id", "string", "id", "Response."),
                col("survey_month", "date", "time", "Survey round (first day of month)."),
                col(
                    "store_id",
                    "string",
                    "id",
                    "Store of the respondent. Empty for head office.",
                    nullable=True,
                ),
                col("engagement_score", "integer", "measure", "Engagement, 1-5."),
                col(
                    "would_recommend_employer",
                    "boolean",
                    "dimension",
                    "Would recommend working here.",
                ),
                dim("main_topic", "What the comment was mostly about.", topics),
            ],
            survey,
            readers=["operations"],
            time_column="survey_month",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric(
                    "staff_survey_responses",
                    "Staff survey responses",
                    "Responses.",
                    "count",
                    "response_id",
                ),
                metric(
                    "average_engagement",
                    "Average engagement",
                    "Average engagement score (1-5).",
                    "avg",
                    "engagement_score",
                ),
            ],
        )
        return [training_t, payroll_t, vac_t, survey_t]

    # ------------------------------------------------------------------ finance

    def finance(self) -> list[Table]:
        accounts = {
            "4000": ("Sales revenue", "Revenue"),
            "4900": ("Sales returns", "Revenue"),
            "4100": ("Gift card breakage", "Revenue"),
            "5000": ("Cost of goods sold", "Cost of sales"),
            "6100": ("Staff costs", "Operating expense"),
            "6200": ("Rent", "Operating expense"),
            "6300": ("Energy", "Operating expense"),
            "6400": ("Marketing", "Operating expense"),
            "6500": ("Repairs and maintenance", "Operating expense"),
            "6600": ("Delivery", "Operating expense"),
        }
        entries, n = [], 0
        for m in MONTHS:
            month_end = (m.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            if m.month == 4:
                month_end = date(2026, 4, 30)
            for s in self.stores:
                if not self.open_on(s["store_id"], month_end):
                    continue
                # Booked from the month's sales: revenue net of cancellations, returns on their
                # own account, cost of sales from revenue. Before reconciliation the booking
                # differs from the sales system by up to 3%.
                sales = self.sales_month.get(
                    (s["store_id"], m), {"gross": 0.0, "cancelled": 0.0, "refunds": 0.0}
                )
                booked = (sales["gross"] - sales["cancelled"]) * self.rng.uniform(0.97, 1.03)
                for code, (name, kind) in accounts.items():
                    if code == "4100" and self.rng.random() < 0.7:
                        continue
                    amount = {
                        "4000": max(booked, 0.0),
                        "4900": sales["refunds"],
                        "4100": self.rng.uniform(10, 120),
                        "5000": max(booked, 0.0) * self.rng.uniform(0.42, 0.48),
                        "6100": self.rng.uniform(6000, 16000),
                        "6200": 0.0 if s["channel"] == "Online" else self.rng.uniform(4000, 11000),
                        "6300": 0.0 if s["channel"] == "Online" else self.rng.uniform(2000, 6500),
                        "6400": self.rng.uniform(300, 3000),
                        "6500": self.rng.uniform(0, 1500),
                        "6600": self.rng.uniform(200, 2200)
                        if s["channel"] == "Online"
                        else self.rng.uniform(0, 300),
                    }[code]
                    if amount == 0:
                        continue
                    n += 1
                    dept = {"Revenue": "Commercial", "Cost of sales": "Commercial"}.get(
                        kind, "Operations" if code != "6400" else "Marketing"
                    )
                    entries.append(
                        [
                            f"GL-{n:05d}",
                            month_end,
                            code,
                            name,
                            kind,
                            dept,
                            s["store_id"],
                            round(amount, 2),
                        ]
                    )
        gl_t = table(
            "gl_entries",
            "General ledger",
            """Monthly ledger postings by account and store, as booked by Finance at month end.
            Accounting sales revenue (account 4000) is booked net of cancellations, with
            returns on their own account (4900); before reconciliation it differs from the
            sales system by a few percent, so it is not net sales. April is provisional until
            the month is closed.""",
            "One row per account, store and month-end posting.",
            ["entry_id"],
            [
                col("entry_id", "string", "id", "Posting."),
                col("posting_date", "date", "time", "Date booked (month end)."),
                col("account_code", "string", "attribute", "Ledger account number."),
                dim("account_name", "Ledger account.", [v[0] for v in accounts.values()]),
                dim(
                    "account_type",
                    "Revenue, Cost of sales or Operating expense.",
                    ["Revenue", "Cost of sales", "Operating expense"],
                ),
                dim("department", "Department charged.", ["Commercial", "Operations", "Marketing"]),
                col("store_id", "string", "id", "Store."),
                col("amount", "decimal", "measure", "Amount booked, EUR (positive).", unit=EUR),
            ],
            entries,
            readers=["finance"],
            time_column="posting_date",
            relationships=[link("store_id", "stores.store_id")],
            metrics=[
                metric(
                    "ledger_amount",
                    "Ledger amount",
                    "Amounts booked to ledger accounts.",
                    "sum",
                    "amount",
                    EUR,
                ),
                metric(
                    "accounting_revenue",
                    "Accounting revenue",
                    "Ledger sales revenue (account 4000), by posting date; not net sales.",
                    "sum",
                    "amount",
                    EUR,
                    filters=[{"column": "account_code", "op": "=", "value": "4000"}],
                ),
                metric(
                    "operating_expenses",
                    "Operating expenses",
                    "Ledger operating expenses.",
                    "sum",
                    "amount",
                    EUR,
                    filters=[{"column": "account_type", "op": "=", "value": "Operating expense"}],
                ),
            ],
        )
        invoices = []
        for i, po in enumerate(
            sorted(
                {p["po_number"]: p for p in self.purchase_orders}.values(),
                key=lambda p: p["po_number"],
            ),
            start=1,
        ):
            lines = [p for p in self.purchase_orders if p["po_number"] == po["po_number"]]
            if po["status"] in ("open", "cancelled"):
                continue
            received = (
                date.fromisoformat(po["received_date"])
                if po["received_date"]
                else date.fromisoformat(po["expected_date"])
            )
            amount = round(
                sum(int(x["quantity_received"] or 0) * float(x["unit_cost"]) for x in lines), 2
            )
            terms = next(
                int(s["payment_terms_days"])
                for s in self.suppliers
                if s["supplier_id"] == po["supplier_id"]
            )
            inv_date = received + timedelta(days=self.rng.randint(0, 3))
            due = inv_date + timedelta(days=terms)
            r = self.rng.random()
            paid = due - timedelta(days=self.rng.randint(0, 5)) if r < 0.7 else None
            if paid and paid > END:
                paid = None
            status = "paid" if paid else ("disputed" if r > 0.93 else "unpaid")
            invoices.append(
                [
                    f"SI-{i:04d}",
                    po["supplier_id"],
                    po["po_number"],
                    inv_date,
                    due,
                    amount,
                    paid,
                    status,
                ]
            )
        inv_t = table(
            "supplier_invoices",
            "Supplier invoices",
            """Invoices from suppliers for purchase orders received: amount, due date and
            payment. Unpaid invoices past their due date are overdue.""",
            "One row per invoice.",
            ["invoice_id"],
            [
                col("invoice_id", "string", "id", "Supplier invoice."),
                col("supplier_id", "string", "id", "Supplier."),
                col("po_number", "string", "attribute", "Purchase order invoiced."),
                col("invoice_date", "date", "time", "Invoice date."),
                col("due_date", "date", "dimension", "Payment due."),
                col("amount", "decimal", "measure", "Invoice amount, EUR.", unit=EUR),
                col("paid_date", "date", "dimension", "When paid. Empty if unpaid.", nullable=True),
                dim("status", "paid, unpaid or disputed.", ["paid", "unpaid", "disputed"]),
            ],
            invoices,
            readers=["finance"],
            time_column="invoice_date",
            relationships=[link("supplier_id", "suppliers.supplier_id")],
            metrics=[
                metric(
                    "invoiced_amount",
                    "Supplier invoices",
                    "Amount invoiced by suppliers.",
                    "sum",
                    "amount",
                    EUR,
                ),
                metric(
                    "unpaid_invoice_amount",
                    "Unpaid supplier invoices",
                    "Amount not yet paid.",
                    "sum",
                    "amount",
                    EUR,
                    filters=[{"column": "status", "op": "in", "value": ["unpaid", "disputed"]}],
                ),
                metric(
                    "invoice_count",
                    "Supplier invoice count",
                    "Invoices received.",
                    "count",
                    "invoice_id",
                ),
            ],
        )
        cats = ["travel", "meals", "store supplies", "training", "marketing"]
        expenses = []
        for n in range(1, 211):
            e = self.rng.choice(self.staff)
            d = START + timedelta(days=self.rng.randint(0, 119))
            cat = self.rng.choice(cats)
            amount = round(
                {
                    "travel": self.rng.uniform(40, 600),
                    "meals": self.rng.uniform(8, 90),
                    "store supplies": self.rng.uniform(5, 250),
                    "training": self.rng.uniform(50, 800),
                    "marketing": self.rng.uniform(20, 400),
                }[cat],
                2,
            )
            approved = self.rng.random() < 0.9
            reimbursed = d + timedelta(days=self.rng.randint(5, 25)) if approved else None
            if reimbursed and reimbursed > END:
                reimbursed = None
            expenses.append(
                [
                    f"EX-{n:04d}",
                    e["employee_id"],
                    e["store_id"] or None,
                    d,
                    cat,
                    amount,
                    approved,
                    reimbursed,
                ]
            )
        exp_t = table(
            "expenses",
            "Employee expense claims",
            """Expenses employees paid themselves and claimed back: category, amount, approval
            and reimbursement. Marketing claims here are small local costs; media spend on
            campaigns is in campaign spend.""",
            "One row per claim.",
            ["expense_id"],
            [
                col("expense_id", "string", "id", "Claim."),
                col("employee_id", "string", "id", "Employee."),
                col(
                    "store_id",
                    "string",
                    "id",
                    "Store charged. Empty for head-office staff.",
                    nullable=True,
                ),
                col("expense_date", "date", "time", "Date of the expense."),
                dim("category", "Kind of expense.", cats),
                col("amount", "decimal", "measure", "Amount claimed, EUR.", unit=EUR),
                col("approved", "boolean", "dimension", "Approved by the manager."),
                col(
                    "reimbursed_date",
                    "date",
                    "dimension",
                    "When paid back. Empty if not yet.",
                    nullable=True,
                ),
            ],
            expenses,
            readers=["finance"],
            time_column="expense_date",
            relationships=[
                link("employee_id", "staff.employee_id"),
                link("store_id", "stores.store_id"),
            ],
            metrics=[
                metric("expense_claims", "Expense claims", "Claims made.", "count", "expense_id"),
                metric(
                    "expenses_claimed", "Expenses claimed", "Amount claimed.", "sum", "amount", EUR
                ),
                metric(
                    "expenses_approved",
                    "Expenses approved",
                    "Amount approved.",
                    "sum",
                    "amount",
                    EUR,
                    filters=[{"column": "approved", "op": "=", "value": True}],
                ),
            ],
        )
        return [gl_t, inv_t, exp_t]

    # ------------------------------------------------------------------ all of it

    def tables(self) -> dict[str, Table]:
        out: list[Table] = [self.customer_segments(), self.customers()]
        out += [*self.loyalty(), self.gift_cards(), self.nps_responses()]
        out += [*self.marketing(), *self.web()]
        out += list(self.catalogue())
        out += self.store_ops()
        out += self.logistics()
        out += self.people()
        out += self.finance()
        return {t.manifest["source"]: t for t in out}


def generate_stage_c(seed: int = SEED_C) -> dict[str, Table]:
    return StageC(seed).tables()
