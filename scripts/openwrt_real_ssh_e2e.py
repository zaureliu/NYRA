"""E2E REAL: Credential Broker -> AsyncSSH password -> configured OpenWrt -> ubus."""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.integrations.openwrt.config import load_config, resolve_password  # noqa: E402
from app.tools.password_ssh_executor import AsyncSSHPasswordExecutor  # noqa: E402


async def main() -> int:
    address = os.environ.get("NYRA_E2E_OPENWRT_ADDRESS", "").strip()
    if not address:
        print("RESULT=SKIP (configure NYRA_E2E_OPENWRT_ADDRESS locally)")
        return 0
    settings = get_settings()
    config = load_config(settings)
    password = resolve_password(settings)
    print(f"BROKER_CREDENTIAL_PRESENT={bool(password)}")
    if not password:
        print("RESULT=FAIL (sem credencial no broker)")
        return 2

    executor = AsyncSSHPasswordExecutor()
    for label, command in (("echo", "echo NYRA_SSH_OK"), ("ubus", "ubus call system info")):
        raw = await executor.execute(
            host_id="gateway",
            address=address,
            port=22,
            username=config.get("username") or "root",
            password=password,
            command=command,
            connect_timeout_seconds=settings.ssh_connect_timeout_seconds,
            command_timeout_seconds=15,
        )
        text = raw.stdout.decode("utf-8", "replace").strip()
        err = raw.stderr.decode("utf-8", "replace").strip()
        print(f"{label}: exit={raw.exit_code} stderr_marker={err[:80]!r}")
        print(f"{label}_STDOUT={text[:300]}")
        if not raw.exit_code == 0 or not text:
            print("RESULT=FAIL")
            return 1
    print("RESULT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
