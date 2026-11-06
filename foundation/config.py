"""Safe local configuration: credentials are environment names, never values."""
import json
from dataclasses import fields

from .echo import EchoBackend
from .host_backends import DoTBackend, CloudWorkBackend
from .providers import ApiProvider, ProviderConfig


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_configuration_key")
        result[key] = value
    return result


def load_config(path):
    with open(path, "r", encoding="utf-8") as source:
        raw = source.read(65537)
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("configuration_too_large")
    config = json.loads(raw, object_pairs_hook=_unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_number")))
    if (not isinstance(config, dict) or set(config) != {"version", "default_backend", "backends"}
            or type(config["version"]) is not int or config["version"] != 1
            or not isinstance(config["backends"], dict) or not 1 <= len(config["backends"]) <= 16):
        raise ValueError("invalid_configuration")
    backends = {}
    allowed = {f.name for f in fields(ProviderConfig)} - {"backend_id"}
    for backend_id, options in config["backends"].items():
        if not isinstance(options, dict) or "type" not in options or "enabled" not in options or type(options["enabled"]) is not bool:
            raise ValueError("invalid_backend_configuration")
        kind = options["type"]
        keys = set(options) - {"type", "enabled"}
        if kind == "api":
            if keys - allowed:
                raise ValueError("unsupported_configuration_field")
            # Validate disabled profiles too, without resolving credentials.
            profile = ProviderConfig(backend_id=backend_id, **{k: options[k] for k in keys})
            backend = ApiProvider(profile)
        elif kind in ("dot", "work", "echo"):
            if keys or backend_id != kind:
                raise ValueError("invalid_builtin_configuration")
            backend = {"dot": DoTBackend, "work": CloudWorkBackend, "echo": EchoBackend}[kind]()
        else:
            raise ValueError("unsupported_backend_type")
        if options["enabled"]:
            backends[backend_id] = backend
    default = config["default_backend"]
    if not isinstance(default, str) or default not in config["backends"]:
        raise ValueError("default_backend_not_declared")
    return default, backends
