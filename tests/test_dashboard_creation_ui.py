"""Exercise creation controls in an optional local headless Chromium.

The page uses the real packaged assets; all API responses are synthetic and no
controller, router or external browser dependency is contacted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / "src/ninja_pce/web"


def browser_executable() -> str | None:
    explicit = os.environ.get("NINJA_PCE_TEST_BROWSER")
    if explicit:
        executable = shutil.which(explicit)
        if executable is None or not Path(executable).is_file():
            raise ValueError("NINJA_PCE_TEST_BROWSER must identify an available browser executable")
        return executable
    # Hosted Ubuntu images can supply both native Chrome and a Chromium launcher
    # whose runtime depends on snap services. Prefer the native installation.
    return next((path for path in (shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
                                   "/opt/google/chrome/chrome", shutil.which("chromium"),
                                   shutil.which("chromium-browser"))
                 if path and Path(path).is_file()), None)


CHROME = browser_executable()

HARNESS = r"""
const calls = JSON.parse(sessionStorage.getItem("test-calls") || "[]");
const reply = (value) => ({ok:true,status:200,json:async()=>value});
const pause = (ms) => new Promise((resolve)=>setTimeout(resolve,ms));
const assert = (value,message) => {if(!value)throw new Error(message);};
const $test = (id) => document.getElementById(id);
const proof = {independent:true,scope:"headend-rsvp",pcep_control:true,checks:["identity","destination","bandwidth","ero","signaling"]};
let statusCalls = 0;
window.fetch = async(path, options={}) => {
  if(path==="/api/controller") return reply({schema_version:1,read_only:true,mode:window.testScenario==="sample"?"demo":"live",sources:[],lsps:[],topology:{nodes:[],links:[]},summary:{},peers:[],events:[],computations:[],refresh_seconds:60});
  if(path==="/api/tunnel-options") {
    if(window.testScenario==="sample") throw new Error("Sample requested live provisioning policy");
    if(window.testScenario==="readonly") return reply({enabled:false,headends:[],destinations:[],max_bandwidth_bps:0,reason:"RSVP bandwidth creation has not been qualified for this controller."});
    const domain = window.testScenario.startsWith("domain"), disabled = window.testScenario==="domain-unavailable";
    return reply({principal:window.testPrincipal||"operator",headends:domain?["P2","PE1","P1"]:["PE1"],destinations:["PE3"],enabled:!(disabled||window.testScenario==="retry"&&sessionStorage.getItem("test-reloaded")),reason:"New delivery temporarily unavailable",requires_plan:true,backend:"router-config",max_bandwidth_bps:5000000,csrf_token:"c".repeat(43),policy:{read_only:false},...(domain?{headend_readiness:{P2:{enabled:false,transport_enrolled:false,pcc_ready:true,reason:"No pinned transport identity is enrolled."},PE1:{enabled:!disabled,transport_enrolled:true,pcc_ready:!disabled,reason:disabled?"PCC session unavailable.":"Provisioning ready."},P1:{enabled:!disabled,transport_enrolled:true,pcc_ready:!disabled,reason:disabled?"PCC session unavailable.":"Provisioning ready."}}}:{})});
  }
  if(path.startsWith("/api/tunnel-operations/")&&["multioperations","principals"].includes(window.testScenario)) return reply({operation:{id:path.split("/").pop(),principal:window.testPrincipal||"operator",status:"PUSHING"}});
  if(path.startsWith("/api/tunnel-operations/")&&window.testScenario==="stale-status") {
    statusCalls+=1; const request=statusCalls;
    await pause(request===1?40:900);
    return reply({operation:{id:"1".repeat(32),status:"REJECTED",reason:"First request rejected before delivery."}});
  }
  calls.push({path,method:options.method,body:JSON.parse(options.body),key:options.headers["Idempotency-Key"]});
  sessionStorage.setItem("test-calls",JSON.stringify(calls));
  if(path==="/api/tunnel-plans") {
    const intent=JSON.parse(options.body), expiry=Date.now()+(window.testScenario==="expiry" ? 900 : 60000), bps=Math.ceil(intent.bandwidth_bps/1000)*1000;
    return reply({plan:{plan_id:(intent.bandwidth_bps===1000000?"e":"f").repeat(64),expires_at:new Date(expiry).toISOString(),rendered_config:`interface Tunnel200\n description <test-config>\n tunnel mpls traffic-eng bandwidth ${bps/1000}\n`,path_nodes:["PE1","P1","PE3"],...intent,bandwidth_bps:bps}});
  }
  if(path==="/api/tunnels") {
    if(["stale-status","multioperations","principals"].includes(window.testScenario)) return reply({operation:{id:String(calls.filter((call)=>call.path===path).length).repeat(32),principal:window.testPrincipal||"operator",status:"PUSHING"}});
    if(window.testScenario==="retry" && calls.filter((call)=>call.path===path).length===1) throw new TypeError("Synthetic lost response");
    if(window.testScenario==="rejected" && calls.filter((call)=>call.path===path).length===1) return reply({operation:{id:"1".repeat(32),status:"REJECTED",reason:"Reviewed plan expired before delivery."}});
    return reply({operation:{id:"1".repeat(32),status:"VERIFIED",verified:true,verification:proof,path_nodes:["PE1","P1","PE3"]}});
  }
  throw new Error("Unexpected request: "+path);
};
"""

CASES = {
    "sample": r"""
      assert(!$test("demo-banner").hidden&&$test("demo-banner").textContent.includes("Sample workspace"),"Sample identity missing");
      assert($test("demo-banner").textContent.includes("No routers are connected"),"Sample connection state unclear");
      assert($test("view-description").textContent.includes("Explore a sample topology"),"Sample overview claims live data");
      assert($test("activity-feed-note").textContent==="Synthetic sample events","Sample events claim captured data");
      document.querySelector('[data-view="topology"]').click();
      document.querySelector('[data-view="overview"]').click();
      assert($test("view-description").textContent.includes("Explore a sample topology"),"Navigation lost sample description");
      $test("create-tunnel-button").click(); await pause(450);
      assert($test("creation-policy").textContent.includes("Connect and configure your network"),"Sample lacks live setup guidance");
      assert($test("creation-fields").disabled&&$test("creation-submit").disabled&&calls.length===0,"Sample requested creation");
    """,
    "preview": r"""
      $test("create-tunnel-button").click(); await pause(20);
      assert($test("creation-submit").disabled,"Create enabled before preview");
      await pause(400);
      assert(!$test("creation-submit").disabled,"Preview did not enable Create");
      assert(calls.length===1&&calls[0].path==="/api/tunnel-plans","Preview mutated router");
      assert($test("creation-preview-config").textContent.includes("<test-config>"),"Exact preview missing");
      assert(!$test("creation-preview-config").querySelector("test-config"),"Configuration interpreted as HTML");
      $test("creation-bandwidth").value="1.999999";
      $test("creation-bandwidth").dispatchEvent(new Event("input",{bubbles:true}));
      assert($test("creation-submit").disabled,"Changed intent retained old approval");
      await pause(400);
      assert($test("creation-preview-config").textContent.includes("bandwidth 2000"),"Preview not refreshed");
      assert($test("creation-preview-bandwidth").textContent.includes("rounded up"),"Rounded RSVP bandwidth not disclosed");
      $test("creation-submit").click(); await pause(20);
      const sent=calls.filter((call)=>call.path==="/api/tunnels");
      assert(sent.length===1&&sent[0].body.approved_plan_id==="f".repeat(64)&&sent[0].body.bandwidth_bps===1999999,"Create not bound to displayed plan and original intent");
      assert($test("creation-status").textContent.includes("RSVP verified on the headend"),"Independent RSVP result not shown");
      assert($test("creation-status").textContent.includes("forwarding remains unverified"),"Unsupported forwarding claim");
    """,
    "expiry": r"""
      $test("create-tunnel-button").click(); await pause(450);
      assert(!$test("creation-submit").disabled,"Initial preview missing");
      await pause(950);
      assert($test("creation-submit").disabled,"Expired preview remained actionable");
      await pause(400);
      assert(!$test("creation-submit").disabled,"Expired plan did not refresh");
      assert(calls.length===2&&calls.every((call)=>call.path==="/api/tunnel-plans"),"Expiry caused mutation");
    """,
    "retry": r"""
      $test("create-tunnel-button").click(); await pause(450);
      if(!sessionStorage.getItem("test-reloaded")) {
        $test("creation-submit").click(); await pause(20);
        assert(calls.filter((call)=>call.path==="/api/tunnels").length===1,"Missing first attempt");
        sessionStorage.setItem("test-reloaded","yes"); location.reload(); return;
      }
      assert(calls.filter((call)=>call.path==="/api/tunnels").length===1,"Reload resubmitted without approval");
      assert($test("creation-submit").textContent==="Retry same request","Pending request lost");
      assert(!$test("creation-submit").disabled,"Existing-operation recovery disabled with new delivery unavailable");
      $test("creation-submit").click(); await pause(20);
      const sent=calls.filter((call)=>call.path==="/api/tunnels");
      assert(sent.length===2&&sent[0].key===sent[1].key&&JSON.stringify(sent[0].body)===JSON.stringify(sent[1].body),"Retry changed intent, approval or idempotency key");
      assert(calls.filter((call)=>call.path==="/api/tunnel-plans").length===1,"Reload replaced approved plan");
    """,
    "rejected": r"""
      $test("create-tunnel-button").click(); await pause(450);
      $test("creation-submit").click(); await pause(20);
      assert(!$test("creation-fields").disabled,"Definitive rejection left fields locked");
      assert($test("creation-submit").disabled,"Rejected plan was retained for another delivery");
      await pause(400);
      assert(!$test("creation-submit").disabled,"Definitive rejection did not refresh preview");
      assert($test("creation-status").textContent.includes("Reviewed plan expired before delivery"),"Rejection reason disappeared during refresh");
      $test("creation-bandwidth").value="2";
      $test("creation-bandwidth").dispatchEvent(new Event("input",{bubbles:true}));
      await pause(400);
      $test("creation-submit").click(); await pause(20);
      const sent=calls.filter((call)=>call.path==="/api/tunnels");
      assert(sent.length===2&&sent[0].key!==sent[1].key,"New intent reused rejected idempotency key");
      assert(sent[1].body.bandwidth_bps===2000000&&sent[1].body.approved_plan_id==="f".repeat(64),"New request did not approve refreshed plan");
    """,
    "stale-status": r"""
      $test("create-tunnel-button").click(); await pause(450);
      $test("creation-submit").click(); await pause(20);
      $test("creation-check").click(); $test("creation-check").click();
      await pause(450);
      assert(!$test("creation-submit").disabled,"First rejection did not permit a new reviewed request");
      $test("creation-submit").click(); await pause(20);
      const newer=JSON.parse(sessionStorage.getItem("ninja-pce-creations:operator")).pending;
      assert(newer.id==="2".repeat(32),"New operation did not start");
      await pause(500);
      const retained=JSON.parse(sessionStorage.getItem("ninja-pce-creations:operator")).pending;
      assert(retained?.id===newer.id&&retained.key===newer.key,"Delayed old rejection cleared or overwrote the newer request");
      assert($test("creation-status").textContent.includes("Operation "+newer.id),"Old response replaced the newer operation status");
    """,
    "domain": r"""
      $test("create-tunnel-button").click(); await pause(450);
      assert(Array.from($test("creation-headend").options).map((item)=>item.value).join(",")==="P2,PE1,P1","Unenrolled transit node disappeared");
      assert($test("creation-headend").value==="PE1","Unavailable node selected by default");
      $test("creation-headend").value="P2";
      $test("creation-headend").dispatchEvent(new Event("change",{bubbles:true})); await pause(400);
      assert(!$test("creation-fields").disabled,"Unavailable headend blocks inspecting other domain nodes");
      assert($test("creation-submit").disabled,"Unenrolled transport permits creation");
      assert($test("creation-readiness").textContent.includes("Transport enrollment required")&&$test("creation-readiness").textContent.includes("PCC synchronized"),"Transport and PCC readiness conflated");
      assert(calls.length===1,"Unavailable headend requested another plan");
      $test("creation-headend").value="P1";
      $test("creation-headend").dispatchEvent(new Event("change",{bubbles:true})); await pause(400);
      assert(!$test("creation-submit").disabled,"Qualified transit headend unavailable");
      $test("creation-submit").click(); await pause(20);
      assert(calls.find((call)=>call.path==="/api/tunnels").body.headend==="P1","Transit headend was replaced");
    """,
    "domain-unavailable": r"""
      $test("create-tunnel-button").click(); await pause(450);
      assert(!$test("creation-fields").disabled,"Unavailable domain cannot be inspected");
      assert($test("creation-headend").options.length===3,"Unavailable domain nodes disappeared");
      $test("creation-headend").value="P1";
      $test("creation-headend").dispatchEvent(new Event("change",{bubbles:true})); await pause(400);
      assert($test("creation-readiness").textContent.includes("PCC unavailable"),"Missing PCC reason");
      assert($test("creation-submit").disabled&&calls.length===0,"Unavailable domain requested delivery or preview");
    """,
    "multioperations": r"""
      $test("create-tunnel-button").click(); await pause(450);
      $test("creation-submit").click(); await pause(20);
      assert(!$test("creation-new").hidden,"Acknowledged operation blocks independent requests");
      $test("creation-new").click(); await pause(400);
      assert(!$test("creation-submit").disabled,"Second plan unavailable");
      $test("creation-submit").click(); await pause(20);
      const saved=JSON.parse(sessionStorage.getItem("ninja-pce-creations:operator"));
      assert(saved.operations.length===2&&saved.pending.id==="2".repeat(32),"Independent operations not retained");
      assert(saved.operations[0].key!==saved.operations[1].key,"Independent operation reused idempotency key");
      assert($test("creation-operation-list").children.length===2,"Operation history disappeared");
      $test("creation-operation-list").children[1].click(); await pause(20);
      assert(JSON.parse(sessionStorage.getItem("ninja-pce-creations:operator")).pending.id==="1".repeat(32),"Existing operation not recovered for polling");
      assert(calls.filter((call)=>call.path==="/api/tunnels").length===2,"History selection resent an operation");
    """,
    "principals": r"""
      $test("create-tunnel-button").click(); await pause(450);
      $test("creation-submit").click(); await pause(20);
      $test("create-tunnel-close").click(); window.testPrincipal="new-user";
      $test("create-tunnel-button").click(); await pause(450);
      assert($test("creation-operations").hidden,"Another principal sees browser operation history");
      assert(!$test("creation-submit").disabled,"New principal inherited another user's pending operation");
      $test("creation-submit").click(); await pause(20);
      const first=JSON.parse(sessionStorage.getItem("ninja-pce-creations:operator"));
      const second=JSON.parse(sessionStorage.getItem("ninja-pce-creations:new-user"));
      assert(first.pending.principal==="operator"&&second.pending.principal==="new-user","Stored requests not scoped to principals");
      assert(first.pending.key!==second.pending.key,"Principal change reused another request");
      $test("create-tunnel-close").click(); window.testPrincipal="operator";
      $test("create-tunnel-button").click(); await pause(20);
      assert($test("creation-status").textContent.includes("1".repeat(32)),"Original principal lost operation history");
      assert(calls.filter((call)=>call.path==="/api/tunnels").length===2,"Principal switch resent a request");
    """,
    "legacy": r"""
      sessionStorage.setItem("ninja-pce-creation",JSON.stringify({key:"legacy-key",intent:{headend:"PE1",destination:"PE3",bandwidth_bps:1000000},approved_plan_id:"e".repeat(64)}));
      $test("create-tunnel-button").click(); await pause(450);
      assert($test("creation-submit").disabled,"Unbound legacy request can be sent by another principal");
      assert($test("creation-status").textContent.includes("no authenticated principal binding"),"Unbound request reconciliation is not explained");
      assert(calls.length===0,"Legacy unknown outcome caused delivery or replacement preview");
    """,
    "readonly": r"""
      $test("create-tunnel-button").click(); await pause(450);
      assert($test("creation-policy").textContent.includes("has not been qualified"),"Read-only dashboard lost qualification reason");
      assert($test("creation-fields").disabled&&$test("creation-submit").disabled&&calls.length===0,"Read-only dashboard requested creation");
    """,
}


def test_browser_selection_prefers_native_chrome_over_chromium(tmp_path, monkeypatch):
    native, launcher = tmp_path / "chrome", tmp_path / "chromium"
    native.touch()
    launcher.touch()
    monkeypatch.delenv("NINJA_PCE_TEST_BROWSER", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: {"google-chrome": str(native),
                                                      "chromium": str(launcher)}.get(name))
    assert browser_executable() == str(native)


def test_explicit_browser_selection_is_used_and_missing_override_fails(tmp_path, monkeypatch):
    selected = tmp_path / "browser"
    selected.touch()
    monkeypatch.setenv("NINJA_PCE_TEST_BROWSER", str(selected))
    monkeypatch.setattr(shutil, "which", lambda name: str(selected) if name == str(selected) else None)
    assert browser_executable() == str(selected)
    selected.unlink()
    with pytest.raises(ValueError, match="NINJA_PCE_TEST_BROWSER"):
        browser_executable()


@pytest.mark.skipif(CHROME is None, reason="Optional headless Chromium is unavailable")
@pytest.mark.parametrize("scenario", CASES)
def test_dashboard_configuration_creation_controls(tmp_path: Path, scenario: str):
    index = (WEB / "index.html").read_text().replace(
        '<script src="app.js" defer></script>',
        '<script src="harness.js" defer></script><script src="app.js" defer></script>'
        '<script src="case.js" defer></script>')
    harness = "window.testScenario=" + json.dumps(scenario) + ";\n" + HARNESS
    test_case = ("(async()=>{try{await pause(10);" +
                 CASES[scenario] +
                 "document.body.dataset.testResult='passed';}catch(error){"
                 "document.body.dataset.testResult='failed';"
                 "document.body.append(document.createTextNode('TEST FAILURE: '+error.message));}})();")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            resources = {"/": (index, "text/html"), "/harness.js": (harness, "text/javascript"),
                         "/case.js": (test_case, "text/javascript")}
            if self.path in resources:
                value, mime = resources[self.path]
            elif self.path in {"/app.js", "/styles.css"}:
                value = (WEB / self.path[1:]).read_text()
                mime = "text/javascript" if self.path.endswith(".js") else "text/css"
            else:
                self.send_error(404)
                return
            body = value.encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = subprocess.run([str(CHROME), "--headless", "--no-sandbox", "--disable-gpu",
                                 "--disable-dev-shm-usage", "--disable-background-networking",
                                 "--no-first-run", "--no-default-browser-check", "--disable-sync",
                                 f"--user-data-dir={tmp_path / 'browser'}", "--virtual-time-budget=6000",
                                 "--dump-dom", f"http://127.0.0.1:{server.server_port}/"],
                                capture_output=True, text=True, timeout=20)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert result.returncode == 0, result.stderr[-2000:]
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-6000:]
