"""Benefit checker orchestration Lambda."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

ASSESSMENT_PATH = "/v1/benefit-checks/assessments"
HEALTH_PATH = "/health"
REQUIRED_FIELDS = {
    "firstName",
    "lastName",
    "nino",
    "dateOfBirth",
    "claimedBenefits",
    "annualIncome",
    "savingsAmount",
    "housingCostsPerMonth",
    "dependantChildren",
    "disabledApplicant",
    "caringResponsibilities",
    "postcode",
}
CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class ConfigurationError(RuntimeError):
    """The Lambda is missing required runtime configuration."""


class ProviderTimeout(RuntimeError):
    """The downstream provider timed out."""


class ProviderUnavailable(RuntimeError):
    """The downstream provider could not be reached."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    body: dict[str, Any]


class SecretCredentialProvider:
    def __init__(self, secret_id: str, ttl_seconds: int = 300, secrets_client=None):
        self.secret_id = secret_id
        self.ttl_seconds = ttl_seconds
        self._client = secrets_client
        self._credentials: Credentials | None = None
        self._expires_at = 0.0

    def get(self) -> Credentials:
        now = time.monotonic()
        if self._credentials and now < self._expires_at:
            return self._credentials

        if self._client is None:
            import boto3

            self._client = boto3.client("secretsmanager")

        value = self._client.get_secret_value(SecretId=self.secret_id)
        try:
            secret = json.loads(value["SecretString"])
            username = secret["username"]
            password = secret["password"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ConfigurationError("Downstream credential secret is invalid") from error

        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise ConfigurationError("Downstream credential secret is invalid")

        self._credentials = Credentials(username, password)
        self._expires_at = now + self.ttl_seconds
        return self._credentials

    def invalidate(self) -> None:
        self._credentials = None
        self._expires_at = 0.0


class BenefitCheckerClient:
    def __init__(self, base_url: str, credentials: SecretCredentialProvider, timeout: float = 5.0, opener=None):
        self.url = f"{base_url.rstrip('/')}{ASSESSMENT_PATH}"
        self.credentials = credentials
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def assess(self, payload: dict[str, Any], correlation_id: str) -> ProviderResponse:
        response = self._send(payload, correlation_id)
        if response.status in {401, 403}:
            self.credentials.invalidate()
            response = self._send(payload, correlation_id)
        elif response.status in RETRYABLE_STATUSES:
            response = self._send(payload, correlation_id)
        return response

    def _send(self, payload: dict[str, Any], correlation_id: str) -> ProviderResponse:
        credentials = self.credentials.get()
        token = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode()
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={
                "authorization": f"Basic {token}",
                "content-type": "application/json",
                "accept": "application/json",
                "x-correlation-id": correlation_id,
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return ProviderResponse(response.status, _decode_json(response.read()))
        except urllib.error.HTTPError as error:
            return ProviderResponse(error.code, _decode_json(error.read()))
        except (TimeoutError, socket.timeout) as error:
            raise ProviderTimeout from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ProviderTimeout from error
            raise ProviderUnavailable from error


def _decode_json(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode())
        return value if isinstance(value, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _build_client() -> BenefitCheckerClient:
    base_url = os.getenv("DOWNSTREAM_BENEFIT_CHECKER_URL", "").strip()
    secret_id = os.getenv("DOWNSTREAM_BASIC_AUTH_SECRET_ID", "").strip()
    if not base_url or not secret_id:
        raise ConfigurationError("Required downstream configuration is missing")
    try:
        timeout = float(os.getenv("DOWNSTREAM_TIMEOUT_SECONDS", "5"))
        ttl = int(os.getenv("SECRET_CACHE_TTL_SECONDS", "300"))
    except ValueError as error:
        raise ConfigurationError("Downstream timeout or secret cache TTL is invalid") from error
    return BenefitCheckerClient(base_url, SecretCredentialProvider(secret_id, ttl), timeout)


_CLIENT: BenefitCheckerClient | None = None


def _client() -> BenefitCheckerClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method, path = _method_and_path(event)
    request_id = _request_id(event, context)

    if method == "GET" and path == HEALTH_PATH:
        return _response(200, {"status": "ok", "service": "benefit-checker-orchestrator"}, request_id)
    if method != "POST" or path != ASSESSMENT_PATH:
        return _error(404, "not_found", "Route not found", request_id)

    try:
        payload = _request_body(event)
    except ValueError as error:
        return _error(400, "invalid_request", str(error), request_id)

    validation_error = _validate_payload(payload)
    if validation_error:
        return _error(400, "invalid_request", validation_error, request_id)

    LOGGER.info(json.dumps({"event": "assessment_requested", "requestId": request_id, "caller": _caller(event)}))
    try:
        provider = _client().assess(payload, request_id)
    except ConfigurationError:
        LOGGER.exception("Benefit checker integration is misconfigured")
        return _error(500, "service_misconfigured", "The service is not configured", request_id)
    except ProviderTimeout:
        LOGGER.warning("Downstream benefit checker timed out", extra={"requestId": request_id})
        return _error(504, "provider_timeout", "The benefit checker timed out", request_id)
    except ProviderUnavailable:
        LOGGER.warning("Downstream benefit checker is unavailable", extra={"requestId": request_id})
        return _error(502, "provider_unavailable", "The benefit checker is unavailable", request_id)

    if provider.status in {200, 201}:
        LOGGER.info(json.dumps({"event": "assessment_completed", "requestId": request_id, "providerStatus": provider.status}))
        return _response(201, {"requestId": request_id, "provider": "mock-benefit-checker", "assessment": provider.body}, request_id)
    if provider.status == 400:
        return _error(400, "provider_rejected_request", _provider_message(provider.body), request_id)
    if provider.status in {401, 403}:
        return _error(502, "provider_authentication_failed", "The benefit checker integration could not authenticate", request_id)
    if provider.status == 429:
        return _error(503, "provider_rate_limited", "The benefit checker is temporarily busy", request_id)
    return _error(502, "provider_error", "The benefit checker returned an error", request_id)


def _request_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if not isinstance(body, str) or not body:
        raise ValueError("A JSON request body is required")
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode()
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ValueError("Request body is not valid base64") from error
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError("Request body is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return payload


def _validate_payload(payload: dict[str, Any]) -> str | None:
    missing = sorted(REQUIRED_FIELDS - payload.keys())
    if missing:
        return f"Missing required fields: {', '.join(missing)}"
    unknown = sorted(payload.keys() - REQUIRED_FIELDS)
    if unknown:
        return f"Unknown fields: {', '.join(unknown)}"
    if not isinstance(payload["claimedBenefits"], list) or not payload["claimedBenefits"]:
        return "claimedBenefits must be a non-empty array"
    return None


def _method_and_path(event: dict[str, Any]) -> tuple[str, str]:
    http = event.get("requestContext", {}).get("http", {})
    return str(http.get("method") or event.get("httpMethod") or "").upper(), str(event.get("rawPath") or event.get("path") or "")


def _request_id(event: dict[str, Any], context: Any) -> str:
    headers = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}
    candidate = headers.get("x-correlation-id")
    if isinstance(candidate, str) and CORRELATION_ID.fullmatch(candidate):
        return candidate
    gateway_id = event.get("requestContext", {}).get("requestId")
    return str(gateway_id or getattr(context, "aws_request_id", None) or uuid.uuid4())


def _caller(event: dict[str, Any]) -> str:
    authorizer = event.get("requestContext", {}).get("authorizer", {}).get("lambda", {})
    return str(authorizer.get("principalId") or authorizer.get("username") or "unknown")


def _provider_message(body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if isinstance(body.get("message"), str):
        return body["message"]
    return "The benefit checker rejected the request"


def _error(status: int, code: str, message: str, request_id: str) -> dict[str, Any]:
    return _response(status, {"requestId": request_id, "error": {"code": code, "message": message}}, request_id)


def _response(status: int, body: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "x-correlation-id": request_id, "cache-control": "no-store"},
        "body": json.dumps(body, separators=(",", ":")),
    }


handler = lambda_handler
