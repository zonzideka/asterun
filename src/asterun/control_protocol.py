"""Pinned runner-control/v1 wire validation and RFC 8785 digests.

Schema validation only establishes shape. Authorization, cross-object invariants,
state transitions, expiry and revision checks belong to the application service.
"""

from __future__ import annotations

from functools import lru_cache
from copy import deepcopy
import hashlib
from importlib.resources import files
import json
import math
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
import rfc8785

from .errors import AsterunError, INVALID_REQUEST


SCHEMA_VERSION = "runner-control/v1"
DRAFT_REVISION = "2026-09-10.draft1"
SCHEMA_SHA256 = "462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76"
MAX_COMMAND_BYTES = 1024 * 1024
_STATE_MACHINES_SHA256 = "99ceb7f0b8a30160d06085d0c2b645e618506a393941a7ebf78ff5c3bfa5ebb6"
_MAX_SAFE_INTEGER = 9007199254740991
_COMMAND_DIGEST_FIELDS = (
    "schema_version", "method", "params", "expected_revision", "expires_at",
)


def _invalid(message: str) -> AsterunError:
    return AsterunError(INVALID_REQUEST, message)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("JSON objects must not contain duplicate keys")
        result[key] = value
    return result


def _integer(raw: str) -> int:
    value = int(raw)
    if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
        raise _invalid("JSON integer exceeds the JavaScript safe integer range")
    return value


def _constant(raw: str) -> Any:
    raise _invalid("JSON numbers must be finite")


def _check_json(value: Any, ancestors: set[int] | None = None) -> None:
    """Apply I-JSON constraints also to values supplied by Python callers."""
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise _invalid("JSON strings must contain valid Unicode") from exc
        return
    if type(value) is int:
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise _invalid("JSON integer exceeds the JavaScript safe integer range")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid("JSON numbers must be finite")
        return
    if type(value) not in (dict, list):
        raise _invalid("Value is not a JSON data type")
    if ancestors is None:
        ancestors = set()
    identity = id(value)
    if identity in ancestors:
        raise _invalid("JSON data must not contain cycles")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise _invalid("JSON object keys must be strings")
                _check_json(key, ancestors)
                _check_json(child, ancestors)
        else:
            for child in value:
                _check_json(child, ancestors)
    finally:
        ancestors.remove(identity)


def loads(raw: str | bytes) -> Any:
    """Parse a bounded UTF-8 JSON wire message, without assuming its schema."""
    if type(raw) not in (str, bytes):
        raise _invalid("JSON input must be a string or UTF-8 bytes")
    try:
        encoded = raw.encode("utf-8", errors="strict") if isinstance(raw, str) else raw
        if len(encoded) > MAX_COMMAND_BYTES:
            raise AsterunError(
                INVALID_REQUEST,
                "JSON message exceeds the advertised byte limit",
                details={"max_command_bytes": MAX_COMMAND_BYTES},
            )
        # Decode explicitly: json.loads(bytes) also accepts UTF-16 and UTF-32.
        text = encoded.decode("utf-8", errors="strict")
        value = json.loads(
            text, object_pairs_hook=_object, parse_int=_integer, parse_constant=_constant,
        )
        _check_json(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _invalid("Malformed UTF-8 JSON message") from exc


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    raw = files("asterun").joinpath("schemas/runner-control-v1.schema.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
        raise RuntimeError("Packaged runner-control/v1 schema differs from the pinned draft")
    schema = json.loads(raw)
    Draft202012Validator.check_schema(schema)
    return schema


@lru_cache(maxsize=128)
def _validator(definition: str) -> Draft202012Validator:
    schema = _schema()
    if definition not in schema["$defs"]:
        raise _invalid("Unknown runner-control/v1 schema definition")
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    )


def validate(value: Any, definition: str) -> dict[str, Any]:
    """Validate an entity or envelope against a pinned Draft 2020-12 definition."""
    if not isinstance(definition, str):
        raise _invalid("Schema definition must be a string")
    try:
        _check_json(value)
        _validator(definition).validate(value)
        if definition == "Command" and len(rfc8785.dumps(value)) > MAX_COMMAND_BYTES:
            raise AsterunError(
                INVALID_REQUEST,
                "Command exceeds the advertised byte limit",
                details={"max_command_bytes": MAX_COMMAND_BYTES},
            )
    except ValidationError as exc:
        # ValidationError.message may include an entire prompt or secret value.
        raise AsterunError(
            INVALID_REQUEST,
            "Value does not conform to the pinned runner-control/v1 schema",
            details={"definition": definition, "keyword": exc.validator},
        ) from exc
    except (RecursionError, rfc8785.CanonicalizationError) as exc:
        raise _invalid("Value cannot be represented as canonical JSON") from exc
    return value


def digest(value: Any) -> str:
    """SHA-256 of RFC 8785 JCS bytes, without inventing JSON serialization rules."""
    try:
        _check_json(value)
        return hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except (RecursionError, rfc8785.CanonicalizationError) as exc:
        raise _invalid("Value cannot be represented as canonical JSON") from exc


def command_digest(command: dict[str, Any]) -> str:
    """Hash the five semantic command fields in SPEC §7.1.

    request_id is a transport identifier; idempotency_key is part of the lookup
    scope. Neither belongs to the semantic digest.
    """
    validate(command, "Command")
    return digest({key: command[key] for key in _COMMAND_DIGEST_FIELDS})


@lru_cache(maxsize=1)
def _state_machines() -> dict[str, Any]:
    raw = files("asterun").joinpath("schemas/state-machines.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != _STATE_MACHINES_SHA256:
        raise RuntimeError("Packaged state machines differ from the pinned draft")
    return json.loads(raw)["transitions"]


def transitions() -> dict[str, Any]:
    """Return draft transition edges; domain guards remain the runtime's duty."""
    return deepcopy(_state_machines())


def discovery() -> dict[str, Any]:
    """Base local profile; the application supplies its actual commands/backends.

    The frozen schema has no schema_sha256 property and disallows extra fields,
    so the pinned digest is published as a feature profile identifier.
    """
    return validate(
        {
            "schema_version": SCHEMA_VERSION,
            "draft_revision": DRAFT_REVISION,
            "supported_commands": ["task.create"],
            "feature_profiles": ["asterun-local-v1", f"schema-sha256:{SCHEMA_SHA256}"],
            "max_command_bytes": MAX_COMMAND_BYTES,
            "event_retention_seconds": 86400,
            "backend_ids": [],
        },
        "DiscoveryResponse",
    )
