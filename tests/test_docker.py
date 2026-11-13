"""Container image rules, checked without Docker (CI also builds and runs the real image).

The image must: use a pinned base (never :latest), run as a non-root user, start the same
command as the pyproject entry point, and never contain secrets or local data (.dockerignore).
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def _instructions(name: str) -> list[str]:
    """The arguments of every instruction of one kind (FROM, USER, ...), in order."""
    pattern = re.compile(rf"^{name}\s+(.+)$", re.MULTILINE)
    return [match.strip() for match in pattern.findall(DOCKERFILE)]


def test_base_images_are_pinned():
    for base in _instructions("FROM"):
        image = base.split()[0]
        assert ":" in image and not image.endswith(":latest"), image


def test_runs_as_a_non_root_user():
    users = _instructions("USER")
    assert users and users[-1] not in ("root", "0")
    # The user is set before the program starts, so nothing runs as root at run time.
    assert DOCKERFILE.rindex("USER ") < DOCKERFILE.index("ENTRYPOINT")


def test_entrypoint_is_the_pipeline_command():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    command = next(iter(scripts["scripts"]))
    assert _instructions("ENTRYPOINT") == [f'["{command}"]']


def test_configs_are_in_the_image():
    assert "COPY config ./config" in DOCKERFILE


@pytest.mark.parametrize(
    "excluded", [".env", "venv/", "data/", ".git/", "tests/", "*.tfstate", "*.tfvars"]
)
def test_secrets_and_local_data_stay_out_of_the_image(excluded):
    assert excluded in DOCKERIGNORE
