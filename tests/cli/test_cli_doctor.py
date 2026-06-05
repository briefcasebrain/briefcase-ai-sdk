"""briefcase doctor: ordered preflight checks via an injected runner + reflection probe, and the
severity -> exit-code mapping. No Docker, no live server."""
from argparse import Namespace

from briefcase.cli.doctor import FAIL, OK, WARN, Doctor, summarize

COMPAT = {
    "briefcase_ai": "3.3.0",
    "oci_jj_api_min": "v1",
    "oci_jj_image_tag": "0.1.0",
    "private_preview": True,
    "oci_jj_images": ["ghcr.io/briefcasebrain/oci-jj-server"],
    "oci_jj_scorer_image": "ghcr.io/briefcasebrain/oci-jj-verdict-worker",
    "verdictml": "v0.1.0",
}


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
    runner = QueuedRunner(returncodes=[0, 0, 0], stdout="27.0")  # docker version, public img, private img
    doctor = Doctor(runner=runner, compat=COMPAT, probe=lambda s: ["oci_jj.v1.VcsService"])
    results = doctor.checks(server="http://127.0.0.1:50051")
    assert _sev(results, "docker") == [OK]
    assert _sev(results, "image") == [OK]
    assert _sev(results, "scorer image") == [OK]
    assert _sev(results, "server") == [OK]
    assert summarize(results) == 0


def test_docker_missing_is_fail():
    def boom(argv, **kw):
        raise FileNotFoundError("docker")

    doctor = Doctor(runner=boom, compat=COMPAT, probe=lambda s: ["oci_jj.v1.VcsService"])
    results = doctor.checks()
    assert _sev(results, "docker") == [FAIL]
    assert summarize(results) == 1


def test_unpublished_images_and_unreachable_server_are_warnings():
    runner = QueuedRunner(returncodes=[0, 1, 1], stdout="27.0")  # docker ok, public + private inspect fail

    def unreachable(server):
        raise RuntimeError("connection refused")

    doctor = Doctor(runner=runner, compat=COMPAT, probe=unreachable)
    results = doctor.checks()
    assert _sev(results, "image") == [WARN]
    assert _sev(results, "scorer image") == [WARN]
    assert _sev(results, "server") == [WARN]
    assert summarize(results) == 0           # warnings are non-fatal by default
    assert summarize(results, strict=True) == 1  # ...but --strict escalates them


def test_old_server_without_v1_is_fail():
    runner = QueuedRunner(returncodes=[0, 0, 0], stdout="27.0")
    doctor = Doctor(runner=runner, compat=COMPAT, probe=lambda s: ["legacy.v0.OldService"])
    results = doctor.checks()
    assert _sev(results, "server") == [FAIL]
    assert summarize(results) == 1


def test_summarize_severity_ordering():
    assert summarize([(OK, "a", "")]) == 0
    assert summarize([(OK, "a", ""), (WARN, "b", "")]) == 0
    assert summarize([(WARN, "b", "")], strict=True) == 1
    assert summarize([(OK, "a", ""), (FAIL, "c", "")]) == 1
