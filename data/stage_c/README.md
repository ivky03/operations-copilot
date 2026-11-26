# Stage C dataset: Brindlewood Home at scale (synthetic)

**All data here is synthetic.** Stage C is Stage B (`../stage_b`, unchanged, which includes
Stage A) plus 35 tables and 95 documents:

| | Stage A | Stage B | Stage C |
|---|---:|---:|---:|
| Tables | 4 | 15 | **50** |
| Columns | 33 | 118 | **450** |
| Documents | 11 | 21 | **114** |
| Search passages | | | 352 |

Stage C exists for the scale experiment: it makes *choosing the right table* hard, the way it is
in a real small company's warehouse. Load it into a database of its own:

```bash
cd backend
uv run python -m app.cli seed-reference --stage c
uv run python -m app.cli publish-reference --stage c    # about 10 s
uv run python -m datagen --stage c                       # regenerate tables, manifests, documents
```

Everything in `tables/`, `manifests/` and `documents/` and `access.yaml` is written by
`backend/datagen/stage_c.py` and `stage_c_docs.py`. Each table is defined once, manifest and
rows together, so the two can't disagree. Don't edit the files by hand: a test fails if they
differ from what the generator writes.

## Tables added

| Domain | Tables | Readers |
|---|---|---|
| Customers and loyalty | `customers`, `customer_segments`, `loyalty_members`, `loyalty_points`, `gift_cards`, `nps_responses` | both groups |
| Marketing and web | `marketing_campaigns`, `campaign_spend_daily`, `email_sends`, `web_sessions_daily`, `product_page_views` (27,270 rows, the largest), `site_searches` | both groups |
| Catalogue and pricing | `product_categories`, `price_changes`, `product_reviews`, `product_attributes` | both groups |
| | `product_costs` | Finance |
| Store operations | `store_footfall_daily`, `energy_usage`, `store_incidents`, `maintenance_requests`, `store_audits`, `opening_hours` | both groups |
| Warehousing | `warehouses`, `warehouse_stock`, `stock_transfers`, `carriers`, `return_inspections` | both groups |
| People | `training_records`, `vacancies`, `staff_survey` | Operations |
| | `payroll` | Finance |
| Finance | `gl_entries`, `supplier_invoices`, `expenses` | Finance |

An Operations employee can read 43 of the 50 tables, and Finance 45.

## Tables that agree with each other

Where two tables record the same events, one is derived from the other, so they differ only in
the ways real systems do:
- **Web analytics orders** are the real online orders, one for one. Their revenue includes VAT
  and isn't reduced by cancellations, so it is always higher than net sales.
- **Campaign-attributed revenue** is real online orders credited to the campaigns running that
  day. One order is often credited to several campaigns, so it adds up to more than the sales.
- **Ledger revenue** (account 4000) is booked from each store's monthly sales, net of
  cancellations, within 3% before reconciliation. Refunds are on account 4900.
- **Loyalty points** are earned at one per euro on real orders.
- **Supplier invoices** are raised for received purchase orders, on each supplier's payment terms.
- **Payroll** covers each employee for the months they were employed.

Documents state the same facts as the tables wherever both hold them: opening hours, supplier
lead times and payment terms, carrier names and delivery days, campaign budgets and dates, the
30-day returns window, and the loyalty rules.

## Look-alikes

| Asked for | Right table | Look-alikes that answer something else |
|---|---|---|
| Revenue | net sales (`transactions`, `returns`) | `web_sessions_daily.online_revenue`, `campaign_spend_daily.attributed_revenue`, `gl_entries` 4000, `sales_daily_summary.dashboard_revenue` |
| Orders | `transactions` | web-tracked orders, attributed orders, `purchase_orders`, `stock_transfers` |
| Stock | `inventory_snapshots` (stores, daily) | `warehouse_stock` (distribution centres, weekly) |
| Satisfaction | depends who: support CSAT, customer NPS, staff engagement, product ratings | each of the others |
| Spend | `campaign_spend_daily` (media) | `marketing_campaigns.budget` (plan), `expenses` (claims), `gl_entries` 6400 |
| Cost | `product_costs` (landed, Finance) | `purchase_orders.unit_cost` (no freight) |
| People | `staff` (employed) | `vacancies` (open roles), `payroll` |
| Visitors | `store_footfall_daily` | orders, web sessions |

## Documents added

Most come in families, which is what makes document search hard at scale: one per store or web
shop (8), supplier (10), carrier (4), campaign (8), distribution centre (3), category (5),
country (6), region and month (12), and new-starter role (4), plus 33 single policies. Two
policies get a second version that changes a rule (loyalty points expiry; expense limits).
Each document holds facts found in no other, so every retrieval question has one right answer.

## Evaluation set

`evals/golden/stage_c.yaml` has 47 questions. Each names the tables a correct plan must use, and
is grouped by the smallest step of the scale experiment that can answer it: 8 need only the
4 Stage A tables, 8 need Stage B (15 tables), 15 need the 30-table step (Stage B plus the 15
tables in `SIZE_30` in `datagen/stage_c.py`), and 16 need all 50. Expected answers come from
the independent pandas oracle (`uv run python -m evals.build_expected stage_c`).

## Scale, honestly

Volumes follow the reference company's small sales history (about 430 order lines over four
months), not a big retailer's, so tables that count visits or credit sales stay believable next
to the orders. The plan estimated 600+ columns and a 100,000-row table; Stage C has 450 columns
and its largest table has 27,270 rows. What the experiment depends on is the number of tables
and how alike they are, and the size of the description the planner reads: about 11,000 tokens
for all 50 tables.
