import asyncio
from pathlib import Path

from .models import HardwareError, now
from .projects import digest


class FlashEngine:
    def __init__(self, discovery, serial, projects, executor):
        self.discovery, self.serial, self.projects, self.executor = discovery, serial, projects, executor
        self._lock = asyncio.Lock()

    async def flash(self, meta):
        async with self._lock:
            if meta.get('reference') or not meta.get('device_id'):
                raise HardwareError('REFERENCE_PROJECT_NOT_PHYSICAL')
            device = await self.discovery.resolve(device_id=meta['device_id'], physical=True)
            self.projects.validate_build_inputs(meta)
            build = meta.get('build', {})
            if not build.get('success') or build.get('source_hashes') != self.projects.source_hash(meta['project_id']):
                raise HardwareError('FIRMWARE_SOURCE_MISMATCH')
            for artifact in build.get('artifacts', []):
                if digest(artifact['path']) != artifact['sha256']:
                    raise HardwareError('FIRMWARE_HASH_MISMATCH')
            if meta['board']['family'] != 'espressif':
                raise HardwareError('BACKUP_RECOVERY_ADAPTER_UNAVAILABLE')
            port = device.get('com_port')
            root = self.projects.path(meta['project_id'])
            await self.serial.close_device(device['device_id'])
            probe = await self.executor.run('chip_probe', port=port, workspace=root)
            from .identification import chip_from_probe
            chip = chip_from_probe(probe)
            if chip != meta['board']['chip']:
                raise HardwareError('BOARD_MCU_MISMATCH')
            backup = root / '.nyra-history' / ('backup-' + meta['project_id'][-8:] + '.bin')
            if backup.exists():
                raise HardwareError('BACKUP_ALREADY_EXISTS_REVIEW_REQUIRED')
            saved = await self.executor.run('backup', port=port, workspace=root, output=backup)
            if not saved.get('success') or not backup.is_file() or backup.stat().st_size < 65536:
                raise HardwareError('FLASH_BACKUP_FAILED')
            meta['backup'] = {'path': str(backup), 'sha256': digest(backup), 'device_id': device['device_id'],
                              'chip': chip, 'at': now(), 'recovery': 'esptool write-flash 0 <verified-backup>'}
            self.projects.save(meta)
            # Backup/probe may reset USB. Revalidate immediately before writing.
            device = await self.discovery.resolve(device_id=device['device_id'], physical=True)
            result = await self.executor.run('flash', port=device.get('com_port'), workspace=root)
            meta['flash'] = {'success': bool(result.get('success')), 'at': now(), 'device_id': device['device_id'],
                             'artifacts': build['artifacts'], 'effect_verified': False, 'reconnected': False}
            self.projects.save(meta)
            if not result.get('success'):
                raise HardwareError('FLASH_ERROR')
            for _ in range(5):
                await asyncio.sleep(1)
                snapshot = await self.discovery.refresh()
                # COM may change, but not the chip/physical serial association.
                rows = [r for r in snapshot['devices'] if r['device_id'] == device['device_id'] or
                        (device.get('serial') and r.get('serial') == device['serial'] and r.get('vid') == device.get('vid'))]
                if len(rows) == 1 and rows[0].get('com_port'):
                    meta['device_id'] = rows[0]['device_id']
                    meta['serial_port'] = rows[0]['com_port']
                    meta['flash']['reconnected'] = True
                    self.projects.save(meta)
                    return meta['flash']
            raise HardwareError('DEVICE_DISCONNECTED')
