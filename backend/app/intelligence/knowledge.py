from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

import aiosqlite

from app.intelligence.models import KnowledgeHit
from app.intelligence.storage import IntelligenceStore
from app.tools.redaction import redact_secrets


WORDS = re.compile(r"[\wÀ-ÿ-]{2,}", re.UNICODE)
SUPPORTED_SUFFIXES = {
    ".md", ".txt", ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".rs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".pdf",
}
EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules", "target", "dist", "build", "data", "logs", "cache", "models"}


class KnowledgeEngine:
    """Incremental local RAG using SQLite FTS plus deterministic feature hashing."""

    def __init__(self, store: IntelligenceStore, allowed_roots: Iterable[Path], *, dimensions: int = 96,
                 max_file_bytes: int = 5_000_000) -> None:
        self.store = store
        self.allowed_roots = tuple(path.resolve() for path in allowed_roots)
        self.dimensions = max(32, min(dimensions, 384))
        self.max_file_bytes = max_file_bytes

    def status(self) -> dict:
        try:
            import pypdf  # type: ignore  # noqa: F401
            pdf = "AVAILABLE"
        except ImportError:
            pdf = "UNCONFIGURED"
        return {"state": "AVAILABLE", "storage": "sqlite_local", "embedding": f"feature_hash_{self.dimensions}", "pdf": pdf, "allowed_roots": [path.name for path in self.allowed_roots]}

    async def ingest(self, path: Path, *, metadata: dict | None = None) -> dict:
        source = self._authorized_file(path)
        stat = source.stat()
        if stat.st_size > self.max_file_bytes:
            raise ValueError("KNOWLEDGE_FILE_TOO_LARGE")
        raw, mime_type = self._extract(source)
        raw = redact_secrets(raw)
        normalized = self._normalize(raw)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        canonical = str(source)
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT id,sha256,chunk_count FROM knowledge_documents WHERE path=?", (canonical,))).fetchone()
            if row and row[1] == digest:
                return {"status": "UNCHANGED", "document_id": row[0], "chunks": int(row[2]), "path": source.name}
            document_id = str(row[0]) if row else f"doc_{uuid4().hex}"
            old_chunk_ids = [item[0] for item in await (await db.execute("SELECT id FROM knowledge_chunks WHERE document_id=?", (document_id,))).fetchall()]
            if old_chunk_ids:
                await db.executemany("DELETE FROM knowledge_fts WHERE chunk_id=?", [(value,) for value in old_chunk_ids])
            await db.execute("DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,))
            chunks = self._chunks(normalized, source.suffix.casefold())
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """INSERT INTO knowledge_documents(id,path,sha256,size,mtime_ns,mime_type,metadata,indexed_at,chunk_count)
                   VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,size=excluded.size,
                   mtime_ns=excluded.mtime_ns,mime_type=excluded.mime_type,metadata=excluded.metadata,indexed_at=excluded.indexed_at,
                   chunk_count=excluded.chunk_count""",
                (document_id, canonical, digest, stat.st_size, stat.st_mtime_ns, mime_type,
                 json.dumps(metadata or {}, ensure_ascii=False), now, len(chunks)),
            )
            for index, content in enumerate(chunks):
                chunk_id = f"chk_{uuid4().hex}"
                await db.execute(
                    "INSERT INTO knowledge_chunks(id,document_id,chunk_index,content,embedding,metadata) VALUES(?,?,?,?,?,?)",
                    (chunk_id, document_id, index, content, json.dumps(self._embed(content)), json.dumps({"lineage": digest, "index": index})),
                )
                await db.execute("INSERT INTO knowledge_fts(chunk_id,content) VALUES(?,?)", (chunk_id, content))
            await db.commit()
        return {"status": "INDEXED" if not row else "UPDATED", "document_id": document_id, "chunks": len(chunks), "path": source.name, "sha256": digest}

    async def ingest_tree(self, root: Path, *, max_files: int = 500) -> dict:
        authorized = self._authorized_root(root)
        indexed = unchanged = failed = 0
        failures: list[dict] = []
        for path in authorized.rglob("*"):
            if indexed + unchanged + failed >= max_files:
                break
            if not path.is_file() or path.suffix.casefold() not in SUPPORTED_SUFFIXES:
                continue
            relative = path.relative_to(authorized)
            if any(part.casefold() in EXCLUDED_PARTS for part in relative.parts):
                continue
            try:
                result = await self.ingest(path, metadata={"root": str(authorized), "relative_path": relative.as_posix()})
                if result["status"] == "UNCHANGED":
                    unchanged += 1
                else:
                    indexed += 1
            except (OSError, ValueError, RuntimeError) as error:
                failed += 1
                failures.append({"path": relative.as_posix(), "error_code": str(error)[:120]})
        return {"indexed": indexed, "unchanged": unchanged, "failed": failed, "failures": failures[:50], "limited": indexed + unchanged + failed >= max_files}

    async def retrieve(self, query: str, *, limit: int = 8) -> list[KnowledgeHit]:
        terms = {word.casefold() for word in WORDS.findall(query)}
        if not terms:
            return []
        query_vector = self._embed(query)
        async with aiosqlite.connect(self.store.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                """SELECT c.*,d.path,d.mime_type,d.metadata AS document_metadata
                   FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id
                   ORDER BY d.indexed_at DESC LIMIT 1000"""
            )).fetchall()
        hits: list[KnowledgeHit] = []
        for row in rows:
            content_terms = {word.casefold() for word in WORDS.findall(row["content"])}
            lexical = len(terms & content_terms) / max(1, len(terms))
            vector = json.loads(row["embedding"])
            semantic = sum(a * b for a, b in zip(query_vector, vector))
            if lexical <= 0 and semantic < 0.15:
                continue
            score = max(0, min(1, lexical * 0.62 + max(0, semantic) * 0.38))
            path = Path(row["path"])
            metadata = json.loads(row["document_metadata"] or "{}")
            hits.append(KnowledgeHit(
                document_id=row["document_id"], chunk_id=row["id"], path=str(path),
                content=row["content"], mime_type=row["mime_type"], score=round(score, 6),
                chunk_index=int(row["chunk_index"]), provenance={"path": str(path), "sha_metadata": metadata, "chunk": int(row["chunk_index"])},
            ))
        return sorted(hits, key=lambda item: item.score, reverse=True)[:max(1, min(limit, 30))]

    async def remove(self, path: Path) -> bool:
        source = self._authorized_path(path)
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT id FROM knowledge_documents WHERE path=?", (str(source),))).fetchone()
            if not row:
                return False
            ids = [item[0] for item in await (await db.execute("SELECT id FROM knowledge_chunks WHERE document_id=?", (row[0],))).fetchall()]
            await db.executemany("DELETE FROM knowledge_fts WHERE chunk_id=?", [(value,) for value in ids])
            await db.execute("DELETE FROM knowledge_documents WHERE id=?", (row[0],))
            await db.commit()
        return True

    def _authorized_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in self.allowed_roots):
            raise PermissionError("RAG_PATH_OUTSIDE_AUTHORIZED_ROOTS")
        return resolved

    def _authorized_root(self, path: Path) -> Path:
        resolved = self._authorized_path(path)
        if not resolved.is_dir():
            raise ValueError("KNOWLEDGE_ROOT_NOT_DIRECTORY")
        return resolved

    def _authorized_file(self, path: Path) -> Path:
        resolved = self._authorized_path(path)
        if not resolved.is_file():
            raise FileNotFoundError("KNOWLEDGE_FILE_NOT_FOUND")
        if resolved.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise ValueError("KNOWLEDGE_FORMAT_UNSUPPORTED")
        return resolved

    @staticmethod
    def _normalize(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()

    def _extract(self, path: Path) -> tuple[str, str]:
        mime = mimetypes.guess_type(path.name)[0] or "text/plain"
        if path.suffix.casefold() != ".pdf":
            return path.read_text(encoding="utf-8", errors="replace"), mime
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as error:
            raise RuntimeError("PDF_EXTRACTOR_UNAVAILABLE") from error
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages), "application/pdf"

    @staticmethod
    def _chunks(text: str, suffix: str, *, target: int = 1400, overlap: int = 180) -> list[str]:
        if not text:
            return []
        separators = "\n\n" if suffix in {".md", ".txt", ".log"} else "\n"
        parts = text.split(separators)
        chunks: list[str] = []
        current = ""
        for part in parts:
            candidate = f"{current}{separators if current else ''}{part}"
            if len(candidate) <= target or not current:
                current = candidate
                continue
            chunks.append(current.strip())
            current = current[-overlap:] + separators + part
            while len(current) > target * 2:
                chunks.append(current[:target].strip())
                current = current[target - overlap:]
        if current.strip():
            chunks.append(current.strip())
        return [value for value in chunks if value]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in WORDS.findall(text.casefold()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log1p(len(token)))
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 8) for value in vector]
