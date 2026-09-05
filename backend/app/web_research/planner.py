"""Separate a natural research request from the actual public search terms."""
import re
import unicodedata


STOP = {'pesquisa', 'pesquise', 'pesquisar', 'procura', 'procure', 'buscar', 'busque', 'busca',
        'consulte', 'consultar', 'na', 'no', 'nas', 'nos', 'da', 'do', 'das', 'dos', 'a', 'o',
        'as', 'os', 'de', 'em', 'e', 'por', 'para', 'sobre', 'qual', 'quais', 'me', 'um', 'uma',
        'veja', 've', 'como', 'usar', 'usa', 'web', 'internet', 'comando', 'porfavor', 'funciona', 'funcionamento', 'oficial',
        'confirma', 'confirme', 'online', 'serve', 'pra', 'que', 'isso', 'isto', 'disso', 'disso?', 'ai', 'entao'}
TRANSLATE = {'documentacao': 'documentation', 'atual': 'current', 'atuais': 'current',
             'versao': 'version', 'biblioteca': 'library', 'repositorio': 'repository'}
GENERIC = {'official', 'documentation', 'docs', 'current', 'latest', 'version', 'release',
           'library', 'repository', 'datasheet', 'pdf', 'hardware', 'software', 'framework'}


def plain(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


def technical_query(text):
    site = re.search(r'\bsite:([a-z0-9.-]+)', text, re.I)
    text = re.sub(r'\bsite:[a-z0-9.-]+', '', text, flags=re.I)
    words = re.findall(r'[\w.+-]+', text)
    normalized = [TRANSLATE.get(plain(word), word).strip('.') for word in words if plain(word) not in STOP]
    # Some public search endpoints weight the leading words heavily. Put the
    # actual product/chip before translated research qualifiers.
    subjects = [word for word in normalized if plain(word) not in GENERIC]
    qualifiers = [word for word in normalized if plain(word) in GENERIC]
    if any(plain(w) == 'pio' for w in subjects) and not any(plain(w) == 'platformio' for w in subjects):
        subjects.insert(0, 'PlatformIO')
    subjects.sort(key=lambda word: not (plain(word) in {'platformio', 'arduino', 'espressif'} or re.match(r'(?i)(?:esp\d|stm32|nrf\d|rp2040)', word)))
    query = ' '.join(subjects + qualifiers) + (' site:' + site[1] if site else '')
    return query[:500]


def focus_terms(text):
    query = technical_query(text)
    command = re.search(r'\bpio\s+([a-z][\w-]*(?:\s+[a-z][\w-]*)?)', plain(query))
    if command:
        # Retain command order and short identifiers; brand-only matches are insufficient.
        return ['pio', command[1].split()[0]]
    words = list(dict.fromkeys(re.findall(r'[\w-]{3,}', plain(query))))
    return [w for w in words if w not in GENERIC | STOP | {'platformio', 'arduino', 'espressif'}] or words


def specific_score(row, query, *, content=''):
    focus = focus_terms(query)
    title, path = plain(row.get('title', '')), plain(row.get('url', '')).replace('_', ' ').replace('/', ' ')
    phrase = ' '.join(focus)
    score = sum(18 * (w in title) + 12 * (w in path) for w in focus)
    if phrase and phrase in title:
        score += 80
    if ' '.join(focus) == 'pio run' and 'remote' in title + path:
        score -= 100
    if any(generic in title for generic in ('what is ', 'homepage', 'welcome to ', 'gateway to ')):
        score -= 70
    if focus[:1] == ['pio'] and ('cli guide' in title or 'core userguide index' in path):
        score += 55
    if re.search(r'\b(?:version|versao|release)\b', plain(query)) and any(word in title + path for word in ('release', 'history', 'changelog')):
        score += 100
    if content:
        score += min(30, sum(5 for w in focus if w in plain(content)))
    return score


def answers_question(source, query):
    focus = focus_terms(query)
    heading = plain(source.title + ' ' + source.url).replace('_', ' ')
    if not focus:
        return False
    if re.search(r'\b(?:versao|version|release)\b', plain(query)):
        version_page = (source.source_type == 'official_framework' and '/downloads/' in source.url)
        return (not source.stale and (version_page or source.source_type == 'official_registry' or any(term in heading for term in ('release', 'history', 'changelog')))
                and bool(re.search(r'\b\d+\.\d+\.\d+\b', source.text)))
    # For a command/API question require the actual command in the document identity,
    # not a navigation sidebar mentioning it on an otherwise unrelated page.
    if focus[:1] == ['pio']:
        return focus[-1] in heading and ('pio' in plain(source.title) or 'cmd ' in heading) and 'remote' not in heading
    if 'pinout' in plain(query) or 'gpio' in plain(query):
        return bool(re.search(r'\b(?:gpio|io)\s*\d+\b', source.text, re.I)) and any(term in heading for term in focus if term not in ('pinout', 'gpio'))
    return sum(term in heading for term in focus) >= min(2, len(focus))


def refinement(query, rows):
    from urllib.parse import urlsplit
    official = next((r for r in rows if 'docs.' in r['url']), None)
    domain = urlsplit(official['url']).hostname if official else ''
    return technical_query(query) + (' site:' + domain if domain else ' official documentation') + ' reference'


def natural_research_request(text):
    value = plain(text)
    return bool(re.search(r'\b(?:pesquis\w*|procur\w*|busc\w*|consult\w*)\b', value) or
                freshness_required(text) or
                re.search(r'\b(?:confirma\w*|confir[ae]|ve|veja)\b.*(?:\b(?:documentacao|online|internet)\b|https://)', value) or
                re.search(r'\b(?:como funciona|como usa|pra que serve|para que serve)\b.*\b(?:pio|platformio|api|sdk|biblioteca)\b', value))


def freshness_required(text):
    value = plain(text)
    return bool(re.search(r'\b(?:versao (?:mais recente|atual|nova)|(?:ultima|nova) versao|release atual|preco atual|noticias?|disponibilidade|api atual|documentacao atual)\b', value))


def standalone_research_request(text):
    """Do not steal physical-device or filesystem intents from their owners."""
    value = plain(text)
    local_target = re.search(r'\b(?:essa placa|esse dispositivo|conect\w*|usb|serial|arquivo|pasta|no meu|do meu)\b', value)
    return natural_research_request(text) and not local_target


def explicit_urls(text):
    return re.findall(r'https://[^\s<>"\[\]]+', text)


def documentation_portals(query):
    # Product-level indexes, not per-question answers or invented search hits.
    # The requested page must still be discovered through actual links/fetches.
    if re.search(r'\b(?:platformio|pio)\b', plain(query)):
        if re.search(r'\b(?:versao|version|release)\b', plain(query)):
            return [{'url': 'https://pypi.org/pypi/platformio/json', 'title': 'PlatformIO current release package version'}]
        return [{'url': 'https://docs.platformio.org/en/latest/core/userguide/index.html', 'title': 'PlatformIO CLI Guide'}]
    if re.search(r'\barduino\b', plain(query)):
        return [{'url': 'https://docs.arduino.cc/language-reference/', 'title': 'Arduino Language Reference'}]
    if re.search(r'\bpython\b', plain(query)) and freshness_required(query):
        return [{'url': 'https://www.python.org/downloads/', 'title': 'Python current release downloads'}]
    return []


def topic_terms(query):
    return {w for w in re.findall(r'[\w-]{3,}', plain(query)) if w not in GENERIC | STOP}


def relevant(row, query):
    terms = topic_terms(query)
    haystack = plain(row.get('url', '') + ' ' + row.get('title', ''))
    # Brand/chip terms need actual overlap. A dictionary definition of the
    # imperative "pesquise" is not technical evidence for a PlatformIO query.
    return bool(terms) and any(term in haystack for term in terms)
