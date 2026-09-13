import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_required_checks.sh"


def test_required_check_guard_passes_by_default() -> None:
    assert subprocess.run([str(SCRIPT)], check=False).returncode == 0


def test_required_check_guard_has_a_reproducible_negative_control() -> None:
    environment = os.environ | {"TEVARTA_CI_FORCE_FAILURE": "1"}
    assert subprocess.run([str(SCRIPT)], env=environment, check=False).returncode != 0
