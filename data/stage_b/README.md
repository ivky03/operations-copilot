# Stage B dataset: Brindlewood Home (synthetic)

**All data here is synthetic.** Stage B is Stage A (`../reference`, unchanged) plus eleven
tables and ten documents, for 15 tables and 21 documents in all. The extra tables are what a
small retailer's operations actually look like, and they make choosing the right table harder.

Load it into a database of its own (Stage A's evaluation set assumes Stage A data):

```bash
cd backend
uv run python -m app.cli seed-reference --stage b
uv run python -m app.cli publish-reference --stage b    # about 3 s
uv run python -m datagen --stage b                       # regenerate the CSVs (deterministic)
```

## Tables added

| Table | Rows | Grain | Readers |
|---|---:|---|---|
| `suppliers` | 10 | One per supplier | both groups |
| `purchase_orders` | 90 | One per purchase order line | both groups |
| `inventory_snapshots` | 22,500 | Store × product × day (end of day) | both groups |
| `promotions` | 5 | One per promotion | both groups |
| `shipments` | 107 | One per shipped online order | both groups |
| `support_tickets` | 289 | One per customer contact | both groups |
| `sales_daily_summary` | 148 | Store × day with orders | both groups |
| `staff` | 35 | One per employee | Operations |
| `shifts` | 2,182 | One per scheduled shift | Operations |
| `sales_targets` | 31 | Store × month | Finance |
| `budgets` | 64 | Month × department × cost category | Finance |

## How the tables agree with each other

Generated from Stage A, not independently (`backend/datagen/stage_b.py`):
- Stock goes **down** with Stage A sales and **up** when purchase orders arrive; cancelled
  orders and returns go back into stock.
- Purchase orders are raised by the reorder rule in the purchasing procedure (stock ≤ 2 → order
  12, or 4 for furniture, from the preferred supplier).
- Every non-cancelled online order has a shipment; promised dates follow the delivery
  standards.
- Every return has a support ticket; late deliveries and some cancellations do too.
- The dashboard summary equals gross sales computed from transactions (tested).

## Deliberate traps

| Trap | Where | Why it matters |
|---|---|---|
| Stock is point-in-time | `inventory_snapshots` | Summing units on hand across days is meaningless |
| Dashboard revenue ≠ net sales | `sales_daily_summary` | Look-alike of the governed revenue measure |
| Orders vs purchase orders | `transactions`, `purchase_orders` | Same word, different things |
| Targets look like sales | `sales_targets` | A plan, not an actual |
| Leavers and joiners | `staff` (Berlin: one leaves 27 Mar, one joins 16 Mar) | Headcount depends on the date |
| Open purchase orders, in-transit parcels, open tickets | several | Empty end dates; in-transit parcels are not "late" |
| April not closed | `budgets` | Actuals are empty, not zero |
| CSAT answered by ~55% | `support_tickets` | Average over respondents only |
| People data for Operations, targets and budgets for Finance | access | Permission boundaries, including in what the planner is shown |

## Questions that change answerability

Some Stage A questions are unanswerable *by design* with Stage A data, and become answerable
here: "How many employees work in the Berlin store?" (staff), "What is our customer
satisfaction score?" (support tickets). Each evaluation set therefore belongs to a data stage.
Questions about profit margin, website visitors or December 2025 sales stay unanswerable:
purchase costs are not cost of goods sold, and there is still no web analytics or 2025 data.
