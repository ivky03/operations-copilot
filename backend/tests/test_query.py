"""The query engine: compiler vs oracle, authorization, safety layers, and value grounding."""

import json
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.cli import publish_reference, seed_reference
from app.models import Source, SourceGrant
from app.models import User as UserModel
from app.query import executor as executor_module
from app.query.catalog import Catalog, PlanError
from app.query.compiler import compile_plan
from app.query.executor import QueryTimeoutError, execute, run_readonly
from app.query.grounding import ground
from app.query.plan import QueryPlan
from evals.golden import GOLDEN_DIR
from tests.conftest import login

OPS = "ops.employee@brindlewood.example"
EXPECTED = json.loads((GOLDEN_DIR / "stage_a.expected.json").read_text())
SPECS = [
    pytest.param(item["query"], item["rows"], id=f"{step}[{i}]")
    for step, items in EXPECTED.items()
    for i, item in enumerate(items)
]


@pytest.fixture
def published(db):
    passwords = seed_reference(db)
    publish_reference(db)
    return passwords


@pytest.fixture
def catalog(db, published):
    olivia = db.scalar(select(UserModel).where(UserModel.email == OPS))
    return Catalog.for_user(db, olivia)


def _month_range(months: list[str]) -> dict:
    first, last = min(months), max(months)
    year, month = map(int, last.split("-"))
    end = date(year + (month == 12), month % 12 + 1, 1)
    return {"start": f"{first}-01", "end": end.isoformat()}


def plan_from_oracle_query(spec: dict) -> QueryPlan:
    """Translate an oracle query (golden set) into a QueryPlan, as the planner LLM will in M3b."""
    filters = dict(spec.get("filters", {}))
    months = filters.pop("month", None)
    group_by = spec.get("group_by", [])
    return QueryPlan(
        metric=spec["metric"],
        dimensions=[d for d in group_by if d != "month"],
        filters=[{"dimension": k, "op": "in", "values": v} for k, v in filters.items()],
        time_range=_month_range(months) if months else None,
        time_grain="month" if "month" in group_by else None,
        order="metric_desc" if spec.get("order_by_value") else "dimensions",
        limit=spec.get("limit"),
    )


def _normalise(rows: list[dict], metric: str) -> list[dict]:
    digits = 4 if metric == "cancellation_rate" else 2
    out = []
    for row in rows:
        value = row[metric]
        out.append({**row, metric: None if value is None else round(float(value), digits)})
    return out


# ----------------------------------------------------------------------------- correctness


@pytest.mark.parametrize(("spec", "oracle_rows"), SPECS)
def test_compiled_sql_matches_the_independent_oracle(catalog, spec, oracle_rows):
    """Every numeric expectation in the golden set, computed two unrelated ways, must agree."""
    plan = plan_from_oracle_query(spec)
    result = execute(compile_plan(plan, catalog))
    got = _normalise(result.rows, spec["metric"])
    # The oracle puts the time column under "month"; the plan's grain is also "month".
    assert got == _normalise(oracle_rows, spec["metric"])


def test_answers_record_the_dataset_versions_they_used(catalog):
    compiled = compile_plan(QueryPlan(metric="net_sales", dimensions=["country"]), catalog)
    assert {(s.slug, s.version) for s in compiled.sources} == {
        ("transactions", 1),
        ("returns", 1),
        ("stores", 1),
    }


def test_dates_in_results_are_json_safe(catalog):
    """Regression: a result with a date column crashed saving the answer trace (eval run 1)."""
    plan = QueryPlan(
        metric="gross_sales",
        dimensions=["opened_date"],
        filters=[{"dimension": "country", "values": ["France"]}],
    )
    rows = execute(compile_plan(plan, catalog)).rows
    assert rows and rows[0]["opened_date"] == "2026-03-02"
    json.dumps(rows)  # must not raise


# ----------------------------------------------------------------------------- authorization


def test_catalog_only_contains_readable_datasets(db, published):
    stores = db.scalar(select(Source).where(Source.slug == "returns"))
    db.query(SourceGrant).filter(SourceGrant.source_id == stores.id).delete()
    db.commit()
    olivia = db.scalar(select(UserModel).where(UserModel.email == OPS))
    catalog = Catalog.for_user(db, olivia)

    assert "returns" not in catalog.datasets
    compile_plan(QueryPlan(metric="gross_sales"), catalog)  # still fine
    with pytest.raises(PlanError, match="Unknown metric"):
        compile_plan(QueryPlan(metric="net_sales"), catalog)  # needs returns.refund_value
    with pytest.raises(PlanError, match="Unknown metric"):
        compile_plan(QueryPlan(metric="refund_value"), catalog)


