"""One bounded serial handle per physical device; real nonce-bound acknowledgments."""
import asyncio
import json
import re
import secrets
import time

from .models import HardwareError, now


class SerialRuntime:
    def __init__(self, discovery, *, factory=None):
        self.discovery = discovery
        self.factory = factory
        self.handles = {}
        self.locks = {}
        self.closed = False
        self.last = {}
        self.protocols = {}

    async def open(self, device_id, baud=115200):
        device = await self.discovery.resolve(device_id=device_id, physical=True)
        port = device.get('com_port')
        if not port or not re.fullmatch(r'COM[1-9]\d{0,3}', port, re.I):
            raise HardwareError('SERIAL_PORT_REQUIRED')
        if device_id in self.handles:
            handle = self.handles[device_id]
            if handle.port == port and handle.is_open:
                return handle
            await self.close_device(device_id)
        if self.closed or len(self.handles) >= 4:
            raise HardwareError('SERIAL_CAPACITY_OR_SHUTDOWN')
        factory = self.factory
        if factory is None:
            import serial
            factory = serial.Serial
        handle = await asyncio.to_thread(factory, port=port, baudrate=baud, timeout=.2, write_timeout=1)
        self.handles[device_id] = handle
        return handle

    async def monitor(self, device_id, seconds=2, max_bytes=8192):
        handle = await self.open(device_id)
        deadline = time.monotonic() + min(seconds, 10)
        chunks = []
        size = 0
        while time.monotonic() < deadline and size < min(max_bytes, 65536) and not self.closed:
            line = await asyncio.to_thread(handle.read_until, b'\n', min(512, max_bytes-size))
            size += len(line)
            if line:
                chunks.append({'at': now(), 'text': line.decode('utf8', errors='replace').strip()[:512]})
        return {'source': 'serial_read', 'device_id': device_id, 'observed_at': now(), 'lines': chunks, 'bytes': size}

    async def request(self, device_id, command='STATUS', *, board_id=None, timeout=3, _legacy=False):
        if not re.fullmatch(r'STATUS|LED ON|LED OFF|LED BLINK (?:[1-9]\d{2,4})', command):
            raise HardwareError('SERIAL_COMMAND_NOT_ALLOWED')
        if command != 'STATUS' and device_id not in self.protocols:
            await self.request(device_id, 'STATUS', board_id=board_id, timeout=timeout)
        protocol = self.protocols.get(device_id, 'nyra' if _legacy else 'kazumi')
        wire = 'NYRA1' if protocol == 'nyra' else 'KAZUMI1'
        lock = self.locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            handle = await self.open(device_id)
            nonce = secrets.token_hex(8)
            await asyncio.to_thread(handle.reset_input_buffer)
            await asyncio.to_thread(handle.write, f'{wire} {nonce} {command}\n'.encode('ascii'))
            deadline = time.monotonic() + min(timeout, 10)
            size = 0
            while time.monotonic() < deadline and size < 16384 and not self.closed:
                line = await asyncio.to_thread(handle.read_until, b'\n', 1024)
                size += len(line)
                prefix = f'{wire} {nonce} '.encode()
                if not line.startswith(prefix):
                    continue
                try:
                    payload = json.loads(line[len(prefix):])
                except (ValueError, UnicodeError):
                    continue
                if (payload.get('protocol') != protocol + '/1' or payload.get('nonce') != nonce
                        or (board_id and payload.get('board') != board_id)):
                    continue
                self.protocols[device_id] = protocol
                self.last = {'success': True, 'device_id': device_id, 'source': 'firmware_ack',
                             'observed_at': now(), 'simulated': bool(self.factory), 'data': payload}
                return self.last
        # Only a read-only probe is retried, never an effectful command.
        if command == 'STATUS' and not _legacy and device_id not in self.protocols:
            return await self.request(device_id, command, board_id=board_id, timeout=timeout, _legacy=True)
        raise HardwareError('SERIAL_PROTOCOL_UNAVAILABLE')

    async def control(self, device_id, intent, profile):
        if profile.led_pin is None or not profile.led_source:
            raise HardwareError('LED_PIN_OR_DRIVER_UNVERIFIED')
        command = {'led_on': 'LED ON', 'led_off': 'LED OFF', 'led_blink': f'LED BLINK {intent.interval_ms}'}[intent.effect]
        result = await self.request(device_id, command, board_id=profile.board_id)
        data = result['data']
        expected_mode = {'led_on': 1, 'led_off': 0, 'led_blink': 2}[intent.effect]
        verified = (data.get('source') == 'gpio_readback' and data.get('pin') == profile.led_pin
                    and type(data.get('value')) is bool and data.get('mode') == expected_mode)
        if intent.effect != 'led_blink':
            verified = verified and data['value'] == (profile.led_active_high if expected_mode == 1 else not profile.led_active_high)
        else:
            # Mode ACK is not an observed blink. Require a second different
            # GPIO readback from the same handle/device and nonce-bound protocol.
            await asyncio.sleep(min(intent.interval_ms / 1000 + .05, 60.05))
            second = await self.request(device_id, 'STATUS', board_id=profile.board_id)
            verified = verified and second['data'].get('pin') == profile.led_pin and second['data'].get('value') is not data.get('value')
            result['second_readback'] = second
        result['effect_verified'] = verified and not result['simulated']
        result['verification_kind'] = 'gpio_readback_not_optical'
        if not verified:
            raise HardwareError('VERIFY_ERROR')
        return result

    async def close_device(self, device_id):
        handle = self.handles.pop(device_id, None)
        if handle:
            await asyncio.to_thread(handle.close)

    async def close(self):
        self.closed = True
        for device_id in tuple(self.handles):
            await self.close_device(device_id)
