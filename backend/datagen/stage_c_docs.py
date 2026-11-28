"""Stage C documents: about 100 more, for roughly 120 in all.

Most come in families: one per store, supplier, carrier, campaign, warehouse, category, region
and month. Families are what make document search hard at scale. "Ferro Kitchenware's minimum
order" has ten look-alike supplier agreements to get wrong, and each document holds facts found
in no other, so every retrieval question has exactly one right answer.

Where a document states something a table also holds (opening hours, supplier terms, carrier
prices, campaign budgets, loyalty rules), it states the same value: both come from here and
datagen/stage_c.py, or from the committed Stage A/B files. Some documents get a second version
that changes a rule, so "current" versus "superseded" matters too.
"""

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
BOTH, OPS, FIN = ["operations", "finance"], ["operations"], ["finance"]


@dataclass
class Document:
    doc_id: str
    version: int
    filename: str
    text: str
    readers: list[str]


def _read(stage: str, table: str) -> list[dict]:
    with (DATA / stage / "tables" / f"{table}.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def doc(
    doc_id: str,
    title: str,
    owner: str,
    effective: date,
    sections: list[tuple[str, str]],
    readers=BOTH,
    version: int = 1,
) -> Document:
    front = [
        "---",
        f"doc_id: {doc_id}",
        f"version: {version}",
        f"title: {json.dumps(title, ensure_ascii=False)}",  # quoted: titles contain colons
        f"owner: {owner}",
        f"effective_date: {effective.isoformat()}",
    ]
    if version > 1:
        front.append(f"supersedes_version: {version - 1}")
    front += ["synthetic: true", "---", "", f"# {title}", ""]
    body = []
    for heading, text in sections:
        body += [f"## {heading}", "", " ".join(text.split()), ""]
    return Document(
        doc_id, version, f"{doc_id}.v{version}.md", "\n".join(front + body), list(readers)
    )


# ----------------------------------------------------------------------------- families


def store_guides(stores, staff) -> list[Document]:
    manager = {e["store_id"]: e["display_name"] for e in staff if e["role"] == "Store Manager"}
    local = {
        "S01": (
            "Customer parking is at the Kingsway car park; validate tickets at the till for "
            "two free hours.",
            "The store opens until 21:00 on Thursdays for late shopping.",
            "Deliveries arrive at the Drury Lane loading bay between 06:00 and 08:00.",
        ),
        "S03": (
            "There is no customer parking; the nearest is Q-Park Grafton, ten minutes away.",
            "Opening hours follow the handbook with no local changes.",
            "Deliveries arrive at the rear door on Clarendon Row before 08:30.",
        ),
        "S04": (
            "Customer parking is in the basement, level -1, free for the first hour.",
            "German law closes the store on Sundays and public holidays.",
            "Deliveries arrive at the Mitte goods entrance between 07:00 and 09:00.",
        ),
        "S06": (
            "Bicycle parking for 40 bikes is in front of the store; there is no car park.",
            "Opening hours follow the handbook with no local changes.",
            "Deliveries come by small electric van to the canal-side door before 09:00.",
        ),
        "S07": (
            "Customer parking is shared with the Gran Vía shopping centre, free for two hours.",
            "Opening hours follow the handbook; the store closes at 15:00 on 6 January.",
            "Deliveries arrive at the shopping centre's service yard, dock 4, before 08:00.",
        ),
    }
    out = []
    for s in stores:
        sid, name = s["store_id"], s["store_name"]
        slug = name.lower().replace(" ", "-")
        if s["channel"] == "Online":
            out.append(
                doc(
                    f"web-shop-guide-{slug}",
                    f"Web Shop Guide: {name}",
                    "E-commerce",
                    date(2026, 1, 1) if sid != "S08" else date(2026, 3, 2),
                    [
                        (
                            "About this web shop",
                            f"{name} serves customers in {s['country']}. It opened on "
                            f"{s['opened_date']} and is part of the {s['region']} region.",
                        ),
                        (
                            "Orders and dispatch",
                            "Orders placed before 14:00 are dispatched the same working "
                            "day from the regional distribution centre. Furniture always ships by two-person delivery.",
                        ),
                        (
                            "Customer contact",
                            f"Customer questions for {name} go to the central customer service "
                            "team by email, chat or phone; the web shop has no local staff.",
                        ),
                    ],
                )
            )
            continue
        parking, hours, deliveries = local[sid]
        out.append(
            doc(
                f"store-guide-{slug}",
                f"Store Guide: {name}",
                "Store Operations",
                date(2026, 1, 1),
                [
                    (
                        "About the store",
                        f"{name} in {s['country']} opened on {s['opened_date']}. "
                        f"The store manager is {manager.get(sid, 'vacant')}.",
                    ),
                    (
                        "Opening hours",
                        "Monday to Saturday 09:00-20:00 and Sunday 11:00-17:00 unless noted. "
                        + hours,
                    ),
                    ("Parking and access", parking),
                    ("Deliveries", deliveries),
                ],
            )
        )
    return out


