"""NapCat (OneBot11) reverse-WebSocket adapter: pure codec plus the WS server."""

from cgbot.napcat.codec import build_send, parse_event
from cgbot.napcat.server import NapCatError, NapCatServer, NotConnectedError

__all__ = [
    "NapCatError",
    "NapCatServer",
    "NotConnectedError",
    "build_send",
    "parse_event",
]
