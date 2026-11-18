"""The reference seed is complete, matches access.yaml, and is safe to run twice."""

import yaml
from sqlalchemy import func, select

from app.cli import REFERENCE, seed_reference
from app.models import Group, Source, SourceGrant, User


def test_seed_creates_groups_users_sources_and_grants(db):
    passwords = seed_reference(db)
    assert len(passwords) == 4
    assert all(len(p) >= 12 for p in passwords.values())
    assert db.scalar(select(func.count()).select_from(Group)) == 2
    assert db.scalar(select(func.count()).select_from(User)) == 4
    assert db.scalar(select(func.count()).select_from(Source)) == 15  # 4 datasets + 11 documents

    access = yaml.safe_load((REFERENCE / "access.yaml").read_text())["grants"]
    for source in db.scalars(select(Source)):
        granted = {g.slug for g in source.granted_groups}
        assert granted == set(access[source.slug]), source.slug


def test_seed_is_idempotent(db):
    seed_reference(db)
    assert seed_reference(db) == {}
    assert db.scalar(select(func.count()).select_from(User)) == 4
    assert db.scalar(select(func.count()).select_from(SourceGrant)) == sum(
        len(v) for v in yaml.safe_load((REFERENCE / "access.yaml").read_text())["grants"].values()
    )
