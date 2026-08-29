# Tools

The 'tools' directory is reserved for test tools and scripts. Nothing
in this directory will be shipped as part of the packaging

## Modern tools (uv workspace)

A tool with real third-party dependencies and its own test suite lives
as a member of the `tools/` uv workspace (`pyproject.toml` in this
directory) rather than as a flat script below -- one shared virtual
environment and lockfile (`uv sync --all-extras` from here) across every
such tool, so adding another one doesn't mean managing another venv.
Simple, dependency-free scripts stay flat in this directory as before.

- `test_catalog/`: read-optimized SQLite catalog of the `features/*.feature`
  suite -- see `test_catalog/README.md`.

When running a member's own tooling (pytest, black, mypy, ...) from
`tools/` rather than from inside the member directory, use
`uv run --directory <member> <command>`, not `--project`. `--project`
only selects which member's dependencies/venv to use -- it does not
change the working directory -- so pytest's own upward config search
(cwd-based) would otherwise walk straight past that member's
`pyproject.toml` and pick up the outer repo's `tox.ini` `[pytest]`
section instead, silently ignoring that member's `addopts`/`markers`.
`--directory` genuinely `cd`s first, so config resolution lands where
you'd expect.

## Files

- build.py: Build the package from the current directory
- build.sh: Shell entrypoint for the build script
- create-lp-release-branches.sh: Generate the stable release branches on Launchpad, after the devel branch is ready
- check-versions-are-consistent.py: Helper script to verify changelog and package version matches.
- README.md: This file.
- refresh-keyrings.sh: Refresh the keyring files for services, stored in the repo
- setup_sbuild.sh: Downloads and prepares chroots used in the build (and test) process.
- test-in-lxd.sh: Build the package and then install it on an LXD instance for testing
- test-in-multipass.sh: Build the package and then install it on a multipass instance for testing
- ua.bash: Bash completion script for `ua` | `pro`
