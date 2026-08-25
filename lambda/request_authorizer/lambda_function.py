"""HTTP API request authorizer supporting Basic and Bearer client credentials."""

import base64
import binascii
import hmac
import json
import logging
import os

import boto3

DYNAMODB = boto3.resource("dynamodb")
SECRETS_MANAGER = boto3.client("secretsmanager")
AUTH_PRINCIPALS_TABLE = os.environ["AUTH_PRINCIPALS_TABLE"]
AUTH_ROLES_TABLE = os.environ["AUTH_ROLES_TABLE"]
LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)


def _deny():
    return {"isAuthorized": False, "context": {}}


def _header(event, name):
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def _item(table_name, key):
    return DYNAMODB.Table(table_name).get_item(Key=key).get("Item")


def _secret(secret_id):
    try:
        return json.loads(SECRETS_MANAGER.get_secret_value(SecretId=secret_id).get("SecretString") or "{}")
    except Exception:
        return {}


def _basic(token):
    try:
        username, password = base64.b64decode(token, validate=True).decode().split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    principal = _item(AUTH_PRINCIPALS_TABLE, {"auth_lookup_key": f"basic#{username}"})
    if not principal or not principal.get("enabled", True):
        return None
    expected = _secret(principal.get("secret_name", "")).get("password")
    return principal if expected and expected != "replace-me" and hmac.compare_digest(password, str(expected)) else None


def _bearer(token):
    token_id, separator, token_value = token.partition(".")
    if not separator or not token_id or not token_value:
        return None
    principal = _item(AUTH_PRINCIPALS_TABLE, {"auth_lookup_key": f"bearer#{token_id}"})
    if not principal or not principal.get("enabled", True):
        return None
    expected = _secret(principal.get("secret_name", "")).get("bearerToken")
    return principal if expected and expected != "replace-me" and hmac.compare_digest(token_value, str(expected)) else None


def lambda_handler(event, _context):
    request_id = event.get("requestContext", {}).get("requestId")
    authorization = _header(event, "authorization")
    if not authorization:
        LOGGER.warning(json.dumps({"event": "authorization_denied", "reason": "missing_header", "requestId": request_id}))
        return _deny()
    try:
        scheme, token = authorization.split(" ", 1)
    except ValueError:
        return _deny()
    principal = _basic(token.strip()) if scheme.lower() == "basic" else _bearer(token.strip()) if scheme.lower() == "bearer" else None
    if not principal:
        LOGGER.warning(json.dumps({"event": "authorization_denied", "reason": "invalid_credentials", "requestId": request_id}))
        return _deny()
    role = _item(AUTH_ROLES_TABLE, {"role_name": principal["role_name"]})
    if not role:
        return _deny()
    LOGGER.info(json.dumps({"event": "authorization_succeeded", "requestId": request_id, "principalId": principal["principal_id"], "roleName": principal["role_name"]}))
    return {
        "isAuthorized": True,
        "context": {
            "principalId": principal["principal_id"],
            "roleName": principal["role_name"],
            "authType": principal["auth_type"],
        },
    }


handler = lambda_handler
