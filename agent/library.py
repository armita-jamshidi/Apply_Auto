"""The candidate's source library: resume, profile, and any essays or project write-ups.

Put source material in the private, git-ignored folder ``profile/library/`` as Markdown,
text, PDF, or Word files. When the library holds more than the resume, written answers are
drafted by an agent that decides which documents to search and read, instead of receiving
everything at once (see ``agent.answers``). Every claim in a draft must still quote one of
these documents exactly.
"""

import logging
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.settings import PROJECT_ROOT

LOGGER = logging.getLogger(__name__)
LIBRARY_DIR = PROJECT_ROOT / "profile" / "library"
WRITING_DIR = PROJECT_ROOT / "profile" / "writing_samples"
WRITING_CHARS = 12000
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
CHUNK_CHARS = 900
READ_CHARS = 6000
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have i in is it my of on or that the this to was "
    "we were what when where which who why with you your how did do does".split()
)


@dataclass(frozen=True, slots=True)
class LibraryDocument:
    """One source document the agent can search and quote."""

    doc_id: str
    title: str
    text: str


def load_library(
    folder: Path | None = None,
    *,
    resume_text: str = "",
    profile_facts: Iterable[str] = (),
    job_description: str = "",
) -> list[LibraryDocument]:
    """The resume, profile facts, job description, and every readable file in the folder."""
    documents: list[LibraryDocument] = []
    if resume_text.strip():
        documents.append(LibraryDocument("resume", "Resume", resume_text.strip()))
    facts = "\n".join(fact for fact in profile_facts if fact.strip())
    if facts:
        documents.append(LibraryDocument("profile", "Profile facts", facts))
    if job_description.strip():
        documents.append(
            LibraryDocument("job_description", "This job's description", job_description.strip())
        )
    folder = folder or LIBRARY_DIR
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            text = _read_file(path)
            if text.strip():
                doc_id = path.relative_to(folder).as_posix()
                documents.append(LibraryDocument(doc_id, _title(path, text), text.strip()))
    return documents


def load_writing_samples(folder: Path | None = None, *, limit: int = WRITING_CHARS) -> list[str]:
    """The candidate's own writing (essays, cover letters, posts), for matching their voice.

    Samples teach style only; they are not evidence for facts. At most limit characters in
    total are kept, whole samples first.
    """
    folder = folder or WRITING_DIR
    samples: list[str] = []
    total = 0
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            text = _read_file(path).strip() if path.is_file() else ""
            if not text:
                continue
            text = text[: max(0, limit - total)]
            if text:
                samples.append(text)
                total += len(text)
    return samples


def has_extra_sources(documents: Iterable[LibraryDocument]) -> bool:
    """Whether the library holds more than the resume, profile, and job description."""
    return any(doc.doc_id not in {"resume", "profile", "job_description"} for doc in documents)


def outline(documents: Iterable[LibraryDocument]) -> list[dict[str, Any]]:
    """Each document's id, title, length, and opening line, for choosing what to read."""
    return [
        {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "characters": len(doc.text),
            "opening": " ".join(doc.text.split())[:160],
        }
        for doc in documents
    ]


def search(
    documents: list[LibraryDocument], query: str, *, limit: int = 8
) -> list[dict[str, Any]]:
    """Passages that best match the query's words, best first (TF-IDF over passages)."""
    terms = [term for term in _WORD.findall(query.casefold()) if term not in _STOPWORDS]
    if not terms:
        return []
    passages = [
        (doc, offset, chunk) for doc in documents for offset, chunk in _chunks(doc.text)
    ]
    if not passages:
        return []
    counts = [Counter(_WORD.findall(chunk.casefold())) for _, _, chunk in passages]
    total = len(passages)
    scored = []
    for (doc, offset, chunk), words in zip(passages, counts, strict=True):
        score = 0.0
        for term in set(terms):
            if words[term]:
                containing = sum(1 for other in counts if other[term])
                score += (1 + math.log(words[term])) * math.log(1 + total / containing)
        if score > 0:
            scored.append((score, doc, offset, chunk))
    scored.sort(key=lambda item: -item[0])
    return [
        {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "offset": offset,
            "passage": " ".join(chunk.split()),
        }
        for _, doc, offset, chunk in scored[:limit]
    ]


def read(documents: list[LibraryDocument], doc_id: str, offset: int = 0) -> dict[str, Any]:
    """Up to READ_CHARS characters of one document from offset, and where the next part starts."""
    doc = next((item for item in documents if item.doc_id == doc_id), None)
    if doc is None:
        return {"error": f"No document {doc_id!r}; call list_documents for the ids."}
    offset = max(0, min(int(offset), len(doc.text)))
    text = doc.text[offset : offset + READ_CHARS]
    following = offset + len(text)
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "offset": offset,
        "text": text,
        "next_offset": following if following < len(doc.text) else None,
    }


def quote_found(documents: Mapping[str, LibraryDocument], doc_id: str, quote: str) -> bool:
    """Whether the quote appears in the named document, ignoring whitespace and case."""
    doc = documents.get(doc_id)
    return bool(doc and quote.strip()) and _normalize(quote) in _normalize(doc.text)


def _chunks(text: str) -> list[tuple[int, str]]:
    """Paragraph-aligned passages of about CHUNK_CHARS characters, with their offsets."""
    chunks: list[tuple[int, str]] = []
    start: int | None = None
    end = 0
    for paragraph in re.finditer(r"\S.*?(?=\n\s*\n|\Z)", text, flags=re.DOTALL):
        if start is None:
            start = paragraph.start()
        end = paragraph.end()
        if end - start >= CHUNK_CHARS:
            chunks.extend(_split(text, start, end))
            start = None
    if start is not None:
        chunks.extend(_split(text, start, end))
    return chunks


def _split(text: str, start: int, end: int) -> list[tuple[int, str]]:
    """One passage, or several when a single paragraph runs far past CHUNK_CHARS."""
    size = CHUNK_CHARS * 2
    return [(offset, text[offset : min(offset + size, end)]) for offset in range(start, end, size)]


def _read_file(path: Path) -> str:
    suffix = path.suffix.casefold()
    try:
        if suffix in TEXT_SUFFIXES:
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".pdf":
            from pypdf import PdfReader

            return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        if suffix == ".docx":
            return _read_docx(path)
    except Exception as error:
        LOGGER.warning("Could not read library file %s: %s", path.name, error)
    return ""


def _read_docx(path: Path) -> str:
    """Paragraph text from a Word file, without extra dependencies."""
    import zipfile
    from xml.etree import ElementTree

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs = [
        "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        for paragraph in root.iter(f"{namespace}p")
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


def _title(path: Path, text: str) -> str:
    heading = re.search(r"^\s*#\s+(.+)$", text, flags=re.MULTILINE)
    if heading:
        return heading.group(1).strip()[:120]
    return path.stem.replace("_", " ").replace("-", " ").strip()[:120]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()
