"""Synthetic capability reports cannot replace exact-plan creation authority."""
from __future__ import annotations

import pytest
import test_dashboard_creation_ui as dashboard_ui

CAPABILITY_HARNESS = r"""
let capabilityMode=window.testScenario, capabilityOptionCalls=0, capabilityManagementUnavailable=false, capabilityOptionsUnauthorized=false;
const capabilityFetch=window.fetch;
window.fetch=async(path,options={})=>{
 if(path==="/api/controller"&&capabilityManagementUnavailable)throw new TypeError("Synthetic management outage");
 if(path==="/api/tunnel-options"){
  if(capabilityOptionsUnauthorized)return {ok:false,status:401,json:async()=>({error:"Authenticated access is required."})};
  const response=await capabilityFetch(path,options), value=await response.json();
  const sequence=++capabilityOptionCalls;
  if(capabilityMode==="capability-missing")delete value.capability;
  if(capabilityMode==="capability-unknown"){
   value.capability.checks.topology_ready.state="unknown";
   value.capability.checks.topology_ready.message="Fresh topology is unconfirmed.";
   value.capability.checks.topology_ready.next_action="Check collector synchronization before retrying.";
  }
  if(capabilityMode==="capability-selected-fail"){
   value.capability.headends.PE1.checks.transport_ready.state="fail";
   value.capability.headends.PE1.checks.transport_ready.message="Pinned transport identity is unavailable.";
   value.capability.headends.PE1.checks.transport_ready.next_action="Verify the enrolled host identity.";
  }
  if(capabilityMode==="capability-missing-check")delete value.capability.checks.pcep_authorized;
  if(capabilityMode==="capability-optional-check")value.capability.checks.pcep_authorized.required=false;
  if(capabilityMode==="capability-missing-node-check")delete value.capability.headends.PE1.checks.transport_enrolled;
  if(capabilityMode==="capability-stale")value.capability.generated_at=new Date(Date.now()-60000).toISOString();
  if(capabilityMode==="capability-future")value.capability.generated_at=new Date(Date.now()+60000).toISOString();
  if(capabilityMode==="capability-unsupported")value.capability.schema_version=2;
  if(capabilityMode==="capability-native")value.requires_plan=false;
  if(capabilityMode==="capability-safety"){
   value.capability.checks.topology_ready.state="unknown";
   value.capability.checks.topology_ready.message="<img src=x onerror=alert(1)>";
   value.capability.checks.topology_ready.next_action="<script>unexpected()</script>";
   value.capability.limitations=["<img src=x onerror=alert(1)>"];
  }
  if(capabilityMode==="capability-race"&&sequence===1)await pause(120);
  return reply(value);
 }
 if(path==="/api/tunnels"&&["capability-unverified","capability-invalid-proof","capability-uncertain","capability-status","capability-status-disconnect"].includes(capabilityMode)){
  calls.push({path,body:JSON.parse(options.body),key:options.headers["Idempotency-Key"]});
  const status=capabilityMode==="capability-unverified"?"APPLIED_UNVERIFIED":capabilityMode==="capability-invalid-proof"?"VERIFIED":capabilityMode==="capability-uncertain"?"ROLLBACK_REQUIRED":"PUSHING";
  return reply({operation:{id:"1".repeat(32),principal:window.testPrincipal||"operator",status,reported:{operational_state:"ACTIVE"},
   ...(status==="VERIFIED"?{verification:{...proof,checks:["identity"]}}:{})}});
 }
 if(path.startsWith("/api/tunnel-operations/")&&["capability-status","capability-status-disconnect"].includes(window.testScenario))
  return reply({operation:{id:"1".repeat(32),principal:window.testPrincipal||"operator",status:"PUSHING"}});
 if(path==="/api/tunnels"&&["capability-recovery","capability-disconnected-recovery"].includes(window.testScenario)){
  calls.push({path,body:JSON.parse(options.body),key:options.headers["Idempotency-Key"]});
  if(calls.filter(item=>item.path===path).length===1)throw new TypeError("Synthetic lost submission response");
  return reply({operation:{id:"1".repeat(32),status:"VERIFIED",verification:proof}});
 }
 return capabilityFetch(path,options);
};
"""

BLOCKED = r"""
 $test("create-tunnel-button").click();await pause(450);
 assert($test("creation-submit").disabled,"Unconfirmed capability enabled new creation");
 assert(calls.length===0,"Unconfirmed capability requested a preview or delivery");
 assert(!$test("creation-fields").disabled,"Capability failure hides discovered endpoints");
"""

