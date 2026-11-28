"""Administrative command-line tool.

Usage (from backend/):
    uv run python -m app.cli create-user --email a@b.co --name "A B" --role admin --groups finance
    uv run python -m app.cli seed-reference     # groups, demo users, sources and grants
    uv run python -m app.cli publish-reference  # validate, load and publish all reference data
    uv run python -m app.cli seed-reference --stage b      # Stage B: 15 tables, 21 documents
    uv run python -m app.cli publish-reference --stage b
    uv run python -m app.cli seed-reference --stage c      # Stage C: 50 tables
    uv run python -m app.cli publish-reference --stage c
    uv run python -m app.cli validate-dataset --manifest m.yaml --csv t.csv   # dry run

Passwords are never accepted as command-line arguments, because those end up in shell
history. create-user prompts for one; seed-reference generates random passwords and prints
them once.
"""

import argparse
import getpass
import secrets
import sys
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.db import _session_factory
from app.documents.index import reindex_all
from app.ingest.manifest import parse_manifest
from app.ingest.publish import PublishResult, publish_dataset, publish_document
from app.ingest.validate import validate_csv
from app.models import Group, Role, Source, SourceGrant, SourceKind, User
from app.security import hash_password

REFERENCE = Path(__file__).resolve().parents[2] / "data" / "reference"
STAGE_B = REFERENCE.parent / "stage_b"
STAGE_C = REFERENCE.parent / "stage_c"
# Each stage is Stage A plus additions; Stage A's files never change, so its answers stay valid.
STAGES = {"a": [REFERENCE], "b": [REFERENCE, STAGE_B], "c": [REFERENCE, STAGE_B, STAGE_C]}

DEMO_USERS = [
    ("ops.employee@brindlewood.example", "Olivia Ops", Role.EMPLOYEE, ["operations"]),
    ("finance.employee@brindlewood.example", "Felix Finance", Role.EMPLOYEE, ["finance"]),
    ("data.steward@brindlewood.example", "Sam Steward", Role.STEWARD, ["operations", "finance"]),
    ("admin@brindlewood.example", "Avery Admin", Role.ADMIN, []),
]


def _get_or_create_group(db: Session, slug: str, name: str) -> Group:
    group = db.scalar(select(Group).where(Group.slug == slug))
    if group is None:
        group = Group(slug=slug, name=name)
        db.add(group)
        db.flush()
    return group


def _create_user(
    db: Session, email: str, name: str, role: Role, groups: list[str], password: str
) -> User | None:
    """Create a user unless one already exists. Returns None if it existed (idempotent)."""
    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        return None
    group_rows = list(db.scalars(select(Group).where(Group.slug.in_(groups))))
    missing = set(groups) - {g.slug for g in group_rows}
    if missing:
        raise SystemExit(f"Unknown groups: {sorted(missing)}")
    user = User(
        email=email,
        display_name=name,
        password_hash=hash_password(password),
        role=role,
        groups=group_rows,
    )
    db.add(user)
    audit.record(db, None, "user.create", email, role=role.value, groups=groups, via="cli")
    return user


def _reference_sources(roots: list[Path]) -> list[tuple[str, SourceKind, str]]:
    """(slug, kind, title) for every manifest and document of a stage."""
    out = []
    for root in roots:
        for path in sorted((root / "manifests").glob("*.yaml")):
            m = yaml.safe_load(path.read_text())
            out.append((m["source"], SourceKind.DATASET, m["title"]))
    seen = set()
    for root in roots:
        for path in sorted((root / "documents").glob("*.md")):
            meta = yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
            if meta["doc_id"] not in seen:
                seen.add(meta["doc_id"])
                out.append((meta["doc_id"], SourceKind.DOCUMENT, meta["title"]))
    return out


def _grants(roots: list[Path]) -> dict[str, list[str]]:
    grants: dict[str, list[str]] = {}
    for root in roots:
        grants |= yaml.safe_load((root / "access.yaml").read_text())["grants"]
    return grants


def seed_reference(db: Session, stage: str = "a") -> dict[str, str]:
    """Idempotent: running it twice creates nothing new. Returns {email: password} for new users.
    Stage B on top of an existing Stage A setup adds only the new sources."""
    roots = STAGES[stage]
    access = yaml.safe_load((REFERENCE / "access.yaml").read_text())
    groups = {slug: _get_or_create_group(db, slug, name) for slug, name in access["groups"].items()}
    grants = _grants(roots)

    new_passwords: dict[str, str] = {}
    for email, name, role, group_slugs in DEMO_USERS:
        password = secrets.token_urlsafe(12)  # 16 characters
        if _create_user(db, email, name, role, group_slugs, password):
            new_passwords[email] = password

    for slug, kind, title in _reference_sources(roots):
        source = db.scalar(select(Source).where(Source.slug == slug))
        if source is None:
            source = Source(slug=slug, kind=kind, title=title)
            db.add(source)
            db.flush()
            for group_slug in grants[slug]:
                db.add(SourceGrant(source_id=source.id, group_id=groups[group_slug].id))
            audit.record(db, None, "source.register", slug, grants=grants[slug], via="cli")
    db.commit()
    return new_passwords


