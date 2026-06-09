"""Factory for instantiating the configured video generation provider.

Set the VIDEO_PROVIDER environment variable to select a provider:

  VIDEO_PROVIDER=kling        Kling.ai API v3.0 (async, webhook) — default
  VIDEO_PROVIDER=nova_reel    Amazon Nova Reel via Bedrock (sync, no webhook needed)
  VIDEO_PROVIDER=runway       Runway Gen-3 Alpha Turbo (sync polling, no webhook needed)

Provider-specific secrets are loaded from Secrets Manager based on the provider:
  kling      → KLING_SECRET_ID (default: kling/api_key), field: api_key
  nova_reel  → no secret needed (uses IAM role)
  runway     → RUNWAY_SECRET_ID (default: runway/api_key), field: api_key
"""
from __future__ import annotations

import os

from shared.secrets import SecretsManagerClient
from video_generation.providers.base import VideoProvider

VIDEO_PROVIDER = os.environ.get("VIDEO_PROVIDER", "kling").lower()
KLING_SECRET_ID = os.environ.get("KLING_SECRET_ID", "kling/api_key")
RUNWAY_SECRET_ID = os.environ.get("RUNWAY_SECRET_ID", "runway/api_key")

SUPPORTED_PROVIDERS = ("kling", "nova_reel", "runway")


def get_provider(secrets_client: SecretsManagerClient | None = None) -> VideoProvider:
    """Instantiate and return the configured video provider.

    Args:
        secrets_client: Optional SecretsManagerClient for dependency injection in tests.

    Returns:
        A VideoProvider instance ready to submit tasks.

    Raises:
        ValueError: if VIDEO_PROVIDER is not a recognised value.
    """
    if VIDEO_PROVIDER not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unknown VIDEO_PROVIDER={VIDEO_PROVIDER!r}. "
            f"Must be one of: {SUPPORTED_PROVIDERS}"
        )

    if VIDEO_PROVIDER == "kling":
        from video_generation.providers.kling_provider import KlingProvider
        client = secrets_client or SecretsManagerClient()
        api_key = client.get_secret(KLING_SECRET_ID)["api_key"]
        return KlingProvider(api_key=api_key)

    elif VIDEO_PROVIDER == "nova_reel":
        from video_generation.providers.nova_reel_provider import NovaReelProvider
        return NovaReelProvider()

    elif VIDEO_PROVIDER == "runway":
        from video_generation.providers.runway_provider import RunwayProvider
        client = secrets_client or SecretsManagerClient()
        api_key = client.get_secret(RUNWAY_SECRET_ID)["api_key"]
        return RunwayProvider(api_key=api_key)
