# Changelog fragments

Add one Markdown file here per change instead of editing `CHANGELOG.md`, so
parallel branches never conflict. Name it after the change
(`changelog.d/my-feature.md`).

```markdown
### Added
- `briefcase-ai thing --flag`: what it does, in one or two sentences.

### Fixed
- What was wrong and what happens now.
```

- Headings: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.
  `### Breaking` is also accepted and becomes a `**Breaking:**` bullet under
  Changed. Any other heading makes the fold fail.
- Write `- **Breaking:** ...` for incompatible changes; they sort first.
- One bullet per change, user-facing, in the present tense of the release
  (describe what ships, not interim states of your branch).

At release time a maintainer runs `python scripts/changelog_fold.py --release
X.Y.Z`, which merges the fragments into `CHANGELOG.md`, deletes them and cuts
the section (`--dry-run` previews, `--check` fails while fragments remain). This
README is never folded.

A fragment can retire an Unreleased bullet that this release replaces (an interim state that never
shipped) with a line `Supersedes: "text from the old bullet"` anywhere in the file. The bullet
already in Unreleased whose whole text equals it (whitespace aside) is removed; if none does, or the text
is empty, the fold fails. Bullets from
other fragments are never removed.
