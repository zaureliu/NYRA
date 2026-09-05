import importlib.util
import json
import shutil

from .models import HardwareError


class Toolchains:
    def __init__(self, executor, research):
        self.executor, self.research = executor, research

    def detect(self):
        site = self.executor.tools_root / 'Lib/site-packages/platformio/__main__.py'
        return {'platformio_managed': self.executor.python.is_file() and site.is_file(),
                **{name: shutil.which(exe) is not None for name, exe in (
                    ('platformio', 'pio'), ('arduino_cli', 'arduino-cli'), ('esp_idf', 'idf.py'),
                    ('cmake', 'cmake'), ('pico_sdk', 'pico'), ('stm32', 'STM32_Programmer_CLI'), ('nordic', 'nrfutil'))}}

    async def ensure(self):
        self.executor.tools_root.parent.mkdir(parents=True, exist_ok=True)
        if not self.executor.python.is_file():
            created = await self.executor.run('create_venv')
            if not created.get('success'):
                raise HardwareError('TOOLCHAIN_ERROR', 'Python venv')
        version = await self.executor.run('toolchain_version')
        if version.get('success'):
            return version
        installed = await self.executor.run('install')
        if not installed.get('success'):
            raise HardwareError('DEPENDENCY_ERROR')
        verified = await self.executor.run('toolchain_version')
        if not verified.get('success'):
            raise HardwareError('TOOLCHAIN_ERROR')
        return verified

    async def select(self, profile):
        # Board definition comes from the official toolchain repository, not
        # snippets or user-supplied build instructions.
        source = await self.research.document(profile.definition_url, query=profile.name)
        if source.source_type != 'official_repository':
            raise HardwareError('UNTRUSTED_BOARD_DEFINITION')
        definition = json.loads(source.text)
        detected_mcu = str(definition.get('build', {}).get('mcu', '')).replace('-', '').lower()
        if detected_mcu != profile.chip.replace('-', '').lower():
            raise HardwareError('BOARD_MCU_MISMATCH')
        if profile.framework not in definition.get('frameworks', []):
            raise HardwareError('FRAMEWORK_NOT_SUPPORTED')
        profile.sources = [source.model_dump(exclude={'text'})]
        return profile
