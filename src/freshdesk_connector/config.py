"""Connector configuration, read from environment variables (or a local .env file)."""

import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FRESHDESK_HOST_SUFFIX = ".freshdesk.com"

# One DNS label: lowercase letters, digits and inner hyphens, at most 63 chars.
_SUBDOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class Settings(BaseSettings):
    """Validated settings. Read from FRESHDESK_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="FRESHDESK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Never echo raw input (which could be the API key) in validation errors.
        hide_input_in_errors=True,
    )

    domain: str
    api_key: SecretStr = Field(min_length=1)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_retry_wait_seconds: float = Field(default=10.0, ge=0, le=60)

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, value: str) -> str:
        """Accept "acme" or "acme.freshdesk.com"; reject every other host.

        The API key is sent with every request, so it must only ever go to a
        Freshdesk host. URLs, paths, ports and custom domains are rejected.
        """
        host = value.strip().lower()
        subdomain = host.removesuffix(FRESHDESK_HOST_SUFFIX)
        if not _SUBDOMAIN_RE.fullmatch(subdomain):
            raise ValueError(
                'must be a Freshdesk subdomain such as "acme" or "acme.freshdesk.com"'
            )
        return subdomain + FRESHDESK_HOST_SUFFIX

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}/api/v2"
