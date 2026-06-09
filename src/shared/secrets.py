"""AWS Secrets Manager client with in-memory caching."""
from __future__ import annotations

import json
from typing import Any

import boto3


class SecretsManagerClient:
    """Wraps boto3 Secrets Manager with in-memory caching.

    Secrets are fetched once per process lifetime and cached to avoid
    redundant API calls on warm Lambda invocations.
    """

    def __init__(self, region_name: str | None = None) -> None:
        self._client = boto3.client("secretsmanager", region_name=region_name)
        self._cache: dict[str, dict] = {}

    def get_secret(self, secret_id: str) -> dict[str, Any]:
        """Retrieve and parse a JSON secret from Secrets Manager.

        Results are cached in memory; subsequent calls for the same
        ``secret_id`` return the cached value without an API call.

        Args:
            secret_id: The name or ARN of the secret.

        Returns:
            Parsed JSON dict from the secret's ``SecretString``.

        Raises:
            KeyError: if the secret value is not valid JSON.
            botocore.exceptions.ClientError: on Secrets Manager API errors.
        """
        if secret_id not in self._cache:
            response = self._client.get_secret_value(SecretId=secret_id)
            self._cache[secret_id] = json.loads(response["SecretString"])
        return self._cache[secret_id]
