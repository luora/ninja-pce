"""Both packaged UIs expose causes and manual guidance without stale claims."""
from __future__ import annotations

import pytest
import test_admin_gui as setup_ui
import test_dashboard_creation_ui as dashboard_ui

REPORT = r"""
let diagnosticUnavailable=false, diagnosticStale=false, lateDiagnostic=false;
let managementUnavailable=false;
const diagnosticCalls=[];
const diagnosticReport=(live=false)=>({schema_version:1,generated_at:new Date(Date.now()-(diagnosticStale?60000:0)).toISOString(),
 scope:live?"live":"snapshot",overall:"blocked",read_only:true,complete:false,operating_mode:"observe",
 summary:{blocker:1,unknown:1,warning:0,ok:0,info:0},control_authority_verified:false,forwarding_verified:false,
 findings:[{code:"GOBGP_PORT_IN_USE",severity:"blocker",category:"runtime",message:"Existing owner occupies port 50051.",
  evidence:{pid:1234,note:"<img src=x onerror=alert(1)>"},remediation:"Review the managed owner before restarting.",observed_at:new Date().toISOString()},
  {code:"CLOCK_UNKNOWN",severity:"unknown",category:"host",message:"Time synchronization is unknown.",evidence:{},remediation:"Review host time.",observed_at:new Date().toISOString()}],
 limitations:["Read-only; no process was stopped."]});
const previousFetch=window.fetch;
window.fetch=async(path,options={})=>{
 if(path==="/api/controller"&&managementUnavailable)throw new Error("Synthetic management outage");
 if(path==="/api/diagnostics"||path==="/api/admin/diagnostics"){
  if(diagnosticUnavailable)throw new Error("Synthetic diagnostic outage");
  diagnosticCalls.push({path,options});
  if(lateDiagnostic)await pause(80);
  return {ok:true,status:200,json:async()=>diagnosticReport(options.method==="POST")};
 }
 return previousFetch(path,options);
};
"""

SETUP_CASES = {
    "diagnostic-guidance": r"""
      assert($test("diagnostics-status").textContent.includes("Blocked"),"Blocker hidden by unknown coverage");
      assert($test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Original startup code missing");
      assert($test("diagnostics-findings").textContent.includes("managed owner"),"Manual guidance missing");
      assert(!$test("diagnostics-findings").querySelector("img"),"Evidence interpreted as HTML");
      $test("diagnostics-filter").value="unknown";$test("diagnostics-filter").dispatchEvent(new Event("change"));
      assert($test("diagnostics-findings").textContent.includes("CLOCK_UNKNOWN"),"Unknown checks hidden");
      assert(!$test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Filter ignored");
    """,
    "diagnostic-live": r"""
      $test("run-live-diagnostics").click();await pause(30);
      const request=diagnosticCalls.find(item=>item.options.method==="POST");
      assert(request&&JSON.parse(request.options.body).live===true,"Live probe was not explicitly requested");
      assert(request.options.headers["X-CSRF-Token"]==="csrf-from-session","Live request omitted session CSRF");
      assert($test("diagnostics-scope").textContent.includes("Live read-only report"),"Live scope missing");
    """,
    "diagnostic-failure": r"""
      diagnosticUnavailable=true;$test("refresh-diagnostics").click();await pause(30);
      assert(!$test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Old findings retained after outage");
      assert($test("diagnostics-status").textContent.includes("unavailable"),"Diagnostic outage unclear");
    """,
    "diagnostic-stale": r"""
      diagnosticStale=true;$test("refresh-diagnostics").click();await pause(30);
      assert(!$test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Stale timestamp accepted");
      assert($test("diagnostics-status").textContent.includes("stale"),"Stale scope unexplained");
    """,
    "diagnostic-logout": r"""
      lateDiagnostic=true;$test("run-live-diagnostics").click();await pause(10);$test("logout").click();await pause(120);
      assert($test("workspace").hidden,"Logout failed");
      assert(!$test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Late response exposed signed-out evidence");
    """,
}


@pytest.mark.skipif(dashboard_ui.CHROME is None, reason="Optional headless browser unavailable")
@pytest.mark.parametrize("scenario", SETUP_CASES)
def test_setup_diagnostics(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(setup_ui, "HARNESS", setup_ui.HARNESS + REPORT)
    monkeypatch.setitem(setup_ui.CASES, scenario, SETUP_CASES[scenario])
    setup_ui.test_guided_administration_controls(tmp_path, scenario)


DASHBOARD_CASES = {
    "diagnostic-dashboard": r"""
      assert($test("diagnostics-summary").textContent==="Blocked","Blocked diagnostic summary missing");
      assert($test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Structured finding missing");
      assert(!$test("diagnostics-findings").querySelector("img"),"Evidence interpreted as HTML");
      $test("poll-toggle").click();
      assert(!$test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Paused diagnostics appear current");
    """,
    "diagnostic-disconnected": r"""
      managementUnavailable=true;$test("refresh-button").click();await pause(30);
      assert(!$test("diagnostics-findings").textContent.includes("GOBGP_PORT_IN_USE"),"Disconnected diagnostics appear current");
      assert($test("diagnostics-summary").textContent==="Unknown","Disconnected coverage appears healthy");
    """,
}


@pytest.mark.skipif(dashboard_ui.CHROME is None, reason="Optional headless browser unavailable")
@pytest.mark.parametrize("scenario", DASHBOARD_CASES)
def test_dashboard_diagnostics(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(dashboard_ui, "HARNESS", dashboard_ui.HARNESS + REPORT)
    monkeypatch.setitem(dashboard_ui.CASES, scenario, DASHBOARD_CASES[scenario])
    dashboard_ui.test_dashboard_configuration_creation_controls(tmp_path, scenario)
