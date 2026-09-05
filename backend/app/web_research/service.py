import asyncio
import hashlib
import time
import json
import re
from urllib.parse import urlsplit
from pathlib import Path

from .cache import ResearchCache
from .extract import extract, excerpts, DocumentParser
from .fetch import PublicFetcher, ResearchError, public_url
from .models import ResearchRequest, Source
from .search import DuckDuckGoSearch, BingRssSearch, public_query
from .sources import source_type, rank
from .planner import technical_query, relevant, topic_terms, specific_score, answers_question, refinement, focus_terms, documentation_portals, explicit_urls, freshness_required
from .diagnostics import record, failure, trace, connectivity


def relevance(row, query):
    import re
    terms = set(re.findall(r'[\w-]{4,}', query.casefold()))
    target = (row['url'] + ' ' + row['title']).casefold()
    return rank(row['url']) + specific_score(row, query) + sum(6 for term in terms if term in target)


class WebResearchService:
    def __init__(self, cache_root: Path, *, fetcher=None, providers=None, provider=None):
        self.fetcher = fetcher or PublicFetcher()
        self.providers = providers or [DuckDuckGoSearch(), BingRssSearch()]
        self.cache = ResearchCache(cache_root)
        self._slots = asyncio.Semaphore(2)
        self.last = {}
        self.provider = provider
        self.catalog = []

    async def search(self, query: str):
        query = technical_query(public_query(query))
        errors = []
        for provider in self.providers:
            record('fallback_selected' if errors else 'provider_selected', provider=provider.name, query=query)
            try:
                raw = await provider.search(self.fetcher, query)
                rows = [row for row in raw if relevant(row, query)]
                record('results_received', provider=provider.name, query=query,
                       parsed_count=len(raw), result_count=len(rows), ranking_discarded=len(raw)-len(rows))
                if rows:
                    unique = {row['url']: row for row in rows}
                    return {'success': True, 'provider': provider.name,
                            'query': query, 'results': sorted(unique.values(), key=lambda r: -relevance(r, query)), 'errors': errors}
                error = {'provider': provider.name, 'error_code': 'NO_RELEVANT_RESULTS' if raw else 'EMPTY_OR_CHALLENGED'}
            except Exception as error:
                error = {'provider': provider.name, **failure(error)}
                errors.append(error)
                record('provider_failed', **error)
                continue
            errors.append(error)
            record('provider_failed', **error)
        return {'success': False, 'query': query, 'results': [], 'errors': errors, 'error_code': 'SEARCH_PROVIDER_DEGRADED'}

    async def document(self, url: str, *, fresh=False, query='') -> Source:
        url = public_url(url)
        parts = urlsplit(url)
        if parts.hostname == 'github.com' and '/blob/' in parts.path and source_type(url) == 'official_repository':
            url = 'https://raw.githubusercontent.com' + parts.path.replace('/blob/', '/', 1)
        cached = self.cache.get(url, ttl=0 if fresh else 86400)
        if cached:
            cached.facts = excerpts(cached.text, query)
            return cached
        try:
            final_url, data, mime = await self.fetcher.fetch(url)
            title, text = await asyncio.to_thread(extract, data, mime)
            if not text.strip() or text.count('\ufffd') > max(10, len(text) // 100):
                raise ResearchError('EXTRACTION_EMPTY_OR_INVALID')
            if source_type(final_url) == 'official_registry':
                package = json.loads(data)
                info = package.get('info', {})
                title = str(info.get('name', 'Package')) + ' published release'
                text = json.dumps({'name': info.get('name'), 'version': info.get('version'),
                    'package_url': info.get('package_url'), 'requires_python': info.get('requires_python'),
                    'uploaded_at': [r.get('upload_time_iso_8601') for r in package.get('urls', [])]}, ensure_ascii=False)
            source = Source(url=final_url, title=title, source_type=source_type(final_url),
                            content_hash=hashlib.sha256(data).hexdigest(), text=text,
                            facts=excerpts(text, query), relevance=rank(final_url) / 100)
            if mime == 'text/html':
                from urllib.parse import urljoin
                parser = DocumentParser()
                parser.feed(data.decode('utf8', errors='replace'))
                for href, label in parser.links:
                    try:
                        linked = public_url(urljoin(final_url, href))
                    except (ResearchError, ValueError):
                        continue
                    if label.strip() and urlsplit(linked).hostname == urlsplit(final_url).hostname:
                        source.links.append({'url': linked, 'title': label.strip()[:150]})
                source.links = source.links[:300]
            try:
                self.cache.put(source)
            except OSError as error:
                # An unwritable cache must not erase a successfully fetched source.
                record('cache_write_failed', url=url, exception=type(error).__name__)
            return source
        except Exception:
            cached = self.cache.get(url, ttl=0, allow_stale=True)
            if cached:
                return cached
            raise

    async def research(self, request: ResearchRequest):
        entries = []
        token = trace.set(entries)
        try:
            public_query(request.query)
            record('web_research_started', query=request.query, freshness_required=request.fresh or freshness_required(request.query))
            result = await self._research(request)
            result['connectivity'] = connectivity(entries, result['search_success'], result['success'])
            result['freshness_required'] = request.fresh or freshness_required(request.query)
            result['diagnostics'] = entries
            record('research_complete', success=result['success'], source_count=len(result['sources']),
                   connectivity=result['connectivity'])
            self.last = result
            return result
        finally:
            trace.reset(token)

    async def _research(self, request: ResearchRequest):
        async with self._slots:
            started = time.perf_counter()
            suffix = {'research': '', 'official_docs': ' official documentation', 'datasheet': ' official datasheet pdf',
                      'repository': ' official github repository', 'library': ' official library license framework'}[request.kind]
            urls = [public_url(url.rstrip('.,;)')) for url in explicit_urls(request.query)[:3]]
            search = ({'success': False, 'results': [], 'errors': [], 'provider': None}
                      if urls else await self.search(request.query + suffix))
            documents, visited = [], set()
            queries = [technical_query(request.query + suffix)]
            queue = ([{'url': url, 'title': 'Requested document'} for url in urls] if urls else
                     list(search['results']) + documentation_portals(request.query))
            query_parts = set(re.findall(r'[a-z0-9]{2,}', request.query.casefold()))
            queue.extend(row for row in self.catalog if set(re.findall(r'[a-z0-9]{2,}', row['title'].casefold())).issubset(query_parts))
            refined = bool(urls)
            fetch_errors = []
            # Bounded best-first crawl follows actual official document links.
            # Refinement is performed once when search only yields general pages.
            while len(visited) < 8:
                queue = [r for r in queue if r['url'] not in visited]
                queue.sort(key=lambda r: -relevance(r, request.query))
                if (not queue or (len(visited) >= 2 and not any(answers_question(d, request.query) for d in documents))) and not refined:
                    query = refinement(request.query, search['results'])
                    queries.append(query)
                    refined = True
                    refined_search = await self.search(query)
                    search['errors'].extend(refined_search['errors'])
                    if refined_search['success']:
                        search['success'], search['provider'] = True, refined_search['provider']
                    queue.extend(refined_search['results'])
                    queue = [r for r in queue if r['url'] not in visited]
                    queue.sort(key=lambda r: -relevance(r, request.query))
                if not queue:
                    break
                row = queue.pop(0)
                visited.add(row['url'])
                record('source_selected', url=row['url'], title=row['title'][:250])
                try:
                    source = await self.document(row['url'], fresh=request.fresh, query=request.query)
                    if not source.title:
                        source.title = row['title']
                    if not any(d.url == source.url for d in documents):
                        documents.append(source)
                    record('extraction_pass', url=source.url, text_characters=len(source.text), cached=source.cached, stale=source.stale)
                    if source.source_type in ('manufacturer', 'official_framework', 'official_repository'):
                        links = sorted(source.links, key=lambda r: -relevance(r, request.query))
                        queue.extend(links[:8])
                    if answers_question(source, request.query):
                        break
                except Exception as error:
                    item = {'url': row['url'], **failure(error)}
                    fetch_errors.append(item)
                    record('source_failed', **item)
                    continue
            documents.sort(key=lambda d: -(relevance({'url': d.url, 'title': d.title}, request.query) + 200 * answers_question(d, request.query)))
            sources = [d.model_dump(exclude={'text', 'links'}) for d in documents[:request.limit]]
            specific = bool(documents and (urls or answers_question(documents[0], request.query)))
            return {'success': bool(sources), 'sources': sources, 'search_provider': search.get('provider'),
                         'search_success': search['success'], 'query': request.query,
                         'queries': queries if not urls else [], 'refined': refined and not urls, 'specific_answer': specific,
                         'search_errors': search['errors'], 'fetch_errors': fetch_errors, 'direct_urls': urls,
                         'elapsed_ms': round((time.perf_counter()-started)*1000, 2),
                         'error_code': None if sources else 'RESEARCH_UNAVAILABLE'}

    async def answer(self, query):
        result = await self.research(ResearchRequest(query=query))
        return await self.present(query, result)

    async def present(self, query, result):
        from .answer import grounded_answer
        if not result.get('specific_answer'):
            return research_reply(result)
        try:
            documents = [await self.document(s['url'], query=query) for s in result['sources'][:2]]
            response = await grounded_answer(self.provider, query, documents)
        except Exception as error:
            record('answer_presentation_failed', **failure(error))
            response = None
        return response or research_reply(result)

    async def close(self):
        await self.fetcher.close()


def research_reply(result):
    if not result.get('success'):
        state = result.get('connectivity')
        if state == 'TLS_ERROR':
            return 'Não consegui validar a conexão segura com as fontes agora. Não vou ignorar a validação do certificado.'
        if state == 'DNS_ERROR':
            return 'Não consegui resolver o endereço das fontes agora. Não tenho evidência suficiente para confirmar o resultado.'
        if state == 'TIMEOUT':
            return 'As fontes consultadas demoraram demais para responder. Não consegui confirmar o resultado agora.'
        return 'A busca está indisponível ou não retornou fontes úteis para esse assunto agora. Isso não comprova falta de Internet; não vou inventar o resultado.'
    lines = ['Encontrei estas fontes para a pesquisa:']
    for source in result['sources'][:3]:
        stale = ' (cache antigo; precisa de revalidação)' if source.get('stale') else ''
        # Quote at most 25 words per source; source excerpts never become commands.
        facts = [f for f in source.get('facts', []) if f.strip().casefold() != source.get('title', '').strip().casefold()
                 and not any(word in f.casefold() for word in ('copyright', 'all rights reserved', 'toggle navigation'))]
        if re.search(r'(?i)\b(?:vers[aã]o|version|release)\b', result.get('query', '')):
            # If local paraphrase validation fails, still show the retrieved
            # version-bearing evidence, not an unrelated download/navigation label.
            facts.sort(key=lambda fact: not bool(re.search(r'\b\d+\.\d+\.\d+\b', fact)))
        quote = ' '.join((facts or [''])[0].split()[:25])
        lines.append(f"[{source['title'] or 'Documentação'}]({source['url']}){stale}" + (f' — “{quote}”' if quote else ''))
    return '\n\n'.join(lines)
