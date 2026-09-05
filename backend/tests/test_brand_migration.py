import json
import sqlite3

import pytest

from app.brand_compat import environment_aliases, migrated_path, preferences
from app.core.config import Settings
from app.events.bus import EventBus, EventType
from app.operator import credentials


def test_environment_precedence_and_legacy_fallback():
    env = {'NYRA_LLM_MODEL': 'legacy-model', 'KAZUMI_LLM_MODEL': 'selected-model', 'NYRA_BACKEND_PORT': '8011'}
    environment_aliases(env)
    assert env['KAZUMI_LLM_MODEL'] == 'selected-model'
    assert env['KAZUMI_BACKEND_PORT'] == '8011'


def test_wake_custom_preferences_and_secrets_are_preserved():
    old = {'wake_word': 'Nyra', 'voice_id': 'private-voice', 'provider': 'gradium',
           'credential_id': 'gradium_api_key', 'transcript': 'Eu usava NYRA ontem',
           'stt_recognition': {'keyterms': ['NYRA', 'ESP32']}}
    result = preferences(old)
    assert result['wake_word'] == 'kazumi'
    assert result['voice_id'] == old['voice_id'] and result['credential_id'] == old['credential_id']
    assert result['transcript'] == old['transcript']
    assert result['stt_recognition']['keyterms'] == ['Kazumi', 'ESP32']
    assert Settings(wake_word='computador').wake_word == 'computador'
    assert Settings(wake_word='NYRA').wake_word == 'kazumi'


def test_paths_are_bounded_not_prose_replacement():
    assert migrated_path('../Nyra-Auto-Code/worktrees') == '../Kazumi-Auto-Code/worktrees'
    assert migrated_path(r'E:\NYRA-Projects\board\main.cpp') == r'E:\Kazumi-Projects\board\main.cpp'
    assert migrated_path(r'E:\nyra-elsewhere\x') == r'E:\nyra-elsewhere\x'
    assert migrated_path('E:/NYRA-Knowledge/doc.pdf') == 'E:/Kazumi-Knowledge/doc.pdf'
    assert preferences({'text': r'E:\nyra is history'}) == {'text': r'E:\nyra is history'}


def test_legacy_startup_policy_preserves_service_behavior():
    from app.runtime.models import StartupPolicy
    assert StartupPolicy('ON_NYRA_START') is StartupPolicy.ON_KAZUMI_START
    assert StartupPolicy('MANUAL') is StartupPolicy.MANUAL


@pytest.mark.asyncio
async def test_old_event_accepted_new_event_emitted_once():
    bus = EventBus()
    received = []
    async def listener(event):
        received.append(event)
    await bus.subscribe(listener)
    event = await bus.publish('NYRA_EMOTION_CHANGED', emotion='happy')
    assert event.type is EventType.KAZUMI_EMOTION_CHANGED
    assert event.model_dump(mode='json')['type'] == 'KAZUMI_EMOTION_CHANGED'
    assert len(received) == 1
    assert EventType.NYRA_RESPONSE is EventType.KAZUMI_RESPONSE


def test_credential_copy_stays_inside_vault(monkeypatch):
    data = json.dumps({'secret': 'fixture-secret', 'metadata': {'kind': 'test'}}).encode()
    vault = {'NYRA_CRED:gradium_api_key': data}
    monkeypatch.setattr(credentials, '_wincred_available', lambda: True)
    monkeypatch.setattr(credentials, '_cred_read', vault.get)
    monkeypatch.setattr(credentials, '_cred_write', lambda target, blob, *args: vault.__setitem__(target, blob))
    value = credentials.CredentialVault().read('gradium_api_key')
    assert value['secret'] == 'fixture-secret'
    assert vault['KAZUMI_CRED:gradium_api_key'] == vault['NYRA_CRED:gradium_api_key']


def test_credential_migration_verify_failure_never_deletes_legacy(monkeypatch):
    data = b'{"secret":"fixture-secret"}'
    monkeypatch.setattr(credentials, '_wincred_available', lambda: True)
    monkeypatch.setattr(credentials, '_cred_read', lambda key: data if key.startswith('NYRA_CRED:') else None)
    monkeypatch.setattr(credentials, '_cred_write', lambda *args: None)
    with pytest.raises(credentials.CredentialError, match='verification failed'):
        credentials.CredentialVault().read('gradium_api_key')


def test_verified_copy_keeps_rollback_and_hashes(tmp_path):
    from app.product_migration import verified_copy, tree_manifest
    source, target, backup = (tmp_path / name for name in ('old', 'new', 'rollback'))
    source.mkdir()
    (source / 'private.pdf').write_bytes(b'controlled-private-fixture')
    result = verified_copy(source, target, archive=backup)
    assert result['hashes_match'] and result['files_before'] == result['files_after'] == 1
    assert tree_manifest(backup) == tree_manifest(target)
    with pytest.raises(ValueError):
        verified_copy(backup, target)


