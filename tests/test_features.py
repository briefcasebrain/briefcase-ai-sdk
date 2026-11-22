"""The feature manifest loader, the manifests shipped in ``briefcase/_features``, and the
check that they travel with the installed package."""

import fnmatch
import json
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from briefcase.features import REQUIRED, VERBS, Catalog, load_catalog, parse_feature

REPO = Path(__file__).resolve().parent.parent
MANIFESTS = REPO / "briefcase" / "_features"
INCLUDE_LINE = '"briefcase/_features/*.json",'

GOOD = {
    "id": "policy-hooks", "title": "Policy for coding tools", "verb": "enforce",
    "one_line": "Allow, ask or deny what Claude Code and Cursor may run.",
    "command": "briefcase policy init", "docs_slug": "enforce/policy-hooks",
    "status": "experimental", "requires_extra": None,
    "verified_against": "Claude Code 2.1.330 (real install); Cursor (docs only)",
}


def write(directory, name, data):
    path = directory / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


# --- loader ---------------------------------------------------------------------

def test_contract_example_loads_unchanged(tmp_path):
    write(tmp_path, "policy-hooks.json", GOOD)
    catalog = load_catalog(tmp_path)
    assert catalog.problems == []
    (feature,) = catalog.features
    assert {key: getattr(feature, key) for key in GOOD} == GOOD
    assert feature.docs_file == "docs/features/policy-hooks.md"
    assert feature.docs_url.endswith("/blob/main/docs/features/policy-hooks.md")


def test_malformed_files_are_skipped_with_a_warning_never_an_exception(tmp_path, caplog):
    write(tmp_path, "a-good.json", dict(GOOD, id="a-good"))
    write(tmp_path, "broken.json", "{ this is not json")
    write(tmp_path, "list.json", "[1, 2, 3]")
    write(tmp_path, "missing.json", {"id": "missing", "title": "No verb"})
    write(tmp_path, "wrong-type.json", dict(GOOD, id="wrong-type", title=7))
    write(tmp_path, "bad-verb.json", dict(GOOD, id="bad-verb", verb="observe"))
    (tmp_path / "binary.json").write_bytes(b"\xff\xfe\x00garbage")
    write(tmp_path, "notes.txt", "ignored: not a .json file")
    (tmp_path / "sub.json").mkdir()                      # a directory named like a manifest
    with caplog.at_level("WARNING", logger="briefcase"):
        catalog = load_catalog(tmp_path)
    assert [f.id for f in catalog.features] == ["a-good"]
    skipped = {problem.split(":")[0] for problem in catalog.problems}
    assert skipped == {"_features/broken.json", "_features/list.json", "_features/missing.json",
                       "_features/wrong-type.json", "_features/bad-verb.json", "_features/binary.json",
                       "_features/sub.json"}
    assert all("skipped" in problem for problem in catalog.problems)


def test_unknown_keys_are_tolerated_and_kept(tmp_path):
    write(tmp_path, "x.json", dict(GOOD, id="x", future_field={"a": 1}, owner="team-a"))
    catalog = load_catalog(tmp_path)
    assert catalog.problems == []
    assert catalog.features[0].extra == {"future_field": {"a": 1}, "owner": "team-a"}
    assert "future_field" not in catalog.features[0].to_dict()


def test_unknown_status_is_shown_as_experimental_and_reported(tmp_path):
    write(tmp_path, "x.json", dict(GOOD, id="x", status="beta"))
    catalog = load_catalog(tmp_path)
    assert catalog.features[0].status == "experimental"
    assert len(catalog.problems) == 1 and "'beta'" in catalog.problems[0]


def test_soft_problems_keep_the_feature(tmp_path):
    write(tmp_path, "other-name.json", dict(GOOD, one_line="x" * 80, requires_extra=["a"]))
    catalog = load_catalog(tmp_path)
    assert [f.id for f in catalog.features] == ["policy-hooks"]
    assert catalog.features[0].requires_extra is None
    text = " | ".join(catalog.problems)
    assert "one_line is 80 characters" in text and "requires_extra must be text" in text
    assert "file name does not match id" in text


def test_duplicate_ids_keep_the_first_file(tmp_path):
    write(tmp_path, "a.json", dict(GOOD, id="same", title="First"))
    write(tmp_path, "b.json", dict(GOOD, id="same", title="Second"))
    catalog = load_catalog(tmp_path)
    assert [f.title for f in catalog.features] == ["First"]
    assert any("already used by _features/a.json" in problem for problem in catalog.problems)


def test_a_missing_directory_is_an_empty_catalog(tmp_path):
    catalog = load_catalog(tmp_path / "nope")
    assert isinstance(catalog, Catalog) and catalog.features == [] and catalog.problems == []


def test_groups_follow_the_flow_order(tmp_path):
    for index, verb in enumerate(reversed(VERBS)):
        write(tmp_path, f"f{index}.json", dict(GOOD, id=f"f{index}", verb=verb))
    catalog = load_catalog(tmp_path)
    assert [verb for verb, _ in catalog.groups()] == list(VERBS)
    data = catalog.to_dict()
    assert data["count"] == 5 and [group["verb"] for group in data["groups"]] == list(VERBS)
    assert parse_feature("nope")[0] is None


# --- the manifests shipped at HEAD ------------------------------------------------

def shipped():
    return sorted(MANIFESTS.glob("*.json"))


