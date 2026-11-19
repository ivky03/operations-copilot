"""Export the API contract (OpenAPI schema) for the frontend.

The React app generates its TypeScript types from this file, so frontend code can't silently
disagree with what the backend returns. A test fails if the committed file is out of date.

Usage (from backend/):  uv run python -m app.openapi_export
"""

import json
from pathlib import Path

from app.main import app

CONTRACT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    CONTRACT.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT.write_text(render(), encoding="utf-8")
    print(f"Wrote {CONTRACT}")


if __name__ == "__main__":
    main()
