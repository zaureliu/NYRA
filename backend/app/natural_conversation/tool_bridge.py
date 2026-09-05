import re
import unicodedata


def cancellation_requested(text: str) -> bool:
    normalized = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    return bool(re.fullmatch(r"\s*(?:para(?: (?:isso|essa tarefa))?|cancela(?: (?:isso|a tarefa))?|nao precisa mais|deixa pra depois)[.!?]*\s*", normalized))


async def cancel_session_task(session, hardware_engine) -> str:
    """Resolve only this conversation's task through the existing task owner.

    Never kill a flash process or guess among multiple unrelated tasks.
    """
    if hardware_engine is None or not session.pending_tool_runs:
        return "Não há uma tarefa em andamento vinculada a esta conversa para cancelar."
    task_ids = list(session.pending_tool_runs)
    if len(task_ids) != 1:
        return "Há mais de uma tarefa desta conversa em andamento. Qual delas você quer cancelar?"
    task_id = task_ids[0]
    goal = next((g for g in hardware_engine.goals.values() if g.task_id == task_id), None)
    if goal and goal.steps and goal.steps[-1].get('phase') in {'flashing', 'flash', 'erase', 'recover', 'bootloader'}:
        return "A gravação está em uma etapa sensível. Não vou cortar a operação no meio e arriscar a placa."
    cancelled = await hardware_engine.services.intelligence.tasks.cancel(task_id)
    if cancelled:
        session.pending_tool_runs.discard(task_id)
        return "Solicitei o cancelamento da tarefa desta conversa."
    return "A tarefa já não está disponível para cancelamento."