def test_unknown_and_restricted_look_the_same(catalog):
    for name in ("profit_margin", "returns.no_such_metric"):
        with pytest.raises(PlanError, match="Unknown metric"):
            compile_plan(QueryPlan(metric=name), catalog)
    with pytest.raises(PlanError, match="Unknown dimension 'web_visitors'"):
        compile_plan(QueryPlan(metric="gross_sales", dimensions=["web_visitors"]), catalog)


# ----------------------------------------------------------------------------- safety


@pytest.mark.parametrize("bad", ["country; DROP TABLE users", "Country", "a.b.c", "1country"])
def test_plan_rejects_names_that_are_not_identifiers(bad):
    with pytest.raises(ValidationError):
        QueryPlan(metric="gross_sales", dimensions=[bad])


def test_filter_values_are_parameters_not_sql(catalog):
    hostile = "x' OR '1'='1"
    plan = QueryPlan(
        metric="gross_sales", filters=[{"dimension": "country", "op": "=", "values": [hostile]}]
    )
    compiled = compile_plan(plan, catalog)
    assert hostile not in compiled.sql and hostile in compiled.params.values()
    assert execute(compiled).rows == []  # nothing matched; nothing leaked


def test_reader_role_cannot_write():
    with pytest.raises(Exception, match="read-only transaction"):
        run_readonly("CREATE TABLE datasets.hack (x int)", {})


def test_reader_role_cannot_see_application_tables(published):
    with pytest.raises(Exception, match="permission denied"):
        run_readonly("SELECT email, password_hash FROM users", {})


def test_slow_queries_are_stopped(monkeypatch):
    monkeypatch.setattr(executor_module, "STATEMENT_TIMEOUT_MS", 200)
    with pytest.raises(QueryTimeoutError):
        run_readonly("SELECT pg_sleep(2)", {})


# ----------------------------------------------------------------------------- grounding


@pytest.mark.parametrize(
    ("phrase", "column", "status", "value"),
    [
        ("UK", None, "alias", "United Kingdom"),
        ("Holland", None, "alias", "Netherlands"),
        ("untied kingdom", None, "fuzzy", "United Kingdom"),
        ("Berlin shop", ("stores", "store_name"), "fuzzy", "Berlin Store"),
        ("textile", None, "fuzzy", "Textiles"),
        ("online", None, "exact", "Online"),
        ("web shops", ("stores", "channel"), "fuzzy", "Online"),
        ("Brass Floor Lamp", None, "exact", "Brass Floor Lamp"),
        ("Narnia", None, "not_found", None),
    ],
)
def test_grounding_resolves_phrases(db, catalog, phrase, column, status, value):
    g = ground(db, catalog, phrase, column)
    assert g.status == status
    assert (g.match.value if g.match else None) == value


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("desk lamp", {"Blue Desk Lamp", "White Desk Lamp", "Desk Lamp Shade"}),
        ("oak table", {"Oak Side Table", "Oak Coffee Table"}),
    ],
)
def test_grounding_reports_ambiguity_instead_of_guessing(db, catalog, phrase, expected):
    g = ground(db, catalog, phrase, ("products", "product_name"))
    assert g.status == "ambiguous" and g.match is None
    assert {c.value for c in g.candidates} == expected


# ----------------------------------------------------------------------------- API


def test_query_and_ground_endpoints(client, published):
    csrf = login(client, OPS, published[OPS])
    h = {"X-CSRF-Token": csrf}
    resp = client.post(
        "/api/query",
        json={
            "metric": "net_sales",
            "filters": [{"dimension": "country", "values": ["Ireland"]}],
            "time_range": {"start": "2026-03-01", "end": "2026-04-01"},
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == [{"net_sales": -509.0}]
    assert {s["source"] for s in body["sources"]} == {"transactions", "returns", "stores"}

    assert client.post("/api/query", json={"metric": "profit"}, headers=h).status_code == 422
    g = client.post("/api/ground", json={"phrase": "UK"}, headers=h).json()
    assert (g["status"], g["match"]["value"]) == ("alias", "United Kingdom")
