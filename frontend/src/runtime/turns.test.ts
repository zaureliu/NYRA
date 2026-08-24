import { describe, expect, it } from 'vitest'
import { TurnFilter, extractTurnId } from './turns'

const event = (type: string, turnId: string) => ({ type, payload: { turn_id: turnId } })

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
