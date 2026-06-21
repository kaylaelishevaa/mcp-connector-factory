"""MCP tool definitions for the Acme Web MCP server.

Each module exports:
- TOOL_SCHEMAS: list of MCP tool definitions (name, description, inputSchema)
- TOOL_HANDLERS: dict mapping tool name → async handler(args) -> dict

read_tools.py  → read tier (shipped)
write_tools.py → write tier (gated, default OFF — placeholder until green-light)
"""
