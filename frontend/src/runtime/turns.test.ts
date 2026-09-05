import { describe, expect, it } from 'vitest'
import { TurnFilter, adoptInputTurn, extractTurnId, isTurnStartEvent, isConversationToolEvent } from './turns'

const event = (type: string, turnId: string) => ({ type, payload: { turn_id: turnId } })

describe('conversation tool provenance', () => {
  it('does not attach the gateway background poll or another turn to the current request', () => {
    const filter = new TurnFilter()
    filter.begin('turn_esp32')
    expect(isConversationToolEvent({ type: 'REMOTE_SHELL_EXECUTION_STARTED', payload: { turn_id: null } }, filter)).toBe(false)
    expect(isConversationToolEvent(event('REMOTE_SHELL_EXECUTION_FINISHED', 'turn_old'), filter)).toBe(false)
    expect(isConversationToolEvent(event('SHELL_EXECUTION_STARTED', 'turn_esp32'), filter)).toBe(true)
  })
  it('preserves genuine approvals even without turn metadata', () => {
    expect(isConversationToolEvent({ type: 'REMOTE_SHELL_APPROVAL_REQUIRED', payload: {} }, new TurnFilter())).toBe(true)
  })
})

describe('TurnFilter', () => {
  it('aceita eventos do turno ativo e descarta eventos tardios de turnos encerrados', () => {
    const filter = new TurnFilter()
    filter.begin('turn_a')
    expect(filter.accept('turn_a')).toBe(true)
    filter.end('turn_a')
    filter.begin('turn_b')
    // Evento tardio do turno A chega durante B (#107/#138): ignorado.
    expect(filter.accept('turn_a')).toBe(false)
    expect(filter.dropped).toBe(1)
    expect(filter.accept('turn_b')).toBe(true)
  })

  it('nunca anexa token do turno antigo à mensagem nova', () => {
    const filter = new TurnFilter()
    filter.begin('turn_1')
    filter.begin('turn_2')
    const tokens = ['turn_1', 'turn_1', 'turn_2', 'turn_1', 'turn_2']
    const appended = tokens.filter((turn) => filter.accept(turn))
    expect(appended).toEqual(['turn_2', 'turn_2'])
  })

  it('eventos sem marcador de turno continuam aceitos (compatibilidade)', () => {
    const filter = new TurnFilter()
    filter.begin('turn_x')
    expect(filter.accept(null)).toBe(true)
    expect(filter.accept(undefined as unknown as null)).toBe(true)
  })

  it('adopta o primeiro turno observado quando nada começou explicitamente', () => {
    const filter = new TurnFilter()
    expect(filter.accept('turn_first')).toBe(true)
    expect(filter.isActive('turn_first')).toBe(true)
    filter.begin('turn_second')
    expect(filter.accept('turn_first')).toBe(false)
  })
})

describe('extractTurnId', () => {
  it('prefere turn_id e usa response_id como fallback', () => {
    expect(extractTurnId({ turn_id: 'turn_ab', response_id: 'cd' })).toBe('turn_ab')
    expect(extractTurnId({ response_id: 'cd' })).toBe('cd')
    expect(extractTurnId({})).toBeNull()
  })
})

describe('global input turn ownership', () => {
  it('moves the playback owner to a turn started outside the Presence', () => {
    const filter = new TurnFilter()
    filter.begin('turn_presence')

    expect(adoptInputTurn(filter, 'USER_TEXT_RECEIVED', 'turn_satellite')).toBe(true)
    expect(filter.accept('turn_presence')).toBe(false)
    expect(filter.accept('turn_satellite')).toBe(true)
  })

  it('does not treat output events as a new turn', () => {
    const filter = new TurnFilter()
    filter.begin('turn_current')

    expect(isTurnStartEvent('TTS_CHUNK_FINISHED')).toBe(false)
    expect(adoptInputTurn(filter, 'TTS_CHUNK_FINISHED', 'turn_late')).toBe(false)
    expect(filter.accept('turn_late')).toBe(false)
    expect(filter.accept('turn_current')).toBe(true)
  })
})