def test_existing_user_runtime_preserves_rows_preferences_and_history(tmp_path):
    from app.product_migration import default_runtime_root, database_inventory
    old = tmp_path / 'NYRA'
    (old / 'data').mkdir(parents=True)
    (old / 'config').mkdir()
    (old / 'identity').mkdir()
    with sqlite3.connect(old / 'data/nyra.db') as db:
        db.execute('CREATE TABLE memory (text TEXT)')
        db.execute('INSERT INTO memory VALUES (?)', ('NYRA was the historical name',))
    db.close()
    before = database_inventory(old / 'data/nyra.db')
    (old / 'config/runtime-settings.json').write_text(json.dumps({
        'wake_word': 'nyra', 'voice_id': 'keep-voice', 'tts_provider': 'gradium',
        'credential_id': 'gradium_api_key'}))
    (old / 'identity/system_prompt.md').write_text('Eu sou a NYRA. Preserve minha personalidade.')
    (old / 'credential.bin').write_bytes(b'encrypted-vault-fixture')
    root = default_runtime_root(tmp_path)
    assert root.name == 'Kazumi' and not old.exists()
    assert database_inventory(root / 'data/kazumi.db') == before
    with sqlite3.connect(root / 'data/kazumi.db') as db:
        assert db.execute('SELECT text FROM memory').fetchone()[0] == 'NYRA was the historical name'
    db.close()
    settings = json.loads((root / 'config/runtime-settings.json').read_text())
    assert settings == {'wake_word': 'kazumi', 'voice_id': 'keep-voice', 'tts_provider': 'gradium', 'credential_id': 'gradium_api_key'}
    assert (root / 'credential.bin').read_bytes() == b'encrypted-vault-fixture'
    assert 'Eu sou a Kazumi.' in (root / 'identity/system_prompt.md').read_text()
    assert default_runtime_root(tmp_path) == root


def test_conflicting_runtime_never_merges_or_discards(tmp_path):
    from app.product_migration import default_runtime_root
    (tmp_path / 'NYRA/data').mkdir(parents=True)
    (tmp_path / 'NYRA/data/nyra.db').write_bytes(b'original')
    (tmp_path / 'Kazumi').mkdir()
    with pytest.raises(RuntimeError, match='Both legacy'):
        default_runtime_root(tmp_path)
    assert (tmp_path / 'NYRA/data/nyra.db').read_bytes() == b'original'


def test_project_workspace_preference_survives_restart_and_mode_toggle(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.hardware_engine.service import HardwareEngineeringService
    monkeypatch.delenv('KAZUMI_PROJECTS_ROOT', raising=False)
    state=tmp_path/'state'; state.mkdir()
    chosen=tmp_path/'migrated-projects'
    (state/'settings.json').write_text(json.dumps({'full':False,'project_root':str(chosen)}))
    services=SimpleNamespace(usb=None,world_state=None,shell=None)
    engine=HardwareEngineeringService(services,state)
    assert engine.projects.root==chosen
    engine.configure(False)
    assert HardwareEngineeringService(services,state).projects.root==chosen
    monkeypatch.setenv('KAZUMI_PROJECTS_ROOT',str(tmp_path/'override'))
    assert HardwareEngineeringService(services,state).projects.root==tmp_path/'override'


def test_voice_profile_nominal_migration_keeps_voice_choice_and_legacy_license():
    from app.voice_hunter.models import CandidateStatus
    profile = {'profile_id':'NYRA_VOICE_AVA_V1','voice':'operator-custom-voice','provider':'gradium'}
    result = preferences(profile)
    assert result == {**profile, 'profile_id':'KAZUMI_VOICE_AVA_V1'}
    assert CandidateStatus('SAFE_FOR_NYRA_REFERENCE') is CandidateStatus.SAFE_FOR_KAZUMI_REFERENCE


def test_persisted_pronunciation_migrates_old_spoken_form_without_changing_custom_terms(tmp_path, monkeypatch):
    from app.brand_compat import pronunciation_document
    from app.speech.pronunciation import lexicon
    from app.speech.pronunciation.engine import PronunciationEngine
    old = {'rules': [
        {'canonical':'Kazumi','aliases':['NYRA'],'spoken_form':'Naira','provider_overrides':{'gradium':'Naira'}},
        {'canonical':'Proxmox','spoken_form':'my custom pronunciation'},
    ]}
    value = pronunciation_document(old)
    assert value['rules'][0]['spoken_form'] == 'Kazumi'
    assert value['rules'][0]['provider_overrides']['gradium'] == 'Kazumi'
    assert value['rules'][1] == old['rules'][1]
    assert old['rules'][0]['spoken_form'] == 'Naira'
    assert pronunciation_document(value) == value
    path = tmp_path / 'defaults.json'
    path.write_text(json.dumps(old), encoding='utf8')
    for name in ('DEFAULT_PATH', 'PACKAGED_DEFAULT_PATH'):
        monkeypatch.setattr(lexicon, name, path)
    for name in ('LEGACY_PATH', 'PACKAGED_LEGACY_PATH', 'OVERRIDE_PATH'):
        monkeypatch.setattr(lexicon, name, tmp_path / 'absent.json')
    result = PronunciationEngine().prepare_for_speech('Oi, eu sou a Kazumi.', provider='gradium')
    assert result.speech_text == 'Oi, eu sou a Kazumi.'
