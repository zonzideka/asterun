from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.resources import files
import json
import struct

import pytest

from asterun.control_protocol import (
    DRAFT_REVISION,
    MAX_COMMAND_BYTES,
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    command_digest,
    digest,
    discovery,
    loads,
    validate,
)
from asterun.errors import AsterunError, INVALID_REQUEST


@pytest.fixture
def command() -> dict:
    return {
        "schema_version": "runner-control/v1",
        "request_id": "req_start001",
        "idempotency_key": "demo-run-start-0001",
        "expected_revision": 2,
        "expires_at": "2026-09-10T00:05:00Z",
        "method": "run.start",
        "params": {
            "task_id": "tsk_demo001",
            "workflow_id": "wf_review001",
            "workflow_revision": 1,
            "configuration_sha256": "a" * 64,
        },
    }


def test_schema_bytes_and_discovery_are_pinned() -> None:
    raw = files("asterun").joinpath("schemas/runner-control-v1.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SCHEMA_SHA256
    assert SCHEMA_SHA256 == "462f0956ca7f7997cc472ffb73e619111c66f25318cb383b677213880f23fe76"
    found = discovery()
    assert found["schema_version"] == SCHEMA_VERSION == "runner-control/v1"
    assert found["draft_revision"] == DRAFT_REVISION == "2026-09-10.draft1"
    assert f"schema-sha256:{SCHEMA_SHA256}" in found["feature_profiles"]
    assert found["max_command_bytes"] == MAX_COMMAND_BYTES == 1048576
    assert validate(found, "DiscoveryResponse") == found


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1,"a":2}',
        '{"a":1,"\\u0061":2}',
        '{"nested":{"a":1,"a":2}}',
        'NaN', 'Infinity', '-Infinity', '1e999', '-1e999',
        '9007199254740992', '-9007199254740992',
        '"\\ud800"', '{"\\udead":1}', '"\ud800"',
        b'"\xff"', '{"a":1}'.encode("utf-16"), b'\xef\xbb\xbf{}',
        '{"unfinished":', '1 2',
        '[' * 2000 + '0' + ']' * 2000,
    ],
)
def test_loads_rejects_ambiguous_or_non_json_messages(raw: str | bytes) -> None:
    with pytest.raises(AsterunError) as caught:
        loads(raw)
    assert caught.value.code == INVALID_REQUEST


def test_loads_preserves_unicode_and_safe_boundaries() -> None:
    raw = '{"text":"中文😀","values":[null,true,false,9007199254740991,-9007199254740991]}'
    assert loads(raw) == loads(raw.encode("utf-8")) == json.loads(raw)
    assert loads('"\\ud83d\\ude00"') == "😀"
    # RFC 8785 uses IEEE 754 for non-integer JSON tokens, including large floats.
    assert loads('1e30') == 1e30


def test_wire_limit_counts_utf8_bytes_before_parsing() -> None:
    exactly = '"' + 'a' * (MAX_COMMAND_BYTES - 2) + '"'
    assert len(loads(exactly)) == MAX_COMMAND_BYTES - 2
    for raw in (exactly + ' ', '"' + '中' * (MAX_COMMAND_BYTES // 3) + '"'):
        with pytest.raises(AsterunError) as caught:
            loads(raw)
        assert caught.value.details == {"max_command_bytes": MAX_COMMAND_BYTES}


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 2**53, {1: "bad"}, (1, 2), "\ud800"])
def test_python_callers_cannot_bypass_json_constraints(value: object) -> None:
    for check in (digest, lambda item: validate(item, "JsonValue")):
        with pytest.raises(AsterunError) as caught:
            check(value)
        assert caught.value.code == INVALID_REQUEST


def test_recursive_objects_fail_with_public_error() -> None:
    value: dict = {}
    value["self"] = value
    with pytest.raises(AsterunError) as caught:
        digest(value)
    assert caught.value.code == INVALID_REQUEST


def test_validate_does_not_mutate_command(command: dict) -> None:
    before = deepcopy(command)
    assert validate(command, "Command") == before
    assert command == before


@pytest.mark.parametrize(
    "path,value",
    [
        (("unknown",), "unexpected"),
        (("params", "unknown"), "unexpected"),
        (("params", "workflow_revision"), True),
        (("params", "workflow_revision"), 0),
        (("params", "configuration_sha256"), "invalid"),
        (("expected_revision",), 9007199254740992.0),
        (("request_id",), "native-provider-session-id"),
        (("method",), "task.set_status"),
        (("schema_version",), "runner-control/v2"),
        (("expires_at",), "2026-02-30T00:00:00Z"),
        (("expires_at",), "2026-09-10T00:00:00+00:00"),
        (("expires_at",), "not-a-dateZ"),
    ],
)
def test_full_schema_rejects_invalid_command_fields(command: dict, path: tuple, value: object) -> None:
    parent = command
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    with pytest.raises(AsterunError) as caught:
        validate(command, "Command")
    assert caught.value.code == INVALID_REQUEST


