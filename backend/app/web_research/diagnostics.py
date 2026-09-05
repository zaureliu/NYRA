"""Bounded, request-local Web evidence. Never infer offline from a search error."""
from contextvars import ContextVar
import logging
import socket
import ssl
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger('kazumi.web_research')
trace: ContextVar[list | None] = ContextVar('web_research_trace', default=None)


def record(event, **fields):
    # Query strings may contain remote state; public search terms are logged
    # separately only AFTER public_query validation. Never log headers/bodies.
    if 'url' in fields:
        parts = urlsplit(fields['url'])
        fields['url'] = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    row = {'event': event, **fields}
    entries = trace.get()
    if entries is not None and len(entries) < 100:
        entries.append(row)
    logger.info(event, extra=fields)
    return row


def failure(error):
    code = getattr(error, 'code', '')
    if not code:
        code = ('TLS_ERROR' if isinstance(error, ssl.SSLError) else
                'DNS_ERROR' if isinstance(error, socket.gaierror) else
                'TIMEOUT' if isinstance(error, TimeoutError) else
                'NETWORK_ERROR' if isinstance(error, OSError) else 'PARSER_ERROR')
    result = {'error_code': code, 'exception': type(error).__name__}
    if code.startswith('HTTP_') and code[5:].isdigit():
        result['http_status'] = int(code[5:])
    return result


def connectivity(events, search_success, source_success):
    reached = any(row.get('http_status') for row in events)
    failures = [row.get('error_code') for row in events if row.get('error_code')]
    if reached:
        if search_success:
            degraded = any(row.get('event') == 'provider_failed' or row.get('error_code') == 'HTTP_202' for row in events)
            return 'SEARCH_PROVIDER_DEGRADED' if degraded else 'ONLINE'
        return 'DIRECT_FETCH_ONLY' if source_success else 'SEARCH_PROVIDER_DEGRADED'
    for code in ('TLS_ERROR', 'DNS_ERROR', 'TIMEOUT'):
        if code in failures:
            return code
    # No successful search is not evidence of an offline machine.
    return 'SEARCH_PROVIDER_DEGRADED'
