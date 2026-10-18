"""Exercise packaged guided setup in an optional local headless browser.

API responses are synthetic. These checks never start GoBGP or contact routers.
"""
from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import test_dashboard_creation_ui as browser_ui

WEB = Path(__file__).parents[1] / "src/ninja_pce/web"

HARNESS = r"""
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const assert = (value, message) => { if (!value) throw new Error(message); };
const $test = id => document.getElementById(id);
const calls = [];
const setup = {version:1,operating_mode:"observe",controller:{name:"NINJA-PCE",router_id:"192.0.2.10",local_as:64512,source_address:"192.0.2.10",bgp_port:-1},bgpls:{peers:[{address:"192.0.2.20",asn:64512}],interval_seconds:5,timeout_seconds:10,max_age_seconds:30},routing_domain:{protocol_id:3,instance_id:0,autonomous_system:64512,bgpls_identifier:0},nodes:[],abr_bindings:[]};
if(window.testScenario==="unsafe-instance") setup.routing_domain.instance_id=9007199254740992;
let currentRevision = ["runtime-failed","runtime-uncertain"].includes(window.testScenario) ? "saved-revision" : null;
let currentSetup = setup;
let previewCount = 0;
let statusUnavailable = false;
let sessionExpired = false;
const reply = (value, status=200) => ({ok:status>=200&&status<300,status,json:async()=>value});
Storage.prototype.setItem = () => { throw new Error("Guided administration wrote to browser storage"); };
window.fetch = async (path, options={}) => {
  assert(!path.includes("?"), "Secret or request state included in URL");
  assert(options.credentials === "same-origin", "Session cookie scope omitted");
  assert(options.cache === "no-store", "Administration response may be cached");
  if(path === "/api/admin/session") return reply({bootstrap_required:window.testScenario==="bootstrap",authenticated:!["bootstrap","login"].includes(window.testScenario),username:"operator",csrf_token:"csrf-from-session"});
  if(path === "/api/admin/config") return reply({revision:currentRevision,setup:currentSetup,...(window.testScenario==="source-unknown"?{}:{source_management:window.testScenario==="source-external"?"external":"managed"})});
  if(path === "/api/admin/status") {
    if(statusUnavailable) throw new Error("Synthetic outage");
    if(sessionExpired) return reply({message:"Session expired"},401);
    return reply({revision:currentRevision,runtime:{state:currentRevision?"STOPPED":"UNCONFIGURED"},readiness:{state:"WAITING_FOR_TOPOLOGY",ready:false,message:"The network source is not connected."}});
  }
  if(path === "/api/admin/audit") return reply({events:[{at:1792324800,action:"<test-audit>",actor:"operator",status:"SAVED"}]});
  const body = JSON.parse(options.body);
  calls.push({path,body,headers:options.headers});
  if(path === "/api/admin/login" || path === "/api/admin/bootstrap") return reply({authenticated:true,username:"operator",csrf_token:"csrf-from-login"});
  assert(options.headers["X-CSRF-Token"] === (window.testScenario === "bootstrap" || window.testScenario === "login" ? "csrf-from-login" : "csrf-from-session"), "Mutation lacks current CSRF token");
  if(path === "/api/admin/logout") return reply({authenticated:false});
  if(path === "/api/admin/preview") {
    if(window.testScenario === "conflict") return reply({message:"Saved revision no longer matches"},409);
    previewCount += 1;
    if(window.testScenario === "delayed") await pause(70);
    return reply({preview_id:"preview-"+previewCount,expected_revision:currentRevision,revision:"new-revision",files:{"controller.json":"{\"operating_mode\":\"observe\"}","gobgpd.toml":"# <test-config>\n[global.config]\n  port = -1"},changes:["Replace observation configuration <test-change>"],write_authority:false});
  }
  if(path === "/api/admin/apply") {
    currentRevision="saved-revision";
    currentSetup=calls.filter(call=>call.path==="/api/admin/preview").at(-1).body.setup;
    if(window.testScenario==="apply-uncertain") return reply({revision:currentRevision,status:"UNCERTAIN",message:"Activation or rollback could not be confirmed.",runtime:{state:"uncertain"}});
    return reply({revision:currentRevision,status:body.activate?"ACTIVE":"SAVED",runtime:{state:body.activate?"RUNNING":"STOPPED"}});
  }
  if(path === "/api/admin/runtime") {
    if(window.testScenario==="runtime-failed") return reply({transaction:{status:"FAILED",error:"Port 50051 occupied by <test-owner>."},runtime:{state:"failed"}});
    if(window.testScenario==="runtime-uncertain") return reply({transaction:{status:"UNCERTAIN",error:"Owned process shutdown was not confirmed."},runtime:{state:"uncertain"}});
    return reply({runtime:{state:body.action==="start"?"RUNNING":"STOPPED"}});
  }
  throw new Error("Unexpected endpoint "+path);
};
const submitPreview = async () => { $test("setup-form").requestSubmit(); await pause(30); };
const editName = value => { $test("controller-name").value=value; $test("controller-name").dispatchEvent(new Event("input",{bubbles:true})); };
"""

