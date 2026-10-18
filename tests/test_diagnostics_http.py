"""Authentication, explicit live scope and responsive independent management."""
from __future__ import annotations

import threading

import pytest
from test_admin_http import admin_http as admin_http

from ninja_pce import diagnostics


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("route", ["/api/admin/diagnostics", "/api/diagnostics"])
def test_diagnostics_do_not_accept_unauthenticated_or_proxy_header_access(admin_http, method, route, monkeypatch):
    monkeypatch.setattr(diagnostics, "admin_report", lambda *args, **kwargs: pytest.fail("Unauthenticated diagnostics"))
    status, _, _ = admin_http.request(route, method=method, body={"live": True},
                                     headers=[("X-Ninja-Principal", "administrator")])
    assert status == 401


def test_protected_dashboard_diagnostics_alias_uses_only_snapshot_scope(admin_http, monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics, "admin_report", lambda *args, **kwargs: calls.append(kwargs) or diagnostics.report([]))
    admin_http.claim()
    status, value, _ = admin_http.request("/api/diagnostics")
    assert status == 200 and value["scope"] == "snapshot"
    assert calls == [{"authenticated": True}]
    assert admin_http.request("/api/diagnostics", method="POST", body={"live": True})[0] == 404
    assert len(calls) == 1


def test_snapshot_and_explicit_live_diagnostics_have_the_existing_csrf_boundary(admin_http, monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics, "admin_report", lambda *args, **kwargs: calls.append(kwargs) or
                        diagnostics.report([diagnostics.finding("TEST_CHECK", "info", "runtime", "Read only", "Review")], live=kwargs.get("live", False)))
    admin_http.claim()
    assert admin_http.request("/api/admin/diagnostics")[0] == 200
    assert calls[-1].get("live", False) is False
    assert admin_http.request("/api/admin/diagnostics", method="POST", body={"live": True}, csrf=None)[0] == 403
    assert admin_http.request("/api/admin/diagnostics", method="POST", body={"live": True}, origin="http://attacker.invalid")[0] == 403
    assert len(calls) == 1
    status, value, _ = admin_http.request("/api/admin/diagnostics", method="POST", body={"live": True})
    assert status == 200 and value["scope"] == "live" and calls[-1]["authenticated"] is True
    status, value, _ = admin_http.request("/api/admin/diagnostics", method="POST", body={"live": True})
    assert status == 200 and value["findings"][0]["code"] == "DIAGNOSTICS_RATE_LIMITED"
    assert len(calls) == 2


@pytest.mark.parametrize("body", [{"live": "true"}, {"live": 1}, {"live": None}, {}, {"live": True, "repair": True},
                                  {"live": True, "pid": 123}, {"live": True, "unit": "other.service"}])
def test_diagnostic_requests_cannot_select_repair_processes_or_services(admin_http, monkeypatch, body):
    admin_http.claim()
    monkeypatch.setattr(diagnostics, "admin_report", lambda *args, **kwargs: pytest.fail("Invalid diagnostic operation"))
    assert admin_http.request("/api/admin/diagnostics", method="POST", body=body)[0] == 400


def test_management_stays_available_while_a_live_diagnostic_probe_is_waiting(admin_http, monkeypatch):
    admin_http.claim()
    entered, release = threading.Event(), threading.Event()
    result = []
    def inspect(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return diagnostics.report([], live=True)
    monkeypatch.setattr(diagnostics, "admin_report", inspect)
    worker = threading.Thread(target=lambda: result.append(admin_http.request("/api/admin/diagnostics", method="POST", body={"live": True})))
    worker.start()
    try:
        assert entered.wait(2)
        assert admin_http.request("/healthz")[0] == 200
        assert admin_http.request("/api/admin/session")[0] == 200
        status, value, _ = admin_http.request("/api/admin/diagnostics", method="POST", body={"live": True})
        assert status == 200 and value["findings"][0]["code"] == "DIAGNOSTICS_BUSY"
    finally:
        release.set()
        worker.join(timeout=3)
    assert result[0][0] == 200


def test_busy_configuration_diagnostics_never_mix_revisions_or_wait_for_activation(admin_http):
    admin_http.claim()
    entered, release = threading.Event(), threading.Event()
    def operation():
        with admin_http.store.locked():
            entered.set()
            release.wait(3)
    worker = threading.Thread(target=operation)
    worker.start()
    try:
        assert entered.wait(2)
        status, value, _ = admin_http.request("/api/admin/diagnostics")
        assert status == 200 and value["findings"][0]["code"] == "DIAGNOSTICS_CONFIGURATION_BUSY"
        assert value["overall"] == "unknown" and not value["complete"]
        assert admin_http.request("/healthz")[0] == 200
    finally:
        release.set()
        worker.join(timeout=3)
