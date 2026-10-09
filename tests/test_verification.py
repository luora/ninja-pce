from pathlib import Path

import pytest

from ninja_pce.verification import CliOperationalVerifier, VerificationExpectation

FIXTURES = Path(__file__).parent / "fixtures" / "operational"
VERSIONS = {
    "iosxr": "Cisco IOS XR Software, Version 7.9.2",
    "iosxe": "Cisco IOS XE Software, Version 17.12.04",
    "junos": "Junos: 23.4R2.13",
}
FILES = {
    "iosxr": "iosxr_7.9_up.mock.txt",
    "iosxe": "iosxe_17.12_up.mock.txt",
    "junos": "junos_23.4_up.mock.txt",
}
COMMANDS = {
    "iosxr": "show mpls traffic-eng tunnels 100 detail",
    "iosxe": "show mpls traffic-eng tunnels tunnel 100",
    "junos": "show mpls lsp name lsp-customer-a-pe1-pe3 extensive",
}


def expectation(fmt):
    return VerificationExpectation(
        {"iosxr": "tunnel-te100", "iosxe": "Tunnel100", "junos": "lsp-customer-a-pe1-pe3"}[fmt],
        "10.255.0.6",
        50_000_000,
        (("strict", "10.0.13.3"), ("strict", "10.0.34.4"), ("strict", "10.0.46.6")),
    )


@pytest.mark.parametrize("fmt", VERSIONS)
def test_complete_mock_operational_proof(fmt):
    text = (FIXTURES / FILES[fmt]).read_text()
    result = CliOperationalVerifier().verify(fmt, VERSIONS[fmt], {COMMANDS[fmt]: text}, expectation(fmt))
    assert result.verified, result.errors


@pytest.mark.parametrize("fmt", VERSIONS)
@pytest.mark.parametrize(
    "change", ["down", "wrong_dest", "wrong_identity", "wrong_bw", "wrong_hop", "empty", "accepted"]
)
def test_missing_or_negative_evidence_never_verifies(fmt, change):
    text = (FIXTURES / FILES[fmt]).read_text()
    expected = expectation(fmt)
    if change == "down":
        text = text.replace("Oper: up", "Oper: down").replace("LSPstate: Up", "LSPstate: Dn")
    elif change == "wrong_dest":
        text = text.replace(expected.destination, "10.255.0.66")
    elif change == "wrong_identity":
        text = text.replace(expected.identity, "other-lsp")
    elif change == "wrong_bw":
        text = text.replace("50000", "40000")
    elif change == "wrong_hop":
        text = text.replace("10.0.34.4", "10.0.54.4")
    elif change == "empty":
        text = ""
    else:
        text = "configuration accepted; all commands succeeded"
    assert not CliOperationalVerifier().verify(fmt, VERSIONS[fmt], {COMMANDS[fmt]: text}, expected).verified


@pytest.mark.parametrize("fmt", VERSIONS)
def test_unsupported_version_fails_closed(fmt):
    text = (FIXTURES / FILES[fmt]).read_text()
    assert (
        not CliOperationalVerifier()
        .verify(fmt, "Version unknown", {COMMANDS[fmt]: text}, expectation(fmt))
        .verified
    )


def test_ero_taken_from_current_lsp_not_historical_configuration():
    text = (FIXTURES / FILES["iosxr"]).read_text().replace("Current LSP Info:", "Previous LSP Info:")
    result = CliOperationalVerifier().verify(
        "iosxr", VERSIONS["iosxr"], {COMMANDS["iosxr"]: text}, expectation("iosxr")
    )
    assert not result.verified


def test_multiple_lsp_responses_are_ambiguous():
    text = (FIXTURES / FILES["iosxr"]).read_text()
    result = CliOperationalVerifier().verify(
        "iosxr", VERSIONS["iosxr"], {COMMANDS["iosxr"]: text + text}, expectation("iosxr")
    )
    assert not result.verified


def test_junos_matching_computed_ero_cannot_hide_mismatched_actual_rro():
    text = (FIXTURES / FILES["junos"]).read_text().replace("10.0.34.4 10.0.46.6", "10.0.54.4 10.0.46.6")
    result = CliOperationalVerifier().verify(
        "junos", VERSIONS["junos"], {COMMANDS["junos"]: text}, expectation("junos")
    )
    assert not result.verified


@pytest.mark.parametrize("fmt", VERSIONS)
def test_ambiguous_version_evidence_fails_closed(fmt):
    text = (FIXTURES / FILES[fmt]).read_text()
    result = CliOperationalVerifier().verify(
        fmt, VERSIONS[fmt] + "\n" + VERSIONS[fmt], {COMMANDS[fmt]: text}, expectation(fmt)
    )
    assert not result.verified


@pytest.mark.parametrize("field", ["Tspec", "Fspec"])
def test_xr_configured_bandwidth_cannot_hide_missing_or_mismatched_signaling(field):
    text = (FIXTURES / FILES["iosxr"]).read_text().replace(f"{field}: avg rate=50000", f"{field}: avg rate=0")
    result = CliOperationalVerifier().verify(
        "iosxr", VERSIONS["iosxr"], {COMMANDS["iosxr"]: text}, expectation("iosxr")
    )
    assert not result.verified
