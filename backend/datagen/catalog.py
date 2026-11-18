"""Fixed reference entities: stores and products.

These are hand-written rather than random, because the edge cases they encode are deliberate
and the evaluation questions refer to them by name.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Store:
    store_id: str
    store_name: str
    country: str
    region: str
    channel: str  # "Retail" (physical) or "Online"
    opened_date: date


@dataclass(frozen=True)
class Product:
    product_id: str
    sku: str
    product_name: str
    category: str
    list_price: float  # EUR
    status: str  # "active" or "discontinued"
    discontinued_date: date | None = None


STORES: list[Store] = [
    Store("S01", "London Flagship", "United Kingdom", "UK & Ireland", "Retail", date(2019, 4, 1)),
    Store("S02", "UK Online", "United Kingdom", "UK & Ireland", "Online", date(2018, 1, 15)),
    Store("S03", "Dublin Store", "Ireland", "UK & Ireland", "Retail", date(2021, 6, 1)),
    Store("S04", "Berlin Store", "Germany", "Central Europe", "Retail", date(2020, 9, 1)),
    Store("S05", "DE Online", "Germany", "Central Europe", "Online", date(2019, 1, 10)),
    Store("S06", "Amsterdam Store", "Netherlands", "Central Europe", "Retail", date(2022, 3, 1)),
    Store("S07", "Madrid Store", "Spain", "Southwest Europe", "Retail", date(2023, 5, 1)),
    # Edge case: launches mid-period, so France has no sales in January or February.
    Store("S08", "FR Online", "France", "Southwest Europe", "Online", date(2026, 3, 2)),
]

PRODUCTS: list[Product] = [
    # Lighting. Edge case: three products match "desk lamp".
    Product("P001", "LGT-1001", "Blue Desk Lamp", "Lighting", 39.00, "active"),
    Product("P002", "LGT-1002", "White Desk Lamp", "Lighting", 39.00, "active"),
    Product("P003", "LGT-1003", "Desk Lamp Shade", "Lighting", 14.50, "active"),
    Product("P004", "LGT-1004", "Brass Floor Lamp", "Lighting", 129.00, "active"),
    Product("P005", "LGT-1005", "Paper Pendant Light", "Lighting", 54.00, "active"),
    # Kitchen
    Product("P006", "KIT-2001", "Stoneware Mug", "Kitchen", 9.50, "active"),
    Product("P007", "KIT-2002", "Enamel Kettle", "Kitchen", 44.00, "active"),
    Product("P008", "KIT-2003", "Olive Wood Board", "Kitchen", 28.00, "active"),
    Product("P009", "KIT-2004", "Glass Storage Jar", "Kitchen", 12.00, "active"),
    Product("P010", "KIT-2005", "Cast Iron Skillet", "Kitchen", 49.00, "active"),
    # Textiles (on promotion in April, see SPRING10 in the pricing policy)
    Product("P011", "TXT-3001", "Linen Throw", "Textiles", 59.00, "active"),
    Product("P012", "TXT-3002", "Wool Cushion Cover", "Textiles", 24.00, "active"),
    Product("P013", "TXT-3003", "Cotton Bath Towel", "Textiles", 18.00, "active"),
    Product("P014", "TXT-3004", "Jute Area Rug", "Textiles", 149.00, "active"),
    Product("P015", "TXT-3005", "Linen Napkin Set", "Textiles", 22.00, "active"),
    # Furniture. Edge case: two products match "oak table".
    Product("P016", "FUR-4001", "Oak Side Table", "Furniture", 189.00, "active"),
    Product("P017", "FUR-4002", "Oak Coffee Table", "Furniture", 329.00, "active"),
    Product("P018", "FUR-4003", "Rattan Lounge Chair", "Furniture", 279.00, "active"),
    Product("P019", "FUR-4004", "Pine Bookshelf", "Furniture", 159.00, "active"),
    Product("P020", "FUR-4005", "Walnut Bench", "Furniture", 239.00, "active"),
    # Decor. Edge case: discontinued at the end of February, so no sales from March on.
    Product("P021", "DEC-5001", "Glass Vase", "Decor", 34.00, "discontinued", date(2026, 2, 28)),
    Product("P022", "DEC-5002", "Ceramic Planter", "Decor", 26.00, "active"),
    Product("P023", "DEC-5003", "Wall Mirror", "Decor", 119.00, "active"),
    Product("P024", "DEC-5004", "Scented Candle", "Decor", 16.00, "active"),
    Product("P025", "DEC-5005", "Framed Print", "Decor", 64.00, "active"),
]

PROMO_CODE = "SPRING10"
PROMO_CATEGORY = "Textiles"
PROMO_MONTH = 4  # April 2026
PROMO_DISCOUNT = 0.10
