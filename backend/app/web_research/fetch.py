"""Bounded public HTTPS fetch with DNS pinning, no browser profile or cookies."""
import asyncio
import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from urllib.parse import urljoin, urlsplit, urlunsplit
from .diagnostics import record, failure


class ResearchError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def decode_content(data: bytes, encoding: str, limit: int) -> bytes:
    encoding = encoding.strip().lower()
    if encoding in ('', 'identity'):
        return data
    if encoding not in ('gzip', 'deflate'):
        raise ResearchError('UNSUPPORTED_CONTENT_ENCODING')
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == 'gzip' else zlib.MAX_WBITS)
    try:
        output = decoder.decompress(data, limit + 1)
        if len(output) > limit or decoder.unconsumed_tail:
            raise ResearchError('DOCUMENT_TOO_LARGE')
        if not decoder.eof or decoder.unused_data:
            raise ResearchError('INVALID_COMPRESSED_DOCUMENT')
        return output
    except zlib.error:
        raise ResearchError('INVALID_COMPRESSED_DOCUMENT') from None


def public_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.port not in (None, 443) or len(url) > 2000
            or re.search(r'[\x00-\x20\\]', url)
            or re.search(r'(?i)(?:key|token|password|signature|secret)=', parts.query)):
        raise ResearchError('UNSAFE_RESEARCH_URL')
    host = parts.hostname.lower()
    if host == 'localhost' or host.endswith(('.local', '.internal', '.localhost')):
        raise ResearchError('PRIVATE_RESEARCH_URL')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ResearchError('PRIVATE_RESEARCH_URL')
    except ValueError:
        pass
    return urlunsplit(('https', parts.netloc, parts.path or '/', parts.query, ''))


class PublicFetcher:
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self):
        self._connections: set[http.client.HTTPSConnection] = set()
        self.closed = False

    async def fetch(self, url: str) -> tuple[str, bytes, str]:
        if self.closed:
            raise ResearchError('RESEARCH_STOPPED')
        url = public_url(url)
        record('fetch_started', url=url)
        try:
            return await asyncio.to_thread(self._fetch, url)
        except Exception as error:
            record('fetch_failed', url=url, **failure(error))
            raise

    def _fetch(self, url: str) -> tuple[str, bytes, str]:
        deadline = time.monotonic() + 30
        for _ in range(4):
            if self.closed or time.monotonic() >= deadline:
                raise ResearchError('RESEARCH_TIMEOUT')
            url = public_url(url)
            parts = urlsplit(url)
            addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                raise ResearchError('PRIVATE_RESEARCH_ADDRESS')
            record('dns_resolved', url=url, dns_state='PASS', address_count=len(addresses))
            # Connect to the address just checked, with TLS verification/SNI of
            # the original host. No second DNS lookup (rebinding) or env proxy.
            raw = socket.create_connection((addresses[0][4][0], 443), timeout=8)
            connection = http.client.HTTPSConnection(parts.hostname, timeout=8)
            try:
                connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname)
                record('tls_verified', url=url, tls_state='PASS')
                self._connections.add(connection)
                connection.request('GET', urlunsplit(('', '', parts.path or '/', parts.query, '')),
                                   headers={'User-Agent': 'NYRA-Technical-Research/1.0', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                record('http_response', url=url, http_status=response.status,
                       content_encoding=response.getheader('Content-Encoding', 'identity'))
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.getheader('Location', ''))
                    continue
                if response.status != 200:
                    raise ResearchError(f'HTTP_{response.status}')
                mime = response.getheader('Content-Type', '').split(';')[0].lower()
                if mime not in ('text/html', 'text/plain', 'application/json', 'application/pdf',
                                 'text/xml', 'application/xml', 'application/rss+xml', 'text/markdown'):
                    raise ResearchError('DOCUMENT_TYPE_NOT_ALLOWED')
                chunks, size = [], 0
                while not self.closed:
                    if time.monotonic() >= deadline:
                        raise ResearchError('RESEARCH_TIMEOUT')
                    chunk = response.read(65536)
                    if not chunk:
                        data = decode_content(b''.join(chunks), response.getheader('Content-Encoding', ''), self.MAX_BYTES)
                        record('fetch_pass', url=url, http_status=200, response_bytes=size,
                               decoded_bytes=len(data), content_type=mime)
                        return url, data, mime
                    size += len(chunk)
                    if size > self.MAX_BYTES:
                        raise ResearchError('DOCUMENT_TOO_LARGE')
                    chunks.append(chunk)
                raise ResearchError('RESEARCH_STOPPED')
            finally:
                self._connections.discard(connection)
                connection.close()
                raw.close()
        raise ResearchError('REDIRECT_LIMIT')

    async def close(self):
        self.closed = True
        for connection in tuple(self._connections):
            connection.close()
