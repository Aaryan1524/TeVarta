"""Templates are committed to a public repository: secrets live in .env, never here."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENTS = sorted((ROOT / "infra" / "environments").glob("*.env.example"))
TEMPLATES = [ROOT / ".env.example", *ENVIRONMENTS]
# The mock server's public fixture token is documented, not a credential.
PUBLIC_FIXTURES = {"TEVARTA_MOCK_ACCESS_TOKEN"}
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CONTACT)", re.IGNORECASE)
PLACEHOLDER = re.compile(r"^(<[^>]*>)?$")


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.name)
def test_secret_names_hold_no_value_in_committed_templates(template):
    leaks = []
    for number, line in enumerate(template.read_text().splitlines(), start=1):
        name, separator, value = line.partition("=")
        if (separator and not line.startswith("#") and SECRET_NAME.search(name)
                and name not in PUBLIC_FIXTURES):
            if not PLACEHOLDER.match(value.strip()):
                leaks.append(f"{template.name}:{number} {name}")
    assert not leaks, f"put these values in .env, not the template: {leaks}"