def test_shipped_manifests_are_all_valid():
    catalog = load_catalog()
    assert catalog.problems == []
    assert len(catalog.features) == len(shipped()) >= 21
    assert {verb for verb, _ in catalog.groups()} == set(VERBS)


@pytest.mark.parametrize("path", shipped(), ids=lambda p: p.stem)
def test_shipped_manifest(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    feature, problems = parse_feature(data, path.name)
    assert feature is not None and problems == []
    assert feature.id == path.stem and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", feature.id)
    assert all(data.get(key) for key in REQUIRED)
    assert feature.docs_slug == f"{feature.verb}/{feature.id}"
    assert len(feature.one_line) < 80 and feature.one_line.endswith(".")
    assert feature.verified_against, "say what was and was not run for real"
    # The page a feature links to must exist in the repository.
    assert (REPO / feature.docs_file.split("#")[0]).exists(), feature.docs_file
    # The first command must be a real briefcase command, unless the manifest says the
    # feature is started from another runtime (`"runtime": "node"`).
    from briefcase.cli.__main__ import build_parser

    words = shlex.split(feature.command)
    if feature.runtime == "node":
        assert words[0] in ("node", "npx", "npm"), feature.command
    else:
        assert feature.runtime in (None, "python")
        assert words[0] == "briefcase", "a non-briefcase command needs an explicit runtime in its manifest"
        parser = build_parser()
        assert words[1] in parser.subparsers.choices, feature.command
        if "--" not in words and "<" not in feature.command:
            parser.parse_args(words[1:])
    if feature.requires_extra:
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(rf"^{re.escape(feature.requires_extra)} = \[", pyproject, re.M), feature.requires_extra


def test_python_snippets_compile():
    for feature in load_catalog().features:
        if feature.snippet:
            compile(feature.snippet, f"<{feature.id}>", "exec")


def test_docs_catalog_page_is_generated_from_the_manifests():
    from briefcase.cli.features_commands import render_markdown

    page = REPO / "docs" / "features" / "catalog.md"
    assert page.read_text(encoding="utf-8") == render_markdown(load_catalog()), (
        "docs/features/catalog.md is stale. Regenerate it with:\n"
        "    briefcase features --markdown > docs/features/catalog.md"
    )
    for target in re.findall(r"\]\(([^)#]+)", page.read_text(encoding="utf-8")):
        assert (page.parent / target).resolve().exists(), target


# --- packaging --------------------------------------------------------------------

def _maturin_include():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("[tool.maturin]")[1].split("include = [", 1)[1].split("\n]", 1)[0]
    return re.findall(r'^\s*"([^"]+)",?\s*$', block, re.M)


def _installed_copy(target: Path, data_patterns) -> Path:
    """Lay ``briefcase`` out the way an installed wheel has it: every ``.py`` file, plus only
    the data files that ``data_patterns`` (the maturin ``include`` globs) match."""
    package = target / "briefcase"
    for source in (REPO / "briefcase").rglob("*"):
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(REPO).as_posix()
        if source.suffix == ".py" or any(fnmatch.fnmatch(relative, pattern) for pattern in data_patterns):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return package


def _count_from(path_entry: Path, cwd: Path) -> dict:
    script = (
        "import json, sys\n"
        "sys.modules['briefcase._native'] = None\n"
        "import briefcase, briefcase.features as f\n"
        "c = f.load_catalog()\n"
        "print(json.dumps({'count': len(c.features), 'problems': c.problems, 'file': briefcase.__file__}))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=str(cwd), capture_output=True, text=True,
                            timeout=120, env={"PYTHONPATH": str(path_entry), "PATH": ""})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_manifests_load_from_an_installed_layout_outside_the_checkout(tmp_path):
    """With the manifests present next to the installed code, they load through
    importlib.resources from a directory that is not the source tree."""
    site = tmp_path / "site-packages"
    _installed_copy(site, _maturin_include() + ["briefcase/_features/*.json"])
    loaded = _count_from(site, tmp_path)
    assert loaded["file"].startswith(str(site))
    assert loaded == {"count": len(shipped()), "problems": [], "file": loaded["file"]}


def test_manifests_load_from_a_zipped_package(tmp_path):
    """importlib.resources, not __file__ arithmetic: the same package imported from a zip
    still finds every manifest."""
    site = tmp_path / "unzipped"
    _installed_copy(site, _maturin_include() + ["briefcase/_features/*.json"])
    archive = tmp_path / "briefcase.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in site.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(site).as_posix())
    loaded = _count_from(archive, tmp_path)
    assert str(archive) in loaded["file"]
    assert loaded["count"] == len(shipped()) and loaded["problems"] == []


def test_the_wheel_ships_every_manifest(tmp_path):
    """maturin only packages the non-.py files that ``[tool.maturin] include`` lists. Build the
    installed layout from that list and load the catalog from it: a manifest that the list does
    not cover is missing for every user who installs the wheel."""
    patterns = _maturin_include()
    missing = [path.name for path in shipped()
               if not any(fnmatch.fnmatch(f"briefcase/_features/{path.name}", pattern) for pattern in patterns)]
    assert not missing, (
        f"pyproject.toml [tool.maturin] include does not ship {len(missing)} feature manifest(s) "
        f"({', '.join(missing[:3])}...). Add this line to the include list:\n    {INCLUDE_LINE}"
    )
    site = tmp_path / "site-packages"
    _installed_copy(site, patterns)
    assert _count_from(site, tmp_path)["count"] == len(shipped())
