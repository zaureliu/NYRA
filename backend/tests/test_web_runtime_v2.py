import gzip
import socket
import ssl
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.turn import TurnContext
from app.tools.models import ToolResult, RiskLevel
from app.web_research.conversation import WebConversationBridge
from app.web_research.diagnostics import connectivity, failure, record, trace
from app.web_research.fetch import decode_content, ResearchError
from app.web_research.models import ResearchRequest
from app.web_research.planner import natural_research_request, standalone_research_request, freshness_required
from app.web_research.service import WebResearchService, research_reply


@pytest.mark.parametrize('encoding,encode', [('gzip', gzip.compress), ('deflate', zlib.compress)])
def test_content_encoding_is_decoded_before_extraction(encoding, encode):
    document = b'<title>Python downloads</title><p>Latest stable release 3.99.1 TEST FIXTURE</p>'
    assert decode_content(encode(document), encoding, 1024) == document
    with pytest.raises(ResearchError, match='DOCUMENT_TOO_LARGE'):
        decode_content(encode(b'x' * 10000), encoding, 100)
    with pytest.raises(ResearchError, match='INVALID_COMPRESSED_DOCUMENT'):
        decode_content(encode(document)[:-3], encoding, 1024)


def test_unknown_encoding_is_controlled_not_binary_text():
    with pytest.raises(ResearchError, match='UNSUPPORTED_CONTENT_ENCODING'):
        decode_content(b'not plain text', 'br', 1024)


@pytest.mark.parametrize('url', ['https://localhost/', 'https://private.internal/',
    'https://user:secret@example.com/', 'http://example.com/', 'https://example.com/?signature=private'])
def test_conversational_urls_cannot_bypass_public_fetch_policy(url):
    from app.web_research.search import public_query
    with pytest.raises(ResearchError):
        public_query('pesquisa na documentação ' + url)


@pytest.mark.parametrize('error,expected', [(ssl.SSLCertVerificationError(), 'TLS_ERROR'),
    (socket.gaierror(), 'DNS_ERROR'), (TimeoutError(), 'TIMEOUT'), (ResearchError('HTTP_202'), 'HTTP_202')])
def test_error_taxonomy_preserves_evidence_without_exception_secrets(error, expected):
    assert failure(error)['error_code'] == expected
    assert 'secret-value' not in str(failure(RuntimeError('secret-value')))


def test_provider_error_is_never_offline_and_https_success_survives_parser_error():
    events = [{'http_status': 202, 'error_code': 'HTTP_202'}, {'http_status': 200}, {'error_code': 'PARSER_ERROR'}]
    assert connectivity(events, True, True) == 'SEARCH_PROVIDER_DEGRADED'
    assert connectivity(events, False, True) == 'DIRECT_FETCH_ONLY'
    assert connectivity(events, False, False) == 'SEARCH_PROVIDER_DEGRADED'
    assert connectivity([{'error_code': 'TLS_ERROR'}], False, False) == 'TLS_ERROR'


class FailedSearch:
    name = 'CONTROLLED_FAILURE'
    async def search(self, fetcher, query):
        raise ResearchError('HTTP_202')


class SuccessSearch:
    name = 'CONTROLLED_FALLBACK'
    async def search(self, fetcher, query):
        return [{'url': 'https://docs.example.com/example-sdk', 'title': 'Example SDK documentation'}]


class Pages:
    async def fetch(self, url):
        record('fetch_pass', url=url, http_status=200)
        return url, b'<title>Example SDK documentation</title><p>Official controlled test content, never live Internet evidence.</p>', 'text/html'
    async def close(self): pass


@pytest.mark.asyncio
async def test_search_fallback_errors_and_result_bridge_provenance(tmp_path):
    engine = WebResearchService(tmp_path, fetcher=Pages(), providers=[FailedSearch(), SuccessSearch()])
    result = await engine.research(ResearchRequest(query='Example SDK documentation'))
    assert result['success'] and result['search_provider'] == 'CONTROLLED_FALLBACK'
    assert result['search_errors'][0]['http_status'] == 202
    assert result['connectivity'] == 'SEARCH_PROVIDER_DEGRADED'
    assert result['sources'][0]['retrieved_at'] and result['sources'][0]['content_hash']
    assert any(row['event'] == 'fallback_selected' for row in result['diagnostics'])


@pytest.mark.asyncio
async def test_explicit_url_bypasses_broken_search_and_keeps_internet_evidence(tmp_path):
    provider = FailedSearch()
    provider.search = AsyncMock(side_effect=AssertionError('Direct URL must not search'))
    engine = WebResearchService(tmp_path, fetcher=Pages(), providers=[provider])
    result = await engine.research(ResearchRequest(query='Confira https://docs.example.com/example-sdk'))
    assert result['success'] and result['connectivity'] == 'DIRECT_FETCH_ONLY'
    assert result['queries'] == []
    provider.search.assert_not_called()
    assert 'indisponível' not in research_reply(result)


