"""Synthetic reference dataset generator for Operations Copilot.

Produces a small, fully deterministic dataset for a fictional home-goods retailer,
"Brindlewood Home". Same seed, same rows, every time, so evaluation questions can have
exact expected answers.

The data is synthetic and contains deliberate edge cases (cancellations, missing customer IDs,
ambiguous product names, a mid-period store launch, a discontinued product, a promotion).
"""
