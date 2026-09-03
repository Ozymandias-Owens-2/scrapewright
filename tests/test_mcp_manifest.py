"""The registry manifest has to agree with the package it describes.

server.json is read by machines we never see, and a mismatch shows up as a
listing that installs something which will not start. These checks are cheap and
catch the ways that drifts: a version bump on one side only, a rename, or an
ownership marker that no longer matches the name being claimed.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "server.json"


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_version_matches_the_package(manifest):
    from scrapewright import __version__

    assert manifest["version"] == __version__
    assert manifest["packages"][0]["version"] == __version__


def test_the_readme_claims_the_same_server_name(manifest):
    """The registry proves ownership of a PyPI package by finding this marker
    in the published README. If it drifts from the name, publishing fails."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert f"mcp-name: {manifest['name']}" in readme


def test_the_name_is_namespaced_to_the_github_account(manifest):
    """GitHub auth grants exactly one namespace, and it is case-sensitive.

    The registry refused `io.github.ozymandias-owens-2/scrapewright` with a 403
    saying the granted namespace was `io.github.Ozymandias-Owens-2/*`. This test
    previously asserted the lowercase form, which is how a wrong belief passes
    its own test.
    """
    assert manifest["name"].startswith("io.github.Ozymandias-Owens-2/")


def test_the_described_command_actually_starts_the_server(manifest):
    """Clients build a command line out of these. It has to be the real one."""
    pkg = manifest["packages"][0]

    assert pkg["identifier"] == "scrapewright"
    assert [a["value"] for a in pkg["packageArguments"]] == ["mcp"]
    # The bare package has no MCP dependency; without the extras the command
    # a client assembles would install something that cannot start.
    extras = pkg["runtimeArguments"][0]["value"]
    assert extras.startswith("scrapewright[") and "mcp" in extras
    assert pkg["transport"]["type"] == "stdio"


def test_the_marker_matches_the_name_exactly_including_case(manifest):
    """The registry compares these byte for byte.

    Publishing was refused with a 403 for claiming
    io.github.ozymandias-owens-2/... when the namespace granted by GitHub auth
    was io.github.Ozymandias-Owens-2/... -- and because PyPI will not accept a
    re-upload of a version, correcting it cost a release.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    marker = readme.split("mcp-name:")[1].split("-->")[0].strip()

    assert marker == manifest["name"]


def test_the_description_fits_the_registry_limit(manifest):
    """The schema caps it at 100; a longer one is rejected at publish time."""
    assert len(manifest["description"]) <= 100


def test_secrets_are_marked_as_secrets(manifest):
    """Clients mask what is flagged. An API key that is not flagged gets shown."""
    env = {v["name"]: v for v in manifest["packages"][0]["environmentVariables"]}

    assert env["ANTHROPIC_API_KEY"]["isSecret"] is True
    assert env["ANTHROPIC_API_KEY"]["isRequired"] is False   # catalogue sites work without it