@pytest.mark.asyncio
async def test_cache_failure_cannot_discard_downloaded_source(tmp_path, monkeypatch):
    engine = WebResearchService(tmp_path, fetcher=Pages(), providers=[SuccessSearch()])
    def denied(source): raise PermissionError('fixture')
    monkeypatch.setattr(engine.cache, 'put', denied)
    result = await engine.research(ResearchRequest(query='Example SDK documentation'))
    assert result['success']
    assert any(row['event'] == 'cache_write_failed' for row in result['diagnostics'])


@pytest.mark.asyncio
async def test_fetch_parser_failure_is_contained_not_false_source(tmp_path):
    fetcher = Pages()
    async def invalid(url):
        record('fetch_pass', url=url, http_status=200)
        return url, b'\xff' * 1000, 'text/html'
    fetcher.fetch = invalid
    engine = WebResearchService(tmp_path, fetcher=fetcher, providers=[SuccessSearch()])
    result = await engine.research(ResearchRequest(query='Example SDK documentation'))
    assert not result['success'] and result['fetch_errors'][0]['error_code'] == 'EXTRACTION_EMPTY_OR_INVALID'
    assert result['connectivity'] == 'ONLINE'
    assert 'Internet ou' not in research_reply(result)


@pytest.mark.parametrize('query', ['Pesquise na internet qual é a versão atual do PlatformIO.',
    'Procure na documentação oficial como funciona o comando pio run.',
    'Qual é a versão atual do Python?', 'confirma isso online', 'vê na documentação',
    'pesquisa isso na internet', 'e pra que serve o pio run?', 'qual o preço atual desse SDK?'])
def test_natural_and_fresh_web_routing(query):
    assert natural_research_request(query)
    assert standalone_research_request(query)


@pytest.mark.parametrize('query', ['conectei um ESP32 na USB, pesquisa o pinout dessa placa',
    'procura o arquivo no meu computador', 'abre o Spotify', 'hoje estou cansado'])
def test_web_does_not_steal_hardware_filesystem_or_casual_turn(query):
    assert not standalone_research_request(query)


def test_freshness_is_not_limited_to_hardcoded_brand():
    assert freshness_required('qual a versão atual do FooSDK?')
    assert freshness_required('confirma a disponibilidade desse produto')


@pytest.mark.asyncio
async def test_multi_turn_bridge_same_session_and_actual_tool_result():
    data = {'success': True, 'specific_answer': True, 'sources': [{'url': 'https://docs.example.com'}]}
    tools = SimpleNamespace(execute=AsyncMock(return_value=ToolResult(tool='web_research', risk=RiskLevel.READ_ONLY, ok=True, data=data, elapsed_ms=1)))
    research = SimpleNamespace(present=AsyncMock(return_value='Grounded test response.'))
    bridge = WebConversationBridge(research, tools)
    first = TurnContext('versão atual do PlatformIO', conversation_id='session-one')
    assert await bridge.reply(first.user_input, first) == 'Grounded test response.'
    second = TurnContext('pesquisa isso na internet', conversation_id='session-one')
    assert await bridge.reply(second.user_input, second) == 'Grounded test response.'
    assert 'PlatformIO' in tools.execute.call_args.args[1]['query']
    assert second.tool_results[0]['data']['sources'] == data['sources']
    other = TurnContext('pesquisa isso na internet', conversation_id='different-session')
    assert 'O que' in await bridge.reply(other.user_input, other)
    assert tools.execute.await_count == 2


def test_web_domain_offers_only_web_schemas_not_shell():
    from app.tools.registry import ToolRegistry, ToolDefinition, classify_domain
    from app.hardware_engine.tools import Query
    registry = ToolRegistry()
    for name in ('web_search', 'web_fetch', 'web_research', 'system_shell'):
        registry.register(ToolDefinition(name, 'test', RiskLevel.READ_ONLY, Query, AsyncMock()))
    domain = classify_domain('Qual é a versão atual do Python?')
    assert domain == 'WEB_RESEARCH'
    assert {s['function']['name'] for s in registry.llm_tools(domain)} == {'web_search', 'web_fetch', 'web_research'}


def test_diagnostics_excludes_url_query_and_headers(caplog):
    entries = []
    token = trace.set(entries)
    try:
        record('fetch_started', url='https://example.com/docs?opaque=private')
    finally:
        trace.reset(token)
    assert entries[0]['url'] == 'https://example.com/docs'


def test_version_fallback_answers_with_retrieved_number_not_navigation():
    response = research_reply({'success': True, 'query': 'Qual é a versão atual do Python?', 'sources': [
        {'url': 'https://www.python.org/downloads/', 'title': 'Download Python', 'facts': [
            'Download Python install manager', 'Or get the standalone installer for Python 3.99.1 TEST FIXTURE']} ]})
    assert '3.99.1' in response and 'install manager' not in response
