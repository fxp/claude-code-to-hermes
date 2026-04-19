# Release runbook

How to cut a new release of `claude-code-migration`. Follow top-to-bottom;
everything is idempotent so it's safe to retry a step if it fails.

## Prerequisites

One-time setup, per machine:

```bash
pip install --upgrade build twine
# For PyPI publishing:
# Get an API token at https://pypi.org/manage/account/token/
# Put it in ~/.pypirc under [pypi] or export TWINE_PASSWORD
```

## Step 1 · Pre-flight checks

```bash
# 1. clean tree
git status
# 2. on main, up to date
git checkout main && git pull
# 3. tests pass
python -m pytest tests/ -q    # expect all green
# 4. fresh install still works
python -m build
pip install --force-reinstall dist/*.whl
ccm --help
ccm --version   # not yet, see note in Step 3
```

## Step 2 · Version bump

Bump in two files that must match:

```bash
# src/claude_code_migration/__init__.py
__version__ = "X.Y.Z"

# pyproject.toml
version = "X.Y.Z"
```

Semver guidance:

| change category | bump |
|---|---|
| backward-incompatible CLI flag removal, dossier schema break | MAJOR |
| new source / target / verb, new scanner data type | MINOR |
| bug fix, docs, internal refactor | PATCH |

Add an entry to `CHANGELOG.md` under `## [Unreleased]`, then move it
into a new `## [X.Y.Z] — YYYY-MM-DD` section. Update the compare links
at the bottom:

```markdown
[Unreleased]: https://github.com/fxp/claude-code-migration/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/fxp/claude-code-migration/releases/tag/vX.Y.Z
```

## Step 3 · Build + verify

```bash
rm -rf dist/ build/ src/*.egg-info
python -m build                  # produces sdist + wheel
python -m twine check dist/*     # must print "PASSED" for both
```

Install the wheel into a scratch venv and smoke the CLI:

```bash
python -m venv /tmp/ccm-release-test
/tmp/ccm-release-test/bin/pip install dist/*.whl
/tmp/ccm-release-test/bin/ccm --help
/tmp/ccm-release-test/bin/python -c "
import claude_code_migration as m
assert m.__version__ == 'X.Y.Z'
from claude_code_migration import WorkspaceDossier, redact
print('OK')"
```

## Step 4 · Commit + tag

```bash
git add pyproject.toml src/claude_code_migration/__init__.py CHANGELOG.md
git commit -m "release: vX.Y.Z"
git push origin main

# Tag + push. Annotated tags show up in `git tag -n` and GitHub Releases.
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin vX.Y.Z
```

## Step 5 · GitHub Release

```bash
gh release create vX.Y.Z \
  --title "X.Y.Z" \
  --notes-file <(awk '/^## \[X.Y.Z\]/,/^## \[/{print}' CHANGELOG.md | head -n -1) \
  dist/claude_code_migration-X.Y.Z-py3-none-any.whl \
  dist/claude_code_migration-X.Y.Z.tar.gz
```

Verify on <https://github.com/fxp/claude-code-migration/releases>.

## Step 6 · Publish to PyPI (optional)

The package is not yet on PyPI. When ready:

```bash
# Dry run against TestPyPI first
python -m twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ claude-code-migration==X.Y.Z

# Production
python -m twine upload dist/*
```

Twine reads `~/.pypirc` or `TWINE_USERNAME` / `TWINE_PASSWORD` env vars.
Use a project-scoped token — not your account password.

## Step 7 · Post-release

- Bump landing page badge if it hard-codes a version (check `index.html`
  for `package-vX.Y.Z`)
- Check that GitHub Pages picked up any docs changes:
  `gh api repos/fxp/claude-code-migration/pages/builds --jq '.[0]'`
- Announce in the usual channels

## Rollback

If a release has a critical bug:

```bash
# PyPI — yank (don't delete) so existing pins don't break
python -m twine yank claude-code-migration==X.Y.Z \
  --reason "critical bug; use X.Y.(Z+1)"

# GitHub — edit the release and mark as pre-release, or delete the tag
gh release delete vX.Y.Z
git push --delete origin vX.Y.Z
git tag -d vX.Y.Z
```

Ship X.Y.(Z+1) with the fix immediately — don't let users sit on the
yanked version.
