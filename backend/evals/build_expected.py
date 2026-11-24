"""Validate the golden set and write its expected numeric answers.

Usage (from backend/):  uv run python -m evals.build_expected [stage_a | stage_b | ...]
"""

import sys
from collections import Counter

from evals.golden import (
    GOLDEN_DIR,
    build_expected,
    load_golden,
    oracle_for,
    render_expected,
    validate,
)


def main(name: str = "stage_a") -> int:
    golden = load_golden(name)
    problems = validate(golden)
    if problems:
        print("Golden set has problems:", *problems, sep="\n  - ")
        return 1
    expected = build_expected(golden, oracle_for(golden))
    out = GOLDEN_DIR / f"{name}.expected.json"
    out.write_text(render_expected(expected), encoding="utf-8")

    counts = Counter(c["category"] for c in golden["cases"])
    print(f"Golden set '{name}': {len(golden['cases'])} cases, valid.")
    for category, n in sorted(counts.items()):
        print(f"  {category:<16} {n}")
    print(f"Expected answers for {len(expected)} steps with data -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:2]))
