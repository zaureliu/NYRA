"""Reviewable source-only dependencies, never downloaded installers/build hooks."""
import hashlib
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.tools.redaction import redact_secrets
from app.web_research.sources import source_type
from .models import HardwareError, now


class LibraryFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str
    relative: str


class LibraryImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{1,40}$')
    version: str = Field(min_length=1, max_length=80)
    license_url: str
    compatibility_url: str
    compatibility_support: str = Field(min_length=10, max_length=1000)
    files: list[LibraryFile] = Field(min_length=1, max_length=24)


async def review_import(request, research):
    urls = [request.license_url, request.compatibility_url] + [f.url for f in request.files]
    if any(source_type(url) != 'official_repository' for url in urls):
        raise HardwareError('DEPENDENCY_SOURCE_UNTRUSTED')
    owners = {tuple(urlsplit(url).path.strip('/').split('/')[:2]) for url in urls}
    if len(owners) != 1:
        raise HardwareError('DEPENDENCY_REPOSITORY_MISMATCH')
    license_doc = await research.document(request.license_url)
    license_text = license_doc.text
    if not any(marker in license_text for marker in ('Permission is hereby granted, free of charge', 'Apache License', 'Redistribution and use in source and binary forms')):
        raise HardwareError('DEPENDENCY_LICENSE_REVIEW_REQUIRED')
    compatibility = await research.document(request.compatibility_url)
    if ' '.join(request.compatibility_support.split()) not in ' '.join(compatibility.text.split()):
        raise HardwareError('DEPENDENCY_COMPATIBILITY_UNPROVEN')
    files = {}
    hashes = {}
    for item in request.files:
        if not re.fullmatch(r'(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:c|cpp|h|hpp)', item.relative):
            raise HardwareError('DEPENDENCY_SOURCE_ONLY')
        source = await research.document(item.url)
        if source.stale or len(source.text) > 100000 or redact_secrets(source.text) != source.text or re.search(r'\.incbin|#\s*(?:include|embed)\s*[<"](?:[A-Za-z]:|/|\.\.)', source.text):
            raise HardwareError('DEPENDENCY_CONTENT_REJECTED')
        path = f'src/{request.name}/{item.relative}'
        files[path] = source.text
        hashes[path] = hashlib.sha256(source.text.encode()).hexdigest()
    files[f'src/{request.name}/LICENSE.txt'] = license_text
    return files, {'name': request.name, 'version': request.version, 'source_urls': urls,
                   'source_hashes': hashes, 'license_url': request.license_url,
                   'compatibility_source': request.compatibility_url, 'retrieved_at': now(), 'install_mode': 'reviewed_static_sources'}
