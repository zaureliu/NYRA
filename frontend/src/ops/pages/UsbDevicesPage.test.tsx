import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const polling = vi.hoisted(() => ({
  states: new Map<string, { data: unknown; loading: boolean; error: string; refresh: () => void }>(),
}))

vi.mock('../hooks', () => ({
  usePolling: (path: string) => polling.states.get(path) ?? {
    data: null, loading: false, error: '', refresh: () => {},
  },
}))

vi.mock('../../runtime/api', () => ({ apiSend: vi.fn() }))

import { UsbDevicesPage } from './UsbDevicesPage'

function set(path: string, data: unknown) {
  polling.states.set(path, { data, loading: false, error: '', refresh: () => {} })
}

describe('UsbDevicesPage', () => {
  beforeEach(() => {
    polling.states.clear()
    const unknown = {
      device_id: 'usb_fixture', name: 'USB Serial Device', friendly_name: null,
      category: 'Serial', vid: '1A86', pid: '7523', vid_pid: '1A86:7523',
      serial: 'CH340-01', com_port: 'COM5', drive_letter: null,
      status: 'CONNECTED', relevance: 'USER_RELEVANT', registered: false,
      known: false, trusted: false, first_seen: '2026-08-27T12:00:00Z',
      last_seen: '2026-08-27T12:01:00Z', last_connection: '2026-08-27T12:00:00Z',
      identity_confidence: 'HIGH', identity_basis: 'USB_SERIAL',
    }
    set('/api/usb/status', {
      monitor_state: 'ACTIVE', connected_count: 1, known_count: 0,
      unknown_count: 1, event_source: 'WINDOWS_CONFIGMGR',
      last_event: { timestamp: '2026-08-27T12:01:00Z', description: 'USB Serial Device conectado', event_type: 'usb.device.unknown' },
    })
    set('/api/usb/devices/connected', { devices: [unknown] })
    set('/api/usb/devices/known', { devices: [] })
    set('/api/usb/history?limit=200', { history: [{
      event_id: 1, timestamp: '2026-08-27T12:01:00Z', event_type: 'usb.device.unknown',
      device_id: 'usb_fixture', name: 'USB Serial Device', vid_pid: '1A86:7523',
      com_port: 'COM5', known: false, level: 'WARNING',
      description: 'Novo dispositivo USB detectado: USB Serial Device.',
    }] })
  })

  it('renderiza estado real, quatro áreas e ações sem valores undefined', () => {
    const html = renderToStaticMarkup(<UsbDevicesPage />)
    expect(html).toContain('Dispositivos USB')
    expect(html).toContain('Visão Geral')
    expect(html).toContain('Conectados Agora')
    expect(html).toContain('Dispositivos Conhecidos')
    expect(html).toContain('Histórico de Eventos')
    expect(html).toContain('USB Serial Device')
    expect(html).toContain('1A86:7523')
    expect(html).toContain('COM5')
    expect(html).toContain('Registrar')
    expect(html).toContain('DESCONHECIDO')
    expect(html).not.toContain('undefined')
    expect(html).not.toContain('Failed to fetch')
  })
})
