"""Reviewed firmware recipes and narrowly bounded diagnostic-driven repairs."""
import re

from .models import HardwareError


PIN_SOURCES = {
    'uno': 'https://raw.githubusercontent.com/arduino/ArduinoCore-avr/master/variants/standard/pins_arduino.h',
    'pico': 'https://raw.githubusercontent.com/arduino/ArduinoCore-mbed/main/variants/RASPBERRY_PI_PICO/pins_arduino.h',
}


async def resolve_led(profile, research):
    url = PIN_SOURCES.get(profile.board_id)
    if not url:
        # RGB/addressable LEDs are not ordinary digital LEDs. Never substitute
        # a plausible ESP32 GPIO or assume one board's pinout applies to another.
        raise HardwareError('LED_PIN_OR_DRIVER_UNVERIFIED')
    source = await research.document(url, query='LED_BUILTIN PIN_LED')
    if source.source_type != 'official_repository':
        raise HardwareError('UNTRUSTED_PINOUT')
    defines = dict(re.findall(r'^\s*#define\s+(\w+)\s+\(?([\w]+)\)?', source.text, re.M))
    constants = dict(re.findall(r'(?:static\s+)?const\s+(?:uint8_t|int)\s+(\w+)\s*=\s*(\d+)\s*;', source.text))
    defines.update(constants)
    value = 'LED_BUILTIN'
    for _ in range(4):
        value = defines.get(value, value)
        if value.isdigit():
            profile.led_pin = int(value)
            profile.led_source = source.url
            profile.sources.append(source.model_dump(exclude={'text'}))
            return profile
    raise HardwareError('LED_PIN_OR_DRIVER_UNVERIFIED')


def generate(profile, intent):
    if intent.effect not in ('led_on', 'led_off', 'led_blink', 'project', 'build', 'button'):
        raise HardwareError('HARDWARE_RECIPE_UNAVAILABLE')
    if profile.led_pin is None or not profile.led_source:
        raise HardwareError('LED_PIN_OR_DRIVER_UNVERIFIED')
    if intent.effect == 'button':
        raise HardwareError('BUTTON_WIRING_UNVERIFIED')
    mode = {'led_on': 1, 'led_off': 0, 'led_blink': 2}.get(intent.effect, 0)
    # Original small protocol implementation, not copied from online examples.
    # No secrets/network stack, arbitrary GPIO writes or external script hooks.
    return '''#include <Arduino.h>
const uint8_t NYRA_LED = @PIN@;
const bool NYRA_ACTIVE_HIGH = @HIGH@;
unsigned long intervalMs = @INTERVAL@, lastTick = 0;
int mode = @MODE@;
bool ledState = false;
String commandLine;
void setLed(bool on) { ledState = on; digitalWrite(NYRA_LED, on == NYRA_ACTIVE_HIGH ? HIGH : LOW); }
void setup() {
  pinMode(NYRA_LED, OUTPUT);
  setLed(mode != 0);
  Serial.begin(115200);
  commandLine.reserve(160);
}
void reply(const String &nonce) {
  Serial.print("NYRA1 "); Serial.print(nonce); Serial.print(" {\\"protocol\\":\\"nyra/1\\",\\"nonce\\":\\"");
  Serial.print(nonce); Serial.print("\\",\\"board\\":\\"@BOARD@\\",\\"pin\\":"); Serial.print(NYRA_LED);
  Serial.print(",\\"value\\":"); Serial.print(digitalRead(NYRA_LED) == HIGH ? "true" : "false");
  Serial.print(",\\"mode\\":"); Serial.print(mode);
  Serial.println(",\\"source\\":\\"gpio_readback\\",\\"capabilities\\":[\\"LED ON\\",\\"LED OFF\\",\\"LED BLINK\\"]}");
}
void handleLine(String line) {
  if (!line.startsWith("NYRA1 ")) return;
  int split = line.indexOf(' ', 6);
  if (split < 0) return;
  String nonce = line.substring(6, split), action = line.substring(split + 1);
  if (nonce.length() != 16) return;
  for (unsigned int i=0; i<nonce.length(); ++i) if (!isHexadecimalDigit(nonce[i])) return;
  if (action == "LED ON") { mode=1; setLed(true); }
  else if (action == "LED OFF") { mode=0; setLed(false); }
  else if (action.startsWith("LED BLINK ")) {
    unsigned long requested = action.substring(10).toInt();
    if (requested < 100 || requested > 60000) return;
    intervalMs=requested; mode=2; lastTick=millis(); setLed(true);
  } else if (action != "STATUS") return;
  reply(nonce);
}
void loop() {
  if (mode == 2 && millis()-lastTick >= intervalMs) { lastTick=millis(); setLed(!ledState); }
  while (Serial.available()) {
    char c=Serial.read();
    if (c=='\\n') { handleLine(commandLine); commandLine=""; }
    else if (c!='\\r') { if (commandLine.length()<159) commandLine += c; else commandLine=""; }
  }
}
'''.replace('@PIN@', str(profile.led_pin)).replace('@HIGH@', 'true' if profile.led_active_high else 'false').replace(
        '@INTERVAL@', str(intent.interval_ms)).replace('@MODE@', str(mode)).replace('@BOARD@', profile.board_id)


def diagnostics(output):
    rows = []
    for line in output.splitlines():
        match = re.search(r'([^\s:]+\.(?:cpp|c|h)):(\d+):(?:(\d+):)?\s*(fatal error|error|warning):\s*(.+)', line)
        if match:
            rows.append({'file': match[1], 'line': int(match[2]), 'severity': match[4], 'message': match[5][:500]})
    return rows[:30]


def repair(source, findings, profile):
    """Only two independently reviewable repairs. Never delete features to pass."""
    for finding in findings:
        if 'LED_BUILTIN' in finding['message'] and 'not declared' in finding['message'] and profile.led_pin is not None and profile.led_source:
            return source.replace('LED_BUILTIN', str(profile.led_pin)), 'documented_led_pin'
        if 'expected' in finding['message'] and ';' in finding['message']:
            lines = source.splitlines()
            for index in (finding['line']-2, finding['line']-1):
                if 0 <= index < len(lines) and re.fullmatch(r'\s*Serial\.begin\(115200\)\s*', lines[index]):
                    lines[index] = lines[index].rstrip() + ';'
                    return '\n'.join(lines) + '\n', 'missing_serial_begin_semicolon'
    return None, None
