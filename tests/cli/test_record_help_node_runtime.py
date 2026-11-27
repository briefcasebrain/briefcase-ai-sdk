"""Compare fixed development probes with the built SDK. Record-help itself never launches Node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from briefcase.cli.record_help_commands import _TABLE, _node_endpoint, route


PACKAGE = Path(__file__).resolve().parents[2] / "js/auto"
TABLE = json.loads(_TABLE.read_text())


@pytest.fixture(scope="module")
def run_node():
    executable = shutil.which("node")
    if not executable or not (PACKAGE / "dist/register.js").is_file():
        pytest.skip("Build js/auto and install a supported Node runtime to compare actual launch behavior")
    major = int(subprocess.check_output([executable, "-p", "process.versions.node.split('.')[0]"], text=True))
    if major < 26:
        pytest.skip("The auto SDK supports Node 26 and higher")

    def run(args, env=None):
        return subprocess.run([executable, *args], cwd=PACKAGE,
                              env={"BRIEFCASE_EXPORTER": "memory", **(env or {})},
                              text=True, capture_output=True, timeout=15)

    return run


def test_packaged_exports_and_actual_preload(run_node):
    package = json.loads((PACKAGE / "package.json").read_text())
    exports = {package["name"] + subpath[1:] for subpath in package["exports"]}
    assert set(TABLE["inlineNode"]["packageExports"]) == exports
    code = 'const {activation}=await import("@briefcase-ai/auto/register");console.log(activation.active)'
    module = "@briefcase-ai/auto/register"
    for argv in (["--import", module, "-e", code], ["-e", code, "--import", module]):
        result = run_node(argv)
        assert result.returncode == 0 and result.stdout.strip() == "true", result.stderr
    for flag, active in [("0", "false"), ("1", "true")]:
        result = run_node(["-e", code], {"BRIEFCASE_AUTO": flag, "NODE_OPTIONS": "--import " + module})
        assert result.returncode == 0 and result.stdout.strip() == active, result.stderr
        launch = f'BRIEFCASE_EXPORTER=memory BRIEFCASE_AUTO={flag} NODE_OPTIONS="--import {module}" node app.js'
        assert route(TABLE, launch)[0] == ("memory_only" if active == "true" else "inactive_preload")
    result = run_node(["--import", module + ".js", "-e", "0"])
    assert result.returncode != 0 and "ERR_PACKAGE_PATH_NOT_EXPORTED" in result.stderr
    assert route(TABLE, "BRIEFCASE_EXPORTER=memory node --import " + module + '.js -e "0"')[0] == "preload_startup"


def test_direct_and_otlp_hosts_match_node_construction(run_node):
    urls = ["https://example.invalid/ingest?region=us", "https://bad%2Fhost/events", "http://127.1:4318/x",
            "https://999.999.999.999./events", "http://external.invalid/path", "https://%65xample.invalid/a",
            "http://0x7f000001/query", "http://[::1]:4318/x", "https://example.invalid:65536/x"]
    # Construct transports only. Never call export or send an HTTP request.
    probe = '''
        import {HttpExporter, OtlpHttpExporter} from "@briefcase-ai/auto";
        const values = JSON.parse(process.argv[1]);
        const accepts = (fn) => {try { fn(); return true; } catch { return false; }};
        console.log(JSON.stringify(values.map(url => [
            accepts(()=>new HttpExporter(url)), accepts(()=>new OtlpHttpExporter(url, {allowInsecure:true}))
        ])));
    '''
    observed = run_node(["--input-type=module", "-e", probe, json.dumps(urls)])
    assert observed.returncode == 0, observed.stderr
    expected = [[_node_endpoint(url, allow_insecure=False), _node_endpoint(url)] for url in urls]
    assert json.loads(observed.stdout) == expected


def test_worker_observe_identity_compared_with_guidance(run_node):
    probe = '''
        import {Worker} from "node:worker_threads";
        import {observe,MemoryExporter} from "@briefcase-ai/auto";
        observe(new MemoryExporter());
        const sdk=import.meta.resolve("@briefcase-ai/auto");
        const source='import {parentPort} from "node:worker_threads";' +
            'import {observe,MemoryExporter} from '+JSON.stringify(sdk)+';' +
            'const forwarded=observe().constructor.name;const own=new MemoryExporter();' +
            'parentPort.postMessage([forwarded,observe(own)===own]);';
        const worker=new Worker(new URL("data:text/javascript,"+encodeURIComponent(source)),{execArgv:[]});
        worker.once("message", async (r)=>{console.log(JSON.stringify(r));await worker.terminate();});
    '''
    actual = run_node(["--input-type=module", "-e", probe])
    assert actual.returncode == 0, actual.stderr
    assert json.loads(actual.stdout) == ["WorkerForwardExporter", True]
    for arg, expected in [("", "worker_forward"), ("new MemoryExporter()", "memory_only")]:
        q = (f"My original Node worker thread called observe({arg}). "
             "The main thread started observing before it created the worker.")
        assert route(TABLE, q)[0] == expected


def test_loader_thread_and_option_quoting(run_node):
    module = "@briefcase-ai/auto/register"
    probe = 'import {status} from "@briefcase-ai/auto";console.log(status().observing)'
    for flag, active in [("--import", "true"), ("--loader", "false")]:
        result = run_node(["--input-type", "module", flag, module, "-e", probe])
        assert result.returncode == 0 and result.stdout.strip() == active, result.stderr
    rejected = run_node(["-e", "0"], {"BRIEFCASE_AUTO": "1", "NODE_OPTIONS": "--import '" + module + "'"})
    assert rejected.returncode and "ERR_MODULE_NOT_FOUND" in rejected.stderr