SUPPLIER_TERMS = {  # minimum order (EUR), defect allowance (%), rebate rule
    "SU01": (800, 1.5, "2% rebate on annual purchases above EUR 20,000"),
    "SU02": (1500, 1.0, "no rebate"),
    "SU03": (600, 2.0, "1.5% rebate on annual purchases above EUR 15,000"),
    "SU04": (1000, 2.5, "no rebate"),
    "SU05": (500, 1.0, "3% rebate on annual purchases above EUR 25,000"),
    "SU06": (2000, 3.0, "no rebate"),
    "SU07": (2500, 0.5, "2.5% rebate on annual purchases above EUR 40,000"),
    "SU08": (3000, 1.0, "no rebate"),
    "SU09": (700, 2.0, "2% rebate on annual purchases above EUR 12,000"),
    "SU10": (400, 1.5, "no rebate"),
}


def supplier_agreements(suppliers) -> list[Document]:
    out = []
    for s in suppliers:
        minimum, defects, rebate = SUPPLIER_TERMS[s["supplier_id"]]
        name = s["supplier_name"]
        slug = name.lower().replace("&", "and").replace(".", "").replace(" ", "-")
        status = "a preferred supplier" if s["is_preferred"] == "true" else "a secondary supplier"
        out.append(
            doc(
                f"supplier-agreement-{slug}",
                f"Supplier Agreement: {name}",
                "Purchasing",
                date(2025, 11, 1),
                [
                    (
                        "Supplier",
                        f"{name} ({s['country']}) supplies {s['category']} and is {status}.",
                    ),
                    (
                        "Ordering",
                        f"The minimum order value is EUR {minimum:,}. Standard lead time is "
                        f"{s['lead_time_days']} days from order to delivery at the distribution centre.",
                    ),
                    (
                        "Payment",
                        f"Invoices are payable {s['payment_terms_days']} days from the invoice date.",
                    ),
                    (
                        "Quality",
                        f"Up to {defects}% of units per delivery may be defective without penalty; "
                        "above that, the supplier credits the full value of defective units.",
                    ),
                    ("Rebates", f"Commercial terms: {rebate}."),
                ],
            )
        )
    return out


def carrier_agreements() -> list[Document]:
    rows = [
        (
            "Parcelwise UK",
            "the United Kingdom and Ireland",
            "4.90",
            4,
            250,
            "claims within 14 days",
        ),
        ("Rhein Logistik", "Germany and the Netherlands", "4.40", 5, 500, "claims within 10 days"),
        ("Hexa Express", "France and Spain", "5.20", 6, 300, "claims within 21 days"),
        (
            "TwoMen Freight",
            "all countries, for furniture",
            "39.00",
            10,
            2500,
            "claims within 7 days, with photos",
        ),
    ]
    out = []
    for name, area, rate, days_, cover, claims in rows:
        slug = name.lower().replace(" ", "-")
        out.append(
            doc(
                f"carrier-agreement-{slug}",
                f"Carrier Agreement: {name}",
                "Fulfilment",
                date(2025, 12, 1),
                [
                    ("Service", f"{name} delivers parcels in {area}."),
                    (
                        "Price and speed",
                        f"Contract price EUR {rate} per parcel. Contractual delivery within "
                        f"{days_} calendar days of dispatch.",
                    ),
                    (
                        "Damage and loss",
                        f"Insurance covers up to EUR {cover:,} per parcel; {claims}.",
                    ),
                ],
            )
        )
    return out