CASES = {
    "capability-missing": BLOCKED + r"""
      assert($test("creation-capability-status").textContent.includes("unconfirmed"),"Missing report lacks guidance");
    """,
    "capability-unknown": BLOCKED + r"""
      assert($test("creation-capability-checks").textContent.includes("UNKNOWN"),"Unknown check presented as failure or pass");
      assert($test("creation-capability-checks").textContent.includes("Check collector synchronization"),"Manual guidance missing");
    """,
    "capability-selected-fail": BLOCKED + r"""
      assert($test("creation-capability-checks").textContent.includes("Pinned transport identity"),"Selected headend failure hidden by overall availability");
      assert($test("creation-capability-checks").textContent.includes("Verify the enrolled host identity"),"Selected headend guidance missing");
    """,
    "capability-missing-check": BLOCKED,
    "capability-optional-check": BLOCKED,
    "capability-missing-node-check": BLOCKED,
    "capability-stale": BLOCKED,
    "capability-future": BLOCKED,
    "capability-unsupported": BLOCKED,
    "capability-native": BLOCKED,
    "capability-safety": BLOCKED + r"""
      const section=document.querySelector(".creation-capability");
      assert(section.textContent.includes("<img src=x"),"Literal guidance disappeared");
      assert(!section.querySelector("img")&&!section.querySelector("script"),"Capability report interpreted as HTML");
      assert($test("creation-capability-checks").children.length<=64,"Capability display is unbounded");
    """,
    "capability-stages": r"""
      $test("create-tunnel-button").click();await pause(450);
      const stages=()=>Array.from($test("creation-stages").children);
      assert(stages()[1].dataset.state==="complete"&&stages()[2].getAttribute("aria-current")==="step","Preview and approval stages conflated");
      assert($test("creation-capability-scope").textContent.includes("qualified lab profile"),"Existing qualification scope missing");
      assert($test("creation-capability-scope").textContent.includes("ownership"),"Ownership limitation missing");
      $test("creation-submit").click();await pause(20);
      assert(stages()[4].dataset.state==="complete","Independent RSVP proof did not advance verification stage");
      assert(stages()[5].dataset.state==="unknown","RSVP proof was treated as forwarding proof");
    """,
    "capability-unverified": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      const stages=$test("creation-stages").children;
      assert(stages[4].dataset.state==="current"&&stages[5].dataset.state==="unknown","ACTIVE or acknowledgment was presented as independent verification");
    """,
    "capability-invalid-proof": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      assert($test("creation-stages").children[4].dataset.state!=="complete","Incomplete proof advanced RSVP stage");
      assert($test("creation-status").textContent.includes("Independent RSVP verification is unavailable"),"Invalid independent proof not explained");
      assert($test("creation-submit").textContent==="Retry same request","Uncertain proof response lost recovery key");
    """,
    "capability-uncertain": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      assert($test("creation-stages").children[3].dataset.state==="uncertain","Delivery uncertainty presented as completion");
      assert($test("creation-status").textContent.includes("Capacity remains held"),"Uncertain hold unexplained");
      assert(calls.filter(item=>item.path==="/api/tunnels").length===1,"Uncertain delivery resent");
    """,
    "capability-race": r"""
      $test("create-tunnel-button").click();await pause(10);$test("create-tunnel-close").click();
      window.testPrincipal="new-user";$test("create-tunnel-button").click();await pause(450);
      $test("creation-submit").click();await pause(20);
      assert(sessionStorage.getItem("ninja-pce-creations:new-user"),"Current principal lost to delayed options response");
      assert(!sessionStorage.getItem("ninja-pce-creations:operator"),"Stale options restored an earlier principal");
      assert(calls.filter(item=>item.path==="/api/tunnel-plans").length===1,"Stale options requested another preview");
    """,
    "capability-recovery": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      capabilityMode="capability-missing";$test("creation-refresh").click();await pause(20);
      assert(!$test("creation-submit").disabled,"Capability loss disabled same-request recovery");
      assert($test("creation-submit").textContent==="Retry same request","Uncertain request lost identity");
      $test("creation-submit").click();await pause(20);
      const sent=calls.filter(item=>item.path==="/api/tunnels");
      assert(sent.length===2&&sent[0].key===sent[1].key&&JSON.stringify(sent[0].body)===JSON.stringify(sent[1].body),"Recovery changed exact intent, approval or idempotency key");
      assert(calls.filter(item=>item.path==="/api/tunnel-plans").length===1,"Capability loss replaced reviewed plan");
    """,
    "capability-status": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      capabilityMode="capability-missing";$test("creation-refresh").click();await pause(20);
      assert(!$test("creation-check").hidden&&!$test("creation-check").disabled,"Capability loss disabled operation status");
      assert($test("creation-status").textContent.includes("Operation "+"1".repeat(32)),"Capability loss hid owned operation");
      assert(calls.filter(item=>item.path==="/api/tunnels").length===1,"Capability refresh resent delivery");
    """,
    "capability-disconnect": r"""
      $test("create-tunnel-button").click();await pause(450);
      assert(!$test("creation-submit").disabled,"Initial reviewed request unavailable");
      capabilityManagementUnavailable=true;$test("refresh-button").click();await pause(30);
      assert($test("creation-submit").disabled,"Management outage left new creation enabled");
      assert($test("creation-preview").hidden,"Management outage retained actionable preview");
      assert(!$test("creation-capability-checks").textContent.includes("PASS"),"Management outage retained current prerequisite claims");
      $test("create-tunnel-form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}));await pause(20);
      assert(calls.filter(item=>item.path==="/api/tunnels").length===0,"Management outage permitted direct form submission");
      capabilityManagementUnavailable=false;$test("refresh-button").click();await pause(30);
      assert($test("creation-submit").disabled,"Reconnect reused capability from prior connection");
      $test("creation-refresh").click();await pause(450);
      assert(!$test("creation-submit").disabled,"Explicit fresh capability did not restore reviewed workflow");
    """,
    "capability-paused": r"""
      $test("create-tunnel-button").click();await pause(450);$test("poll-toggle").click();await pause(20);
      assert($test("creation-submit").disabled&&$test("creation-preview").hidden,"Paused polling left new creation actionable");
      assert(!$test("creation-capability-checks").textContent.includes("PASS"),"Paused polling retained current prerequisite claims");
      $test("creation-refresh").click();await pause(450);
      assert($test("creation-submit").disabled,"Refreshing capability bypassed paused controller state");
      assert(calls.filter(item=>item.path==="/api/tunnel-plans").length===1,"Paused polling permitted another preview");
    """,
    "capability-options-unauthorized": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      assert($test("creation-stages").children[4].dataset.state==="complete","Initial verification unavailable");
      capabilityOptionsUnauthorized=true;$test("creation-refresh").click();await pause(30);
      assert($test("creation-stages").children[4].dataset.state!=="complete","Unauthorized options retained current RSVP verification stage");
      assert($test("creation-policy").textContent.includes("Authenticated access"),"Authorization loss lacks guidance");
      assert(!$test("creation-check").hidden,"Authorization loss discarded operation identity");
      const saved=JSON.parse(sessionStorage.getItem("ninja-pce-creations:operator"));
      assert(saved.pending.id==="1".repeat(32),"Authorization loss discarded historical request");
    """,
    "capability-disconnected-recovery": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      capabilityManagementUnavailable=true;$test("refresh-button").click();await pause(30);
      assert(!$test("creation-submit").disabled&&$test("creation-submit").textContent==="Retry same request","Management outage disabled exact uncertain recovery");
      $test("creation-submit").click();await pause(20);
      const sent=calls.filter(item=>item.path==="/api/tunnels");
      assert(sent.length===2&&sent[0].key===sent[1].key&&JSON.stringify(sent[0].body)===JSON.stringify(sent[1].body),"Management outage recovery changed exact request");
      assert($test("creation-stages").children[4].dataset.state!=="complete","Disconnected controller was presented as current RSVP readiness");
    """,
    "capability-status-disconnect": r"""
      $test("create-tunnel-button").click();await pause(450);$test("creation-submit").click();await pause(20);
      capabilityManagementUnavailable=true;$test("refresh-button").click();await pause(30);
      assert(!$test("creation-check").hidden&&!$test("creation-check").disabled,"Management outage disabled owned operation status");
      $test("creation-check").click();await pause(30);
      assert($test("creation-status").textContent.includes("Operation "+"1".repeat(32)),"Owned status could not be recovered during management outage");
      assert(calls.filter(item=>item.path==="/api/tunnels").length===1,"Checking status during outage resent delivery");
    """,
}


@pytest.mark.skipif(dashboard_ui.CHROME is None, reason="Optional headless browser unavailable")
@pytest.mark.parametrize("scenario", CASES)
def test_capability_driven_creation(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(dashboard_ui, "HARNESS", dashboard_ui.HARNESS + CAPABILITY_HARNESS)
    monkeypatch.setitem(dashboard_ui.CASES, scenario, CASES[scenario])
    dashboard_ui.test_dashboard_configuration_creation_controls(tmp_path, scenario)
