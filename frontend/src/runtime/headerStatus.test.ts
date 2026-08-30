import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ apiGet: vi.fn() }))
vi.mock('./api', () => ({ apiGet: mocks.apiGet }))

import { readHeaderStatus } from './headerStatus'

describe('readHeaderStatus', () => {
  beforeEach(() => mocks.apiGet.mockReset())

  it('usa o mesmo client REST oficial com bypass de cache', async () => {
    mocks.apiGet.mockResolvedValue({ status: 'online' })
    await expect(readHeaderStatus('/api/health')).resolves.toEqual({ status: 'online' })
    expect(mocks.apiGet).toHaveBeenCalledWith('/api/health', 12000, 'no-store')
  })

  it('mantém a lista explícita de endpoints do header', async () => {
    await expect(readHeaderStatus('/api/tasks')).rejects.toThrow('HEADER_STATUS_PATH_NOT_ALLOWED')
    expect(mocks.apiGet).not.toHaveBeenCalled()
  })
})