CASES = {
    "bootstrap": r"""
      assert(!$test("auth-panel").hidden&&$test("workspace").hidden,"Bootstrap exposed configuration without authentication");
      assert(!$test("bootstrap-token-label").hidden,"Bootstrap token control missing");
      assert($test("auth-password").autocomplete==="new-password","Bootstrap password autocomplete incorrect");
      assert($test("auth-username").maxLength===64&&$test("auth-password").minLength===15&&$test("auth-password").maxLength===128,"Credential bounds differ from server validation");
      assert($test("bootstrap-hint").textContent.includes("ninja-pce admin init")&&$test("bootstrap-hint").textContent.includes("ninja-pce admin bootstrap-token"),"Bootstrap token source is incorrect");
      $test("bootstrap-token").value="one-time-secret";
      $test("auth-username").value="operator";
      $test("auth-password").value="private-account-password";
      $test("auth-form").requestSubmit(); await pause(30);
      assert(!$test("workspace").hidden&&$test("auth-panel").hidden,"Bootstrap did not open authenticated setup");
      assert(calls[0].path==="/api/admin/bootstrap"&&calls[0].body.token==="one-time-secret","Bootstrap body incorrect");
      assert(!$test("auth-password").value&&!$test("bootstrap-token").value,"Credentials retained in inputs");
      assert(!document.body.textContent.includes("one-time-secret")&&!document.body.textContent.includes("private-account-password"),"Credentials rendered");
      await submitPreview();
      assert(calls.at(-1).headers["X-CSRF-Token"]==="csrf-from-login","Login CSRF token not used");
      $test("logout").click(); await pause(20);
      assert($test("workspace").hidden&&!$test("auth-panel").hidden,"Logout retained administration UI");
    """,
    "login": r"""
      assert(!$test("auth-panel").hidden&&$test("bootstrap-token-label").hidden,"Login requested bootstrap token");
      $test("auth-username").value="operator";
      $test("auth-password").value="private-account-password";
      $test("auth-form").requestSubmit(); await pause(30);
      assert(calls[0].path==="/api/admin/login"&&!Object.hasOwn(calls[0].body,"token"),"Login sent bootstrap credentials");
      assert(!$test("auth-password").value&&!$test("workspace").hidden,"Login did not clear credentials and open setup");
    """,
    "preview-save": r"""
      assert($test("save-button").disabled&&$test("activate-button").disabled,"Save enabled without review");
      assert($test("domain-protocol").options.length===1&&$test("domain-protocol").value==="3","UI offers an unsupported routing protocol");
      await submitPreview();
      assert(!$test("preview-panel").hidden&&!$test("save-button").disabled,"Preview did not enable save");
      assert(calls.length===1&&calls[0].path==="/api/admin/preview","Preview mutated configuration");
      assert($test("preview-files").textContent.includes("<test-config>")&&!$test("preview-files").querySelector("test-config"),"Generated files interpreted as markup");
      assert(!$test("preview-changes").querySelector("test-change"),"Change list interpreted as markup");
      assert($test("audit-events").textContent.includes("<test-audit>")&&!$test("audit-events").querySelector("test-audit"),"Audit event interpreted as markup");
      assert($test("audit-events").textContent.includes("operator")&&$test("audit-events").querySelector("time").textContent,"Audit actor or timestamp missing");
      $test("save-button").click(); await pause(40);
      const apply=calls.find(call=>call.path==="/api/admin/apply");
      assert(apply&&apply.body.preview_id==="preview-1"&&apply.body.expected_revision===null&&apply.body.activate===false,"Save not bound to reviewed revision");
      assert($test("saved-revision").textContent.includes("saved-revisi"),"Saved revision missing");
      assert($test("runtime-state").textContent==="STOPPED","Save silently activated runtime");
      assert($test("readiness-state").textContent==="WAITING FOR TOPOLOGY"&&!$test("readiness-state").classList.contains("good"),"Saved state falsely claims network readiness");
      assert($test("message").textContent.includes("Activation has not been requested"),"Saved and active states conflated");
      assert($test("save-button").disabled,"Consumed preview can be reused");
      editName("changed");
      assert($test("runtime-start").disabled,"Unsaved edits can start unreviewed config");
    """,
    "edit-invalidates": r"""
      await submitPreview();
      editName("SECOND");
      assert($test("preview-panel").hidden&&$test("save-button").disabled,"Edits retained prior approval");
      await submitPreview();
      assert(calls.at(-1).body.setup.controller.name==="SECOND","Preview missed edited value");
      $test("activate-button").click(); await pause(40);
      const apply=calls.find(call=>call.path==="/api/admin/apply");
      assert(apply.body.preview_id==="preview-2"&&apply.body.activate===true,"Activation used obsolete preview");
      assert($test("message").textContent.includes("Check network readiness"),"Activation claims network readiness");
    """,
    "bindings": r"""
      assert($test("add-abr").disabled,"ABR without explicit node is allowed");
      $test("add-node").click();
      const node=$test("node-rows").firstElementChild;
      node.querySelector(".node-router-id").value="192.0.2.1";
      node.querySelector(".node-name").value="ABR1";
      node.querySelector(".node-name").dispatchEvent(new Event("input",{bubbles:true}));
      $test("add-abr").click();
      const abr=$test("abr-rows").firstElementChild;
      abr.querySelector(".abr-node").value="ABR1";
      abr.querySelector(".abr-node").dispatchEvent(new Event("change",{bubbles:true}));
      assert(abr.querySelector(".abr-te-router-id").value==="192.0.2.1"&&abr.querySelector(".abr-te-router-id").readOnly,"ABR TE identity not tied to explicit node");
      abr.querySelector(".abr-igp-router-id").value="192.0.2.101";
      abr.querySelector(".abr-areas").value="0.0.0.0, 0.0.0.17";
      await submitPreview();
      const sent=calls.at(-1).body.setup;
      assert(sent.operating_mode==="observe"&&sent.version===1,"UI enables control mode");
      assert(JSON.stringify(sent.nodes)==='[{"router_id":"192.0.2.1","name":"ABR1"}]',"Node labels malformed");
      assert(JSON.stringify(sent.abr_bindings)==='[{"node_id":"ABR1","te_router_id":"192.0.2.1","igp_router_id":"192.0.2.101","areas":["0.0.0.0","0.0.0.17"]}]',"Explicit binding malformed");
      node.querySelector(".remove").click();
      assert(!abr.querySelector(".abr-te-router-id").value&&$test("save-button").disabled,"Removed node retained binding approval");
    """,
    "conflict": r"""
      editName("MY-UNSAVED-EDIT");
      await submitPreview();
      assert($test("message").textContent.includes("Reload saved configuration"),"Stale revision lacks recovery guidance");
      assert($test("controller-name").value==="MY-UNSAVED-EDIT","409 silently discarded edits");
      assert($test("save-button").disabled&&calls.every(call=>call.path!=="/api/admin/apply"),"409 permits stale apply");
      $test("reload-config").click(); await pause(20);
      assert($test("controller-name").value==="NINJA-PCE","Explicit reload failed");
    """,
    "delayed": r"""
      $test("setup-form").requestSubmit(); await pause(10);
      editName("CHANGED-IN-FLIGHT"); await pause(90);
      assert($test("preview-panel").hidden&&$test("save-button").disabled,"Late preview approved changed form values");
    """,
    "unavailable": r"""
      statusUnavailable=true;
      $test("refresh-status").click(); await pause(20);
      assert($test("runtime-state").textContent==="Unavailable"&&$test("readiness-state").textContent==="Unavailable","Outage retains current status claim");
      assert(!$test("workspace").hidden&&!$test("preview-button").disabled,"Network/status outage disables setup editing");
      await submitPreview();
      assert(!$test("save-button").disabled,"Unreachable source blocks config preview");
    """,
    "source-managed": r"""
      assert($test("source-management-hint").textContent.includes("This setup owns GoBGP")&&$test("source-management-hint").textContent.includes("applies the reviewed BGP configuration"),"Managed source ownership is unclear");
      assert($test("controller-source-address").getAttribute("aria-describedby")==="source-management-hint"&&$test("peer-rows").querySelector(".peer-address").getAttribute("aria-describedby")==="source-management-hint","Source ownership guidance is not tied to source fields");
    """,
    "source-external": r"""
      assert($test("source-management-hint").textContent.includes("GoBGP is managed externally")&&$test("source-management-hint").textContent.includes("applies collector settings"),"External GoBGP changes imply native activation");
      assert($test("source-management-hint").textContent.includes("external service workflow"),"External source lacks configuration review guidance");
    """,
    "source-unknown": r"""
      assert($test("source-management-hint").textContent==="Check source ownership before activation.","Unknown source ownership claims managed activation");
    """,
    "apply-uncertain": r"""
      await submitPreview();
      $test("activate-button").click(); await pause(40);
      assert($test("message").classList.contains("error")&&!$test("message").classList.contains("success"),"Uncertain activation appears successful");
      assert($test("message").textContent.includes("could not be confirmed")&&$test("message").textContent.includes("ninja-pce admin reconcile"),"Uncertain activation lacks explicit local recovery guidance");
    """,
    "runtime-failed": r"""
      $test("runtime-start").click(); await pause(40);
      assert($test("message").classList.contains("error"),"Failed runtime action lacks error state");
      assert($test("message").textContent.includes("Port 50051 occupied by <test-owner>")&&!$test("message").querySelector("test-owner"),"Original runtime cause missing or interpreted as HTML");
      assert(!$test("message").textContent.includes("start requested"),"Failed runtime action presents a start confirmation");
    """,
    "runtime-uncertain": r"""
      $test("runtime-stop").click(); await pause(40);
      assert($test("message").classList.contains("error")&&$test("message").textContent.includes("shutdown was not confirmed"),"Uncertain stop lacks original cause");
      assert($test("message").textContent.includes("ninja-pce admin reconcile"),"Uncertain stop lacks local recovery guidance");
    """,
    "unsafe-instance": r"""
      assert($test("setup-fields").disabled&&$test("preview-button").disabled,"Unsafe integer revision can be edited and rounded");
      assert($test("message").textContent.includes("exact integer range"),"Integer precision limitation is unexplained");
      assert(calls.length===0,"Unsafe revision issued a mutation");
    """,
    "expired-session": r"""
      await submitPreview();
      sessionExpired=true;
      $test("refresh-status").click(); await pause(20);
      assert($test("workspace").hidden&&!$test("auth-panel").hidden,"Expired session retains controls");
      assert($test("save-button").disabled,"Expired session retains preview authority");
    """,
}


@pytest.mark.skipif(browser_ui.CHROME is None, reason="Optional headless Chromium is unavailable")
@pytest.mark.parametrize("scenario", CASES)
def test_guided_administration_controls(tmp_path: Path, scenario: str):
    index = (WEB / "setup.html").read_text().replace(
        '<script src="/setup.js" defer></script>',
        '<script src="/harness.js" defer></script><script src="/setup.js" defer></script>'
        '<script src="/case.js" defer></script>')
    harness = "window.testScenario=" + json.dumps(scenario) + ";\n" + HARNESS
    test_case = ("(async()=>{try{await pause(40);" + CASES[scenario] +
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
            elif self.path in {"/setup.js", "/setup.css"}:
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
        result = subprocess.run([str(browser_ui.CHROME), "--headless", "--no-sandbox", "--disable-gpu",
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
    assert 'data-test-result="passed"' in result.stdout, result.stdout[-9000:]
