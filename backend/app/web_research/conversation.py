"""Web-only bridge into the existing turn, tools and research service."""
from collections import OrderedDict
import time

from .diagnostics import record
from .fetch import ResearchError
from .planner import standalone_research_request, technical_query, topic_terms, freshness_required, explicit_urls
from .search import public_query
from .service import research_reply


class WebConversationBridge:
    def __init__(self, research, tools):
        self.research, self.tools = research, tools
        # Query context only, never cached facts or a second conversation session.
        self.topics = OrderedDict()

    async def reply(self, text, turn):
        if not standalone_research_request(text):
            return None
        query = text
        subject = topic_terms(technical_query(text))
        if not subject and not explicit_urls(text):
            previous = self.topics.get(turn.conversation_id)
            if previous and time.monotonic() - previous[1] < 1800:
                query = previous[0] + ' ' + text
            else:
                return 'O que você quer que eu pesquise? Me diga o assunto ou envie a URL da documentação.'
        try:
            public_query(query)
        except ResearchError:
            return 'Essa consulta contém identificadores privados. Envie só o nome público do produto ou da documentação.'
        self.topics[turn.conversation_id] = (technical_query(query), time.monotonic())
        self.topics.move_to_end(turn.conversation_id)
        while len(self.topics) > 32:
            self.topics.popitem(last=False)
        record('web_conversation_routed', turn_id=turn.turn_id, conversation_id=turn.conversation_id,
               intent='web_research', freshness_required=freshness_required(query), capability='web.research', tool='web_research')
        result = await self.tools.execute('web_research', {'query': query[:500], 'fresh': True})
        data = result.data
        turn.tool_calls.append({'name': 'web_research', 'arguments': {'query': query[:500], 'fresh': True}})
        turn.tool_results.append(result.model_dump(mode='json'))
        response = await self.research.present(query, data) if result.ok else research_reply(data)
        record('web_conversation_result', turn_id=turn.turn_id, conversation_id=turn.conversation_id,
               source_count=len(data.get('sources', [])), success=bool(data.get('success')))
        return response
