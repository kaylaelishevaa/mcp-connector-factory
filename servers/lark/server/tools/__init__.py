"""MCP tool definitions for the Lark MCP server.

Each module exports:
- TOOL_SCHEMAS: list of MCP tool definitions (name, description, inputSchema)
- TOOL_HANDLERS: dict mapping tool name → async handler(args) -> dict

Registry combines all tool modules.
"""
