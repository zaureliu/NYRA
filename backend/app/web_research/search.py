"""Replaceable no-key search providers; never scrape the operator's browser."""
import re
from urllib.parse import urlencode, parse_qs, urlsplit, unquote
from xml.etree import ElementTree

from app.tools.redaction import redact_secrets
from .extract import DocumentParser
from .fetch import ResearchError, public_url


def public_query(query: str) -> str:
    if (redact_secrets(query) != query or re.search(r'(?i)\b[A-Z]:[\\/]|\\\\|\bCOM\d+\b|'
            r'\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b|'
            r'(?i:serial[_ ]?number|senha|password|token|api[_ -]?key)\s*[:=]', query)):
        raise ResearchError('PRIVATE_QUERY_REJECTED')
    # A URL supplied as conversational context must pass the SAME public URL
    # policy before it can be logged or sent to a search provider.
    for url in re.findall(r'https?://[^\s<>"\[\]]+', query, flags=re.I):
        public_url(url.rstrip('.,;)'))
    return query


class DuckDuckGoSearch:
    name = 'duckduckgo_html'

    async def search(self, fetcher, query):
        _, data, _ = await fetcher.fetch('https://html.duckduckgo.com/html/?' + urlencode({'q': query}))
        parser = DocumentParser()
        parser.feed(data.decode('utf8', errors='replace'))
        found = []
        for url, title in parser.links:
            if 'uddg=' in url:
                url = parse_qs(urlsplit(url).query).get('uddg', [''])[0]
            try:
                url = public_url(unquote(url))
            except (ValueError, ResearchError):
                continue
            if 'duckduckgo.com' not in urlsplit(url).netloc and title.strip():
                found.append({'url': url, 'title': title.strip()[:250]})
        return found[:15]


class BingRssSearch:
    name = 'bing_rss'

    async def search(self, fetcher, query):
        _, data, _ = await fetcher.fetch('https://www.bing.com/search?' + urlencode({'q': query, 'format': 'rss'}))
        root = ElementTree.fromstring(data)
        found = []
        for item in root.findall('.//item')[:15]:
            try:
                url = public_url(item.findtext('link', ''))
                found.append({'url': url, 'title': item.findtext('title', '')[:250]})
            except (ValueError, ResearchError):
                continue
        return found
