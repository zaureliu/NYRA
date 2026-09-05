import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }))

vi.mock('./backend', () => ({ kazumiFetch: mocks.fetch }))

import { sendChat } from './conversation'

describe('sendChat pelo transporte oficial', () => {
  beforeEach(() => mocks.fetch.mockReset())

  it('envia o payload do chat pelo client central', async () => {
    mocks.fetch.mockResolvedValue(new Response(JSON.stringify({
      response: 'Olá', display_text: 'Olá', state: 'happy',
    }), { status: 200 }))

    await expect(sendChat({ message: 'oi', synthesize: true })).resolves.toMatchObject({ response: 'Olá' })
    expect(mocks.fetch).toHaveBeenCalledWith('/api/chat', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ message: 'oi', synthesize: true }),
    }))
  })

  it('preserva o envelope grounded de erro do backend', async () => {
    mocks.fetch.mockResolvedValue(new Response(JSON.stringify({
      detail: { error_code: 'PIPELINE_FAILURE', exception_type: 'RuntimeError' },
    }), { status: 502 }))

    await expect(sendChat({ message: 'oi', synthesize: true })).rejects.toThrow(
      'PIPELINE_FAILURE (RuntimeError)',
    )
  })

})
