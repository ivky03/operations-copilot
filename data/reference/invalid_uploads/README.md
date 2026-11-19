# Deliberately invalid uploads (synthetic)

Example files that must be **rejected**, used by tests and demos of validation messages.

| File | What's wrong |
|---|---|
| `transactions_with_errors.csv` | Text in a number column, missing required value, decimal quantity, duplicate key, non-ISO date, invalid boolean, missing columns on a row |
| `transactions_unknown_store.csv` | Every row is well-formed, but references a store (`S99`) and product (`P999`) that don't exist in the published `stores` and `products` datasets |