def test_schema_error_does_not_echo_request_body(command: dict) -> None:
    command["secret"] = "sensitive-request-body"
    with pytest.raises(AsterunError) as caught:
        validate(command, "Command")
    assert "sensitive-request-body" not in json.dumps(caught.value.to_dict())


def test_direct_command_validation_also_enforces_size(command: dict) -> None:
    command["method"] = "state.put"
    command["params"] = {
        "task_id": "tsk_demo001", "key": "domain.example", "value": "x" * MAX_COMMAND_BYTES,
        "assertion_class": "observation", "evidence_ids": [],
    }
    with pytest.raises(AsterunError) as caught:
        validate(command, "Command")
    assert caught.value.details == {"max_command_bytes": MAX_COMMAND_BYTES}


def test_unknown_definition_is_rejected_locally() -> None:
    with pytest.raises(AsterunError) as caught:
        validate({}, "https://example.invalid/schema")
    assert caught.value.code == INVALID_REQUEST


def test_rfc8785_serialization_example() -> None:
    # RFC 8785 §§3.2.2–3.2.4: source JSON and canonical UTF-8 byte vector.
    # https://www.rfc-editor.org/rfc/rfc8785.html#section-3.2.4
    source = r'''{
      "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
      "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
      "literals": [null, true, false]
    }'''
    canonical = bytes.fromhex(
        "7b 22 6c 69 74 65 72 61 6c 73 22 3a 5b 6e 75 6c 6c 2c 74 72 "
        "75 65 2c 66 61 6c 73 65 5d 2c 22 6e 75 6d 62 65 72 73 22 3a "
        "5b 33 33 33 33 33 33 33 33 33 2e 33 33 33 33 33 33 33 2c 31 "
        "65 2b 33 30 2c 34 2e 35 2c 30 2e 30 30 32 2c 31 65 2d 32 37 "
        "5d 2c 22 73 74 72 69 6e 67 22 3a 22 e2 82 ac 24 5c 75 30 30 "
        "30 66 5c 6e 41 27 42 5c 22 5c 5c 5c 5c 5c 22 2f 22 7d"
    )
    assert digest(loads(source)) == hashlib.sha256(canonical).hexdigest()


def test_rfc8785_utf16_property_sorting_example() -> None:
    # The official ordering vector includes an astral character before U+FB33.
    # https://www.rfc-editor.org/rfc/rfc8785.html#section-3.2.3
    source = {
        "€": "Euro Sign", "\r": "Carriage Return", "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One", "😀": "Emoji: Grinning Face", "\x80": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    canonical = (
        '{"\\r":"Carriage Return","1":"One","\x80":"Control",'
        '"ö":"Latin Small Letter O With Diaeresis","€":"Euro Sign",'
        '"😀":"Emoji: Grinning Face","דּ":"Hebrew Letter Dalet With Dagesh"}'
    ).encode("utf-8")
    assert digest(source) == hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize(
    "binary,canonical",
    [
        ("0000000000000000", "0"),
        ("8000000000000000", "0"),
        ("0000000000000001", "5e-324"),
        ("8000000000000001", "-5e-324"),
        ("7fefffffffffffff", "1.7976931348623157e+308"),
        ("ffefffffffffffff", "-1.7976931348623157e+308"),
        ("44b52d02c7e14af5", "9.999999999999997e+22"),
        ("44b52d02c7e14af6", "1e+23"),
        ("44b52d02c7e14af7", "1.0000000000000001e+23"),
        ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
        ("3eb0c6f7a0b5ed8d", "0.000001"),
        ("41b3de4355555555", "333333333.3333333"),
        ("becbf647612f3696", "-0.0000033333333333333333"),
    ],
)
def test_rfc8785_official_number_vectors(binary: str, canonical: str) -> None:
    # https://www.rfc-editor.org/rfc/rfc8785.html#appendix-B
    value = struct.unpack(">d", bytes.fromhex(binary))[0]
    assert digest(value) == hashlib.sha256(canonical.encode("ascii")).hexdigest()


def test_command_digest_uses_exact_normative_fields(command: dict) -> None:
    semantic = {key: command[key] for key in (
        "schema_version", "method", "params", "expected_revision", "expires_at",
    )}
    original = command_digest(command)
    assert original == digest(semantic)
    command["request_id"] = "req_another001"
    command["idempotency_key"] = "another-key-value-0001"
    assert command_digest(command) == original
    command["params"] = dict(reversed(list(command["params"].items())))
    assert command_digest(command) == original
    command["expires_at"] = "2026-09-10T00:06:00Z"
    assert command_digest(command) != original


def test_canonical_numbers_and_unicode_are_not_naive_json_dump() -> None:
    assert digest({"a": 1}) == digest({"a": 1.0})
    assert digest({"a": 0}) == digest({"a": -0.0})
    assert digest("é") != digest("e\u0301")
