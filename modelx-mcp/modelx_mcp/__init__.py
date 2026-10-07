"""modelx-mcp: an MCP server that lets an AI client read a live modelx session.

    modelx-mcp --sample BasicTerm_S

README.md (the PyPI page, https://pypi.org/project/modelx-mcp/) says how to
add it to Claude Code or Claude Desktop. The six tools read a live modelx
session through `modelx_bridge.Bridge`, the same dispatcher the lifelib Studio
frontend talks to; `calculate` is the only one that computes. `run_python`,
which is not read-only, exists only with --allow-python.
"""

__version__ = "0.1.0"