def campaign_briefs() -> list[Document]:
    briefs = [
        (
            "CMP-01",
            "New year refresh",
            "email",
            "2026-01-02 to 2026-01-31",
            400,
            "Regulars and VIP customers",
            "repeat purchase rate",
        ),
        (
            "CMP-02",
            "Winter lighting",
            "social",
            "2026-01-10 to 2026-02-15",
            650,
            "people interested in home decor, aged 25-45",
            "reach and video views",
        ),
        (
            "CMP-03",
            "Brand search",
            "search",
            "2026-01-01 to 2026-04-30",
            1200,
            "people searching for Brindlewood",
            "cost per order",
        ),
        (
            "CMP-04",
            "Kitchen essentials",
            "display",
            "2026-02-01 to 2026-02-28",
            350,
            "recent kitchen browsers",
            "new customers",
        ),
        (
            "CMP-05",
            "Spring textiles",
            "email",
            "2026-04-01 to 2026-04-30",
            300,
            "all opted-in customers",
            "use of code SPRING10",
        ),
        (
            "CMP-06",
            "Spring textiles social",
            "social",
            "2026-04-01 to 2026-04-30",
            550,
            "lookalikes of textile buyers",
            "use of code SPRING10",
        ),
        (
            "CMP-07",
            "France launch",
            "search",
            "2026-03-02 to 2026-04-30",
            900,
            "shoppers in France",
            "first orders on FR Online",
        ),
        (
            "CMP-08",
            "Furniture showcase",
            "display",
            "2026-03-01 to 2026-03-31",
            450,
            "visitors who viewed furniture",
            "furniture page views",
        ),
    ]
    out = []
    for cid, name, channel, dates, budget, audience, kpi in briefs:
        slug = name.lower().replace(" ", "-")
        out.append(
            doc(
                f"campaign-brief-{slug}",
                f"Campaign Brief: {name}",
                "Marketing",
                date(2025, 12, 15),
                [
                    (
                        "Campaign",
                        f"{name} ({cid}) runs on {channel} from {dates} with a media budget of EUR {budget:,}.",
                    ),
                    ("Audience", f"Target audience: {audience}."),
                    (
                        "Success measure",
                        f"The campaign is judged on {kpi}. Attributed revenue from the ad "
                        "platform is reported for information only; it is not net sales.",
                    ),
                ],
            )
        )
    return out


def warehouse_manuals() -> list[Document]:
    rows = [
        (
            "WH1",
            "Midlands DC",
            "Northline Logistics",
            4200,
            "UK & Ireland",
            "06:00-14:00",
            "Coventry",
        ),
        ("WH2", "Rhine DC", "Kontor 3PL", 5600, "Central Europe", "07:00-15:00", "Duisburg"),
        (
            "WH3",
            "Zaragoza DC",
            "Iberia Fulfilment",
            3100,
            "Southwest Europe",
            "08:00-16:00",
            "Zaragoza",
        ),
    ]
    return [
        doc(
            f"warehouse-manual-{name.lower().replace(' ', '-')}",
            f"Warehouse Manual: {name}",
            "Supply Chain",
            date(2026, 1, 1),
            [
                (
                    "Site",
                    f"{name} ({wid}) in {town} is run by {op} and supplies the {region} region. "
                    f"It has {cap:,} pallet spaces.",
                ),
                (
                    "Receiving",
                    f"Supplier deliveries are received {hours}, Monday to Friday. Deliveries "
                    "outside the window are refused unless booked in advance.",
                ),
                (
                    "Stock counts",
                    "Stock is counted every Monday; the count is the figure reported for the week.",
                ),
            ],
        )
        for wid, name, op, cap, region, hours, town in rows
    ]


