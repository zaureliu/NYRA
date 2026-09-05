import hashlib
import time

from .code import diagnostics, repair
from .models import BoardProfile, HardwareError, now
from .projects import digest


class BuildEngine:
    MAX_REPAIR_CYCLES = 5

    def __init__(self, projects, executor):
        self.projects, self.executor = projects, executor

    async def build(self, meta, progress=None, *, general_repair=None):
        seen = set()
        root = self.projects.path(meta['project_id'])
        self.projects.validate_build_inputs(meta)
        for attempt in range(self.MAX_REPAIR_CYCLES + 1):
            self.projects.validate_build_inputs(meta)
            if progress:
                await progress('building', {'attempt': attempt + 1})
            start = time.perf_counter()
            result = await self.executor.run('build', workspace=root)
            output = str(result.get('stdout', '')) + '\n' + str(result.get('stderr', ''))
            findings = diagnostics(output)
            binaries = [root / '.pio/build/nyra' / name for name in ('firmware.bin', 'firmware.hex', 'firmware.uf2', 'program.exe')]
            artifacts = [p for p in binaries if p.is_file() and p.stat().st_size > 0]
            meta['build'] = {'success': bool(result.get('success') and artifacts), 'at': now(), 'attempt': attempt + 1,
                             'elapsed_ms': round((time.perf_counter()-start)*1000, 2), 'diagnostics': findings,
                             'error_code': result.get('error_code'), 'source_hashes': self.projects.source_hash(meta['project_id']),
                             'artifacts': [{'path': str(p), 'sha256': digest(p)} for p in artifacts]}
            self.projects.save(meta)
            if meta['build']['success']:
                return meta['build']
            signature = hashlib.sha256(str(findings or output[-3000:]).encode()).hexdigest()
            if signature in seen or attempt >= self.MAX_REPAIR_CYCLES:
                break
            seen.add(signature)
            source = (root / 'src/main.cpp').read_text(encoding='utf8')
            updated, reason = repair(source, findings, BoardProfile.model_validate(meta['board']))
            if not updated or updated == source:
                if general_repair and await general_repair(findings, output):
                    continue
                break
            self.projects.checkpoint(meta)
            self.projects.write(meta['project_id'], 'src/main.cpp', updated)
            self.projects.checkpoint(meta)
            if progress:
                await progress('repair_applied', {'reason': reason})
        raise HardwareError('BUILD_ERROR')
