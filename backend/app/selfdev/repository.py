from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from app.selfdev.storage import atomic_write_json, load_json


SUPPORTED_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".json", ".yaml", ".yml", ".toml", ".ps1"}
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "target", "dist", "build",
    "__pycache__", ".pytest_cache", ".test-temp", ".tmp", "data", "logs",
    "cache", "downloads", "models", "artifacts", "worktrees", "rejected",
}
EXCLUDED_NAMES = {".env", ".nyra-runtime.json", "credentials-vault.bin"}
ROUTE_RE = re.compile(r"(?:@router\.(?:get|post|put|patch|delete)\(\s*|fetch\(\s*|api(?:Send)?\(\s*)[\"']([^\"']+)")
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def _is_excluded_directory(name: str) -> bool:
    lowered = name.casefold()
    return lowered in EXCLUDED_PARTS or lowered.startswith(".venv")


@dataclass(frozen=True)
class IndexStats:
    files: int
    changed: int
    reused: int
    removed: int


class RepositoryMapper:
    """Incremental, metadata-only source index; source text is never persisted."""

    def __init__(self, repository_root: Path, index_path: Path) -> None:
        self.repository_root = repository_root.resolve()
        self.index_path = index_path
        self._index: dict[str, Any] = {"version": 1, "files": {}}

    @property
    def index(self) -> dict[str, Any]:
        return self._index

    def build(self) -> IndexStats:
        previous = load_json(self.index_path, {"version": 1, "files": {}})
        old_files = previous.get("files", {}) if isinstance(previous, dict) else {}
        files: dict[str, Any] = {}
        changed = reused = 0
        for path in self._source_files():
            relative = path.relative_to(self.repository_root).as_posix()
            stat = path.stat()
            prior = old_files.get(relative)
            if (
                isinstance(prior, dict)
                and prior.get("mtime_ns") == stat.st_mtime_ns
                and prior.get("size") == stat.st_size
                and prior.get("sha256")
            ):
                files[relative] = prior
                reused += 1
                continue
            parsed = self._parse(path)
            parsed.update({
                "path": relative,
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "sha256": self._sha256(path),
                "language": self._language(path.suffix.casefold()),
            })
            files[relative] = parsed
            changed += 1
        removed = len(set(old_files) - set(files))
        self._index = {"version": 1, "root_name": self.repository_root.name, "files": files}
        atomic_write_json(self.index_path, self._index)
        return IndexStats(len(files), changed, reused, removed)

    def load(self) -> dict[str, Any]:
        self._index = load_json(self.index_path, {"version": 1, "files": {}})
        return self._index

    def _source_files(self) -> Iterable[Path]:
        # Prune generated/private trees before walking them. Path.rglob still
        # enumerates every descendant before a per-file exclusion check, which
        # made startup scale with multi-gigabyte target/node_modules trees.
        for directory, dirnames, filenames in os.walk(
            self.repository_root, topdown=True, followlinks=False,
        ):
            dirnames[:] = sorted(
                name for name in dirnames
                if not _is_excluded_directory(name)
            )
            base = Path(directory)
            for filename in sorted(filenames):
                path = base / filename
                if path.is_symlink() or path.suffix.casefold() not in SUPPORTED_SUFFIXES:
                    continue
                lowered = path.name.casefold()
                if lowered in EXCLUDED_NAMES or lowered.startswith(".env"):
                    continue
                try:
                    if path.stat().st_size > 1_000_000:
                        continue
                except OSError:
                    continue
                yield path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _language(suffix: str) -> str:
        if suffix == ".py":
            return "python"
        if suffix in {".ts", ".tsx", ".js", ".jsx"}:
            return "typescript"
        if suffix == ".rs":
            return "rust"
        return "config"

    def _parse(self, path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"symbols": [], "imports": [], "routes": [], "references": []}
        if path.suffix.casefold() == ".py":
            return self._parse_python(text)
        if path.suffix.casefold() in {".ts", ".tsx", ".js", ".jsx"}:
            return self._parse_typescript(text)
        if path.suffix.casefold() == ".rs":
            return self._parse_rust(text)
        return self._parse_config(text, path.suffix.casefold())

    @staticmethod
    def _parse_python(text: str) -> dict[str, Any]:
        symbols: list[dict[str, Any]] = []
        imports: set[str] = set()
        routes: list[str] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return {"symbols": [], "imports": [], "routes": [], "references": []}
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                decorators = [ast.unparse(item) for item in node.decorator_list]
                symbols.append({"name": node.name, "kind": kind, "line": node.lineno, "decorators": decorators})
                for decorator in decorators:
                    match = re.search(r"router\.(?:get|post|put|patch|delete)\(['\"]([^'\"]+)", decorator)
                    if match:
                        routes.append(match.group(1))
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        references = sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})
        return {"symbols": symbols, "imports": sorted(imports), "routes": sorted(set(routes)), "references": references[:1000]}

    @staticmethod
    def _parse_typescript(text: str) -> dict[str, Any]:
        symbol_re = re.compile(r"\b(?:export\s+)?(?:default\s+)?(?:function|class|interface|type|const|let)\s+([A-Za-z_$][\w$]*)")
        import_re = re.compile(r"\b(?:import|export)\b[^\n]*?\bfrom\s+[\"']([^\"']+)[\"']")
        symbols = [{"name": match.group(1), "kind": "symbol", "line": text.count("\n", 0, match.start()) + 1} for match in symbol_re.finditer(text)]
        return {
            "symbols": symbols,
            "imports": sorted(set(import_re.findall(text))),
            "routes": sorted(set(ROUTE_RE.findall(text))),
            "references": sorted(set(IDENTIFIER_RE.findall(text)))[:1000],
        }

    @staticmethod
    def _parse_rust(text: str) -> dict[str, Any]:
        symbol_re = re.compile(r"\b(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|mod)\s+([A-Za-z_][\w]*)")
        use_re = re.compile(r"^\s*use\s+([^;]+);", re.MULTILINE)
        symbols = [{"name": match.group(1), "kind": "symbol", "line": text.count("\n", 0, match.start()) + 1} for match in symbol_re.finditer(text)]
        return {
            "symbols": symbols,
            "imports": sorted(set(use_re.findall(text))),
            "routes": [],
            "references": sorted(set(IDENTIFIER_RE.findall(text)))[:1000],
        }

    @staticmethod
    def _parse_config(text: str, suffix: str) -> dict[str, Any]:
        keys: list[str] = []
        if suffix == ".json":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    keys = [str(key) for key in parsed][:1000]
            except ValueError:
                pass
        else:
            keys = re.findall(r"(?m)^\s*([A-Za-z_][\w.-]*)\s*[:=]", text)[:1000]
        return {"symbols": [{"name": key, "kind": "setting", "line": 0} for key in keys], "imports": [], "routes": [], "references": keys}


