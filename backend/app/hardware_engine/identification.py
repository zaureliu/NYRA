import re

from .adapters import identify_descriptor


def chip_from_probe(result):
    if not result.get('success'):
        return None
    match = re.search(r'(?i)Chip (?:is|type:)\s*(ESP32(?:[- ]?(?:S[23]|C[236]))?|ESP8266)', str(result.get('stdout', '')))
    return match[1].lower().replace('-', '').replace(' ', '') if match else None


async def identify(device, executor=None, workspace=None):
    identity = identify_descriptor(device)
    if executor and executor.full and device.get('com_port') and identity['family'] in ('espressif', 'generic_serial') and executor.python.is_file():
        result = await executor.run('chip_probe', port=device['com_port'], workspace=workspace)
        chip = chip_from_probe(result)
        if chip:
            identity.update(chip=chip, chip_evidence='serial_chip_probe')
            # Chip evidence does not manufacture a retail board identity.
            if identity['board'] and identity['board']['chip'] != chip:
                identity.update(board=None, board_status='CONFLICT_REPLAN_REQUIRED')
    return identity
