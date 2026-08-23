"""The MCP surface: what an AI agent sees when it connects.

These assert the contract (tool names, argument shapes, schema plumbing), not
the network — so they run offline like everything else.
"""

import asyncio

import pytest

pytest.importorskip("mcp")

from scrapewright.mcp_server import _schema_for, build_server  # noqa: E402
from scrapewright.schema import PRODUCT_SCHEMA  # noqa: E402

EXPECTED_TOOLS = {
    "detect_site", "scrape_catalog", "extract_page",
    "crawl_site", "list_learned_sites",
}


def _tools():
    return asyncio.run(build_server().list_tools())


def test_server_exposes_the_documented_tools():
    assert {t.name for t in _tools()} == EXPECTED_TOOLS


def test_every_tool_is_described_for_the_agent():
    # An undescribed tool is an uncallable tool — the model picks by description.
    for tool in _tools():
        assert tool.description and len(tool.description) > 40, tool.name


def test_extract_page_accepts_a_custom_field_list():
    tool = next(t for t in _tools() if t.name == "extract_page")
    props = tool.input_schema["properties"]
    assert set(props) == {"url", "fields", "js"}
    assert tool.input_schema.get("required") == ["url"]


def test_crawl_site_can_save_to_a_file():
    tool = next(t for t in _tools() if t.name == "crawl_site")
    assert "save_to" in tool.input_schema["properties"]


def test_schema_for_maps_agent_arguments():
    assert _schema_for(None) is PRODUCT_SCHEMA
    assert _schema_for([]) is PRODUCT_SCHEMA
    custom = _schema_for(["title", "salary:number", "tags:list"])
    assert custom.field_names == ("title", "salary", "tags")
    assert custom.list_fields == {"tags"}


def test_server_carries_instructions_and_version():
    server = build_server()
    from scrapewright import __version__
    assert server.version == __version__
    assert "structured data" in (server.instructions or "")
