"""Leakage suite: a source someone can't read must not reach them through ANY path.

Method: canaries. A dataset and a document are planted whose content carries words that
appear nowhere else in the data ("zanzibar" in values, column names and text; "rebate" in names
and titles). Then the source is hidden from an Operations employee and steward in one of four
ways, and every surface they can reach is probed for those words:

    restricted  granted to Finance only, from the start
    revoked     Operations had access (and used it), then the grant was removed
    archived    a steward archived it
    deleted     an administrator deleted it (then nobody may see it, and its content must be
                gone from the database and storage altogether)

Surfaces: source listings and lookups, the steward console (versions, jobs, "used by"), the
catalog and the schema description the planner reads (all of it, or the tables schema
retrieval chooses), document titles, document search,
value grounding, everything sent to the language model (plan and answer prompts, including
conversation history), the answers and saved conversations themselves, cached answers,
notifications, and error messages, which must not differ between "restricted" and "doesn't
exist".

Each scenario first runs the same probes while access is granted and requires them to FIND
the canaries, so the suite can't pass by probing the wrong place. Pass threshold: zero leaks.
"""

import json

import pytest
import yaml
from sqlalchemy import func, select, text

from app.answer.context import build_context
from app.answer.passages import document_titles
from app.answer.pipeline import answer_question
from app.aws import get_client
from app.cli import publish_reference, seed_reference
from app.config import get_settings
from app.documents.search import search
from app.ingest.publish import publish_dataset, publish_document
from app.models import (
    AnswerCacheEntry,
    AnswerRun,
    Base,
    Conversation,
    Group,
    Notification,
    Source,
    SourceKind,
    Subscription,
    User,
)
from app.query.catalog import Catalog
from app.query.grounding import ground
from app.query.schema_retrieval import select_tables
from tests.conftest import login
from tests.test_answer_pipeline import fake
from tests.test_ingestion_jobs import work

pytestmark = pytest.mark.integration

CANARIES = ("zanzibar", "rebate")
CONTENT_CANARY = "zanzibar"  # in content only; must vanish from storage when deleted
ADMIN, STEWARD = "admin@example.com", "sam.steward@example.com"
OPS, FIN = "olga.ops@example.com", "fiona.finance@example.com"
DATASET, DOCUMENT = "vendor_rebates", "vendor-rebate-terms"

MANIFEST = {
    "source": DATASET,
    "title": "Vendor rebates",
    "description": "Rebates agreed with vendors, by store and Zanzibar tier.",
    "grain": "One row per rebate agreement.",
    "primary_key": ["rebate_id"],
    "time_column": "agreed_date",
    "columns": [
        {"name": "rebate_id", "type": "string", "role": "id", "description": "Rebate id."},
        {"name": "store_id", "type": "string", "role": "id", "description": "Store."},
        {"name": "agreed_date", "type": "date", "role": "time", "description": "Agreed on."},
        {
            "name": "zanzibar_tier",
            "type": "string",
            "role": "dimension",
            "description": "Zanzibar programme tier of the vendor.",
            "value_index": True,
            "aliases": {"ZG": "Zanzibar Gold"},
        },
        {
            "name": "rebate_amount",
            "type": "decimal",
            "role": "measure",
            "unit": "EUR",
            "description": "Rebate in EUR.",
        },
    ],
    "relationships": [{"column": "store_id", "references": "stores.store_id"}],
    "metrics": [
        {
            "name": "rebate_value",
            "title": "Rebate value",
            "description": "Total rebates agreed (Zanzibar programme).",
            "aggregation": "sum",
            "expression": "rebate_amount",
            "unit": "EUR",
        }
    ],
}
CSV = (
    b"rebate_id,store_id,agreed_date,zanzibar_tier,rebate_amount\n"
    b"RB1,S01,2026-03-02,Zanzibar Gold,7731.00\n"
    b"RB2,S03,2026-03-09,Zanzibar Silver,4410.50\n"
)
DOC = b"""---
doc_id: vendor-rebate-terms
version: 1
title: Vendor rebate terms
effective_date: 2026-01-01
---

# Vendor rebate terms

## Zanzibar tiers

Zanzibar Gold vendors earn a 4% rebate, paid quarterly. Zanzibar Silver vendors earn 2%.
"""

