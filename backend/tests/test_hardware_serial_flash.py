import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.hardware_engine.flash import FlashEngine
from app.hardware_engine.models import GoalIntent, HardwareError
from app.hardware_engine.serial import SerialRuntime
from app.hardware_engine.projects import ProjectStore
from app.hardware_engine.code import generate
from test_hardware_engineering import profile


class SerialFixture:
    def __init__(self, port, **kwargs):
        self.port, self.is_open = port, True
        self.line = b''
        self.stale = False
    def reset_input_buffer(self): self.line = b''
    def write(self, command):
        nonce = command.decode().split()[1]
        data = {'protocol': 'nyra/1', 'nonce': 'stale' if self.stale else nonce,
                'board': 'uno', 'pin': 13, 'value': True, 'mode': 1, 'source': 'gpio_readback', 'capabilities': ['LED ON']}
        self.line = ('NYRA1 ' + nonce + ' ' + json.dumps(data) + '\n').encode()
        return len(command)
    def read_until(self, end, size):
        line, self.line = self.line, b''
        return line[:size]
    def close(self): self.is_open = False


@pytest.mark.asyncio
async def test_serial_nonce_readback_simulation_marker_and_shutdown():
    discovery = SimpleNamespace(resolve=AsyncMock(return_value={'device_id': 'fixture', 'com_port': 'COM7'}))
    serial = SerialRuntime(discovery, factory=SerialFixture)
    result = await serial.control('fixture', GoalIntent(effect='led_on'), profile())
    assert result['success'] and result['simulated'] and not result['effect_verified']
    assert result['verification_kind'] == 'gpio_readback_not_optical'
    handle = serial.handles['fixture']
    await serial.close()
    assert not handle.is_open and not serial.handles


@pytest.mark.asyncio
async def test_serial_rejects_arbitrary_commands_and_stale_ack():
    discovery = SimpleNamespace(resolve=AsyncMock(return_value={'device_id': 'fixture', 'com_port': 'COM7'}))
    serial = SerialRuntime(discovery, factory=SerialFixture)
    with pytest.raises(HardwareError, match='SERIAL_COMMAND_NOT_ALLOWED'):
        await serial.request('fixture', 'ERASE ALL')
    handle = await serial.open('fixture')
    handle.stale = True
    with pytest.raises(HardwareError, match='SERIAL_PROTOCOL_UNAVAILABLE'):
        await serial.request('fixture', timeout=.01)
    await serial.close()


@pytest.mark.asyncio
async def test_serial_revalidates_presence_and_com_reassignment():
    discovery = SimpleNamespace(resolve=AsyncMock(return_value={'device_id': 'fixture', 'com_port': 'COM7'}))
    serial = SerialRuntime(discovery, factory=SerialFixture)
    old = await serial.open('fixture')
    discovery.resolve.return_value['com_port'] = 'COM9'
    new = await serial.open('fixture')
    assert not old.is_open and new.port == 'COM9'
    discovery.resolve.side_effect = HardwareError('DEVICE_NOT_FOUND')
    with pytest.raises(HardwareError, match='DEVICE_NOT_FOUND'):
        await serial.request('fixture', 'LED ON')
    await serial.close()


@pytest.mark.asyncio
async def test_flash_never_executes_for_missing_or_changed_firmware(tmp_path):
    store = ProjectStore(tmp_path/'projects', tmp_path/'state')
    meta = store.create(profile(), {'device_id': 'fixture'})
    store.write(meta['project_id'], 'src/main.cpp', generate(profile(), GoalIntent(effect='led_on')))
    store.checkpoint(meta)
    executor = SimpleNamespace(run=AsyncMock())
    flasher = FlashEngine(SimpleNamespace(resolve=AsyncMock(return_value={'device_id': 'fixture'})),
                          SimpleNamespace(close_device=AsyncMock()), store, executor)
    with pytest.raises(HardwareError, match='FIRMWARE_SOURCE_MISMATCH'):
        await flasher.flash(meta)
    assert not executor.run.called
