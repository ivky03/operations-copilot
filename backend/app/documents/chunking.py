"""Split a Markdown document into searchable sections ("chunks").

Policy documents are written in sections with headings, and a heading is the author's own
statement of what the section is about. So we split at headings rather than every N tokens:
a passage never stops mid-rule, and a citation can name the section ("Return window").

- Text before the first `##` heading (an introduction) becomes its own chunk.
- `###` subsections become separate chunks with a "Section › Subsection" heading.
- A section longer than MAX_WORDS is split at paragraph breaks, keeping its heading.
"""

import re
from dataclasses import dataclass

MAX_WORDS = 250  # about 330 tokens; comfortably inside the embedding model's 512-token limit
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str
    content: str

    def embedding_text(self, doc_title: str) -> str:
        """What gets embedded: the title and heading give a short section its context."""
        return f"{doc_title}: {self.heading}\n{self.content}"


def _split_long(text: str) -> list[str]:
    if len(text.split()) <= MAX_WORDS:
        return [text]
    parts, current = [], []
    for para in re.split(r"\n\s*\n", text):
        if current and len(" ".join(current + [para]).split()) > MAX_WORDS:
            parts.append("\n\n".join(current))
            current = []
        current.append(para)
    if current:
        parts.append("\n\n".join(current))
    return parts


def chunk_markdown(body: str) -> tuple[str, list[Chunk]]:
    """Return (document title, chunks). The title comes from the first `#` heading."""
    title = ""
    sections: list[tuple[str, list[str]]] = []
    section, sub = "Introduction", None
    lines: list[str] = []

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append((f"{section} › {sub}" if sub else section, [text]))
        lines.clear()

    for line in body.splitlines():
        match = HEADING.match(line)
        if not match:
            lines.append(line)
            continue
        level, name = len(match.group(1)), match.group(2)
        if level == 1 and not title:
            flush()
            title = name
        elif level <= 2:
            flush()
            section, sub = name, None
        else:
            flush()
            sub = name
    flush()

    chunks: list[Chunk] = []
    for heading, (text,) in sections:
        parts = _split_long(text)
        for i, part in enumerate(parts, start=1):
            label = f"{heading} (part {i})" if len(parts) > 1 else heading
            chunks.append(Chunk(len(chunks), label, part))
    return title or "Untitled", chunks
