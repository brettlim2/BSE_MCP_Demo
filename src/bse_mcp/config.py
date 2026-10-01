"""Environment-driven configuration.

All secrets come from environment variables — nothing is stored on disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from the process environment."""

    # --- Client auth gate for our own MCP endpoint ---
    # Comma-separated list of bearer tokens/API keys accepted from MCP clients.
    # If empty, the auth gate is DISABLED (fine for local dev, never in prod).
    mcp_api_keys: list[str] = field(default_factory=list)

    # --- Meltwater MIRA (upstream) ---
    meltwater_api_key: str | None = None
    meltwater_base_url: str = "https://api.meltwater.com"

    # --- BSE (upstream, unofficial JSON API) ---
    bse_api_base: str = "https://api.bseindia.com/BseIndiaAPI/api"
    bse_web_base: str = "https://www.bseindia.com/"

    # --- HTTP tuning ---
    http_timeout: float = 20.0  # BSE: fast endpoints
    mira_timeout: float = 120.0  # MIRA: grounded answers can take ~30s+
    max_retries: int = 2
    micro_cache_ttl: float = 5.0  # seconds; in-memory only, collapses duplicate bursts

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    mount_path: str = "/mcp"

    # --- Optional hardening (DNS-rebinding protection) ---
    # If allowed_hosts is set, we enable Host/Origin validation restricted to these
    # values (recommended in production: set your public domain). If empty, we leave
    # the SDK's HTTP default in place.
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.mcp_api_keys)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mcp_api_keys=_split_csv(os.getenv("MCP_API_KEYS")),
            meltwater_api_key=os.getenv("MELTWATER_API_KEY") or None,
            meltwater_base_url=os.getenv("MELTWATER_BASE_URL", "https://api.meltwater.com"),
            bse_api_base=os.getenv("BSE_API_BASE", "https://api.bseindia.com/BseIndiaAPI/api"),
            bse_web_base=os.getenv("BSE_WEB_BASE", "https://www.bseindia.com/"),
            http_timeout=float(os.getenv("HTTP_TIMEOUT", "20")),
            mira_timeout=float(os.getenv("MIRA_TIMEOUT", "120")),
            max_retries=int(os.getenv("MAX_RETRIES", "2")),
            micro_cache_ttl=float(os.getenv("MICRO_CACHE_TTL", "5")),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            mount_path=os.getenv("MCP_MOUNT_PATH", "/mcp"),
            allowed_hosts=_split_csv(os.getenv("ALLOWED_HOSTS")),
            allowed_origins=_split_csv(os.getenv("ALLOWED_ORIGINS")),
        )


settings = Settings.from_env()
