"""Synthetic RFC wire fixtures; capability evidence is not live qualification."""
from __future__ import annotations

import struct
from ipaddress import IPv4Address
from typing import Any

import pytest

from ninja_pce.pcep import (
    MESSAGE_NAMES,
    PCINITIATE,
    PcepProtocolError,
    encode_object,
    encode_open,
    parse_objects,
    parse_open,
    parse_reports,
    parse_tlvs,
)
from ninja_pce.pcep_initiation import (
    MAX_BANDWIDTH_BPS,
    MAX_ERO_HOPS,
    encode_delete,
    encode_initiate,
    encoded_bandwidth,
    encoded_bandwidth_bps,
    supports_rsvp_initiation,
)

NAME = "NINJA_CREATE_001"
SOURCE, DESTINATION = "10.255.0.1", "10.255.0.6"
ERO = ("10.0.12.2", "10.0.25.5", "10.0.56.6")


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value + bytes(-len(value) % 4)


def _open(*extra: bytes, flags: int = 5) -> dict[str, Any]:
    return parse_open(encode_object(1, bytes.fromhex("201e7800") +
                                    _tlv(16, struct.pack("!I", flags)) + b"".join(extra)))


def _create(**changes: Any) -> bytes:
    arguments: dict[str, Any] = {"srp_id": 1, "symbolic_name": NAME, "source": SOURCE,
                                 "destination": DESTINATION, "ero": ERO, "bandwidth_bps": 1_000_000}
    arguments.update(changes)
    return encode_initiate(**arguments)


def test_rsvp_creation_exact_wire_and_required_object_order() -> None:
    # Independently specified RFC object/TLV fields, not an operational capture.
    expected = bytes.fromhex(
        "200c0078"
        "211000140000000000000001001c000400000000"
        "2010001c00000008001100104e494e4a415f4352454154455f303031"
        "0410000c0aff00010aff0006"
        "0710001c01080a000c02200001080a001905200001080a0038062000"
        "0910001400000000000000000000000007070000"
        "0510000847f42400"
    )
    message = _create()
    assert message == expected
    assert message[1] == PCINITIATE and MESSAGE_NAMES[PCINITIATE] == "PCInitiate"
    assert int.from_bytes(message[2:4], "big") == len(message)
    objects = parse_objects(message[4:])
    assert [(obj.object_class, obj.object_type, obj.flags) for obj in objects] == [
        (33, 1, 0), (32, 1, 0), (4, 1, 0), (7, 1, 0), (9, 1, 0), (5, 1, 0),
    ]
    assert parse_tlvs(objects[0].body[8:]) == ((28, bytes(4)),)
    assert int.from_bytes(objects[1].body[:4], "big") == 8  # A only; PLSP/C/D/O all zero.
    assert parse_tlvs(objects[1].body[4:]) == ((17, NAME.encode()),)
    assert struct.unpack("!f", objects[-1].body)[0] * 8 == 1_000_000
    with pytest.raises(PcepProtocolError):
        parse_objects(message[4:-1])


def test_owned_single_lsp_deletion_has_no_route_or_attribute_objects() -> None:
    expected = bytes.fromhex(
        "200c0034"
        "211000140000000100000002001c000400000000"
        "2010001c00011000001100104e494e4a415f4352454154455f303031"
    )
    message = encode_delete(2, 17, NAME)
    assert message == expected
    srp, lsp = parse_objects(message[4:])
    assert (srp.object_class, lsp.object_class) == (33, 32)
    assert int.from_bytes(srp.body[:4], "big") == 1
    assert int.from_bytes(lsp.body[:4], "big") == 17 << 12  # LSP.R remains clear.


@pytest.mark.parametrize("requested,expected", [
    (1, 1), (7, 7), (8, 8), (1_000_000, 1_000_000), (16_777_217, 16_777_218),
    (50_000_001, 50_000_004), (999_999_995_904, 999_999_995_904),
])
def test_bandwidth_rounds_to_least_float32_that_does_not_underreserve(
    requested: int, expected: int,
) -> None:
    body, actual = encoded_bandwidth(requested)
    assert len(body) == 4 and actual == expected == encoded_bandwidth_bps(requested)
    assert requested <= actual <= MAX_BANDWIDTH_BPS
    assert struct.unpack("!f", body)[0] * 8 == actual
    previous = (int.from_bytes(body, "big") - 1).to_bytes(4, "big")
    assert struct.unpack("!f", previous)[0] * 8 < requested
    bandwidth = parse_objects(_create(bandwidth_bps=requested)[4:])[-1]
    assert bandwidth.body == body


