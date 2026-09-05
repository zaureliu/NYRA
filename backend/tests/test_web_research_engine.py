import hashlib
from pathlib import Path

import pytest

from app.web_research.cache import ResearchCache
from app.web_research.extract import extract
from app.web_research.fetch import ResearchError, public_url
from app.web_research.models import Source, ResearchRequest
from app.web_research.search import public_query
from app.web_research.service import WebResearchService, research_reply
from app.web_research.sources import rank, source_type


@pytest.mark.parametrize('url', ['http://example.com', 'https://127.0.0.1/', 'https://localhost/',
    'https://192.168.0.1/', 'https://[::1]/', 'https://user:password@example.com/',
    'https://example.com:8000/', 'file:///etc/passwd', 'https://example.com/?token=secret',
    'https://example.local/', 'https://example.com/\r\nHeader:bad'])
def test_public_fetch_rejects_private_or_credential_urls(url):
    with pytest.raises((ResearchError, ValueError)):
        public_url(url)


@pytest.mark.parametrize('query', ['serial number=private', 'Read E:\\Projects\\secret',
    'my 192.168.1.10 sensor', 'COM7 esp32', 'aa:bb:cc:dd:ee:ff', 'password=private'])
def test_search_does_not_exfiltrate_local_identifiers(query):
    with pytest.raises(ResearchError):
        public_query(query)


def test_official_ranking_is_hostname_and_org_aware():
    assert rank('https://docs.espressif.com/pinout') > rank('https://community.example/pinout')
    assert source_type('https://docs.espressif.com.evil.test/pinout') == 'community'
    assert source_type('https://github.com/stranger/espressif') == 'community'
    assert source_type('https://github.com/espressif/esptool') == 'official_repository'


def test_extract_drops_scripts_and_does_not_execute_document():
    title, text = extract(b'<title>Docs</title><script>malicious()</script><p>GPIO documented.</p>', 'text/html')
    assert title == 'Docs' and 'GPIO documented.' in text and 'malicious' not in text


def test_bounded_cache_and_stale_provenance(tmp_path):
    cache = ResearchCache(tmp_path, max_entries=2)
    for index in range(3):
        cache.put(Source(url=f'https://example.com/{index}', content_hash=str(index), text='documentation'))
    assert len(list(tmp_path.glob('*.json'))) == 2
    cached = cache.get('https://example.com/2', ttl=-1, allow_stale=True)
    assert cached.cached and cached.stale and cached.content_hash == '2'


class FetchFixture:
    def __init__(self): self.calls = 0; self.closed = False
    async def fetch(self, url):
        self.calls += 1
        return url, b'<title>Official</title><p>This is a controlled documentation fixture, not real Web validation.</p>', 'text/html'
    async def close(self): self.closed = True


class FailedSearch:
    name = 'failed'
    async def search(self, fetcher, query): raise OSError('offline')


class SearchFixture:
    name = 'SIMULATED_SEARCH'
    async def search(self, fetcher, query):
        return [{'url': 'https://docs.espressif.com/test', 'title': 'ESP32 Docs'}, {'url': 'https://community.example/test', 'title': 'ESP32 Community'}]


@pytest.mark.asyncio
async def test_provider_fallback_fetch_extract_hash_provenance_and_close(tmp_path):
    fetch = FetchFixture()
    engine = WebResearchService(tmp_path, fetcher=fetch, providers=[FailedSearch(), SearchFixture()])
    result = await engine.research(ResearchRequest(query='ESP32 pinout', limit=1))
    assert result['success'] and result['search_provider'] == 'SIMULATED_SEARCH'
    source = result['sources'][0]
    assert source['retrieved_at'] and source['source_type'] == 'manufacturer'
    assert source['content_hash'] == hashlib.sha256((await fetch.fetch(source['url']))[1]).hexdigest()
    assert 'https://docs.espressif.com/test' in research_reply(result)
    await engine.close()
    assert fetch.closed


@pytest.mark.asyncio
async def test_offline_is_not_fake_search_success(tmp_path):
    engine = WebResearchService(tmp_path, fetcher=FetchFixture(), providers=[FailedSearch()])
    result = await engine.research(ResearchRequest(query='ESP32 pinout'))
    assert not result['success'] and result['sources'] == []
    assert 'indisponível' in research_reply(result)


def test_natural_research_query_and_irrelevant_results():
    from app.web_research.planner import technical_query, relevant
    query = technical_query('Pesquise na Web a documentação atual do comando pio run do PlatformIO.')
    assert 'Pesquise' not in query and 'PlatformIO' in query and 'pio run' in query
    assert not relevant({'url': 'https://www.dicio.com.br/pesquise/', 'title': 'Pesquise'}, query)
    assert relevant({'url': 'https://docs.platformio.org/en/latest/core/userguide/cmd_run.html', 'title': 'pio run'}, query)
