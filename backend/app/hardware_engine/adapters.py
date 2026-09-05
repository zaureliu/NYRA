"""Extensible family/board recipes. A chip name NEVER selects a specific board."""
import re

from app.usb.hardware import plain
from .models import BoardProfile


FAMILIES = {
    'espressif': r'\besp(?:32(?:[- ]?(?:s[23]|c[236]))?|8266)\b',
    'arduino_avr': r'\b(?:arduino|atmega(?:328p|2560))\b',
    'rp2040': r'\b(?:rp2040|raspberry pi pico)\b',
    'stm32': r'\bstm32\w*\b',
    'nordic': r'\bnrf52\w*\b',
    'm5stack': r'\b(?:m5stack|cardputer|m5stick)\b',
    'lilygo': r'\b(?:lilygo|t-display|t-deck|t-watch)\b',
    'generic_serial': r'\b(?:serial|uart|cdc|cp210|ch340|ftdi)\w*\b',
}

# Exact descriptor signatures only. No VID/PID -> retail-board shortcuts.
# More adapters may add recipes; absence is an explicit identification blocker.
PROFILES = [
    (r'\barduino uno\b', BoardProfile(board_id='uno', name='Arduino Uno', family='arduino_avr',
        chip='atmega328p', platform='atmelavr', docs_url='https://docs.arduino.cc/hardware/uno-rev3/',
        definition_url='https://raw.githubusercontent.com/platformio/platform-atmelavr/master/boards/uno.json')),
    (r'\braspberry pi pico\b(?!\s*w)', BoardProfile(board_id='pico', name='Raspberry Pi Pico', family='rp2040',
        chip='rp2040', platform='raspberrypi', docs_url='https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html',
        definition_url='https://raw.githubusercontent.com/platformio/platform-raspberrypi/master/boards/pico.json')),
    (r'\besp32-s3-devkitc-1\b', BoardProfile(board_id='esp32-s3-devkitc-1', name='ESP32-S3-DevKitC-1',
        family='espressif', chip='esp32s3', platform='espressif32',
        docs_url='https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/index.html',
        definition_url='https://raw.githubusercontent.com/platformio/platform-espressif32/master/boards/esp32-s3-devkitc-1.json')),
]


def identify_descriptor(device: dict) -> dict:
    descriptor = plain(f"{device.get('name', '')} {device.get('product', '')}")
    family = next((key for key, pattern in FAMILIES.items() if re.search(pattern, descriptor)), 'unknown')
    profiles = [profile for pattern, profile in PROFILES if re.search(pattern, descriptor)]
    chip = re.search(r'\b(?:esp32(?:[- ]?(?:s[23]|c[236]))?|esp8266|rp2040|atmega\d+p?)\b', descriptor)
    return {'device_id': device['device_id'], 'family': family,
            'chip': chip.group().replace('-', '').replace(' ', '') if chip else None,
            'chip_evidence': 'usb_descriptor' if chip else None,
            'board': profiles[0].model_dump() if len(profiles) == 1 else None,
            'board_status': 'DESCRIPTOR_IDENTIFIED' if len(profiles) == 1 else 'UNKNOWN',
            'source': 'usb_discovery', 'simulated': False}
