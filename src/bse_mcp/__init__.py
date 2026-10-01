"""BSE + Meltwater MIRA MCP server.

A single public, remote MCP server exposing live-and-on-demand tools for:
  * BSE corporate disclosures (announcements/filings)
  * Current BSE stock prices (live quotes)
  * Meltwater MIRA grounded intelligence

Design pillar: every tool call is a stateless passthrough to the upstream API.
Nothing is persisted to a database or the filesystem.
"""

__version__ = "0.1.0"
