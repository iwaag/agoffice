from typing import Any, Protocol


class ChatTransport(Protocol):
    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send one structured server event to the client."""

    async def close(self) -> None:
        """Close the underlying realtime connection."""
