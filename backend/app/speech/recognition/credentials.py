from app.operator.credentials import CredentialBroker, CredentialError


class STTCredentialBroker:
    """Backend-only, fixed-identifier access to the official credential vault."""

    credential_id = "deepgram_api_key"

    def __init__(self, broker: CredentialBroker | None):
        self._broker = broker

    def configured(self) -> bool:
        # Windows Credential Manager may outlive the broker's in-memory index.
        return bool(self.resolve())

    def resolve(self) -> str | None:
        return self._broker.resolve(self.credential_id) if self._broker else None

    def save(self, secret: str) -> None:
        if not self._broker:
            raise CredentialError("BROKER_DISABLED", "Credential Broker unavailable")
        self._broker.create(self.credential_id, secret, kind="stt_api_key",
                            description="Deepgram cloud speech recognition", operator_direct=True)

    def remove(self) -> None:
        if self._broker:
            self._broker.delete(self.credential_id, operator_direct=True)
