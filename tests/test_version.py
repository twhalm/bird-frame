"""How the running version is determined.

Releases are identified by git tag alone: nothing commits a version into
pyproject.toml or __init__.py. The release build passes the tag in as
BIRDFRAME_VERSION so /healthz can report which image is on the wall. If that
wiring breaks, /healthz silently reports "dev" for a real release, which is
exactly the question you go to /healthz to answer.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import birdframe

REPO_ROOT = Path(__file__).resolve().parent.parent


def _version_with_env(value: str | None) -> str:
    """Read __version__ in a fresh interpreter with BIRDFRAME_VERSION set.

    A subprocess rather than monkeypatch + reload, because the value is captured
    at import time and this module is already imported by the time a test runs.
    """
    code = "import birdframe; print(birdframe.__version__)"
    env = {"PATH": "", "SYSTEMROOT": ""}
    if value is not None:
        env["BIRDFRAME_VERSION"] = value
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        env=env,
    ).stdout.strip()


class TestRuntimeVersion:
    def test_falls_back_to_dev_without_the_env_var(self):
        assert _version_with_env(None) == "dev"

    def test_reports_the_release_version_when_set(self):
        assert _version_with_env("1.4.2") == "1.4.2"

    def test_empty_env_var_is_treated_as_unset(self):
        """An unsubstituted build-arg must not put an empty version on /healthz."""
        assert _version_with_env("") == "dev"

    def test_importable_without_flask(self):
        """__init__ must stay import-light: the Dockerfile healthcheck and any
        version probe should not need the web stack."""
        assert importlib.import_module("birdframe").__version__


@pytest.fixture
def releaserc():
    with (REPO_ROOT / ".releaserc.json").open(encoding="utf-8") as fh:
        return json.load(fh)


class TestReleaseConfig:
    """The config is the release process, so assert the parts that would fail
    quietly rather than loudly."""

    def test_tags_carry_no_v_prefix(self, releaserc):
        """Docker tags are the version itself, so `v1.0.0` would produce
        ghcr.io/...:v1.0.0 and break the documented pinning."""
        assert releaserc["tagFormat"] == "${version}"

    def test_releases_only_from_main(self, releaserc):
        assert releaserc["branches"] == ["main"]

    def test_no_plugin_commits_to_the_repo(self, releaserc):
        """No @semantic-release/git and no changelog plugin: a release must not
        push a commit back to main."""
        names = [p if isinstance(p, str) else p[0] for p in releaserc["plugins"]]
        assert "@semantic-release/git" not in names
        assert "@semantic-release/changelog" not in names

    def test_no_npm_publish(self, releaserc):
        """@semantic-release/npm is in the default plugin list and would try to
        publish this Python project to the npm registry."""
        names = [p if isinstance(p, str) else p[0] for p in releaserc["plugins"]]
        assert "@semantic-release/npm" not in names

    def test_github_plugin_writes_the_release_notes(self, releaserc):
        names = [p if isinstance(p, str) else p[0] for p in releaserc["plugins"]]
        assert "@semantic-release/github" in names
        assert "@semantic-release/release-notes-generator" in names

    def test_no_changelog_file_is_tracked(self):
        assert not (REPO_ROOT / "CHANGELOG.md").exists()


class TestPyprojectVersion:
    def test_is_a_placeholder_not_a_release_version(self):
        """If someone starts hand-bumping this, it will disagree with the tags.
        The placeholder documents that tags are the only source of truth."""
        import tomllib

        with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
            declared = tomllib.load(fh)["project"]["version"]
        assert declared == "0.0.0"
        assert re.match(r"^\d+\.\d+\.\d+$", declared)

    def test_package_version_is_independent_of_pyproject(self):
        """The two are deliberately decoupled; the runtime one is authoritative."""
        assert birdframe.__version__ == "dev"
