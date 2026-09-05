"""Local generated workspaces with bounded history and source/build association."""
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from app.tools.redaction import redact_secrets
from .models import HardwareError, now


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


class ProjectStore:
    def __init__(self, root: Path, state_root: Path):
        self.root, self.state_root = root.resolve(), state_root.resolve()
        self.index_path = self.state_root / 'projects.json'
        self.index = {}
        self.active = None
        if self.index_path.is_file():
            value = json.loads(self.index_path.read_text(encoding='utf8'))
            self.index, self.active = value.get('projects', {}), value.get('active')

    def path(self, project_id):
        name = self.index.get(project_id)
        if not name or not re.fullmatch(r'[a-z0-9-]{1,90}', name):
            raise HardwareError('PROJECT_NOT_FOUND')
        path = (self.root / name).resolve()
        if path == self.root or not path.is_relative_to(self.root) or path.is_symlink():
            raise HardwareError('PROJECT_OUTSIDE_WORKSPACE')
        return path

    def read(self, project_id):
        path = self.path(project_id) / '.nyra-project.json'
        return json.loads(path.read_text(encoding='utf8'))

    def save(self, meta):
        path = self.path(meta['project_id']) / '.nyra-project.json'
        encoded = json.dumps(meta, ensure_ascii=False, indent=2)
        if redact_secrets(encoded) != encoded:
            raise HardwareError('PROJECT_SECRET_REJECTED')
        path.write_text(encoded, encoding='utf8')
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps({'projects': self.index, 'active': self.active}), encoding='utf8')

    def create(self, profile, device=None):
        self.root.mkdir(parents=True, exist_ok=True)
        project_id = 'project_' + uuid4().hex
        name = re.sub('[^a-z0-9-]', '-', profile.board_id.casefold())[:60] + '-' + project_id[-8:]
        path = self.root / name
        path.mkdir(exist_ok=False)
        (path / 'src').mkdir()
        (path / '.nyra-history').mkdir()
        self.index[project_id] = name
        self.active = project_id
        meta = {'project_id': project_id, 'name': name, 'created_at': now(),
                'board': profile.model_dump(), 'device_id': (device or {}).get('device_id'),
                'device_identity': {k: (device or {}).get(k) for k in ('device_instance_id', 'serial', 'vid', 'pid')},
                'toolchain': 'platformio', 'framework': profile.framework, 'serial_port': (device or {}).get('com_port'),
                'sources': profile.sources, 'build': {}, 'flash': {}, 'history': [], 'source_hashes': {}, 'notes': []}
        config = f'[env:nyra]\nplatform = {profile.platform}\nboard = {profile.board_id}\nframework = {profile.framework}\nmonitor_speed = 115200\n'
        self.write(project_id, 'platformio.ini', config)
        self.write(project_id, 'README.md', f'# {profile.name}\n\nGenerated locally by NYRA.\n\nDocumentation: {profile.docs_url}\n\nBuild/flash are not proof of physical LED illumination.\n')
        self.save(meta)
        return meta

    def write(self, project_id, relative, content):
        if not self.allowed_file(relative):
            raise HardwareError('PROJECT_FILE_NOT_ALLOWED')
        root = self.path(project_id)
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target.is_symlink():
            raise HardwareError('PROJECT_PATH_ESCAPE')
        if len(content) > 100000 or redact_secrets(content) != content:
            raise HardwareError('PROJECT_CONTENT_REJECTED')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf8')
        return str(target)

    def source_hash(self, project_id):
        root = self.path(project_id)
        return {relative: digest(root / relative) for relative in self.inspect(project_id)}

    @staticmethod
    def allowed_file(relative):
        return relative in ('platformio.ini', 'README.md', 'nyra.code-workspace') or bool(
            re.fullmatch(r'(?:src|include)/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:c|cpp|h|hpp)', relative)
            or re.fullmatch(r'src/[a-z][a-z0-9_]{1,40}/LICENSE\.txt', relative))

    def inspect(self, project_id):
        root = self.path(project_id)
        files = [root / 'platformio.ini']
        for folder in ('src', 'include'):
            files.extend(sorted((root / folder).rglob('*')))
        result = {}
        for file in files:
            if not file.is_file():
                continue
            relative = file.relative_to(root).as_posix()
            if file.is_symlink() or not file.resolve().is_relative_to(root) or not self.allowed_file(relative):
                raise HardwareError('UNREVIEWED_PROJECT_SOURCE')
            if file.stat().st_size > 100000:
                raise HardwareError('PROJECT_CONTEXT_TOO_LARGE')
            result[relative] = file.read_text(encoding='utf8')
        if len(result) > 40 or sum(map(len, result.values())) > 160000:
            raise HardwareError('PROJECT_CONTEXT_TOO_LARGE')
        return result

    def select(self, project_id):
        meta = self.read(project_id)
        self.active = project_id
        self.save(meta)
        return meta

    def checkpoint(self, meta):
        root = self.path(meta['project_id'])
        state = self.source_hash(meta['project_id'])
        key = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        history = root / '.nyra-history' / key
        history.mkdir(exist_ok=True)
        for relative in state:
            archived = history / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes((root / relative).read_bytes())
        meta['history'] = (meta['history'] + [{'state': key, 'at': now(), 'source_hashes': state}])[-20:]
        meta['source_hashes'] = state
        self.save(meta)
        return key

    def validate_build_inputs(self, meta):
        root = self.path(meta['project_id'])
        expected = meta.get('source_hashes', {})
        if not expected or self.source_hash(meta['project_id']) != expected:
            raise HardwareError('SOURCE_CHANGED_REVIEW_REQUIRED')
        profile = meta['board']
        from .adapters import PROFILES
        trusted = next((p for _, p in PROFILES if p.board_id == profile['board_id']), None)
        if trusted is None or any(profile.get(key) != getattr(trusted, key) for key in ('chip', 'platform', 'framework')):
            raise HardwareError('UNTRUSTED_BUILD_TARGET')
        expected_config = f"[env:nyra]\nplatform = {profile['platform']}\nboard = {profile['board_id']}\nframework = {profile['framework']}\nmonitor_speed = 115200\n"
        if (root / 'platformio.ini').read_text(encoding='utf8') != expected_config:
            raise HardwareError('UNREVIEWED_BUILD_CONFIGURATION')
        # PlatformIO automatically executes extra scripts and builds local libs.
        # V1 generated projects cannot introduce unreviewed build extensions.
        for folder in ('lib', 'scripts', 'test'):
            if (root / folder).exists() and any((root / folder).iterdir()):
                raise HardwareError('UNREVIEWED_BUILD_EXTENSION')
        self.inspect(meta['project_id'])
