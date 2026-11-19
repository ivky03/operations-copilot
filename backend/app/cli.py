"""Administrative command-line tool.

Usage (from backend/):
    uv run python -m app.cli create-user --email a@b.co --name "A B" --role admin --groups finance
    uv run python -m app.cli seed-reference     # groups, demo users, sources and grants
    uv run python -m app.cli publish-reference  # validate, load and publish all reference data
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
from app.ingest.manifest import parse_manifest
from app.ingest.publish import PublishResult, publish_dataset, publish_document
from app.ingest.validate import validate_csv
from app.models import Group, Role, Source, SourceGrant, SourceKind, User
from app.security import hash_password

REFERENCE = Path(__file__).resolve().parents[2] / "data" / "reference"

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


def _reference_sources() -> list[tuple[str, SourceKind, str]]:
    """(slug, kind, title) for every reference manifest and document."""
    out = []
    for path in sorted((REFERENCE / "manifests").glob("*.yaml")):
        m = yaml.safe_load(path.read_text())
        out.append((m["source"], SourceKind.DATASET, m["title"]))
    seen = set()
    for path in sorted((REFERENCE / "documents").glob("*.md")):
        meta = yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
        if meta["doc_id"] not in seen:
            seen.add(meta["doc_id"])
            out.append((meta["doc_id"], SourceKind.DOCUMENT, meta["title"]))
    return out


def seed_reference(db: Session) -> dict[str, str]:
    """Idempotent: running it twice creates nothing new. Returns {email: password} for new users."""
    access = yaml.safe_load((REFERENCE / "access.yaml").read_text())
    groups = {slug: _get_or_create_group(db, slug, name) for slug, name in access["groups"].items()}

    new_passwords: dict[str, str] = {}
    for email, name, role, group_slugs in DEMO_USERS:
        password = secrets.token_urlsafe(12)  # 16 characters
        if _create_user(db, email, name, role, group_slugs, password):
            new_passwords[email] = password

    for slug, kind, title in _reference_sources():
        source = db.scalar(select(Source).where(Source.slug == slug))
        if source is None:
            source = Source(slug=slug, kind=kind, title=title)
            db.add(source)
            db.flush()
            for group_slug in access["grants"][slug]:
                db.add(SourceGrant(source_id=source.id, group_id=groups[group_slug].id))
            audit.record(
                db, None, "source.register", slug, grants=access["grants"][slug], via="cli"
            )
    db.commit()
    return new_passwords


def _dataset_order() -> list[Path]:
    """Manifests ordered so every dataset comes after the datasets it depends on
    (relationships and cross-dataset metrics). A cycle is reported as an error."""
    manifests = {
        p.stem: yaml.safe_load(p.read_text()) for p in (REFERENCE / "manifests").glob("*.yaml")
    }
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
    return [REFERENCE / "manifests" / f"{n}.yaml" for n in ordered]


def publish_reference(db: Session) -> list[PublishResult]:
    results = []
    for manifest_path in _dataset_order():
        manifest = yaml.safe_load(manifest_path.read_text())
        csv_bytes = (REFERENCE / "tables" / f"{manifest['source']}.csv").read_bytes()
        results.append(publish_dataset(db, manifest, csv_bytes))
    for doc_path in sorted((REFERENCE / "documents").glob("*.md")):  # v1 sorts before v2
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

    sub.add_parser("seed-reference", help="Create groups, demo users, sources and grants")
    sub.add_parser("publish-reference", help="Validate, load and publish all reference data")
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
            passwords = seed_reference(db)
            if passwords:
                print("Demo users created. Passwords are shown ONCE; store them now:\n")
                for email, pw in passwords.items():
                    print(f"  {email:<40} {pw}")
            else:
                print("Reference setup already present; nothing changed.")
        elif args.command == "publish-reference":
            results = publish_reference(db)
            for r in results:
                _print_result(r)
            if any(r.status == "invalid" for r in results):
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
