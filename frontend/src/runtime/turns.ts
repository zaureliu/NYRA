export interface TurnBoundEvent {
  type: string
  payload: Record<string, unknown>
}

export const extractTurnId = (payload: Record<string, unknown>): string | null => {
  const turn = payload.turn_id
  if (typeof turn === 'string' && turn.startsWith('turn_')) return turn
  const response = payload.response_id
  if (typeof response === 'string' && response.length > 0) return response
  return null
}

/**
 * Every input accepted by the backend starts a globally visible turn. The
 * packaged Desktop Presence is the single audio owner, so it must adopt turns
 * started by the dashboard, microphone or a Voice Satellite as well as turns
 * submitted by its own text box.
 */
export const isTurnStartEvent = (eventType: string): boolean =>
  eventType === 'USER_TEXT_RECEIVED'

export const adoptInputTurn = (
  filter: TurnFilter,
  eventType: string,
  turnId: string | null,
): boolean => {
  if (!turnId || !isTurnStartEvent(eventType)) return false
  filter.begin(turnId)
  return true
}

/**
 * Drops events that belong to a finished or superseded turn so a late token,
 * response or audio chunk from turn A can never be attached to turn B (#26, #27).
 * Events without any turn marker (legacy) are accepted unchanged.
 */
export class TurnFilter {
  private activeTurn: string | null = null
  private readonly finished = new Set<string>()
  private readonly seen = new Set<string>()
  dropped = 0

  begin(turnId: string | null): void {
    if (!turnId || this.activeTurn === turnId) return
    if (this.activeTurn) this.finished.add(this.activeTurn)
    this.activeTurn = turnId
    this.seen.add(turnId)
  }

  end(turnId: string | null): void {
    if (!turnId) return
    this.finished.add(turnId)
    if (this.activeTurn === turnId) this.activeTurn = null
  }

  isActive(turnId: string | null): boolean {
    return !!turnId && turnId === this.activeTurn
  }

  accept(turnId: string | null): boolean {
    if (!turnId) return true
    if (this.activeTurn === turnId) return true
    if (this.finished.has(turnId)) {
      this.dropped += 1
      return false
    }
    this.seen.add(turnId)
    // Turno desconhecido que não é o ativo: só aceita enquanto nada novo começou.
    if (this.activeTurn === null) {
      this.begin(turnId)
      return true
    }
    this.dropped += 1
    return false
  }
}