DATA_PLAN = {
    "kind": "data",
    "queries": [{"metric": "rebate_value", "dimensions": ["zanzibar_tier"]}],
}
DOC_PLAN = {"kind": "documents", "doc_searches": ["vendor rebate tiers"]}
CACHED_QUESTION = "Which vendor tiers earn the most?"  # asked on its own, so it's cached
IRELAND = {
    "kind": "data",
    "queries": [
        {
            "metric": "net_sales",
            "filters": [{"dimension": "country", "values": ["Ireland"]}],
            "time_range": {"start": "2026-03-01", "end": "2026-04-01"},
        }
    ],
    "doc_searches": ["vendor tier terms"],  # finds the canary document while it's readable
}


# ----------------------------------------------------------------------------- setup


@pytest.fixture
def world(db, make_user, client, ingest_queues):
    seed_reference(db)
    publish_reference(db)
    make_user(ADMIN, role="admin")
    make_user(STEWARD, role="steward", groups=("operations",))
    make_user(OPS, groups=("operations",))
    make_user(FIN, groups=("finance",))
    groups = {g.slug: g for g in db.scalars(select(Group))}
    for slug, kind, title in (
        (DATASET, SourceKind.DATASET, "Vendor rebates"),
        (DOCUMENT, SourceKind.DOCUMENT, "Vendor rebate terms"),
    ):
        db.add(
            Source(
                slug=slug,
                kind=kind,
                title=title,
                granted_groups=[groups["operations"], groups["finance"]],
            )
        )
    db.commit()
    assert publish_dataset(db, MANIFEST, CSV).status == "published"
    assert publish_document(db, DOC).status == "published"
    return db


def user(db, email) -> User:
    db.expire_all()
    return db.scalar(select(User).where(User.email == email))


def as_user(client, email):
    client.cookies.clear()
    client.headers["X-CSRF-Token"] = login(client, email)
    return client


def ask(db, who: User, question, plan, conversation=None, answer="See [D1].", cached=False):
    planner, p = fake(plan, plan)  # twice: the planner is asked again after an unknown name
    writer, w = fake({"answer": answer}, {"answer": answer})
    result = answer_question(db, who, question, planner, writer, conversation, use_cache=cached)
    return result, p.prompts, w.prompts


def use_it(db, client) -> Conversation:
    """Olga (Operations) uses both canary sources while she still can: in a conversation, by
    following them (and being told about a new version), and with an answer that's cached."""
    olga = user(db, OPS)
    as_user(client, OPS)
    for slug in (DATASET, DOCUMENT):
        assert client.put(f"/api/sources/{slug}/subscription").status_code == 200
    chat = Conversation(user_id=olga.id, title="Vendor deals")
    db.add(chat)
    db.commit()
    data, _, _ = ask(db, olga, "What did we agree with vendors, by tier?", DATA_PLAN, chat)
    docs, _, _ = ask(db, olga, "What are the vendor terms?", DOC_PLAN, chat, "4% [C1].")
    assert data.status == "answered" and docs.status == "answered"
    # A new version goes through the real upload path, so its files are in storage too.
    as_user(client, STEWARD)
    files = {
        "manifest": ("m.yaml", yaml.safe_dump(MANIFEST).encode()),
        "data": ("d.csv", CSV + b"RB3,S02,2026-03-20,Zanzibar Gold,100.00\n"),
    }
    assert client.post(f"/api/steward/sources/{DATASET}/uploads", files=files).status_code == 202
    work()
    resp = client.post(f"/api/steward/sources/{DATASET}/versions/2/publish")
    assert resp.status_code == 200, resp.text  # Olga follows it: she's notified
    cached, _, _ = ask(db, olga, CACHED_QUESTION, DOC_PLAN, answer="Gold [C1].", cached=True)
    assert cached.status == "answered" and not cached.cached
    return chat


# ----------------------------------------------------------------------------- the probes


