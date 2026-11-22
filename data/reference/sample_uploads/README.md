# Sample uploads (synthetic)

Files for trying the steward console (**Manage sources**) by hand. Upload them as new
versions; nothing changes for anyone until you press **Publish**.

| File | Upload to | What happens |
|---|---|---|
| `stores_with_lisbon.csv` (+ `manifests/stores.yaml`) | `stores` | Ready to review: 9 rows instead of 8 (adds S09, Lisbon). Publishes. |
| `stores_without_london.csv` (+ `manifests/stores.yaml`) | `stores` | Ready to review, but **Publish is refused**: published `transactions` still use store `S01`. Discard it. |
| `shipping-and-delivery.v2.md` | `shipping-and-delivery` | Ready to review, with one new section ("Weekend deliveries"). Publishes. |
| `../invalid_uploads/transactions_with_errors.csv` (+ `manifests/transactions.yaml`) | `transactions` | Rejected, with each problem listed by row. |

Publishing changes what answers use from the next question, so try this on a copy of the
database if you want to keep the reference data as generated.