def category_guides() -> list[Document]:
    rows = [
        (
            "Lighting",
            "Priya N.",
            "Use bulbs no stronger than the rating printed on the fitting. Dust with a dry cloth.",
        ),
        (
            "Kitchen",
            "Tomás R.",
            "Cast iron must be dried and oiled after washing; stoneware is dishwasher safe.",
        ),
        ("Textiles", "Ingrid S.", "Wash linen at 40°C; wool cushion covers are dry clean only."),
        (
            "Furniture",
            "Karl W.",
            "Oak and walnut need oiling twice a year; keep rattan out of direct sun.",
        ),
        (
            "Decor",
            "Amélie D.",
            "Candles must never be left burning unattended; trim wicks to 5 mm.",
        ),
    ]
    return [
        doc(
            f"category-guide-{cat.lower()}",
            f"Category Guide: {cat}",
            "Commercial",
            date(2026, 1, 1),
            [
                (
                    "Category",
                    f"{cat} is managed by {mgr}. Customers may return {cat.lower()} items within 30 days.",
                ),
                ("Care advice for customers", care),
            ],
        )
        for cat, mgr, care in rows
    ]


def region_briefings() -> list[Document]:
    rows = [
        (
            "United Kingdom",
            "GBP, converted to EUR at the monthly rate",
            "20%",
            "Two stores' worth of trade: London Flagship and UK Online.",
        ),
        ("Ireland", "EUR", "23%", "Dublin Store; online orders go through UK Online."),
        ("Germany", "EUR", "19%", "Berlin Store and DE Online; Sunday trading is not allowed."),
        ("Netherlands", "EUR", "21%", "Amsterdam Store; online orders go through DE Online."),
        (
            "Spain",
            "EUR",
            "21%",
            "Madrid Store; online orders go through FR Online since March 2026.",
        ),
        ("France", "EUR", "20%", "FR Online, opened on 2 March 2026; no physical store."),
    ]
    return [
        doc(
            f"country-briefing-{country.lower().replace(' ', '-')}",
            f"Country Briefing: {country}",
            "Commercial",
            date(2026, 1, 1),
            [
                ("Trading", presence),
                ("Tax and currency", f"Standard VAT rate {vat}. Sales are reported in {currency}."),
            ],
        )
        for country, currency, vat, presence in rows
    ]


def monthly_reviews() -> list[Document]:
    notes = {
        (
            "UK & Ireland",
            1,
        ): "Dublin's lift was out of order from 12 to 19 January; furniture was delivered from the stockroom by hand.",
        (
            "UK & Ireland",
            2,
        ): "London Flagship ran the first Thursday late-night event on 12 February with a local designer.",
        (
            "UK & Ireland",
            3,
        ): "UK Online moved returns labels to a QR code; printed labels stop in May.",
        ("UK & Ireland", 4): "Dublin trialled self-checkout on two tills from 14 April.",
        (
            "Central Europe",
            1,
        ): "Berlin's heating failed on 8 January; the store opened late at 11:00.",
        (
            "Central Europe",
            2,
        ): "Amsterdam replaced its door counters on 3 February, so footfall before then is approximate.",
        ("Central Europe", 3): "DE Online added Klarna as a payment method on 16 March.",
        ("Central Europe", 4): "Berlin hired a new visual merchandiser starting 20 April.",
        ("Southwest Europe", 1): "Madrid closed at 15:00 on 6 January for Epiphany.",
        (
            "Southwest Europe",
            2,
        ): "Madrid's window display won the Gran Vía retail award on 21 February.",
        ("Southwest Europe", 3): "FR Online opened on 2 March; Spanish online orders moved to it.",
        ("Southwest Europe", 4): "FR Online added Colissimo pick-up points on 22 April.",
    }
    month_name = {1: "January", 2: "February", 3: "March", 4: "April"}
    out = []
    for (region, month), note in notes.items():
        slug = region.lower().replace(" & ", "-").replace(" ", "-")
        out.append(
            doc(
                f"ops-review-{slug}-2026-{month:02d}",
                f"Operations Review: {region}, {month_name[month]} 2026",
                "Store Operations",
                date(2026, month + 1, 5) if month < 4 else date(2026, 4, 30),
                [
                    ("Highlights", note),
                    (
                        "Figures",
                        "Sales, stock and staffing figures are in the reporting datasets; this note "
                        "records events only, not numbers.",
                    ),
                ],
                readers=OPS,
            )
        )
    return out


