"""Public packaging/defaults contracts; no external service or operator data."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from app.hardware_engine.service import HardwareEngineeringService

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('public_release_gate', ROOT / 'scripts/public_release_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

def test_public_project_root_is_user_relative_and_overridable(tmp_path, monkeypatch):
    monkeypatch.delenv('KAZUMI_PROJECTS_ROOT', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    services = SimpleNamespace(usb=None, world_state=None, shell=None)
    engine = HardwareEngineeringService(services, tmp_path / 'state')
    assert engine.projects.root == tmp_path / 'Kazumi-Projects'
    monkeypatch.setenv('KAZUMI_PROJECTS_ROOT', str(tmp_path / 'chosen'))
    engine = HardwareEngineeringService(services, tmp_path / 'state')
    assert engine.projects.root == tmp_path / 'chosen'

def test_public_gate_does_not_hide_secret_beside_fixture():
    candidate = 'api_key = "' + 'sk-' + '9' * 24 + '" # fixture-secret'
    assert gate.scan_text('backend/tests/test_controlled.py', candidate)
    assert not gate.scan_text('backend/tests/test_controlled.py', 'secret = "fixture-secret"')

def test_public_gate_keeps_runtime_source_but_rejects_personal_paths():
    assert 'runtime' not in gate.BLOCKED_PARTS
    candidate = 'C:' + chr(92) + 'Users' + chr(92) + 'PersonalAccount' + chr(92) + 'file.txt'
    assert gate.scan_text('docs/example.md', candidate)
