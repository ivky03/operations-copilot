# Retrieval evaluation: stage_a documents (2026-11-19)

> **retrieval evaluation (deterministic; local embeddings, no language model).** Same data and code give the same numbers.

- Commit `00c394a`; embedding model `BAAI/bge-small-en-v1.5`; top 5
- 34 ranked queries + 5 leak probes

| Mode | hit@1 | hit@3 | hit@5 | recall@5 | MRR | leaks | stale |
|---|---|---|---|---|---|---|---|
| keyword | 0.735 | 0.882 | 0.912 | 0.897 | 0.811 | 0 | 0 |
| vector | 0.853 | 0.971 | 0.971 | 0.971 | 0.907 | 0 | 0 |
| hybrid | 0.853 | 0.971 | 1.0 | 0.985 | 0.914 | 0 | 0 |

## Hybrid: queries where the first relevant section was not ranked 1st

- **r-003** (operations) “What was the return window before March 2026?”: rank 2
  - wanted: returns-refund-policy@v1#Return window
  - got: returns-refund-policy@v2#Return window, returns-refund-policy@v1#Return window, store-operations-handbook@v1#Processing a return in store
- **r-014** (operations) “How long do customers have to send products back?”: rank 3
  - wanted: returns-refund-policy@v2#Return window
  - got: cancellation-policy@v1#When customers can cancel, returns-refund-policy@v2#Condition of returned goods, returns-refund-policy@v2#Return window
- **r-015** (operations) “how long can customers send stuff back”: rank 2
  - wanted: returns-refund-policy@v2#Return window
  - got: cancellation-policy@v1#When customers can cancel, returns-refund-policy@v2#Return window, store-operations-handbook@v1#Escalation
- **r-018** (operations) “Steps for taking back an item at the till”: rank 4
  - wanted: store-operations-handbook@v1#Processing a return in store
  - got: cancellation-policy@v1#When customers can cancel, store-operations-handbook@v1#End-of-day cash-up, data-quality-notes@v1#Missing return reasons
- **r-034** (operations) “How fast were refunds paid under the old policy?”: rank 2
  - wanted: returns-refund-policy@v1#Refund timing
  - got: returns-refund-policy@v2#Refund timing, returns-refund-policy@v1#Refund timing, returns-refund-policy@v2#Introduction
