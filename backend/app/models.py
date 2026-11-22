"""Database tables for identity, access and sources, as SQLAlchemy ORM classes.

Each class is one table in the `public` schema. Alembic compares these classes with the live
database to generate migrations. Steward-uploaded dataset tables live in the separate
`datasets` schema and are created at publish time, not here.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    EMPLOYEE = "employee"  # ask questions about sources their groups can read
    STEWARD = "steward"  # employee + manage sources (from M6)
    ADMIN = "admin"  # manage users, groups and grants


class SourceKind(StrEnum):
    DATASET = "dataset"
    DOCUMENT = "document"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"  # hidden from answers, restorable
    DELETED = "deleted"  # tombstone: content removed, row kept so late jobs can see it


class VersionStatus(StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    INVALID = "invalid"
    READY = "ready"  # validated and processed, not yet visible
    PUBLISHED = "published"  # the version answers use (at most one per source)
    SUPERSEDED = "superseded"  # an older published version
    DISCARDED = "discarded"  # a ready version the steward chose not to publish; content removed


class JobStatus(StrEnum):
    PENDING = "pending"  # waiting for a worker (message sent, or about to be re-sent)
    RUNNING = "running"  # claimed by a worker until locked_until
    SUCCEEDED = "succeeded"  # processed; the result may still be "invalid" (bad file, not a crash)
    FAILED = "failed"  # gave up after repeated errors; a steward can retry


def _enum(e: type[StrEnum], name: str) -> Enum:
    # Store the lowercase values ("employee"), not Python member names ("EMPLOYEE").
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


def _uuid_pk() -> Mapped[uuid.UUID]:
    # Random UUIDs instead of 1, 2, 3...: IDs in URLs can't be guessed or enumerated.
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)  # stored lowercase
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # argon2; never the password
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    groups: Mapped[list["Group"]] = relationship(secondary="user_groups", back_populates="members")


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    members: Mapped[list[User]] = relationship(secondary="user_groups", back_populates="groups")


class UserGroup(Base):
    """Membership: which groups a user belongs to (and therefore which sources they can read)."""

    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class Session(Base):
    """A logged-in browser. The cookie holds a random token; we store only its SHA-256 hash,
    so a leaked database backup does not hand out working sessions."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship()


class Source(Base):
    """A dataset or document, independent of its versions."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[SourceStatus] = mapped_column(
        _enum(SourceStatus, "source_status"), nullable=False, default=SourceStatus.ACTIVE
    )
    created_at: Mapped[datetime] = _created_at()

    versions: Mapped[list["SourceVersion"]] = relationship(back_populates="source")
    granted_groups: Mapped[list[Group]] = relationship(secondary="source_grants")


class SourceVersion(Base):
    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint("source_id", "version"),
        # At most one published version per source, enforced by the database itself,
        # so two concurrent publishes can't both win.
        Index(
            "uq_one_published_version",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        _enum(VersionStatus, "version_status"), nullable=False, default=VersionStatus.UPLOADED
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 of the file
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[Source] = relationship(back_populates="versions")


class SourceGrant(Base):
    """Which groups may read a source. Granted by administrators only."""

    __tablename__ = "source_grants"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )


class AuditLog(Base):
    """Append-only record of administrative and steward actions: who did what, when."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    at: Mapped[datetime] = _created_at()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str] = mapped_column(String(300), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ValueIndexEntry(Base):
    """Distinct values of value-indexed columns in one dataset version, plus aliases.

    Used for value grounding: mapping words in a question ("UK", "desk lamp") to values that
    actually exist ("United Kingdom", "Blue Desk Lamp"). `value_norm` is lowercased and has a
    trigram index for fuzzy matching. For an alias row, `value` is the alias and `canonical` is
    the real value it stands for; for a real value, both are the same.
    """

    __tablename__ = "value_index"
    __table_args__ = (
        Index(
            "ix_value_index_trgm",
            "value_norm",
            postgresql_using="gin",
            postgresql_ops={"value_norm": "gin_trgm_ops"},
        ),
        UniqueConstraint("source_version_id", "column_name", "value"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    column_name: Mapped[str] = mapped_column(String(63), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_norm: Mapped[str] = mapped_column(Text, nullable=False)
    canonical: Mapped[str] = mapped_column(Text, nullable=False)
    is_alias: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DocumentContent(Base):
    """The text of one document version, exactly as published. Chunks are built from it."""

    __tablename__ = "document_contents"

    source_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_versions.id", ondelete="CASCADE"), primary_key=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)


EMBEDDING_DIMS = 384  # BAAI/bge-small-en-v1.5


class DocChunk(Base):
    """One searchable section of a document version.

    `search_text` is a generated column: Postgres keeps the full-text index in step with the
    text, so it can never go stale. Title and heading are weighted above the body.
    """

    __tablename__ = "doc_chunks"
    __table_args__ = (
        UniqueConstraint("source_version_id", "ordinal"),
        Index("ix_doc_chunks_search_text", "search_text", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)  # position in the document
    doc_title: Mapped[str] = mapped_column(Text, nullable=False)
    heading: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMS), nullable=False)
    # Vectors from different models aren't comparable; recorded so a model change is detectable.
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    search_text: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', doc_title || ' ' || heading), 'A') || "
            "setweight(to_tsvector('english', content), 'B')",
            persisted=True,
        ),
    )


class Conversation(Base):
    """A chat: a titled sequence of questions by one user. Each turn is an AnswerRun, so the
    whole trace of every answer stays with the conversation, and deleting the conversation
    deletes its turns (and their model-call logs) by cascade."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnswerRun(Base):
    """One question and everything needed to explain its answer later: the plan, groundings,
    SQL and rows, document passages, the answer text, the source versions used, and how it
    ended."""

    __tablename__ = "answer_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = _created_at()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )  # None for single questions (e.g. evaluation runs)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    passages: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source_versions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    verification: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class LlmCall(Base):
    """One request to a language model provider: who, what for, which model, tokens, latency.
    Prompts are not stored here; the answer run holds the structured trace."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    at: Mapped[datetime] = _created_at()
    answer_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("answer_runs.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)  # plan | answer
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)  # ok | error | invalid_output
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    """One uploaded file set being processed in the background.

    The database row is the source of truth for a job's state; the SQS message only says
    "look at job X". So a lost, duplicated or late message can't corrupt anything: the worker
    claims the row atomically, and does the work and marks the job done in one transaction.
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # prepare_dataset | _document
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), nullable=False, default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # S3 keys of the uploaded files
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)  # last failure, for the steward
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_versions.id", ondelete="SET NULL")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the message was last sent (or, after a failed attempt, when it is due back).
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[Source] = relationship()
    version: Mapped[SourceVersion | None] = relationship()
    creator: Mapped[User | None] = relationship()
