from __future__ import annotations

import re
import hashlib

from app.operator.credentials import CredentialBroker, CredentialError


TTS_CREDENTIAL_IDS = {
    "openai": "tts_openai_api_key",
    "elevenlabs": "tts_elevenlabs_api_key",
    "gradium": "gradium_api_key",
}


class TtsCredentialBroker:
    """Narrow, provider-bound access to KAZUMI's official Credential Broker."""

    def __init__(self, broker: CredentialBroker | None) -> None:
        self._broker = broker

    @property
    def available(self) -> bool:
        return self._broker is not None

    @staticmethod
    def credential_id(provider_id: str) -> str:
        if re.fullmatch(r"custom:[a-z0-9][a-z0-9_-]{0,47}", provider_id):
            return "custom_tts_" + hashlib.sha256(provider_id.encode()).hexdigest()[:32]
        try:
            return TTS_CREDENTIAL_IDS[provider_id]
        except KeyError as exc:
            raise CredentialError("PROVIDER_NOT_AUTHORIZED", "Provider sem credencial TTS autorizada.") from exc

    def has_credential(self, provider_id: str) -> bool:
        if self._broker is None:
            return False
        # The protected vault survives restart; the metadata index does not.
        # Resolve only this authorized provider ID, returning existence only.
        return bool(self._broker.resolve(self.credential_id(provider_id)))

    def save_credential(self, provider_id: str, secret: str) -> dict:
        if self._broker is None:
            raise CredentialError("BROKER_DISABLED", "Credential Broker desabilitado.")
        credential_id = self.credential_id(provider_id)
        # A click on "Save securely" is the direct local operator action. The
        # official broker owns both create and replacement semantics.
        result = self._broker.create(
            credential_id,
            secret,
            kind="tts_api_key",
            description=f"{provider_id} online voice provider",
            operator_direct=True,
        )
        return {"success": bool(result.get("success")), "credential_id": credential_id, "configured": True}

    def delete_credential(self, provider_id: str) -> dict:
        if self._broker is None:
            raise CredentialError("BROKER_DISABLED", "Credential Broker desabilitado.")
        result = self._broker.delete(self.credential_id(provider_id), operator_direct=True)
        return {"success": bool(result.get("success")), "configured": False}

    def get_for_authorized_provider(self, provider_id: str) -> str | None:
        """Raw secret escape hatch for the matching backend adapter only."""
        if self._broker is None:
            return None
        return self._broker.resolve(self.credential_id(provider_id))
