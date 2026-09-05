"""One-release compatibility for the NYRA -> Kazumi product migration.

Only identifiers, known paths and the old default wake word are translated.
Conversation text, model choices, voice IDs and secret values are never rewritten.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, MutableMapping

LEGACY_PREFIX = 'NYRA_'
PREFIX = 'KAZUMI_'


def environment_aliases(env: MutableMapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    for name, value in list(env.items()):
        if name.startswith(LEGACY_PREFIX):
            env.setdefault(PREFIX + name[len(LEGACY_PREFIX):], value)


def migrated_path(value: str) -> str:
    # Old public defaults were relative to the clone. Preserve the relative
    # layout; do not expand or rewrite arbitrary user-selected directories.
    for old, new in (('Nyra-Auto-Code', 'Kazumi-Auto-Code'),
                     ('NYRA-GitHub-Public', 'Kazumi-GitHub-Public')):
        for separator in ('/', '\\'):
            prefix = '..' + separator + old
            if value.casefold() == prefix.casefold() or value.casefold().startswith(prefix.casefold() + separator):
                return '..' + separator + new + value[len(prefix):]
    mappings = (
        (r'E:\NYRA-Projects', r'E:\Kazumi-Projects'),
        (r'E:\NYRA-Knowledge', r'E:\Kazumi-Knowledge'),
        (r'E:\Nyra-Auto-Code', r'E:\Kazumi-Auto-Code'),
        (r'E:\NYRA-GitHub-Public', r'E:\Kazumi-GitHub-Public'),
        (r'E:\nyra', r'E:\Kazumi'),
    )
    for before, after in mappings:
        for source, target in ((before, after), (before.replace('\\', '/'), after.replace('\\', '/'))):
            if value.casefold() == source.casefold() or value.casefold().startswith(source.casefold() + source[2]):
                return target + value[len(source):]
    # Runtime default location only, not arbitrary user prose or file contents.
    return re.sub(r'(?i)([\\/]AppData[\\/](?:Local|Roaming)[\\/])NYRA(?=[\\/]|$)',
                  lambda match: match[1] + 'Kazumi', value)


def preferences(value: Any, key: str = '') -> Any:
    if isinstance(value, dict):
        return {name: preferences(item, name) for name, item in value.items()}
    if key == 'keyterms' and isinstance(value, list):
        return list(dict.fromkeys('Kazumi' if str(item).casefold() == 'nyra' else item for item in value))
    if isinstance(value, list):
        return [preferences(item, key) for item in value]
    if isinstance(value, str):
        if key in {'profile_id', 'identity_id'} and value in {'NYRA_VOICE', 'NYRA_VOICE_AVA_V1'}:
            return value.replace('NYRA_', 'KAZUMI_', 1)
        if key == 'wake_word' and value.strip().casefold() == 'nyra':
            return 'kazumi'
        if any(part in key.casefold() for part in ('path', 'root', 'workspace', 'directory', 'file')):
            result = migrated_path(value)
            if key == 'database_path' and re.search(r'(?i)(?:^|[\\/])data[\\/]nyra\.db$', result):
                result = re.sub(r'(?i)nyra\.db$', 'kazumi.db', result)
            return result
    return value


def pronunciation_document(document: dict) -> dict:
    """Migrate only the product's old pronunciation, including partial upgrades."""
    from copy import deepcopy
    result = deepcopy(document)
    if result.get('profile') == 'NYRA_VOICE':
        result['profile'] = 'KAZUMI_VOICE'
    def nominal(term):
        return ('KAZUMI' if term.isupper() else 'Kazumi') if isinstance(term, str) and term.casefold() == 'nyra' else term
    for rule in result.get('rules', []):
        if not isinstance(rule, dict) or str(rule.get('canonical', '')).casefold() not in {'nyra', 'kazumi'}:
            continue
        rule['canonical'] = 'Kazumi'
        rule['aliases'] = list(dict.fromkeys(nominal(alias) for alias in rule.get('aliases', [])))
        if str(rule.get('spoken_form', '')).casefold() in {'naira', 'nyra'}:
            rule['spoken_form'] = 'Kazumi'
        rule['provider_overrides'] = {
            provider: 'Kazumi' if str(value).casefold() in {'naira', 'nyra'} else value
            for provider, value in rule.get('provider_overrides', {}).items()
        }
    if isinstance(result.get('terms'), dict):
        result['terms'] = {
            nominal(term): 'Kazumi' if term.casefold() in {'nyra', 'kazumi'} and str(value).casefold() in {'naira', 'nyra'} else value
            for term, value in result['terms'].items()
        }
    return result
