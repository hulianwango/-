from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import fitz

from .config import settings
from .database import db_session, utcnow


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class PdfPage:
    page_number: int
    text: str


@dataclass
class ExtractedPdf:
    paper_id: str
    file_hash: str
    title: str
    authors: str
    year: int | None
    journal: str
    doi: str
    pages: list[PdfPage]


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _metadata_value(metadata: dict, key: str) -> str:
    value = metadata.get(key) or ""
    return normalize_text(str(value))


def _guess_year(*texts: str) -> int | None:
    for text in texts:
        match = YEAR_RE.search(text or "")
        if match:
            return int(match.group(0))
    return None


def _guess_doi(text: str) -> str:
    match = DOI_RE.search(text or "")
    return match.group(0).rstrip(".") if match else ""


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            split_at = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if split_at > start + chunk_size // 2:
                end = split_at + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(0, end - overlap)
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def extract_pdf(path: Path) -> ExtractedPdf:
    file_hash = hash_file(path)
    paper_id = f"p_{file_hash[:16]}"
    pages: list[PdfPage] = []

    with fitz.open(path) as document:
        metadata = document.metadata or {}
        for index, page in enumerate(document, start=1):
            pages.append(PdfPage(page_number=index, text=normalize_text(page.get_text("text"))))

    first_pages = "\n".join(page.text for page in pages[:3])
    title = _metadata_value(metadata, "title") or path.stem
    authors = _metadata_value(metadata, "author")
    doi = _guess_doi(first_pages)
    year = _guess_year(_metadata_value(metadata, "creationDate"), first_pages, path.stem)

    return ExtractedPdf(
        paper_id=paper_id,
        file_hash=file_hash,
        title=title,
        authors=authors,
        year=year,
        journal="",
        doi=doi,
        pages=pages,
    )


def index_pdf(connection: sqlite3.Connection, path: Path) -> str:
    extracted = extract_pdf(path)
    now = utcnow()

    connection.execute(
        """
        INSERT INTO papers (
            paper_id, title, authors, year, journal, doi, file_path, file_hash,
            page_count, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id) DO UPDATE SET
            title = excluded.title,
            authors = excluded.authors,
            year = excluded.year,
            journal = excluded.journal,
            doi = excluded.doi,
            file_path = excluded.file_path,
            file_hash = excluded.file_hash,
            page_count = excluded.page_count,
            updated_at = excluded.updated_at
        """,
        (
            extracted.paper_id,
            extracted.title,
            extracted.authors,
            extracted.year,
            extracted.journal,
            extracted.doi,
            str(path),
            extracted.file_hash,
            len(extracted.pages),
            now,
            now,
        ),
    )

    connection.execute("DELETE FROM paper_pages WHERE paper_id = ?", (extracted.paper_id,))
    connection.execute("DELETE FROM paper_chunks WHERE paper_id = ?", (extracted.paper_id,))
    connection.execute("DELETE FROM paper_chunks_fts WHERE paper_id = ?", (extracted.paper_id,))

    for page in extracted.pages:
        connection.execute(
            """
            INSERT INTO paper_pages (paper_id, page_number, text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (extracted.paper_id, page.page_number, page.text, now, now),
        )

        for chunk_index, chunk in enumerate(
            _chunk_text(page.text, settings.chunk_size, settings.chunk_overlap), start=1
        ):
            chunk_id = f"{extracted.paper_id}_p{page.page_number:04d}_c{chunk_index:03d}"
            connection.execute(
                """
                INSERT INTO paper_chunks (
                    chunk_id, paper_id, page_number, chunk_index, text, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    extracted.paper_id,
                    page.page_number,
                    chunk_index,
                    chunk,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_chunks_fts (
                    paper_id, chunk_id, page_number, title, authors, text
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    extracted.paper_id,
                    chunk_id,
                    page.page_number,
                    extracted.title,
                    extracted.authors,
                    chunk,
                ),
            )
    return extracted.paper_id


def scan_papers() -> dict[str, int | list[str]]:
    if not settings.papers_dir.exists():
        return {"scanned": 0, "indexed": 0, "failed": 0, "errors": ["papers_dir_missing"]}

    pdfs = [path for path in settings.papers_dir.rglob("*.pdf") if path.is_file()]
    indexed = 0
    errors: list[str] = []

    with db_session() as connection:
        for path in pdfs:
            try:
                index_pdf(connection, path)
                indexed += 1
            except Exception as exc:
                errors.append(type(exc).__name__)

    return {
        "scanned": len(pdfs),
        "indexed": indexed,
        "failed": len(errors),
        "errors": errors[:10],
    }


def watch_and_index() -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class PdfHandler(FileSystemEventHandler):
        def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory and str(event.src_path).lower().endswith(".pdf"):
                with db_session() as connection:
                    index_pdf(connection, Path(event.src_path))

        def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
            if not event.is_directory and str(event.src_path).lower().endswith(".pdf"):
                with db_session() as connection:
                    index_pdf(connection, Path(event.src_path))

    observer = Observer()
    observer.schedule(PdfHandler(), str(settings.papers_dir), recursive=True)
    observer.start()
    try:
        while True:
            observer.join(1)
    finally:
        observer.stop()
        observer.join()

