import hashlib
import json
from pathlib import Path
import time

from .models import Source


class ResearchCache:
    """Bounded technical cache, separate from personal knowledge. No executables."""
    def __init__(self, root: Path, max_entries=64):
        self.root, self.max_entries = root, max_entries

    def path(self, url):
        return self.root / (hashlib.sha256(url.encode()).hexdigest() + '.json')

    def get(self, url, ttl=86400, allow_stale=False):
        path = self.path(url)
        try:
            stale = time.time() - path.stat().st_mtime > ttl
            if stale and not allow_stale:
                return None
            source = Source.model_validate_json(path.read_text(encoding='utf8'))
            return source.model_copy(update={'cached': True, 'stale': stale})
        except (OSError, ValueError):
            return None

    def put(self, source):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(source.url)
        temp = path.with_suffix('.tmp')
        temp.write_text(source.model_dump_json(), encoding='utf8')
        temp.replace(path)
        for old in sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime)[:-self.max_entries]:
            old.unlink()  # Only the cache's hashed, generated entries.
