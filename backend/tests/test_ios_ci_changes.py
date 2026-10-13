import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ios_ci_changes.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, path: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(path)
    git(repo, "add", path)
    git(repo, "-c", "user.name=ci", "-c", "user.email=ci@localhost", "commit", "-qm", path)
    return git(repo, "rev-parse", "HEAD")


def decide(repo: Path, event: str, base: str, head: str) -> str:
    return subprocess.run(
        [str(SCRIPT), event, base, head], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    commit(tmp_path, "README.md")
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "ios/TeVarta/TeVartaApp.swift",
        "fixtures/v1/feed_success.json",
        "contracts/v1/openapi.json",
        "Makefile",
        ".github/workflows/ios.yml",
        "scripts/ios_ci_changes.sh",
    ],
)
def test_ios_inputs_run_the_build(repo: Path, path: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    assert decide(repo, "pull_request", base, commit(repo, path)) == "ios=true"


@pytest.mark.parametrize(
    "path",
    ["backend/app/onboarding.py", "migrations/versions/x.py", "fixtures/components/v1/a.yaml"],
)
def test_backend_only_changes_skip_the_build(repo: Path, path: str) -> None:
    base = git(repo, "rev-parse", "HEAD")
    assert decide(repo, "push", base, commit(repo, path)) == "ios=false"


def test_unknown_ranges_build_rather_than_skip(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    assert decide(repo, "workflow_dispatch", head, head) == "ios=true"
    assert decide(repo, "push", "0" * 40, head) == "ios=true"
    assert decide(repo, "push", "", head) == "ios=true"
