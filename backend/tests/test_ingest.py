"""Manifest validation, CSV validation, and transactional publishing of datasets and documents."""

import copy

import pytest
import yaml
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.cli import REFERENCE, publish_reference, seed_reference
from app.ingest import publish as publish_module
from app.ingest.expressions import ExpressionError, Name, names, parse
from app.ingest.manifest import parse_manifest
from app.ingest.publish import publish_dataset, publish_document, published_version
from app.ingest.validate import validate_csv
from app.models import Source, SourceVersion, ValueIndexEntry, VersionStatus
from evals.oracle import Oracle, Query
from tests.conftest import login

MANIFESTS = REFERENCE / "manifests"
TABLES = REFERENCE / "tables"
INVALID = REFERENCE / "invalid_uploads"


def manifest(name: str) -> dict:
    return yaml.safe_load((MANIFESTS / f"{name}.yaml").read_text())


# ----------------------------------------------------------------------------- expressions


def test_expression_grammar_accepts_arithmetic():
    assert names(parse("-1 * quantity * unit_price")) == {"quantity", "unit_price"}
    assert names(parse("gross_sales - returns.refund_value")) == {
        "gross_sales",
        "returns.refund_value",
    }
    assert parse("(a)") == Name("a")


@pytest.mark.parametrize(
    "bad",
    ["price; DROP TABLE users", "sum(x)", "a +", "", "x' OR '1'='1", "a == b", "(a", "a b"],
)
def test_expression_grammar_rejects_anything_else(bad):
    with pytest.raises(ExpressionError):
        parse(bad)


# ----------------------------------------------------------------------------- manifests


@pytest.mark.parametrize("name", ["stores", "products", "transactions", "returns"])
def test_reference_manifests_are_valid(name):
    m, problems = parse_manifest(manifest(name))
    assert problems == [] and m is not None


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda m: m["columns"][0].update(name="line id; drop"), "should match pattern"),
        (lambda m: m.update(primary_keys=["line_id"]), "Extra inputs are not permitted"),
        (lambda m: m["metrics"][0].update(expression="quantity * price"), "unknown column 'price'"),
        (lambda m: m["metrics"][0].update(expression="store_id"), "can't sum non-numeric"),
        (lambda m: m["metrics"][0].update(expression="quantity; DELETE"), "not allowed"),
        (
            lambda m: m["derived_metrics"][1].update(formula="gross_sales / orders"),
            "unknown metric 'orders'",
        ),
        (lambda m: m.update(time_column="store_id"), "time_column must be a date"),
        (lambda m: m["columns"][4].update(aliases={"x": "S01"}), "aliases require value_index"),
        (lambda m: m["metrics"][0]["filters"][0].update(column="nope"), "filter on unknown column"),
    ],
)
def test_manifest_problems_are_reported_readably(mutate, expected):
    m = copy.deepcopy(manifest("transactions"))
    mutate(m)
    parsed, problems = parse_manifest(m)
    assert parsed is None
    assert any(expected in p for p in problems), problems


# ----------------------------------------------------------------------------- CSV validation


def test_reference_csv_is_valid():
    m, _ = parse_manifest(manifest("transactions"))
    result = validate_csv(m, (TABLES / "transactions.csv").read_bytes())
    assert result.ok and len(result.rows) == 430


def test_row_level_problems_are_reported_with_row_and_column():
    m, _ = parse_manifest(manifest("transactions"))
    result = validate_csv(m, (INVALID / "transactions_with_errors.csv").read_bytes())
    assert [str(i) for i in result.issues] == [
        "row 3, column unit_price: 'abc' is not a decimal number",
        "row 4, column store_id: value is required",
        "row 5, column quantity: '2.5' is not a whole number",
        "row 6, column line_id: duplicate key ('INV-900001-1',) (first seen on row 2)",
        "row 7, column invoice_date: '03/05/2026 14:00' is not a timestamp (YYYY-MM-DDTHH:MM:SS)",
        "row 8, column is_cancellation: 'maybe' is not true/false",
        "row 9: expected 11 values, found 9",
    ]


def test_header_mismatch_and_encoding_problems():
    m, _ = parse_manifest(manifest("stores"))
    wrong_header = (
        b"store_id,name,country,region,channel,opened_date\nS01,x,y,z,Retail,2020-01-01\n"
    )
    assert "missing ['store_name']" in str(validate_csv(m, wrong_header).issues[0])
    assert "not UTF-8" in str(validate_csv(m, b"\xff\xfe\x00").issues[0])
    bom = b"\xef\xbb\xbf" + (TABLES / "stores.csv").read_bytes()  # Excel's byte-order mark
    assert validate_csv(m, bom).ok


# ----------------------------------------------------------------------------- publishing


@pytest.fixture
def published(db):
    passwords = seed_reference(db)
    results = publish_reference(db)
    assert all(r.status == "published" for r in results), [
        r for r in results if r.status != "published"
    ]
    return passwords


def _row_count(db, slug: str) -> int:
    table = published_version(db, slug).meta["table"]
    return db.scalar(text(f'SELECT count(*) FROM datasets."{table}"'))  # noqa: S608


def test_reference_data_publishes_and_republishing_is_a_no_op(db, published):
    assert {
        name: _row_count(db, name) for name in ("stores", "products", "transactions", "returns")
    } == {
        "stores": 8,
        "products": 25,
        "transactions": 430,
        "returns": 24,
    }
    assert {r.status for r in publish_reference(db)} == {"unchanged"}
    assert db.scalar(select(func.count()).select_from(SourceVersion)) == 16


