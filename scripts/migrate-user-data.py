"""Upgrade the default local runtime without exporting credentials or audio."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.product_migration import default_runtime_root


if __name__ == '__main__':
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    root = default_runtime_root(base)
    print('Kazumi runtime migration verified: ' + str(root))
