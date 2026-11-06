"""Deterministic management syntax; parsing never grants authority."""
import re

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def valid_name(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def management_command(text):
    """Return exact slash command data. Quotes and identity are checked by core."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if parts == ["/status"]:
        return ("status", None)
    if parts == ["/new"]:
        return ("new_session", None)
    if len(parts) == 2 and parts[0] in ("/backend", "/model", "/stop") and valid_name(parts[1]):
        return ({"/backend": "backend", "/model": "model", "/stop": "stop"}[parts[0]], parts[1])
    return None
