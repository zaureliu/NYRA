"""Transactional filesystem primitives for the NYRA -> Kazumi migration.

Invoked by an explicit local migration operation, never by an LLM capability.
No historical conversation text is rewritten and no secrets are exported.
"""
from __future__ import annotations
from contextlib import closing

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import re
import os

from .brand_compat import preferences, pronunciation_document


def tree_manifest(root: Path, *, full_hash: bool = True) -> dict:
    root = root.resolve(strict=True)
    files = {}
    paths = sorted(root.rglob('*'))
    for path in paths:
        if path.is_symlink():
            link = os.readlink(path)
            if full_hash or Path(link).is_absolute() or not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError('Migration requires explicit handling of external or directory symbolic links')
            files[path.relative_to(root).as_posix()] = {'bytes': 0, 'relative_link': link}
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        row = {'bytes': path.stat().st_size}
        if full_hash:
            with path.open('rb') as stream:
                row['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        files[relative] = row
    if not full_hash:
        # Full inventory plus deterministic samples, bounded for model caches.
        for relative in list(files)[:8] + list(files)[-8:]:
            if 'relative_link' in files[relative]:
                continue
            with (root / relative).open('rb') as stream:
                files[relative]['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return files


def verified_copy(source: Path, target: Path, *, archive: Path | None = None) -> dict:
    source = source.resolve(strict=True)
    target = target.resolve()
    if source.parent == source or target.exists() or target == source or target.is_relative_to(source):
        raise ValueError('Unsafe or conflicting migration path')
    if archive is not None:
        archive = archive.resolve()
        if archive.exists() or archive.is_relative_to(source) or archive.is_relative_to(target):
            raise ValueError('Unsafe rollback archive path')
    before = tree_manifest(source)
    shutil.copytree(source, target)
    after = tree_manifest(target)
    if before != after:
        # Both source and failed target are kept for inspection/recovery.
        raise RuntimeError('Migration copy verification failed; original retained')
    if archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        source.rename(archive)
    return {'files_before': len(before), 'files_after': len(after),
            'bytes': sum(row['bytes'] for row in before.values()), 'hashes_match': True}


def verified_move(source: Path, target: Path) -> dict:
    source = source.resolve(strict=True)
    target = target.resolve()
    if source.parent != target.parent or target.exists() or source.parent == source:
        raise ValueError('Atomic migration requires distinct sibling paths')
    before = tree_manifest(source, full_hash=False)
    source.rename(target)
    try:
        after = tree_manifest(target, full_hash=False)
        if before != after:
            raise RuntimeError('Migration move verification failed')
    except BaseException:
        target.rename(source)
        raise
    return {'files_before': len(before), 'files_after': len(after),
            'bytes': sum(row['bytes'] for row in before.values()), 'hash_samples_match': True}


def database_inventory(path: Path) -> dict:
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {table: db.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                  for table in tables}
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise RuntimeError('Database integrity check failed')
    return {'tables': counts, 'integrity': integrity}


def migrate_database_filename(root: Path) -> dict:
    old, new = root / 'data/nyra.db', root / 'data/kazumi.db'
    if not old.exists():
        return {'state': 'NOT_PRESENT_OR_ALREADY_MIGRATED'}
    if new.exists():
        raise ValueError('Both databases exist; refusing to merge or replace user data')
    with closing(sqlite3.connect(old)) as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = database_inventory(old)
    old.rename(new)
    try:
        after = database_inventory(new)
        if before != after:
            raise RuntimeError('Database row counts changed')
    except BaseException:
        new.rename(old)
        raise
    return {'state': 'MIGRATED', 'before': before, 'after': after, 'rows_preserved': True}


def migrate_settings_file(path: Path) -> bool:
    if not path.is_file():
        return False
    before = json.loads(path.read_text(encoding='utf-8-sig'))
    after = preferences(before)
    if after == before:
        return False
    temporary = path.with_name(path.name + '.kazumi-migration.tmp')
    temporary.write_text(json.dumps(after, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return True


def migrate_runtime_content(root: Path) -> dict:
    """Update nominal configuration; never rewrite conversation/history or vaults.

    Caller checkpoints the directory and stops the application first. Unknown
    files are retained. Original documents stay in memory for failure rollback.
    """
    import yaml
    changes = {}
    for directory in ('config', 'data/hardware-engine'):
        folder = root / directory
        if not folder.is_dir():
            continue
        for path in folder.glob('*.json'):
            if any(word in path.name.lower() for word in ('credential', 'secret', 'token')):
                continue
            try:
                old = path.read_bytes()
                document = json.loads(old.decode('utf-8-sig'))
                new = preferences(document)
                if new != document:
                    changes[path] = (old, (json.dumps(new, ensure_ascii=False, indent=2) + '\n').encode())
            except (ValueError, UnicodeError):
                continue
    config = root / 'config/default.yaml'
    if config.is_file():
        old = config.read_bytes()
        document = yaml.safe_load(old.decode('utf-8-sig'))
        new = preferences(document)
        if document != new:
            changes[config] = (old, yaml.safe_dump(new, allow_unicode=True, sort_keys=False).encode())
    identity = root / 'identity'
    if identity.is_dir():
        for path in identity.iterdir():
            if path.suffix not in ('.md', '.json') or not path.is_file():
                continue
            old = path.read_bytes()
            text = old.decode('utf-8-sig')
            # These are identity settings, not historical transcripts.
            if path.name.startswith('pronunciation') and path.suffix == '.json':
                new = json.dumps(pronunciation_document(json.loads(text)), ensure_ascii=False, indent=2) + '\n'
            else:
                new = re.sub(r'\bnyra\b', 'Kazumi', text, flags=re.IGNORECASE)
            if new != text:
                changes[path] = (old, new.encode())
    try:
        for path, (_, new) in changes.items():
            temporary = path.with_name(path.name + '.kazumi-migration.tmp')
            temporary.write_bytes(new)
            temporary.replace(path)
        database = migrate_database_filename(root)
    except BaseException:
        for path, (old, _) in changes.items():
            path.write_bytes(old)
        raise
    return {'changed': [path.relative_to(root).as_posix() for path in changes], 'database': database}


def default_runtime_root(base: Path) -> Path:
    """One-time default-directory upgrade; explicit user overrides stay explicit."""
    old, new = base / 'NYRA', base / 'Kazumi'
    if not old.is_dir():
        return new
    if new.exists():
        if (old / 'data/nyra.db').exists():
            raise RuntimeError('Both legacy and current runtime data exist; resolve migration before startup')
        return new
    import psutil
    if any((p.info.get('name') or '').casefold() in ('nyra-backend.exe', 'nyra-desktop.exe')
           for p in psutil.process_iter(['name'])):
        raise RuntimeError('Close the legacy application before migrating runtime data')
    verified_move(old, new)
    try:
        migrate_runtime_content(new)
    except BaseException:
        new.rename(old)
        raise
    return new
