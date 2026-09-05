"""Only indexed projects can be resolved from conversation/artifact/IDE hints."""
from pathlib import Path

from .models import HardwareError


class ProjectContext:
    def __init__(self, store):
        self.store = store

    def resolve(self, text, *, project_id=None, artifact_paths=(), workspace=None, memory_projects=(), loop_projects=()):
        if project_id:
            return self.store.select(project_id)
        explicit = [key for key, name in self.store.index.items() if name.casefold() in text.casefold() or key in text]
        if len(explicit) == 1:
            return self.store.select(explicit[0])
        if len(explicit) > 1:
            raise HardwareError('AMBIGUOUS_PROJECT')
        # An explicit project name wins. Otherwise current workspace and recent
        # artifacts are stronger than an old active-project preference.
        for hint in ([workspace] if workspace else []) + list(artifact_paths):
            if not hint:
                continue
            candidate = Path(hint).resolve()
            matches = [key for key in self.store.index if candidate.is_relative_to(self.store.path(key))]
            if len(matches) == 1:
                return self.store.select(matches[0])
        for candidates in (loop_projects, memory_projects):
            found = list(dict.fromkeys(p for p in candidates if p in self.store.index))
            if len(found) == 1:
                return self.store.select(found[0])
        if self.store.active:
            return self.store.select(self.store.active)
        if len(self.store.index) == 1:
            return self.store.select(next(iter(self.store.index)))
        raise HardwareError('PROJECT_NOT_FOUND' if not self.store.index else 'AMBIGUOUS_PROJECT')