@pytest.mark.parametrize("amount", [0, -1, True, 1.0, float("nan"), float("inf"),
                                    MAX_BANDWIDTH_BPS, MAX_BANDWIDTH_BPS + 1, 1 << 200])
def test_invalid_or_unrepresentable_policy_bandwidth_rejected(amount: Any) -> None:
    with pytest.raises(ValueError):
        encoded_bandwidth(amount)


@pytest.mark.parametrize("changes", [
    {"srp_id": 0}, {"srp_id": -1}, {"srp_id": True}, {"srp_id": 0xFFFFFFFF},
    {"srp_id": 1 << 32}, {"symbolic_name": ""}, {"symbolic_name": "X" * 65},
    {"symbolic_name": "1_bad"}, {"symbolic_name": "NINJA bad"}, {"symbolic_name": "NINJA\x00x"},
    {"symbolic_name": "NINJA_é"}, {"symbolic_name": "NINJA;show"}, {"symbolic_name": None},
    {"source": "0.0.0.0"}, {"source": "127.0.0.1"}, {"source": "169.254.1.1"},
    {"source": "224.0.0.1"}, {"source": "255.255.255.255"}, {"source": "::1"},
    {"source": 123}, {"destination": SOURCE}, {"destination": " 10.255.0.6"},
    {"ero": ()}, {"ero": list(ERO)}, {"ero": (ERO[0], ERO[0])}, {"ero": ("10.0.12.2/32",)},
    {"ero": ("224.0.0.1",)}, {"ero": ("::1",)}, {"ero": (None,)},
    {"ero": tuple("10.0.12.2" for _ in range(MAX_ERO_HOPS + 1))},
    {"setup_priority": True}, {"setup_priority": 8}, {"hold_priority": -1},
    {"setup_priority": 0, "hold_priority": 7},
])
def test_unsupported_or_ambiguous_creation_inputs_reject(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _create(**changes)


@pytest.mark.parametrize("plsp", [0, -1, True, 0xFFFFF, 1 << 20, 1.0])
def test_deletion_never_encodes_wildcard_or_reserved_identity(plsp: Any) -> None:
    with pytest.raises(ValueError):
        encode_delete(1, plsp, NAME)


def test_identifier_name_and_priority_upper_bounds_encode_without_loss() -> None:
    message = _create(srp_id=0xFFFFFFFE, symbolic_name="N" * 64, setup_priority=7, hold_priority=0)
    objects = parse_objects(message[4:])
    assert int.from_bytes(objects[0].body[4:8], "big") == 0xFFFFFFFE
    assert parse_tlvs(objects[1].body[4:]) == ((17, b"N" * 64),)
    assert objects[-2].body[12:] == bytes((7, 0, 0, 0))
    assert int.from_bytes(parse_objects(encode_delete(1, 0xFFFFE, NAME)[4:])[1].body[:4], "big") == (
        0xFFFFE << 12
    )


def test_open_default_remains_passive_and_initiation_is_explicit_rsvp_only() -> None:
    assert encode_open().hex() == "2001001401100010201e78000010000400000000"
    assert parse_open(encode_open(updates=True)[4:])["stateful_flags"] == 1
    assert not supports_rsvp_initiation(parse_open(encode_open()[4:]))
    assert not supports_rsvp_initiation(parse_open(encode_open(updates=True)[4:]))
    opened = parse_open(encode_open(updates=True, initiation=True)[4:])
    assert opened["stateful_flags"] == 5
    assert opened["tlvs"] == [
        {"type": 16, "value_hex": "00000005"}, {"type": 34, "value_hex": "0000000100"},
    ]
    assert supports_rsvp_initiation(opened)
    for options in ({"initiation": True}, {"stateful": False, "updates": True, "initiation": True},
                    {"updates": True, "initiation": 1}):
        with pytest.raises(ValueError):
            encode_open(**options)


def test_base_rsvp_and_captured_legacy_capabilities_do_not_claim_platform_qualification() -> None:
    assert supports_rsvp_initiation(_open())
    # Existing actual IOS XE 16.9.7 OPEN, reused only as capability-format evidence.
    captured = bytes.fromhex("2001001c01100018202878010010000400000005001a000400000010")
    assert supports_rsvp_initiation(parse_open(captured[4:]))
    assert not supports_rsvp_initiation(_open(flags=4))  # Local profile requires U as well as I.
    assert not supports_rsvp_initiation(_open(flags=1))


@pytest.mark.parametrize("value,accepted", [
    (b"", False), (bytes(3), False), (bytes(5), False), (bytes(4), False),
    (bytes.fromhex("00000200"), False),  # N does not exempt the zero MSD check.
    (bytes.fromhex("00000010"), True), (bytes.fromhex("00000100"), True),
    (bytes.fromhex("000001ff"), True),  # X makes MSD irrelevant.
    (bytes.fromhex("fffffc10"), True),  # Reserved fields ignored on receipt.
])
def test_legacy_capability_validates_length_and_x_msd_semantics(value: bytes, accepted: bool) -> None:
    assert supports_rsvp_initiation(_open(_tlv(26, value))) is accepted


@pytest.mark.parametrize("value,accepted", [
    (bytes(4), False), (bytes.fromhex("0000000100"), True),
    (bytes.fromhex("ffffff0100"), True),  # Reserved bytes ignored.
    (bytes.fromhex("000000020000"), True),  # Duplicate PST entries ignored.
    (bytes.fromhex("000000020001"), False),  # SR requires its nested capability.
    (bytes.fromhex("0000000101"), False), (bytes.fromhex("0000000200"), False),
    (bytes.fromhex("000000010000"), False),  # Incorrect declared padding/length.
    (bytes.fromhex("0000000200010000001a000400000010"), True),
    (bytes.fromhex("0000000200010000001a000400000000"), False),
    (bytes.fromhex("0000000200010000001a000300001000"), False),
    (bytes.fromhex("00000001000000001234000400000000"), False),  # Unknown setup sub-TLV.
])
def test_modern_pst_capability_requires_explicit_rsvp_and_valid_supported_nested_form(
    value: bytes, accepted: bool,
) -> None:
    assert supports_rsvp_initiation(_open(_tlv(34, value))) is accepted


def test_modern_capability_precedence_and_first_capability_rules() -> None:
    rsvp = _tlv(34, bytes.fromhex("0000000100"))
    sr_only = _tlv(34, bytes.fromhex("0000000101"))
    legacy = _tlv(26, bytes.fromhex("00000010"))
    assert not supports_rsvp_initiation(_open(sr_only, legacy))
    assert supports_rsvp_initiation(_open(rsvp, _tlv(26, b"malformed")))
    assert supports_rsvp_initiation(_open(rsvp, sr_only))
    assert not supports_rsvp_initiation(_open(sr_only, rsvp))
    assert supports_rsvp_initiation(_open(legacy, _tlv(26, bytes(4))))
    assert not supports_rsvp_initiation(_open(_tlv(26, bytes(4)), legacy))


@pytest.mark.parametrize("peer", [
    None, [], {}, {"stateful": False}, {"stateful": True, "stateful_flags": True},
    {"stateful": True, "stateful_flags": 5, "tlvs": None},
    {"stateful": True, "stateful_flags": 5, "tlvs": []},
    {"stateful": True, "stateful_flags": 5, "tlvs": [None]},
    {"stateful": True, "stateful_flags": 5, "tlvs": [{"type": 16, "value_hex": "0z"}]},
    {"stateful": True, "stateful_flags": 5, "tlvs": [{"type": 16, "value_hex": "00000001"}]},
    {"stateful": True, "stateful_flags": 5, "tlvs": [{"type": True, "value_hex": "00000005"}]},
])
def test_malformed_parsed_capability_data_fails_closed_without_raising(peer: Any) -> None:
    assert not supports_rsvp_initiation(peer)


@pytest.mark.parametrize("created", [False, True])
def test_pcrpt_create_flag_is_preserved_separately_from_state_and_delegation(created: bool) -> None:
    identifiers = (IPv4Address(SOURCE).packed + struct.pack("!HH", 3, 101) +
                   IPv4Address(SOURCE).packed + IPv4Address(DESTINATION).packed)
    bits = (17 << 12) | 1 | 8 | (2 << 4) | (128 if created else 0)
    report_body = (encode_object(33, struct.pack("!II", 0, 1)) +
                   encode_object(32, struct.pack("!I", bits) + _tlv(17, NAME.encode()) + _tlv(18, identifiers)) +
                   encode_object(7, bytes.fromhex("01080a000c022000")))
    report = parse_reports(report_body, allow_delegation=True)[0]
    assert report["created"] is created
    assert report["delegate"] is True and report["removed"] is False and report["sync"] is False
    assert report["administrative_up"] is True and report["operational_state"] == "ACTIVE"
    assert report["plsp_id"] == 17 and report["srp_id"] == 1
    assert report["identifiers"]["tunnel_id"] == 101
