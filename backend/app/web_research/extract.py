from html.parser import HTMLParser
from io import BytesIO
import re


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.title = [], [], ''
        self.hidden = 0
        self.in_title = False
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'a':
            self.anchor = [values.get('href', ''), '']

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self.anchor:
            self.links.append(tuple(self.anchor))
            self.anchor = None
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title += data
            if self.anchor:
                self.anchor[1] += data


def extract(data: bytes, mime: str) -> tuple[str, str]:
    if mime == 'application/pdf':
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages[:80])
        return str((reader.metadata or {}).get('/Title', 'PDF datasheet'))[:250], text[:120000]
    decoded = data.decode('utf-8', errors='replace')
    if mime == 'text/html':
        parser = DocumentParser()
        parser.feed(decoded)
        return parser.title.strip()[:250], re.sub(r'[ \t]+', ' ', ''.join(parser.parts))[:120000]
    return '', decoded[:120000]


def excerpts(text: str, query: str) -> list[str]:
    words = set(re.findall(r'[\w-]{3,}', query.casefold()))
    lines = [' '.join(line.split()) for line in text.splitlines() if len(line.strip()) > 30]
    scored = sorted(enumerate(lines), key=lambda item: -sum(w in item[1].casefold() for w in words))
    # Short extracts, not invented technical specifications. Total quotation
    # budget is deliberately small; callers can inspect the cached document.
    return [line[:350] for _, line in scored[:3]]
