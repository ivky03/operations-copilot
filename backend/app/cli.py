"""Administrative command-line tool.

Usage (from backend/):
    uv run python -m app.cli create-user --email a@b.co --name "A B" --role admin --groups finance
    uv run python -m app.cli seed-reference     # groups, demo users, sources and grants

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    cu = sub.add_parser("create-user", help="Create a user (prompts for the password)")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", choices=[r.value for r in Role], default=Role.EMPLOYEE.value)
    cu.add_argument("--groups", nargs="*", default=[])

    sub.add_parser("seed-reference", help="Create groups, demo users, sources and grants")
    args = parser.parse_args(argv)

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
