"""Deterministic recipes use the existing audited SystemShell, never model shell."""
import asyncio
from pathlib import Path
import shutil

from app.agent.context import current_agent_run_id
from .models import HardwareError


def quote(value):
    value = str(value)
    if any(c in value for c in ('\0', '\n', '\r')):
        raise HardwareError('INVALID_TOOL_ARGUMENT')
    return "'" + value.replace("'", "''") + "'"


class RecipeExecutor:
    """Internal-only runner. Not an LLM tool; callers pass fixed recipe enums.

    FULL is explicit operator configuration. Its one-use grants remain bound
    to the exact deterministic command/cwd/timeout/run. UAC and SSH unchanged.
    """
    def __init__(self, shell, tools_root: Path, projects_root: Path, *, full=False):
        self.shell, self.tools_root, self.projects_root = shell, tools_root.resolve(), projects_root.resolve()
        self.full = full
        self._tasks = set()

    @property
    def python(self):
        return self.tools_root / ('Scripts/python.exe' if __import__('os').name == 'nt' else 'bin/python')

    async def run(self, recipe: str, *, workspace=None, port=None, output=None):
        if not self.full:
            raise HardwareError('HARDWARE_AUTONOMY_DISABLED')
        cwd = Path(workspace).resolve() if workspace else self.tools_root.parent
        if workspace and (cwd == self.projects_root or not cwd.is_relative_to(self.projects_root)):
            raise HardwareError('PROJECT_OUTSIDE_WORKSPACE')
        if port and not __import__('re').fullmatch(r'COM[1-9]\d{0,3}', port, __import__('re').I):
            raise HardwareError('INVALID_SERIAL_PORT')
        python = str(self.python)
        commands = {
            'install': [python, '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check',
                        '--index-url', 'https://pypi.org/simple', 'platformio==6.1.18', 'esptool==5.0.2'],
            'toolchain_version': [python, '-m', 'platformio', '--version'],
            'build': [python, '-m', 'platformio', 'run', '-e', 'nyra'],
            'flash': [python, '-m', 'platformio', 'run', '-e', 'nyra', '-t', 'nobuild', '-t', 'upload', '--upload-port', port or ''],
            'chip_probe': [python, '-m', 'esptool', '--port', port or '', 'chip-id'],
            'flash_info': [python, '-m', 'esptool', '--port', port or '', 'flash-id'],
            'reset': [python, '-m', 'esptool', '--port', port or '', 'run'],
        }
        if recipe == 'create_venv':
            host = shutil.which('py.exe') or shutil.which('python.exe')
            if not host:
                raise HardwareError('PYTHON_TOOLCHAIN_NOT_FOUND')
            commands[recipe] = [host] + (['-3'] if Path(host).stem == 'py' else []) + ['-m', 'venv', str(self.tools_root)]
        if recipe == 'backup':
            file = Path(output).resolve()
            if file.parent != cwd / '.nyra-history' or file.suffix != '.bin':
                raise HardwareError('INVALID_BACKUP_TARGET')
            commands[recipe] = [python, '-m', 'esptool', '--port', port or '', 'read-flash', '0', 'ALL', str(file)]
        if recipe not in commands:
            raise HardwareError('RECIPE_NOT_ALLOWED')
        if recipe in ('flash', 'chip_probe', 'flash_info', 'backup', 'reset') and not port:
            raise HardwareError('SERIAL_PORT_REQUIRED')
        args = commands[recipe]
        command = '& ' + ' '.join(quote(a) for a in args)
        timeout = min(self.shell.settings.shell_max_timeout_seconds, 600 if recipe in ('install', 'build', 'backup', 'flash') else 45)
        assessment = self.shell.classifier.classify(command, 'powershell')
        record = self.shell.approvals.request(command=command, shell='powershell', working_directory=str(cwd),
                                             timeout_seconds=timeout, risk_level=assessment.level,
                                             agent_run_id=current_agent_run_id.get())
        self.shell.approvals.grant(record.approval_id, 'operator_hardware_full:deterministic_recipe:' + recipe)
        task = asyncio.create_task(self.shell.execute(command, shell='powershell', working_directory=str(cwd),
                                  timeout_seconds=timeout, approval_id=record.approval_id,
                                  reason='Hardware recipe: ' + recipe))
        self._tasks.add(task)
        try:
            return await task
        finally:
            self._tasks.discard(task)

    async def close(self):
        # SystemShell owns process-tree cancellation; never kill by name.
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
