from __future__ import annotations

import ssl

from app.integrations.proxmox.client import ProxmoxReadOnlyClient


def test_uses_private_ca_from_localappdata_when_present(tmp_path, monkeypatch):
    ca_file = tmp_path / "KAZUMI" / "certs" / "proxmox-root-ca.pem"
    ca_file.parent.mkdir(parents=True)
    ca_file.write_text("test CA", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    expected_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    seen: dict[str, str] = {}

    def create_context(*, cafile):
        seen["cafile"] = cafile
        return expected_context

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    client = ProxmoxReadOnlyClient(
        "https://proxmox.example.local:8006", "user@pve!kazumi", "secret"
    )

    assert client._httpx_verify() is expected_context
    assert seen["cafile"] == str(ca_file)
    assert expected_context.verify_mode == ssl.CERT_REQUIRED
    assert expected_context.check_hostname is True


def test_uses_system_trust_when_private_ca_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    client = ProxmoxReadOnlyClient(
        "https://proxmox.example.local:8006", "user@pve!kazumi", "secret"
    )

    assert client._httpx_verify() is True
