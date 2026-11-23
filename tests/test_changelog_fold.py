"""Tests for scripts/changelog_fold.py using small fixture changelogs."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "changelog_fold.py"
spec = importlib.util.spec_from_file_location("changelog_fold", SCRIPT)
fold = importlib.util.module_from_spec(spec)
sys.modules["changelog_fold"] = fold
spec.loader.exec_module(fold)

HEADER = """# Changelog

## [Unreleased]

### Added
- Existing feature.

### Changed
- Existing change.
- **Breaking:** existing break.

## [1.0.0] - 2026-01-01

### Added
- First release.
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(HEADER, encoding="utf-8")
    return tmp_path


def frag(repo, name, text):
    (repo / "changelog.d" / name).write_text(text, encoding="utf-8")


def run(repo, *args):
    return fold.main(["--root", str(repo), *args])


def changelog(repo):
    return (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def unreleased(repo):
    return fold.unreleased_text(changelog(repo))


def test_folds_and_deletes_fragments(repo, capsys):
    frag(repo, "a.md", "### Added\n- New thing.\n\n### Fixed\n- A bug.\n")
    assert run(repo) == 0
    text = unreleased(repo)
    assert text.index("### Added") < text.index("### Changed") < text.index("### Fixed")
    assert "- Existing feature.\n- New thing." in text
    assert "### Fixed\n- A bug." in text
    assert "## [1.0.0] - 2026-01-01" in changelog(repo)
    assert not list((repo / "changelog.d").glob("*.md"))


def test_canonical_order_regardless_of_fragment_order(repo):
    frag(repo, "a.md", "### Security\n- S.\n\n### Removed\n- R.\n\n### Deprecated\n- D.\n")
    run(repo)
    text = unreleased(repo)
    order = [text.index(f"### {n}") for n in ("Added", "Changed", "Deprecated", "Removed", "Security")]
    assert order == sorted(order)


def test_breaking_bullets_come_first_including_existing(repo):
    frag(repo, "a.md", "### Changed\n- Soft change.\n- **Breaking:** new break.\n")
    run(repo)
    section = unreleased(repo).split("### Changed\n")[1]
    lines = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert lines[0].startswith("- **Breaking:**") and lines[1].startswith("- **Breaking:**")
    assert [ln.startswith("- **Breaking") for ln in lines] == [True, True, False, False]


def test_breaking_heading_maps_to_changed_with_prefix(repo):
    frag(repo, "a.md", "### Breaking\n- Dropped Python 3.8.\n")
    run(repo)
    changed = unreleased(repo).split("### Changed\n")[1]
    assert changed.splitlines()[:2] == ["- **Breaking:** existing break.", "- **Breaking:** Dropped Python 3.8."]


def test_unknown_heading_is_reported_and_nothing_changes(repo, capsys):
    frag(repo, "a.md", "### Added\n- Fine.\n")
    frag(repo, "b.md", "### Improvements\n- Vague.\n")
    before = changelog(repo)
    assert run(repo) == 2
    assert "cannot map heading 'Improvements'" in capsys.readouterr().err
    assert changelog(repo) == before
    assert (repo / "changelog.d" / "a.md").exists() and (repo / "changelog.d" / "b.md").exists()


def test_text_before_first_heading_is_an_error(repo, capsys):
    frag(repo, "a.md", "- orphan bullet\n")
    assert run(repo) == 2
    assert "before the first heading" in capsys.readouterr().err


def test_identical_bullets_are_dropped_whitespace_insensitively(repo, capsys):
    frag(repo, "a.md", "### Added\n- Shared   entry\n  across lines.\n- Existing feature.\n")
    frag(repo, "b.md", "### Added\n- Shared entry across lines.\n")
    assert run(repo) == 0
    text = unreleased(repo)
    assert text.count("Shared entry across lines.") == 1
    assert text.count("Existing feature.") == 1
    assert "dropped duplicate" in capsys.readouterr().err


def test_same_text_in_different_sections_is_kept(repo):
    frag(repo, "a.md", "### Fixed\n- Existing feature.\n")
    run(repo)
    assert unreleased(repo).count("Existing feature.") == 2


def test_near_duplicates_are_reported_not_merged(repo, capsys):
    frag(repo, "a.md", "### Added\n- `tracer.start_span`, `tracer.end_span` and `SpanKind` record "
                       "every call with timings.\n")
    frag(repo, "b.md", "### Changed\n- `tracer.start_span` and `tracer.end_span` now record spans "
                       "per call, with `SpanKind`.\n")
    assert run(repo) == 0
    err = capsys.readouterr().err
    assert "possible near-duplicate" in err
    text = unreleased(repo)
    assert "tracer.start_span" in text.split("### Changed")[0]
    assert "tracer.start_span" in text.split("### Changed")[1]


def test_strict_aborts_on_near_duplicates(repo):
    frag(repo, "a.md", "### Added\n- `tracer.start_span`, `tracer.end_span` and `SpanKind` record calls.\n")
    frag(repo, "b.md", "### Changed\n- `tracer.start_span` and `tracer.end_span` use `SpanKind`.\n")
    before = changelog(repo)
    assert run(repo, "--strict") == 2
    assert changelog(repo) == before
    assert (repo / "changelog.d" / "a.md").exists()


def test_long_bullets_wrap_at_80_with_hanging_indent(repo):
    frag(repo, "a.md", "### Added\n- " + "word " * 40 + "\n")
    run(repo)
    new = [ln for ln in unreleased(repo).splitlines() if "word" in ln]
    assert len(new) > 1
    assert all(len(ln) <= 80 for ln in new)
    assert new[0].startswith("- ") and all(ln.startswith("  ") for ln in new[1:])


def test_idempotent(repo):
    frag(repo, "a.md", "### Added\n- New thing.\n")
    run(repo)
    once = changelog(repo)
    assert run(repo) == 0
    assert changelog(repo) == once


def test_readme_in_fragment_dir_is_ignored_and_kept(repo):
    (repo / "changelog.d" / "README.md").write_text("how to write fragments\n")
    assert run(repo, "--check") == 0
    frag(repo, "a.md", "### Added\n- X.\n")
    assert run(repo) == 0
    assert (repo / "changelog.d" / "README.md").exists()


def test_check(repo, capsys):
    assert run(repo, "--check") == 0
    frag(repo, "a.md", "### Added\n- X.\n")
    assert run(repo, "--check") == 1
    assert "changelog.d/a.md" in capsys.readouterr().err
    assert changelog(repo) == HEADER and (repo / "changelog.d" / "a.md").exists()


def test_dry_run_prints_and_changes_nothing(repo, capsys):
    frag(repo, "a.md", "### Added\n- Preview me.\n")
    assert run(repo, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Preview me." in out and "changelog.d/a.md" in out
    assert changelog(repo) == HEADER
    assert (repo / "changelog.d" / "a.md").exists()


def test_release_renames_section_and_adds_fresh_unreleased(repo):
    frag(repo, "a.md", "### Fixed\n- A bug.\n")
    assert run(repo, "--release", "1.1.0", "--date", "2026-02-03") == 0
    text = changelog(repo)
    assert "## [Unreleased]\n\n## [1.1.0] - 2026-02-03\n\n### Added" in text
    assert text.index("## [1.1.0]") < text.index("## [1.0.0]")
    assert "### Fixed\n- A bug." in text
    assert text.count("## [Unreleased]") == 1
    assert fold.unreleased_text(text).strip() == "## [Unreleased]"
    assert not list((repo / "changelog.d").glob("a.md"))


def test_release_without_fragments_and_twice(repo, capsys):
    assert run(repo, "--release", "1.1.0", "--date", "2026-02-03") == 0
    assert run(repo, "--release", "1.1.1") == 1  # Unreleased is now empty
    assert "nothing to release" in capsys.readouterr().err


def test_release_refuses_existing_version_and_bad_args(repo):
    assert run(repo, "--release", "1.0.0") == 2
    with pytest.raises(SystemExit):
        run(repo, "--release", "not-a-version")
    with pytest.raises(SystemExit):
        run(repo, "--date", "2026-01-01")
    with pytest.raises(SystemExit):
        run(repo, "--release", "1.1.0", "--date", "yesterday")


def test_release_updates_compare_links(repo):
    (repo / "CHANGELOG.md").write_text(
        HEADER + "\n[Unreleased]: https://github.com/o/r/compare/v1.0.0...HEAD\n"
        "[1.0.0]: https://github.com/o/r/releases/tag/v1.0.0\n", encoding="utf-8")
    assert run(repo, "--release", "1.1.0", "--date", "2026-02-03") == 0
    text = changelog(repo)
    assert "[Unreleased]: https://github.com/o/r/compare/v1.1.0...HEAD" in text
    assert "[1.1.0]: https://github.com/o/r/compare/v1.0.0...v1.1.0" in text
    assert "[1.0.0]: https://github.com/o/r/releases/tag/v1.0.0" in text


def test_no_links_means_none_added(repo):
    run(repo, "--release", "1.1.0")
    assert "compare/" not in changelog(repo)


def test_nested_bullets_are_preserved(repo):
    frag(repo, "a.md", "### Added\n- Parent item:\n  - child one\n  - child two\n")
    run(repo)
    assert "- Parent item:\n  - child one\n  - child two" in unreleased(repo)