def test_superseded_document_version_is_kept(db, published):
    versions = db.execute(
        select(SourceVersion.version, SourceVersion.status)
        .join(Source)
        .where(Source.slug == "returns-refund-policy")
        .order_by(SourceVersion.version)
    ).all()
    assert versions == [(1, VersionStatus.SUPERSEDED), (2, VersionStatus.PUBLISHED)]


def test_database_allows_only_one_published_version_per_source(db, published):
    v1 = db.scalar(
        select(SourceVersion)
        .join(Source)
        .where(Source.slug == "returns-refund-policy", SourceVersion.version == 1)
    )
    v1.status = VersionStatus.PUBLISHED
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_sql_on_loaded_tables_matches_the_independent_oracle(db, published):
    """First cross-check of the two implementations: Postgres SQL vs pandas."""
    tx = published_version(db, "transactions").meta["table"]
    rt = published_version(db, "returns").meta["table"]
    gross = dict(
        db.execute(
            text(
                f"SELECT to_char(invoice_date, 'YYYY-MM'), sum(quantity * unit_price) "  # noqa: S608
                f'FROM datasets."{tx}" WHERE NOT is_cancellation GROUP BY 1'
            )
        ).all()
    )
    refunds = dict(
        db.execute(
            text(
                f"SELECT to_char(return_date, 'YYYY-MM'), sum(refund_amount) "  # noqa: S608
                f'FROM datasets."{rt}" GROUP BY 1'
            )
        ).all()
    )
    oracle = Oracle()
    assert {k: float(v) for k, v in gross.items()} == {
        r["month"]: r["gross_sales"] for r in oracle.run(Query("gross_sales", ["month"]))
    }
    assert {k: float(v) for k, v in refunds.items()} == {
        r["month"]: r["refund_value"] for r in oracle.run(Query("refund_value", ["month"]))
    }


def test_value_index_holds_real_values_and_aliases(db, published):
    uk = db.scalar(select(ValueIndexEntry).where(ValueIndexEntry.value_norm == "uk"))
    assert (uk.column_name, uk.canonical, uk.is_alias) == ("country", "United Kingdom", True)
    lamps = set(
        db.scalars(
            select(ValueIndexEntry.value).where(
                text("'lamp' <% value_norm"), ValueIndexEntry.column_name == "product_name"
            )
        )
    )
    assert {"Blue Desk Lamp", "White Desk Lamp", "Desk Lamp Shade"} <= lamps


def test_new_version_supersedes_and_keeps_history(db, published):
    shorter = b"".join((TABLES / "returns.csv").read_bytes().splitlines(keepends=True)[:-1])
    result = publish_dataset(db, manifest("returns"), shorter)
    assert (result.status, result.version, result.row_count) == ("published", 2, 23)
    statuses = dict(
        db.execute(
            select(SourceVersion.version, SourceVersion.status)
            .join(Source)
            .where(Source.slug == "returns")
        ).all()
    )
    assert statuses == {1: VersionStatus.SUPERSEDED, 2: VersionStatus.PUBLISHED}
    # Both physical tables still exist: historical answers can still show what they used.
    tables = set(db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'datasets'")))
    assert len(tables) == 5


def test_unknown_references_are_rejected_and_recorded(db, published):
    bad = (INVALID / "transactions_unknown_store.csv").read_bytes()
    result = publish_dataset(db, manifest("transactions"), bad)
    assert result.status == "invalid" and result.version == 2
    assert "row 3, column store_id: 'S99' not found in stores.store_id" in result.issues
    assert "row 4, column product_id: 'P999' not found in products.product_id" in result.issues
    assert published_version(db, "transactions").version == 1  # still the old one

    again = publish_dataset(db, manifest("transactions"), bad)  # same broken file
    assert (again.status, again.version, again.issues) == ("invalid", 2, result.issues)
    assert (
        db.scalar(
            select(func.count())
            .select_from(SourceVersion)
            .join(Source)
            .where(Source.slug == "transactions")
        )
        == 2
    )


def test_a_failure_mid_publish_leaves_nothing_behind(db, published, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("simulated crash after the table was created")

    monkeypatch.setattr(publish_module, "_build_value_index", explode)
    shorter = b"".join((TABLES / "returns.csv").read_bytes().splitlines(keepends=True)[:-1])
    tables_before = set(
        db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='datasets'"))
    )
    with pytest.raises(RuntimeError):
        publish_dataset(db, manifest("returns"), shorter)
    db.rollback()
    tables_after = set(
        db.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='datasets'"))
    )
    assert tables_after == tables_before  # CREATE TABLE was rolled back too
    assert published_version(db, "returns").version == 1


def test_unregistered_sources_and_wrong_document_versions_are_rejected(db, published):
    stray = manifest("stores") | {"source": "suppliers"}
    result = publish_dataset(db, stray, (TABLES / "stores.csv").read_bytes())
    assert result.status == "invalid" and "is not registered" in result.issues[0]

    doc = (REFERENCE / "documents" / "market-launches.v1.md").read_text()
    result = publish_document(db, doc.replace("version: 1", "version: 5").encode())
    assert result.status == "invalid" and "next version is 2" in result.issues[0]


def test_source_list_shows_published_versions(client, published):
    login(client, "ops.employee@brindlewood.example", published["ops.employee@brindlewood.example"])
    sources = {s["slug"]: s for s in client.get("/api/sources").json()}
    assert sources["returns-refund-policy"]["published_version"] == 2
    assert sources["transactions"]["published_version"] == 1
