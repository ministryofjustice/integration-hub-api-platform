import base64
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("AUTH_PRINCIPALS_TABLE", "principals")
os.environ.setdefault("AUTH_ROLES_TABLE", "roles")
sys.modules.setdefault("boto3", SimpleNamespace(resource=lambda _: None, client=lambda _: None))
path = Path(__file__).parents[1] / "lambda" / "request_authorizer" / "lambda_function.py"
spec = importlib.util.spec_from_file_location("request_authorizer", path)
authorizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(authorizer)


class Table:
    def __init__(self, items):
        self.items = items

    def get_item(self, Key):
        value = next(iter(Key.values()))
        return {"Item": self.items[value]} if value in self.items else {}


class Dynamo:
    def __init__(self):
        self.tables = {
            "principals": Table({
                "basic#client": {"principal_id": "client", "role_name": "consumer", "auth_type": "basic", "enabled": True, "secret_name": "client-secret"},
                "bearer#system": {"principal_id": "system", "role_name": "consumer", "auth_type": "bearer", "enabled": True, "secret_name": "system-secret"},
            }),
            "roles": Table({"consumer": {"role_name": "consumer"}}),
        }

    def Table(self, name):
        return self.tables[name]


class Secrets:
    def get_secret_value(self, SecretId):
        values = {"client-secret": {"password": "password"}, "system-secret": {"bearerToken": "token"}}
        return {"SecretString": json.dumps(values[SecretId])}


class AuthorizerTests(unittest.TestCase):
    def setUp(self):
        authorizer.DYNAMODB = Dynamo()
        authorizer.SECRETS_MANAGER = Secrets()

    def test_basic_auth(self):
        token = base64.b64encode(b"client:password").decode()
        response = authorizer.lambda_handler({"headers": {"Authorization": f"Basic {token}"}}, None)
        self.assertTrue(response["isAuthorized"])
        self.assertEqual("client", response["context"]["principalId"])

    def test_bearer_auth(self):
        response = authorizer.lambda_handler({"headers": {"authorization": "Bearer system.token"}}, None)
        self.assertTrue(response["isAuthorized"])

    def test_invalid_credentials_are_denied(self):
        token = base64.b64encode(b"client:wrong").decode()
        self.assertFalse(authorizer.lambda_handler({"headers": {"authorization": f"Basic {token}"}}, None)["isAuthorized"])


if __name__ == "__main__":
    unittest.main()
