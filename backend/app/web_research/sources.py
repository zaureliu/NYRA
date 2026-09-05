from urllib.parse import urlsplit


MANUFACTURERS = ('espressif.com', 'raspberrypi.com', 'arduino.cc', 'st.com',
                 'nordicsemi.com', 'm5stack.com', 'lilygo.cc')
FRAMEWORKS = ('platformio.org', 'python.org', 'pyserial.readthedocs.io', 'cmake.org')
OFFICIAL_GITHUB = {'espressif', 'platformio', 'arduino', 'arduino-libraries', 'raspberrypi', 'stmicroelectronics',
                   'nrfconnect', 'nordicsemiconductor', 'm5stack', 'xinyuan-lilygo'}


def source_type(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if host == 'pypi.org' and parts.path == '/pypi/platformio/json':
        return 'official_registry'
    if any(host == d or host.endswith('.' + d) for d in MANUFACTURERS):
        return 'official_datasheet' if parts.path.lower().endswith('.pdf') else 'manufacturer'
    if any(host == d or host.endswith('.' + d) for d in FRAMEWORKS):
        return 'official_framework'
    if host in ('github.com', 'raw.githubusercontent.com') and parts.path.split('/')[1].lower() in OFFICIAL_GITHUB:
        return 'official_repository'
    return 'community'


def rank(url: str) -> int:
    return {'manufacturer': 100, 'official_datasheet': 99, 'official_repository': 90,
            'official_framework': 85, 'official_registry': 95, 'community': 10}[source_type(url)]