def _dataset_order(roots: list[Path]) -> list[Path]:
    """Manifests ordered so every dataset comes after the datasets it depends on
    (relationships and cross-dataset metrics). A cycle is reported as an error."""
    paths = {p.stem: p for root in roots for p in (root / "manifests").glob("*.yaml")}
    manifests = {name: yaml.safe_load(p.read_text()) for name, p in paths.items()}
    deps = {}
    for name, m in manifests.items():
        refs = {r["references"].split(".")[0] for r in m.get("relationships", [])}
        refs |= {
            token.split(".")[0]
            for d in m.get("derived_metrics", [])
            for token in d["formula"].replace("(", " ").replace(")", " ").split()
            if "." in token
        }
        deps[name] = refs - {name}
    ordered: list[str] = []
    while deps:
        ready = sorted(n for n, d in deps.items() if d <= set(ordered))
        if not ready:
            raise SystemExit(f"Circular dependencies between datasets: {sorted(deps)}")
        ordered += ready
        for n in ready:
            del deps[n]
    return [paths[n] for n in ordered]


def publish_reference(db: Session, stage: str = "a") -> list[PublishResult]:
    roots = STAGES[stage]
    results = []
    for manifest_path in _dataset_order(roots):
        manifest = yaml.safe_load(manifest_path.read_text())
        root = manifest_path.parents[1]
        csv_bytes = (root / "tables" / f"{manifest['source']}.csv").read_bytes()
        results.append(publish_dataset(db, manifest, csv_bytes))
    for root in roots:
        for doc_path in sorted((root / "documents").glob("*.md")):  # v1 sorts before v2
            results.append(publish_document(db, doc_path.read_bytes()))
    return results


def _print_result(r: PublishResult) -> None:
    version = f"v{r.version}" if r.version else "  "
    detail = f"{r.row_count} rows" if r.row_count is not None else ""
    print(f"  {r.status:<10} {r.source:<28} {version:<4} {detail}")
    for issue in r.issues:
        print(f"      - {issue}")
    if r.issue_count > len(r.issues):
        print(f"      ... and {r.issue_count - len(r.issues)} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    cu = sub.add_parser("create-user", help="Create a user (prompts for the password)")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", choices=[r.value for r in Role], default=Role.EMPLOYEE.value)
    cu.add_argument("--groups", nargs="*", default=[])

    seed = sub.add_parser("seed-reference", help="Create groups, demo users, sources and grants")
    seed.add_argument("--stage", choices=sorted(STAGES), default="a")
    pub = sub.add_parser("publish-reference", help="Validate, load and publish all reference data")
    pub.add_argument("--stage", choices=sorted(STAGES), default="a")
    sub.add_parser(
        "index-documents", help="Rebuild search chunks for every stored document version"
    )
    vd = sub.add_parser("validate-dataset", help="Check a CSV against a manifest (no changes)")
    vd.add_argument("--manifest", type=Path, required=True)
    vd.add_argument("--csv", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "validate-dataset":
        manifest, problems = parse_manifest(yaml.safe_load(args.manifest.read_text()))
        if manifest is None:
            print("Manifest is invalid:", *problems, sep="\n  - ")
            return 1
        result = validate_csv(manifest, args.csv.read_bytes())
        if result.ok:
            print(f"Valid: {len(result.rows)} rows match the manifest.")
            return 0
        print(f"Invalid: {result.issue_count} problem(s)")
        for issue in result.issues:
            print(f"  - {issue}")
        if result.issue_count > len(result.issues):
            print(f"  ... and {result.issue_count - len(result.issues)} more")
        return 1

    with _session_factory()() as db:
        if args.command == "create-user":
            password = getpass.getpass("Password (min 12 characters): ")
            user = _create_user(db, args.email, args.name, Role(args.role), args.groups, password)
            db.commit()
            print("Created." if user else "A user with that email already exists; nothing changed.")
        elif args.command == "seed-reference":
            passwords = seed_reference(db, args.stage)
            if passwords:
                print("Demo users created. Passwords are shown ONCE; store them now:\n")
                for email, pw in passwords.items():
                    print(f"  {email:<40} {pw}")
            else:
                print("Reference setup already present; nothing changed.")
        elif args.command == "publish-reference":
            results = publish_reference(db, args.stage)
            for r in results:
                _print_result(r)
            if any(r.status == "invalid" for r in results):
                return 1
        elif args.command == "index-documents":
            for label, count in reindex_all(db).items():
                print(f"  {label:<36} {count} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