def role_guides() -> list[Document]:
    rows = [
        (
            "Sales Associate",
            "Till system, Fire safety, Manual handling and Data protection in the first two weeks.",
        ),
        (
            "Stockroom Assistant",
            "Manual handling and Fire safety before the first shift; Till system within a month.",
        ),
        (
            "Store Manager",
            "Leading a team within three months, plus every course a sales associate takes.",
        ),
        (
            "Customer Service Agent",
            "Data protection and the returns and refund policy in the first week.",
        ),
    ]
    return [
        doc(
            f"new-starter-guide-{role.lower().replace(' ', '-')}",
            f"New Starter Guide: {role}",
            "People",
            date(2026, 1, 1),
            [
                ("Mandatory training", courses),
                ("Probation", "Probation lasts three months, with a review at six weeks."),
            ],
            readers=OPS,
        )
        for role, courses in rows
    ]


# ----------------------------------------------------------------------------- single policies


def policies() -> list[Document]:
    d = date
    return [
        doc(
            "loyalty-programme-terms",
            "Brindlewood Rewards Terms",
            "Marketing",
            d(2025, 6, 1),
            [
                (
                    "Earning",
                    "Members earn one point per euro spent, rounded down, on every order line that is not cancelled.",
                ),
                ("Redeeming", "500 points can be exchanged for a EUR 10 voucher."),
                ("Tiers", "Silver from 600 points and Gold from 1,500 points held."),
                ("Expiry", "Points expire 12 months after they are earned."),
            ],
        ),
        doc(
            "loyalty-programme-terms",
            "Brindlewood Rewards Terms",
            "Marketing",
            d(2026, 2, 1),
            [
                (
                    "Earning",
                    "Members earn one point per euro spent, rounded down, on every order line that is not cancelled.",
                ),
                ("Redeeming", "500 points can be exchanged for a EUR 10 voucher."),
                ("Tiers", "Silver from 600 points and Gold from 1,500 points held."),
                (
                    "Expiry",
                    "From 1 February 2026 points no longer expire while the member buys at least once a year.",
                ),
            ],
            version=2,
        ),
        doc(
            "gift-card-terms",
            "Gift Card Terms",
            "Customer Operations",
            d(2025, 9, 1),
            [
                (
                    "Validity",
                    "Gift cards are valid for 24 months from purchase and can be used in stores and online.",
                ),
                ("Values", "Cards are sold in values from EUR 25 to EUR 200."),
                ("Not refundable", "Gift cards cannot be exchanged for cash."),
            ],
        ),
        doc(
            "gift-card-accounting",
            "Gift Card Accounting",
            "Finance",
            d(2025, 9, 1),
            [
                (
                    "Liability",
                    "Selling a gift card creates a liability, not revenue. Revenue is recognised when the card is spent.",
                ),
                (
                    "Breakage",
                    "Balances unused at expiry are booked to account 4100, Gift card breakage.",
                ),
            ],
            readers=FIN,
        ),
        doc(
            "nps-methodology",
            "Net Promoter Score Methodology",
            "Customer Insight",
            d(2025, 10, 1),
            [
                (
                    "Question",
                    "Customers are asked, a few days after buying: how likely are you to recommend Brindlewood, from 0 to 10?",
                ),
                (
                    "Calculation",
                    "Promoters score 9-10 and detractors 0-6. NPS is the percentage of promoters minus the percentage of detractors, from -100 to +100. It is not an average score.",
                ),
                (
                    "Not to be confused with",
                    "CSAT is the 1-5 rating after a support ticket; the staff survey measures employee engagement.",
                ),
            ],
        ),
        doc(
            "web-analytics-definitions",
            "Web Analytics Definitions",
            "E-commerce",
            d(2025, 11, 1),
            [
                (
                    "Sessions and users",
                    "A session is one visit; a user is a distinct visitor on a day. Users cannot be added across days.",
                ),
                ("Conversion rate", "Orders tracked in the browser divided by sessions."),
                (
                    "Online revenue",
                    "Browser-tracked order value including VAT, counted at checkout before cancellations and returns. It is always higher than net sales and must not be reported as revenue.",
                ),
            ],
        ),
        doc(
            "campaign-attribution-guide",
            "Campaign Attribution Guide",
            "Marketing",
            d(2025, 11, 1),
            [
                (
                    "How credit is given",
                    "Ad platforms credit an order to every campaign the customer clicked in the previous 7 days, so one order can be credited to several campaigns.",
                ),
                (
                    "Reporting",
                    "Attributed revenue is for comparing campaigns with each other. Total company revenue is net sales.",
                ),
            ],
        ),
        doc(
            "customer-data-protection",
            "Customer Data Protection",
            "Legal",
            d(2025, 5, 25),
            [
                (
                    "Lawful use",
                    "Customer data is used to fulfil orders, run the loyalty programme and, with consent, send marketing.",
                ),
                (
                    "Marketing consent",
                    "Only customers who opted in may receive marketing email; consent can be withdrawn at any time.",
                ),
                ("Retention", "Customer records are deleted 6 years after the last order."),
            ],
        ),
        doc(
            "customer-segments-guide",
            "Customer Segments Guide",
            "Marketing",
            d(2025, 12, 1),
            [
                (
                    "Segments",
                    "New (first order in 90 days), Regulars (2-5 orders in 12 months), VIP (6+ orders), Lapsed (no order in 12 months) and Trade accounts.",
                ),
                (
                    "Trade accounts",
                    "Interior designers and businesses on trade terms get a standing 12% discount; VIP customers get 5%.",
                ),
            ],
        ),
        doc(
            "email-marketing-standards",
            "Email Marketing Standards",
            "Marketing",
            d(2025, 12, 1),
            [
                ("Frequency", "No customer receives more than two marketing emails a week."),
                (
                    "Unsubscribe",
                    "Every email has a one-click unsubscribe; requests take effect within 48 hours.",
                ),
            ],
        ),
        doc(
            "health-and-safety-policy",
            "Health and Safety Policy",
            "Store Operations",
            d(2025, 4, 1),
            [
                (
                    "Responsibility",
                    "The store manager is the responsible person for health and safety in each store.",
                ),
                ("First aid", "Every shift must include at least one trained first aider."),
                ("Fire drills", "Each store runs a fire drill every six months."),
            ],
        ),
        doc(
            "incident-reporting-procedure",
            "Incident Reporting Procedure",
            "Store Operations",
            d(2025, 4, 1),
            [
                (
                    "When to report",
                    "Report every injury, theft, damage to stock, fire alarm and aggressive customer the same day.",
                ),
                (
                    "Severity",
                    "High severity means anyone was injured or losses exceed EUR 600; high severity incidents are reported to the area manager within one hour.",
                ),
            ],
        ),
        doc(
            "maintenance-service-levels",
            "Maintenance Service Levels",
            "Facilities",
            d(2025, 7, 1),
            [
                (
                    "Priorities",
                    "Urgent repairs (safety, security, no heating) are completed within 2 days, normal repairs within 10 days and low priority within 21 days.",
                ),
                (
                    "Costs",
                    "Repairs over EUR 1,500 need the area manager's approval before work starts.",
                ),
            ],
        ),
        doc(
            "store-audit-standards",
            "Store Audit Standards",
            "Store Operations",
            d(2025, 9, 1),
            [
                (
                    "Audits",
                    "Stores are audited for health and safety, visual merchandising and cash handling.",
                ),
                (
                    "Pass mark",
                    "A score of 80% or more is a pass. A failed audit is repeated within 30 days.",
                ),
            ],
        ),
        doc(
            "energy-and-sustainability-policy",
            "Energy and Sustainability Policy",
            "Facilities",
            d(2025, 6, 1),
            [
                (
                    "Targets",
                    "Electricity use per store is to fall 10% by 2027 against 2025, and 80% of electricity is to come from renewables.",
                ),
                ("In store", "Lights and heating switch to night mode 30 minutes after closing."),
            ],
        ),
        doc(
            "cash-handling-procedure",
            "Cash Handling Procedure",
            "Store Operations",
            d(2025, 3, 1),
            [
                ("Till counts", "Tills are counted at opening and closing by two people."),
                (
                    "Differences",
                    "Differences over EUR 20 are reported to the store manager the same day.",
                ),
            ],
        ),
        doc(
            "stock-transfer-procedure",
            "Stock Transfer Procedure",
            "Supply Chain",
            d(2025, 10, 1),
            [
                (
                    "Requests",
                    "Stores request stock from their regional distribution centre by 12:00 for dispatch within 3 working days.",
                ),
                (
                    "Receiving",
                    "Transfers are received on the day they arrive; a transfer not received within 5 days of dispatch is chased by the warehouse.",
                ),
            ],
        ),
        doc(
            "returned-goods-grading",
            "Returned Goods Grading",
            "Supply Chain",
            d(2025, 10, 1),
            [
                (
                    "Grades",
                    "Grade A items go back on sale at full price, grade B items are sold at a 30% discount, grade C items are written off.",
                ),
                (
                    "Write-off value",
                    "The write-off value is the stock value lost, which is different from the refund paid to the customer.",
                ),
            ],
        ),
        doc(
            "price-change-approval",
            "Price Change Approval",
            "Commercial",
            d(2025, 8, 1),
            [
                (
                    "Who approves",
                    "The category manager approves shelf price changes; changes of more than 20% also need the commercial director.",
                ),
                ("Timing", "Price changes take effect on a Monday, except clearance."),
            ],
        ),
        doc(
            "review-moderation-policy",
            "Product Review Moderation",
            "E-commerce",
            d(2025, 8, 1),
            [
                (
                    "Publishing",
                    "Reviews are published within 48 hours unless they contain personal data or offensive language.",
                ),
                (
                    "Verified purchase",
                    "A review is marked verified when the reviewer bought the product with the same account.",
                ),
            ],
        ),
        doc(
            "product-information-standards",
            "Product Information Standards",
            "Commercial",
            d(2025, 8, 1),
            [
                (
                    "Required details",
                    "Every product page shows colour, main material, dimensions in centimetres, weight and country of origin.",
                ),
                (
                    "Two-person delivery",
                    "Items over 30 kg or wider than 120 cm are delivered by two people.",
                ),
            ],
        ),
        doc(
            "mandatory-training-policy",
            "Mandatory Training Policy",
            "People",
            d(2025, 9, 1),
            [
                (
                    "Mandatory courses",
                    "Fire safety, Manual handling and Data protection are mandatory for everyone and are renewed every year.",
                ),
                (
                    "Overdue training",
                    "Training is overdue when the due date has passed without completion; managers are told weekly.",
                ),
            ],
            readers=OPS,
        ),
        doc(
            "recruitment-policy",
            "Recruitment Policy",
            "People",
            d(2025, 9, 1),
            [
                (
                    "Vacancies",
                    "A vacancy is opened when a role is approved and closed when a candidate accepts an offer.",
                ),
                (
                    "Time to hire",
                    "The target is to fill store roles within 30 days and manager roles within 60 days.",
                ),
            ],
            readers=OPS,
        ),
        doc(
            "staff-survey-guide",
            "Staff Engagement Survey Guide",
            "People",
            d(2025, 12, 1),
            [
                ("When", "The survey runs in January and April. Answers are anonymous."),
                (
                    "Scores",
                    "Engagement is rated 1 to 5; results are only reported for groups of five or more people.",
                ),
            ],
            readers=OPS,
        ),
        doc(
            "expense-policy",
            "Expense Policy",
            "Finance",
            d(2025, 1, 1),
            [
                ("Limits", "Meals up to EUR 30 per day; hotels up to EUR 120 per night."),
                ("Claims", "Claims must be submitted within 60 days with receipts."),
            ],
        ),
        doc(
            "expense-policy",
            "Expense Policy",
            "Finance",
            d(2026, 3, 1),
            [
                ("Limits", "Meals up to EUR 35 per day; hotels up to EUR 140 per night."),
                ("Claims", "Claims must be submitted within 30 days with receipts."),
            ],
            version=2,
        ),
        doc(
            "payroll-calendar-2026",
            "Payroll Calendar 2026",
            "Finance",
            d(2025, 12, 1),
            [
                (
                    "Pay day",
                    "Employees are paid on the 25th of each month, or the working day before.",
                ),
                (
                    "Overtime",
                    "Overtime is paid at 1.5 times the hourly rate in the month after it is worked.",
                ),
            ],
            readers=FIN,
        ),
        doc(
            "chart-of-accounts",
            "Chart of Accounts Guide",
            "Finance",
            d(2025, 1, 1),
            [
                (
                    "Revenue",
                    "4000 Sales revenue, booked at month end net of cancellations; 4100 Gift card breakage; 4900 Sales returns (refunds).",
                ),
                (
                    "Costs",
                    "5000 Cost of goods sold; 6100 Staff costs; 6200 Rent; 6300 Energy; 6400 Marketing; 6500 Repairs and maintenance; 6600 Delivery.",
                ),
                (
                    "Revenue is not net sales",
                    "Ledger revenue is reconciled to net sales after month-end close; before close they can differ.",
                ),
            ],
            readers=FIN,
        ),
        doc(
            "supplier-invoice-approval",
            "Supplier Invoice Approval",
            "Finance",
            d(2025, 6, 1),
            [
                (
                    "Three-way match",
                    "An invoice is paid only when it matches the purchase order and the goods received.",
                ),
                ("Disputes", "Disputed invoices are put on hold and are not counted as overdue."),
            ],
            readers=FIN,
        ),
        doc(
            "product-cost-methodology",
            "Product Cost and Margin Methodology",
            "Finance",
            d(2025, 6, 1),
            [
                (
                    "Landed cost",
                    "Margin reporting uses landed cost: purchase cost plus freight, set monthly per product.",
                ),
                (
                    "Purchase orders",
                    "The unit cost on a purchase order excludes freight, so margins computed from it are overstated.",
                ),
            ],
            readers=FIN,
        ),
        doc(
            "security-and-loss-prevention",
            "Security and Loss Prevention",
            "Store Operations",
            d(2025, 5, 1),
            [
                ("Tagging", "All items over EUR 50 carry a security tag."),
                (
                    "Theft",
                    "Staff must never challenge a suspected thief; report it as an incident.",
                ),
            ],
        ),
        doc(
            "it-acceptable-use",
            "IT Acceptable Use",
            "IT",
            d(2025, 2, 1),
            [
                (
                    "Accounts",
                    "Accounts are personal and must not be shared; passwords are changed every 180 days.",
                ),
                ("Devices", "Store tablets are for work use only."),
            ],
        ),
        doc(
            "customer-service-faq",
            "Customer Service FAQ",
            "Customer Operations",
            d(2026, 1, 15),
            [
                (
                    "Where is my order?",
                    "Check the carrier tracking link first; parcels count as late only after the promised date.",
                ),
                (
                    "Can I change my delivery address?",
                    "Only before dispatch; after dispatch contact the carrier.",
                ),
            ],
        ),
        doc(
            "store-staff-faq",
            "Store Staff FAQ",
            "Store Operations",
            d(2026, 1, 15),
            [
                (
                    "Who do I call for a broken door?",
                    "Log a maintenance request with priority urgent if the store cannot be locked.",
                ),
                (
                    "Can I swap a shift?",
                    "Yes, with the store manager's approval at least 48 hours ahead.",
                ),
            ],
            readers=OPS,
        ),
        doc(
            "finance-faq",
            "Finance FAQ",
            "Finance",
            d(2026, 1, 15),
            [
                (
                    "When is the month closed?",
                    "Five working days after month end; April 2026 is not closed yet.",
                ),
                (
                    "Which revenue figure do I quote?",
                    "Net sales, as defined in the sales metric definitions.",
                ),
            ],
            readers=FIN,
        ),
    ]


def generate_documents() -> list[Document]:
    stores = _read("reference", "stores")
    staff = _read("stage_b", "staff")
    suppliers = _read("stage_b", "suppliers")
    docs = []
    docs += store_guides(stores, staff)
    docs += supplier_agreements(suppliers)
    docs += carrier_agreements()
    docs += campaign_briefs()
    docs += warehouse_manuals()
    docs += category_guides()
    docs += region_briefings()
    docs += monthly_reviews()
    docs += role_guides()
    docs += policies()
    ids = [d.filename for d in docs]
    assert len(ids) == len(set(ids)), "duplicate document file"
    return docs
