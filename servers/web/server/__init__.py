"""Acme Web MCP Server — FastAPI app exposing example.com (NestJS admin API)
via the MCP protocol.

Web-only connector. Wraps the existing NestJS admin REST API at
admin.example.com/api — it never talks to MySQL directly and never touches
Lark (that is a separate connector / workstream). See 00_DESIGN.md.
"""