class RepositoryQueryEngine:
    def __init__(self, mapper: RepositoryMapper) -> None:
        self.mapper = mapper

    def definitions(self, symbol: str) -> list[dict[str, Any]]:
        needle = symbol.casefold()
        results = []
        for path, entry in self._files().items():
            for item in entry.get("symbols", []):
                if str(item.get("name", "")).casefold() == needle:
                    results.append({"path": path, **item})
        return results

    def consumers(self, name: str) -> list[str]:
        needle = name.casefold()
        return sorted({
            path for path, entry in self._files().items()
            if any(needle in str(value).casefold() for value in (*entry.get("imports", []), *entry.get("references", [])))
        })

    def related_tests(self, name: str) -> list[str]:
        return [path for path in self.consumers(name) if self._is_test(path)]

    def route_consumers(self, route: str) -> list[str]:
        return sorted(path for path, entry in self._files().items() if route in entry.get("routes", []))

    def dependents(self, module_name: str) -> list[str]:
        needle = module_name.replace("\\", "/").replace(".py", "").replace("/", ".").casefold()
        return sorted(path for path, entry in self._files().items() if any(needle in str(item).casefold() for item in entry.get("imports", [])))

    def query(self, question: str) -> dict[str, Any]:
        route = re.search(r"/api/[A-Za-z0-9_./{}-]+", question)
        symbol = re.search(r"\b[A-Z][A-Za-z0-9_]{2,}\b", question)
        if route:
            value = route.group(0)
            return {"kind": "route_consumers", "query": value, "results": self.route_consumers(value)}
        if symbol:
            value = symbol.group(0)
            return {
                "kind": "symbol",
                "query": value,
                "definitions": self.definitions(value),
                "consumers": self.consumers(value),
                "tests": self.related_tests(value),
            }
        return {"kind": "text", "query": question[:200], "results": []}

    def _files(self) -> dict[str, Any]:
        return self.mapper.index.get("files", {})

    @staticmethod
    def _is_test(path: str) -> bool:
        lowered = path.casefold()
        return "/tests/" in f"/{lowered}" or ".test." in lowered or Path(lowered).name.startswith("test_")
