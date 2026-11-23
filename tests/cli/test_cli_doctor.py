"""briefcase-ai doctor: ordered preflight checks via an injected runner + reflection probe, and the
severity -> exit-code mapping. No Docker, no live server."""
from argparse import Namespace

from briefcase.cli.doctor import FAIL, OK, WARN, Doctor, summarize


class QueuedRunner:
    """Returns a queued returncode per call (default 0)."""

    def __init__(self, returncodes=None, stdout=""):
        self._rcs = list(returncodes or [])
        self._stdout = stdout
        self.calls = []

    def __call__(self, argv, capture_output=False, text=False, **kw):
        self.calls.append(argv)
        rc = self._rcs.pop(0) if self._rcs else 0
        return Namespace(returncode=rc, stdout=self._stdout)


def _sev(results, label):
    return [sev for sev, lab, _ in results if lab == label]


def test_all_green_when_docker_images_and_server_ok():
    runner = QueuedRunner(returncodes=[0], stdout="27.0")  # docker version
    doctor = Doctor(runner=runner, probe=lambda s: ["oci_jj.v1.VcsService"])
    results = doctor.checks(server="http://127.0.0.1:50051")
    assert _sev(results, "docker") == [OK]
    assert _sev(results, "image") == [] and _sev(results, "bundled compose") == []
    assert _sev(results, "server") == [OK]
    assert summarize(results) == 0


def test_docker_missing_is_fail():
    def boom(argv, **kw):
        raise FileNotFoundError("docker")

    doctor = Doctor(runner=boom, probe=lambda s: ["oci_jj.v1.VcsService"])
    results = doctor.checks()
    assert _sev(results, "docker") == [FAIL]
    assert summarize(results) == 1


def test_unreachable_server_is_a_warning():
    runner = QueuedRunner(returncodes=[0], stdout="27.0")

    def unreachable(server):
        raise RuntimeError("connection refused")

    doctor = Doctor(runner=runner, probe=unreachable)
    results = doctor.checks()
    assert _sev(results, "server") == [WARN]
    assert summarize(results) == 0           # warnings are non-fatal by default
    assert summarize(results, strict=True) == 1  # ...but --strict escalates them


def test_old_server_without_v1_is_fail():
    runner = QueuedRunner(returncodes=[0], stdout="27.0")
    doctor = Doctor(runner=runner, probe=lambda s: ["legacy.v0.OldService"])
    results = doctor.checks()
    assert _sev(results, "server") == [FAIL]
    assert summarize(results) == 1


def test_summarize_severity_ordering():
    assert summarize([(OK, "a", "")]) == 0
    assert summarize([(OK, "a", ""), (WARN, "b", "")]) == 0
    assert summarize([(WARN, "b", "")], strict=True) == 1
    assert summarize([(OK, "a", ""), (FAIL, "c", "")]) == 1


def test_secured_server_is_reported_as_auth_not_unreachable():
    """A server with OCI_JJ_AUTH_TOKENS rejects reflection with UNAUTHENTICATED: doctor must say a
    token is needed, not that the stack is down."""
    def probe(server):
        raise RuntimeError("status = StatusCode.UNAUTHENTICATED details = missing or invalid bearer token")

    results = Doctor(runner=QueuedRunner(), probe=probe).checks()
    server = [(sev, detail) for sev, lab, detail in results if lab == "server"]
    assert server[0][0] == FAIL
    assert "OCI_JJ_AUTH_TOKEN" in server[0][1]
