# Schema retrieval: Stage C (2026-11-28, commit b859fad)

Schema retrieval evaluation (deterministic, local embeddings, no model). Embedding model `BAAI/bge-small-en-v1.5`. A case is *complete* when
every table it needs is among the tables described to the planner. Tokens are
estimated (characters ÷ 4) for the table descriptions only.

## Tuning set (stage_c.yaml)

| Tables | k | Complete | Tables shown | Description tokens | Full description |
|---:|---:|---:|---:|---:|---:|
| 4 | 3 | 8/8 | 4 | 1,020 | 1,020 |
| 4 | 5 | 8/8 | 4 | 1,020 | 1,020 |
| 4 | 8 | 8/8 | 4 | 1,020 | 1,020 |
| 15 | 3 | 15/16 | 5.3 | 1,415 | 3,590 |
| 15 | 5 | 16/16 | 7.4 | 2,042 | 3,590 |
| 15 | 8 | 16/16 | 9.8 | 2,682 | 3,590 |
| 30 | 3 | 31/32 | 5.5 | 1,335 | 6,258 |
| 30 | 5 | 32/32 | 8.0 | 2,009 | 6,258 |
| 30 | 8 | 32/32 | 11.3 | 2,829 | 6,258 |
| 50 | 3 | 45/47 | 5.6 | 1,275 | 9,628 |
| 50 | 5 | 47/47 | 8.1 | 1,879 | 9,628 |
| 50 | 8 | 47/47 | 11.7 | 2,724 | 9,628 |

Missed:

- 15 tables, k=3: c4-05 (needs transactions)
- 30 tables, k=3: c4-05 (needs transactions)
- 50 tables, k=3: c4-05 (needs transactions)
- 50 tables, k=3: c15-05 (needs staff)

## Heldout set (stage_c.schema_heldout.yaml)

| Tables | k | Complete | Tables shown | Description tokens | Full description |
|---:|---:|---:|---:|---:|---:|
| 4 | 3 | 5/5 | 4 | 1,020 | 1,020 |
| 4 | 5 | 5/5 | 4 | 1,020 | 1,020 |
| 4 | 8 | 5/5 | 4 | 1,020 | 1,020 |
| 15 | 3 | 12/13 | 5.2 | 1,371 | 3,594 |
| 15 | 5 | 13/13 | 7.2 | 1,983 | 3,594 |
| 15 | 8 | 13/13 | 9.5 | 2,650 | 3,594 |
| 30 | 3 | 23/24 | 5.4 | 1,271 | 6,272 |
| 30 | 5 | 23/24 | 7.8 | 1,909 | 6,272 |
| 30 | 8 | 23/24 | 11.2 | 2,796 | 6,272 |
| 50 | 3 | 34/36 | 5.4 | 1,201 | 9,631 |
| 50 | 5 | 35/36 | 8.1 | 1,858 | 9,631 |
| 50 | 8 | 35/36 | 11.9 | 2,720 | 9,631 |

Missed:

- 15 tables, k=3: h-02 (needs returns, transactions)
- 30 tables, k=3: h-02 (needs returns, transactions)
- 30 tables, k=5: h-02 (needs returns, transactions)
- 30 tables, k=8: h-02 (needs returns, transactions)
- 50 tables, k=3: h-02 (needs returns, transactions)
- 50 tables, k=3: h-08 (needs purchase_orders)
- 50 tables, k=5: h-02 (needs returns, transactions)
- 50 tables, k=8: h-02 (needs returns, transactions)
