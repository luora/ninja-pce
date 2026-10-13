"""Exercise creation controls in an optional local headless Chromium.

The page uses the real packaged assets; all API responses are synthetic and no
controller, router or external browser dependency is contacted.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WEB = Path(__file__).parents[1] / "src/ninja_pce/web"
CHROME = next((path for path in (shutil.which("chromium"), shutil.which("google-chrome"),
                                 "/opt/google/chrome/chrome") if path and Path(path).is_file()), None)

HARNESS = r"""
const calls = JSON.parse(sessionStorage.getItem("test-calls") || "[]");
const reply = (value) => ({ok:true,status:200,json:async()=>value});
const pause = (ms) => new Promise((resolve)=>setTimeout(resolve,ms));
const assert = (value,message) => {if(!value)throw new Error(message);};
const $test = (id) => document.getElementById(id);
const proof = {independent:true,scope:"headend-rsvp",pcep_control:true,checks:["identity","destination","bandwidth","ero","signaling"]};
let statusCalls = 0;
window.fetch = async(path, options={}) => {
  if(path==="/api/controller") return reply({schema_version:1,read_only:true,mode:"live",sources:[],lsps:[],topology:{nodes:[],links:[]},summary:{},peers:[],events:[],computations:[],refresh_seconds:60});
  if(path==="/api/tunnel-options") return reply({headends:["PE1"],destinations:["PE3"],enabled:!(window.testScenario==="retry"&&sessionStorage.getItem("test-reloaded")),reason:"New delivery temporarily unavailable",requires_plan:true,backend:"router-config",max_bandwidth_bps:5000000,csrf_token:"c".repeat(43),policy:{read_only:false}});
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
    if(window.testScenario==="stale-status") return reply({operation:{id:String(calls.filter((call)=>call.path===path).length).repeat(32),status:"PUSHING"}});
    if(window.testScenario==="retry" && calls.filter((call)=>call.path===path).length===1) throw new TypeError("Synthetic lost response");
    if(window.testScenario==="rejected" && calls.filter((call)=>call.path===path).length===1) return reply({operation:{id:"1".repeat(32),status:"REJECTED",reason:"Reviewed plan expired before delivery."}});
    return reply({operation:{id:"1".repeat(32),status:"VERIFIED",verified:true,verification:proof,path_nodes:["PE1","P1","PE3"]}});
  }
  throw new Error("Unexpected request: "+path);
};
"""

CASES = {
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
      const newer=JSON.parse(sessionStorage.getItem("ninja-pce-creation"));
      assert(newer.id==="2".repeat(32),"New operation did not start");
      await pause(500);
      const retained=JSON.parse(sessionStorage.getItem("ninja-pce-creation"));
      assert(retained?.id===newer.id&&retained.key===newer.key,"Delayed old rejection cleared or overwrote the newer request");
      assert($test("creation-status").textContent.includes("Operation "+newer.id),"Old response replaced the newer operation status");
    """,
}


@pytest.mark.skipif(CHROME is None, reason="Optional headless Chromium is unavailable")
@pytest.mark.parametrize("scenario", CASES)
def test_dashboard_configuration_creation_controls(tmp_path: Path, scenario: str):
    index = (WEB / "index.html").read_text().replace(
        '<script src="app.js" defer></script>',
        '<script src="harness.js" defer></script><script src="app.js" defer></script>'
        '<script src="case.js" defer></script>')
    test_case = ("window.testScenario=" + json.dumps(scenario) + ";\n(async()=>{try{await pause(10);" +
                 CASES[scenario] +
                 "document.body.dataset.testResult='passed';}catch(error){"
                 "document.body.dataset.testResult='failed';"
                 "document.body.append(document.createTextNode('TEST FAILURE: '+error.message));}})();")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            resources = {"/": (index, "text/html"), "/harness.js": (HARNESS, "text/javascript"),
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
