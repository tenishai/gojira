"""scripts/release.sh: a release is a tag on main with the version bumped
first. Runs the real script against fake git/gh/uv in a scratch tree; the
refusals are the point — a release cut from the wrong place is the kind of
mistake a tag makes permanent."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE = r"""#!/usr/bin/env bash
tool="$(basename "$0")"
echo "$tool $*" >> "$CALLS"
case "$tool $*" in
  "git rev-parse --abbrev-ref HEAD") echo "${BRANCH:-main}" ;;
  "git status --porcelain --untracked-files=no") [ -n "${DIRTY:-}" ] && echo " M gojira/x.py" ;;
  "git rev-parse HEAD") echo aaaa ;;
  "git rev-parse origin/main") echo "${ORIGIN:-aaaa}" ;;
  "git rev-parse -q --verify refs/tags/"*) [ -n "${TAG_EXISTS:-}" ] && exit 0; exit 1 ;;
esac
exit 0
"""


@pytest.fixture
def run(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "gojira").mkdir()
    (tree / "scripts" / "release.sh").write_text((ROOT / "scripts" / "release.sh").read_text())
    (tree / "pyproject.toml").write_text('[project]\nname = "tenish-gojira"\nversion = "0.1.0"\n')
    (tree / "gojira" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (tree / "uv.lock").write_text('version = 1\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("git", "gh", "uv"):
        (bindir / tool).write_text(FAKE)
        (bindir / tool).chmod(0o755)
    calls = tmp_path / "calls"

    def _run(version="v1.2.0", **env):
        calls.write_text("")
        done = subprocess.run(
            ["bash", str(tree / "scripts" / "release.sh"), version],
            capture_output=True, text=True,
            env={"PATH": f"{bindir}:{os.environ['PATH']}", "CALLS": str(calls), **env},
        )
        return done, calls.read_text().splitlines(), tree

    return _run


def test_a_release_bumps_tags_pushes_and_publishes(run):
    done, calls, tree = run()
    assert done.returncode == 0, done.stderr
    assert 'version = "1.2.0"' in (tree / "pyproject.toml").read_text()
    assert '__version__ = "1.2.0"' in (tree / "gojira" / "__init__.py").read_text()
    assert "uv lock --quiet" in calls
    assert "git add pyproject.toml gojira/__init__.py uv.lock" in calls
    assert "git commit --quiet --signoff -m release: v1.2.0" in calls
    assert "git tag -a v1.2.0 -m Gojira v1.2.0" in calls
    assert "git push --quiet origin main v1.2.0" in calls
    assert "gh release create v1.2.0 --title Gojira v1.2.0 --generate-notes" in calls
    assert calls.index("git fetch --quiet --tags origin") < calls.index("git tag -a v1.2.0 -m Gojira v1.2.0")


@pytest.mark.parametrize("version", ["1.2.0", "v1.2", "v1.2.0-rc1", "latest"])
def test_only_a_plain_vx_y_z_is_a_version(run, version):
    done, calls, _ = run(version)
    assert done.returncode == 2 and "must look like v1.2.0" in done.stderr
    assert not any(c.startswith("git push") for c in calls)


@pytest.mark.parametrize("env,word", [
    ({"BRANCH": "feat/x"}, "release from main"),
    ({"DIRTY": "1"}, "tracked files have changes"),
    ({"ORIGIN": "bbbb"}, "not at origin/main"),
    ({"TAG_EXISTS": "1"}, "already exists"),
])
def test_refusals_happen_before_anything_is_written(run, env, word):
    done, calls, tree = run(**env)
    assert done.returncode == 2 and word in done.stderr, done.stderr
    assert 'version = "0.1.0"' in (tree / "pyproject.toml").read_text()
    assert not any(c.startswith(("git tag", "git push", "git commit", "gh ")) for c in calls)
