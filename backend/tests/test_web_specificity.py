from unittest.mock import AsyncMock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.web_research.models import Source, ResearchRequest
from app.web_research.planner import specific_score, answers_question, natural_research_request
from app.web_research.service import WebResearchService
from app.web_research.answer import grounded_answer
from app.llm.structured import local_proposal
from app.hardware_engine.engineering import EngineeringPlan


@pytest.mark.parametrize('query', ['como funciona o comando pio run?', 'procura como usa o pio run',
    'qual a versão atual do PlatformIO?', 'procura o pinout oficial do ESP32-S3 DevKitC'])
def test_natural_research_detection(query):
    assert natural_research_request(query)


def test_specific_command_page_outranks_general_official_page_and_remote_command():
    query = 'como funciona o comando pio run?'
    specific = {'title': 'pio run — PlatformIO documentation', 'url': 'https://docs.platformio.org/en/latest/core/userguide/cmd_run.html'}
    general = {'title': 'What is PlatformIO?', 'url': 'https://docs.platformio.org/en/latest/what-is-platformio.html'}
    remote = {'title': 'pio remote run', 'url': 'https://docs.platformio.org/en/latest/core/userguide/remote/cmd_run.html'}
    assert specific_score(specific, query) > specific_score(general, query)
    assert specific_score(specific, query) > specific_score(remote, query)
    assert answers_question(Source(**specific), query)
    assert not answers_question(Source(**general, text='Sidebar: pio run'), query)
    assert not answers_question(Source(**remote), query)


def test_current_version_requires_version_document_and_freshness():
    current = Source(url='https://docs.platformio.org/core/history.html', title='Release notes', text='6.1.19 released on a controlled date')
    assert answers_question(current, 'qual a versão atual do PlatformIO?')
    assert not answers_question(current.model_copy(update={'stale': True}), 'qual a versão atual do PlatformIO?')


class RefineSearch:
    name = 'SIMULATED_SEARCH'
    def __init__(self): self.queries = []
    async def search(self, fetcher, query):
        self.queries.append(query)
        if 'reference' in query:
            return [{'url': 'https://docs.example.com/foosdk/widget.html', 'title': 'FooSDK widget API reference'}]
        return [{'url': 'https://docs.example.com/about.html', 'title': 'What is FooSDK?'},
                {'url': 'https://docs.example.com/welcome.html', 'title': 'Welcome to FooSDK'}]


class Pages:
    async def fetch(self, url):
        title = 'FooSDK widget API reference' if 'widget' in url else 'What is FooSDK?'
        return url, f'<title>{title}</title><p>Controlled documentation fixture: widget accepts a value parameter.</p>'.encode(), 'text/html'
    async def close(self): pass


@pytest.mark.asyncio
async def test_generic_results_trigger_one_bounded_refinement(tmp_path):
    provider = RefineSearch()
    engine = WebResearchService(tmp_path, fetcher=Pages(), providers=[provider])
    result = await engine.research(ResearchRequest(query='FooSDK widget API'))
    assert result['refined'] and len(provider.queries) == 2
    assert result['specific_answer'] and result['sources'][0]['url'].endswith('/widget.html')


@pytest.mark.asyncio
async def test_grounded_natural_answer_requires_exact_support_and_rejects_numbers():
    source = Source(url='https://docs.platformio.org/pio-run', title='pio run', text='Run project targets over environments declared in platformio.ini.')
    provider = SimpleNamespace(name='ollama', base_url='http://127.0.0.1:11434', structured=AsyncMock(return_value='{"statements":[{"text":"O comando processa os ambientes definidos no platformio.ini.","url":"https://docs.platformio.org/pio-run","support":"Run project targets over environments declared in platformio.ini."}]}'))
    result = await grounded_answer(provider, 'como funciona pio run?', [source])
    assert result and 'processa' in result and '[pio run]' in result
    provider.structured.return_value = '{"statements":[{"text":"A versão atual é 9.9.9.","url":"https://docs.platformio.org/pio-run","support":"Run project targets over environments declared in platformio.ini."}]}'
    assert await grounded_answer(provider, 'versão?', [source]) is None


def test_typographic_quotes_and_double_escaped_unicode_are_not_factual_changes():
    from app.web_research.answer import normalized, presentation_text
    assert normalized('“platformio.ini”') == normalized('\\"platformio.ini\\"')
    assert presentation_text('configura\\u00e7\\u00e3o') == 'configuração'


@pytest.mark.asyncio
async def test_brain_manager_preserves_its_model_route_for_typed_proposals():
    from app.brain.manager import BrainManager
    brain = object.__new__(BrainManager)
    brain._route_model = AsyncMock(return_value=('selected-local', 'fallback-local'))
    called = []
    async def structured(messages, schema): return '{"valid":true}'
    def provider(model):
        called.append(model)
        return SimpleNamespace(structured=structured)
    brain._provider = provider
    assert await brain.structured([], {'title': 'test'}) == '{"valid":true}'
    assert called == ['selected-local']


@pytest.mark.asyncio
async def test_local_proposals_do_not_send_project_source_to_cloud():
    provider = SimpleNamespace(name='ollama', base_url='https://cloud.example', structured=AsyncMock())
    with pytest.raises(ValueError, match='LOCAL_ENGINEERING_MODEL_REQUIRED'):
        await local_proposal(provider, EngineeringPlan, 'propose changes', {'private_source': 'code'})
    provider.structured.assert_not_called()
