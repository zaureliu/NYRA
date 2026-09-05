"""Bounded, local-only typed proposals. This helper never executes model text."""
import asyncio
import json
import httpx
from pydantic import ValidationError
from urllib.parse import urlsplit

from .base import LLMMessage


async def local_proposal(provider, schema, instruction, context, *, timeout=180):
    if provider is None or provider.name != 'ollama' or urlsplit(provider.base_url).hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('LOCAL_ENGINEERING_MODEL_REQUIRED')
    messages = [LLMMessage(role='system', content=instruction + '\nReturn ONLY JSON matching this schema. No reasoning or markdown.\n' + json.dumps(schema.model_json_schema())),
                LLMMessage(role='user', content=json.dumps(context, ensure_ascii=False))]
    for attempt in range(2):
        try:
            raw = await asyncio.wait_for(provider.structured(messages, schema.model_json_schema()), timeout)
        except httpx.HTTPError as error:
            raise ValueError('LOCAL_PROPOSAL_TRANSPORT_UNAVAILABLE') from error
        if len(raw) > 150000:
            raise ValueError('PROPOSAL_TOO_LARGE')
        raw = raw.strip()
        if raw.startswith('```') and raw.endswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            return schema.model_validate_json(raw)
        except ValidationError as error:
            if attempt:
                raise ValueError('PROPOSAL_SCHEMA_REJECTED') from error
            # One format/schema correction only. No rejected proposal is applied.
            messages.extend([LLMMessage(role='assistant', content=raw), LLMMessage(role='user', content=
                'Correct ONLY this JSON to satisfy the schema. Escape code newlines/quotes correctly. Validation errors: ' +
                str(error.errors(include_input=False, include_url=False))[:1500])])