def probe(db, client, conversation: Conversation | None) -> dict[str, str]:
    """Everything Olga and Sam can reach, as text. Keys name the surface."""
    seen: dict[str, str] = {}
    olga = user(db, OPS)

    as_user(client, OPS)
    seen["GET /api/sources"] = client.get("/api/sources").text
    for slug in (DATASET, DOCUMENT):
        seen[f"GET /api/sources/{slug}"] = client.get(f"/api/sources/{slug}").text
    if conversation:
        seen["saved conversation"] = client.get(f"/api/conversations/{conversation.id}").text
    seen["notifications"] = client.get("/api/notifications").text

    as_user(client, STEWARD)
    seen["steward sources"] = client.get("/api/steward/sources").text
    seen["steward jobs"] = client.get("/api/steward/jobs").text
    for slug in (DATASET, DOCUMENT):
        seen[f"steward versions {slug}"] = client.get(f"/api/steward/sources/{slug}/versions").text
        seen[f"steward preview {slug}"] = client.get(f"/api/steward/sources/{slug}/versions/1").text

    catalog = Catalog.for_user(db, olga)
    seen["catalog metrics"] = " ".join(catalog.metric_names())
    seen["schema context"] = build_context(db, catalog).text
    chosen = select_tables(db, catalog, ["zanzibar gold vendor rebates by tier"], k=5)
    seen["schema retrieval"] = json.dumps(chosen.as_dict())
    seen["retrieved schema context"] = build_context(db, catalog, chosen.tables).text
    seen["document titles"] = " ".join(document_titles(db, olga))
    hits = search(db, olga, "zanzibar gold rebate tiers", k=20, include_superseded=True)
    seen["document search"] = json.dumps([h.__dict__ for h in hits], default=str)
    g = ground(db, catalog, "Zanzibar Gold")
    seen["value grounding"] = json.dumps([c.__dict__ for c in g.candidates], default=str)

    # The cached question again: reused only while nothing about Olga's access has changed.
    again, _, _ = ask(db, olga, CACHED_QUESTION, DOC_PLAN, answer="Gold [C1].", cached=True)
    seen["cached answer"] = json.dumps(again.__dict__, default=lambda o: o.__dict__)

    # A new question, in the old conversation if there is one (so history is in the prompt).
    result, plan_prompts, write_prompts = ask(
        db, olga, "Ireland net sales in March 2026?", IRELAND, conversation, "-€509.00 [D1]."
    )
    seen["planner prompt"] = json.dumps(plan_prompts)
    seen["writer prompt"] = json.dumps(write_prompts)
    seen["answer"] = json.dumps(result.__dict__, default=lambda o: o.__dict__)
    return seen


def leaks(seen: dict[str, str]) -> dict[str, list[str]]:
    return {
        surface: [c for c in CANARIES if c in content.lower()]
        for surface, content in seen.items()
        if any(c in content.lower() for c in CANARIES)
    }


def asking_for_it_looks_like_asking_for_nothing(db):
    """Naming the hidden metric must get the same reply as naming one that never existed, so
    the reply can't be used to discover it."""
    olga = user(db, OPS)
    hidden, _, _ = ask(db, olga, "Total rebates?", DATA_PLAN)
    made_up = {"kind": "data", "queries": [{"metric": "made_up", "dimensions": ["zanzibar_tier"]}]}
    missing, _, _ = ask(db, olga, "Total made up?", made_up)
    assert hidden.status == missing.status == "unanswerable"
    assert hidden.answer.replace("rebate_value", "X") == missing.answer.replace("made_up", "X")


