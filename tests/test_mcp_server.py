from __future__ import annotations


def test_mcp_dependency_is_optional():
    from dwg_reader import mcp_server

    try:
        server = mcp_server.create_server()
    except RuntimeError as error:
        assert "install MCP support" in str(error)
    else:
        assert server.name == "DWG Reader"
