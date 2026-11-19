"""Build the searchable chunks for one document version.

Idempotent: indexing a version again replaces its chunks, so a retried or repeated job can't
create duplicates. The caller owns the transaction, so chunks appear together with the
publish (or not at all).
"""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.documents.chunking import chunk_markdown
from app.documents.embedder import Embedder, get_embedder
from app.models import DocChunk, DocumentContent, SourceVersion


def index_document(
    db: Session, version: SourceVersion, body: str, embedder: Embedder | None = None
) -> int:
    embedder = embedder or get_embedder()
    title, chunks = chunk_markdown(body)
    title = str(version.meta.get("title") or title)
    vectors = embedder.passages([c.embedding_text(title) for c in chunks])
    db.execute(delete(DocChunk).where(DocChunk.source_version_id == version.id))
    db.add_all(
        DocChunk(
            source_version_id=version.id,
            ordinal=c.ordinal,
            doc_title=title,
            heading=c.heading,
            content=c.content,
            embedding=vector,
            embedding_model=embedder.model_name,
        )
        for c, vector in zip(chunks, vectors, strict=True)
    )
    db.flush()
    return len(chunks)


def reindex_all(db: Session, embedder: Embedder | None = None) -> dict[str, int]:
    """(Re)build chunks for every stored document version. Used after a model change or to
    backfill versions published before indexing existed."""
    rows = db.execute(
        select(SourceVersion, DocumentContent.body).join(
            DocumentContent, DocumentContent.source_version_id == SourceVersion.id
        )
    ).all()
    counts = {}
    for version, body in rows:
        counts[f"{version.source.slug} v{version.version}"] = index_document(
            db, version, body, embedder
        )
    db.commit()
    return counts
