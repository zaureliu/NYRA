"""Reuses the one native USB monitor and refresh; no second watcher or poller."""
import asyncio
from datetime import datetime, timezone
import time

from app.usb.hardware import plain
from .adapters import identify_descriptor
from .models import HardwareError


class HardwareDiscovery:
    def __init__(self, usb, world=None):
        self.usb, self.world = usb, world
        self.last = {}
        self.overhead_ms = 0.0

    async def refresh(self):
        start = time.perf_counter()
        snapshot = await asyncio.wait_for(self.usb.refresh(reason='hardware_grounding'), 10)
        self.overhead_ms = round((time.perf_counter()-start)*1000, 2)
        if not snapshot.get('discovery_success'):
            raise HardwareError('DISCOVERY_ERROR')
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(snapshot['observed_at'])).total_seconds()
        if not 0 <= age <= 15:
            raise HardwareError('STALE_DEVICE_OBSERVATION')
        rows = snapshot.get('observed_devices') or []
        if (getattr(self.usb.discovery, 'source', None) != 'windows_setupapi'
                or getattr(self.usb.discovery, 'simulated', True) is not False
                or any(r.get('metadata', {}).get('simulated') for r in rows)):
            raise HardwareError('SIMULATED_HARDWARE')
        connected = [r for r in rows if r.get('status') == 'CONNECTED' and r.get('device_instance_id')]
        recent = [r for r in connected if not r.get('present_at_startup') and self._recent(r.get('last_connection'))]
        self.last = {'devices': connected, 'recent': recent, 'serial_ports': [r['com_port'] for r in connected if r.get('com_port')],
                     'source': 'usb_discovery', 'observed_at': snapshot['observed_at'], 'simulated': False}
        return self.last

    @staticmethod
    def _recent(value):
        try:
            return 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(value)).total_seconds() <= 300
        except (ValueError, TypeError):
            return False

    async def resolve(self, target='', device_id=None, *, physical=False):
        snapshot = await self.refresh()
        candidates = snapshot['devices']
        if device_id:
            candidates = [d for d in candidates if d['device_id'] == device_id]
        elif target:
            target = plain(target).replace('-', '')
            candidates = [d for d in candidates if target in plain(f"{d.get('name', '')} {d.get('product', '')}").replace('-', '')]
        else:
            recent = snapshot['recent']
            if len(recent) == 1:
                candidates = recent
            elif physical:
                candidates = [d for d in candidates if identify_descriptor(d)['family'] not in ('unknown', 'generic_serial')]
        if not candidates:
            raise HardwareError('DEVICE_NOT_FOUND')
        if len(candidates) != 1:
            raise HardwareError('AMBIGUOUS_DEVICE')
        return {**candidates[0], 'observed_at': snapshot['observed_at'], 'source': 'usb_discovery', 'simulated': False}