def storage_traces(db) -> dict[str, int]:
    """Rows anywhere in the database whose text contains the content canary, plus files."""
    found = {}
    tables = [f'public."{t.name}"' for t in Base.metadata.sorted_tables]
    tables += [
        f'datasets."{name}"'
        for name in db.scalars(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='datasets'")
        )
    ]
    for table in tables:
        n = db.scalar(
            text(f"SELECT count(*) FROM {table} t WHERE lower(t::text) LIKE :c"),  # noqa: S608
            {"c": f"%{CONTENT_CANARY}%"},
        )
        if n:
            found[table] = n
    listed = get_client("s3").list_objects_v2(
        Bucket=get_settings().upload_bucket, Prefix=f"uploads/{DATASET}/"
    )
    if listed.get("KeyCount"):
        found["s3"] = listed["KeyCount"]
    # Rows that hold no canary text themselves but point at the deleted sources.
    erased = select(AnswerRun.id).where(AnswerRun.redacted_reason.is_not(None))
    gone = select(Source.id).where(Source.slug.in_([DATASET, DOCUMENT]))
    for name, query in (
        ("cache entries for erased answers", AnswerCacheEntry.answer_run_id.in_(erased)),
        ("notifications", Notification.source_id.in_(gone)),
        ("subscriptions", Subscription.source_id.in_(gone)),
    ):
        table = query.left.table
        if n := db.scalar(select(func.count()).select_from(table).where(query)):
            found[name] = n
    db.rollback()  # end the read: an open transaction would hold locks a delete has to wait for
    return found


# ----------------------------------------------------------------------------- scenarios


def hide(scenario: str, client) -> None:
    if scenario == "revoked":
        as_user(client, ADMIN)
        for slug in (DATASET, DOCUMENT):
            body = {"groups": ["finance"]}
            assert client.put(f"/api/admin/sources/{slug}/grants", json=body).status_code == 200
    elif scenario == "archived":
        as_user(client, STEWARD)
        for slug in (DATASET, DOCUMENT):
            assert client.post(f"/api/steward/sources/{slug}/archive").status_code == 200
    elif scenario == "deleted":
        as_user(client, ADMIN)
        for slug in (DATASET, DOCUMENT):
            resp = client.post(f"/api/admin/sources/{slug}/delete", json={"confirm": slug})
            assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("scenario", ["revoked", "archived", "deleted"])
def test_a_source_taken_away_leaks_nowhere(world, client, scenario):
    db = world
    chat = use_it(db, client)
    before = leaks(probe(db, client, chat))
    # Control: while Olga has access, the probes do find the canaries on every main surface.
    for surface in (
        "GET /api/sources",
        "saved conversation",
        "notifications",
        "cached answer",
        "steward sources",
        "steward preview vendor_rebates",
        "steward jobs",
        "catalog metrics",
        "schema context",
        "schema retrieval",
        "retrieved schema context",
        "document titles",
        "document search",
        "value grounding",
        "planner prompt",
    ):
        assert surface in before, f"probe {surface!r} found nothing even with access"
    assert storage_traces(db)

    hide(scenario, client)
    if scenario == "deleted":  # checked first: the probes below store new (canary-free) runs
        assert storage_traces(db) == {}
    after = leaks(probe(db, client, chat))
    if scenario == "archived":
        # By design the source's own steward still sees it in the console, to restore it.
        # Nothing else does, including every path into an answer.
        console = {s for s in after if s.startswith("steward ")}
        assert {"steward sources", "steward preview vendor_rebates"} <= console
        after = {s: c for s, c in after.items() if s not in console}
    assert after == {}
    asking_for_it_looks_like_asking_for_nothing(db)


def test_a_restricted_source_leaks_nowhere(world, client):
    db = world
    as_user(client, ADMIN)
    for slug in (DATASET, DOCUMENT):
        client.put(f"/api/admin/sources/{slug}/grants", json={"groups": ["finance"]})
    assert leaks(probe(db, client, None)) == {}
    asking_for_it_looks_like_asking_for_nothing(db)
    # ...while Finance, who is granted it, does see it (the rule is the grant, nothing else).
    fiona = user(db, FIN)
    assert "vendor_rebates.rebate_value" in Catalog.for_user(db, fiona).metric_names()


def test_restoring_an_archived_source_brings_everything_back(world, client):
    db = world
    chat = use_it(db, client)
    hide("archived", client)
    as_user(client, STEWARD)
    for slug in (DATASET, DOCUMENT):
        assert client.post(f"/api/steward/sources/{slug}/restore").status_code == 200
    seen = leaks(probe(db, client, chat))
    assert {"saved conversation", "schema context", "notifications", "cached answer"} <= set(seen)
