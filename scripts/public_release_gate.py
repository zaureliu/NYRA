"""Bounded public-source privacy gate. Prints locations, never secret values."""
from __future__ import annotations
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_PARTS = {'.tmp', 'tmp', 'temp', 'data', 'logs', 'recordings', 'node_modules',
                 'target', 'dist', 'build', '.venv', 'venv', '__pycache__', 'worktrees',
                 'backups', 'NYRA-Knowledge', 'NYRA-Projects', 'screenshots'}
BLOCKED_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.log', '.wav', '.mp3', '.ogg',
                    '.pcm', '.raw', '.flac', '.pdf', '.exe', '.dll', '.msi', '.pyc',
                    '.pyo', '.pem', '.key', '.pfx', '.p12', '.moc3', '.bin', '.onnx'}
RULES = {
    'private_key': re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----'),
    'provider_key': re.compile(r'\bsk-(?:or-)?[A-Za-z0-9_-]{16,}'),
    'github_token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'jwt': re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'),
    'assigned_secret': re.compile(r'''(?i)\b(?:api[_-]?key|token|password|passwd|secret)["']?\s*[:=]\s*["'][^<>"'\n]{12,}["']'''),
    'bearer': re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}'),
}
# Exact existing fixture values only, and only inside tests. A fixture marker
# must never suppress a different credential on the same line.
FIXTURES = re.compile(r'(?<![\w-])(?:abcdefghijklmnopqrstuvwxyz|NYRA_SECRET_LEAK_TEST_a7f3d9c2b1|NYRA_SECRET_LEAK_TEST|legacy-functional-token|'
    r'tok-abcdef-123456|monitor-tok-9876|ha-live-token-9f2c|supersecrettokenvalue1234|'
    r'token-super-secreto-da-api-9876|s3cret-NYRA-openwrt-PW|fixture-credential-never-log-this|fixture-secret|'
    r'secret-token-value|private-token)(?![\w-])')
PERSONAL_PATH = re.compile(r'(?i)\b[A-Z]:[\\/]+Users[\\/]+(?![<%{])([^\\/\s]+)')

def scan_text(relative: str, text: str) -> list[dict]:
    findings = []
    testing = 'tests' in Path(relative).parts or '.test.' in relative
    for number, line in enumerate(text.splitlines(), 1):
        candidate = FIXTURES.sub('', line) if testing else line
        for name, pattern in RULES.items():
            if pattern.search(candidate):
                findings.append({'path': relative, 'line': number, 'rule': name})
        for match in PERSONAL_PATH.finditer(line):
            # Standard shared Windows directory and explicit redaction fixture.
            if match[1] == 'Public' or testing and match[1] == 'Operator':
                continue
            findings.append({'path': relative, 'line': number, 'rule': 'personal_path'})
    return findings

def source_files(root: Path) -> list[str]:
    output = subprocess.check_output(['git', '-C', str(root), 'ls-files',
        '--cached', '--others', '--exclude-standard', '-z'])
    return sorted(set(output.decode('utf-8').split('\0')) - {''})

def scan(root: Path) -> dict:
    findings = []; checked = 0
    for relative in source_files(root):
        path = root / relative
        if not path.exists():
            continue  # tracked removal awaiting stage
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            findings.append({'path': relative, 'rule': 'path_escape'}); continue
        parts = set(Path(relative).parts)
        if parts & BLOCKED_PARTS or path.suffix.lower() in BLOCKED_SUFFIXES or relative.startswith('runtime/'):
            findings.append({'path': relative, 'rule': 'private_runtime_artifact'}); continue
        if path.name.startswith('.env') and path.name != '.env.example':
            findings.append({'path': relative, 'rule': 'environment_file'}); continue
        if any(relative.endswith(s) for s in ('.model3.json', '.physics3.json', '.exp3.json', '.vtube.json')):
            findings.append({'path': relative, 'rule': 'third_party_model'}); continue
        checked += 1
        if path.stat().st_size > 2_000_000:
            findings.append({'path': relative, 'rule': 'unreviewed_large_file'}); continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            if relative not in {'desktop/src-tauri/icons/32x32.png', 'desktop/src-tauri/icons/128x128.png', 'desktop/src-tauri/icons/icon.ico'}:
                findings.append({'path': relative, 'rule': 'unreviewed_binary'})
            continue
        findings.extend(scan_text(relative, text))
    return {'files_checked': checked, 'findings': findings, 'passed': not findings}

if __name__ == '__main__':
    result = scan(ROOT)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['passed'] else 1)
