import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Contract/unit tests exercise these lifecycles explicitly. Generic TestClient
# lifespans must not load real Ollama or Faster-Whisper models as side effects.
os.environ.setdefault("NYRA_OLLAMA_PRELOAD", "false")
os.environ.setdefault("NYRA_CONVERSATION_ENGINE", "false")
os.environ.setdefault("NYRA_TESTING", "true")

# ---------------------------------------------------------------------------
# Hermetic suite (final release closure): operator-persisted UI state in
# data/settings-v33.json must never change unit/integration outcomes. These
# flags have contract defaults ("default OFF"); tests that need them enabled
# pass explicit Settings.from_sources(...) overrides, which win over env.
# ---------------------------------------------------------------------------
os.environ.setdefault("NYRA_AGENT_READ_ONLY", "false")
os.environ.setdefault("NYRA_PROACTIVE_OPERATOR_ENABLED", "false")
_TEST_SESSION_ROOT = Path(tempfile.gettempdir()) / "nyra-pytest"
os.environ.setdefault("NYRA_DATA_HOME", str(_TEST_SESSION_ROOT / "runtime"))
os.environ.setdefault(
    "NYRA_SELFDEV_WORKSPACE",
    str(_TEST_SESSION_ROOT / "selfdev" / "workspace"),
)
os.environ.setdefault(
    "NYRA_PUBLIC_SNAPSHOT",
    str(_TEST_SESSION_ROOT / "selfdev" / "public"),
)

# ---------------------------------------------------------------------------
# Controlled basetemp under the hermetic NYRA test runtime. Generic
# %TEMP%\pytest-* can hit Access Denied on Windows shell operations (Explorer
# window verification), but mutable test state must never live in the source
# repository. Grant only the current user access to this dedicated directory.
# ---------------------------------------------------------------------------

_TEST_TEMP_ROOT = Path(os.environ["NYRA_DATA_HOME"]) / "tmp" / "pytest-root"


def _grant_current_user_acl(directory: Path) -> bool:
    """icacls grant for the current user ONLY on this project folder."""
    try:
        completed = subprocess.run(  # noqa: S603
            ["icacls.exe", str(directory),
             "/grant", f"{os.environ.get('USERNAME', 'CURRENT_USER')}:(OI)(CI)F"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_configure(config) -> None:
    if config.option.basetemp:
        return  # operador mandou o basetemp explicitamente: respeito total
    try:
        _TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _grant_current_user_acl(_TEST_TEMP_ROOT)
    config.option.basetemp = str(_TEST_TEMP_ROOT / "pytest")
