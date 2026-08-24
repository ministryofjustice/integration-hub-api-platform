import base64
import io
import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "lambda" / "benefit_orchestrator"))
import lambda_function as api


PAYLOAD = {
    "firstName": "Ada",
    "lastName": "Lovelace",
    "nino": "QQ123456C",
    "dateOfBirth": "1990-01-01",
    "claimedBenefits": ["UNIVERSAL_CREDIT"],
    "annualIncome": 18000,
    "savingsAmount": 1000,
    "housingCostsPerMonth": 800,
    "dependantChildren": 1,
    "disabledApplicant": False,
    "caringResponsibilities": False,
    "postcode": "SW1A 1AA",
}


def event(method="POST", path=api.ASSESSMENT_PATH, body=PAYLOAD, correlation_id="test-request"):
    return {
        "rawPath": path,
        "headers": {"x-correlation-id": correlation_id},
        "requestContext": {
            "requestId": "gateway-request",
            "http": {"method": method},
            "authorizer": {"lambda": {"principalId": "test-client"}},
        },
        "body": json.dumps(body) if body is not None else None,
    }


class LambdaHandlerTests(unittest.TestCase):
    def setUp(self):
        api._CLIENT = None

    def test_health_does_not_initialise_provider(self):
        with patch.object(api, "_build_client") as build:
            response = api.lambda_handler(event("GET", "/health", None), Mock())
        self.assertEqual(200, response["statusCode"])
        build.assert_not_called()

    def test_unknown_route_returns_404(self):
        self.assertEqual(404, api.lambda_handler(event("GET", "/missing", None), Mock())["statusCode"])

    def test_invalid_json_returns_400(self):
        request = event()
        request["body"] = "not-json"
        self.assertEqual(400, api.lambda_handler(request, Mock())["statusCode"])

    def test_base64_body_is_supported(self):
        client = Mock()
        client.assess.return_value = api.ProviderResponse(201, {"decision": "NOT_ELIGIBLE"})
        api._CLIENT = client
        request = event()
        request["body"] = base64.b64encode(json.dumps(PAYLOAD).encode()).decode()
        request["isBase64Encoded"] = True
        self.assertEqual(201, api.lambda_handler(request, Mock())["statusCode"])

    def test_missing_field_is_rejected_without_provider_call(self):
        payload = dict(PAYLOAD)
        payload.pop("nino")
        client = Mock()
        api._CLIENT = client
        response = api.lambda_handler(event(body=payload), Mock())
        self.assertEqual(400, response["statusCode"])
        client.assess.assert_not_called()

    def test_unknown_field_is_rejected(self):
        payload = {**PAYLOAD, "internalOverride": True}
        response = api.lambda_handler(event(body=payload), Mock())
        self.assertEqual(400, response["statusCode"])
        self.assertIn("Unknown fields", response["body"])

    def test_success_wraps_provider_response(self):
        api._CLIENT = Mock()
        api._CLIENT.assess.return_value = api.ProviderResponse(201, {"assessmentId": "bca_123"})
        response = api.lambda_handler(event(), Mock())
        body = json.loads(response["body"])
        self.assertEqual(201, response["statusCode"])
        self.assertEqual("test-request", body["requestId"])
        self.assertEqual("mock-benefit-checker", body["provider"])
        self.assertEqual("bca_123", body["assessment"]["assessmentId"])

    def test_invalid_correlation_id_uses_gateway_id(self):
        api._CLIENT = Mock()
        api._CLIENT.assess.return_value = api.ProviderResponse(201, {})
        response = api.lambda_handler(event(correlation_id="invalid id"), Mock())
        self.assertEqual("gateway-request", response["headers"]["x-correlation-id"])

    def test_provider_400_is_normalised(self):
        api._CLIENT = Mock()
        api._CLIENT.assess.return_value = api.ProviderResponse(400, {"error": {"message": "Invalid NINO"}})
        response = api.lambda_handler(event(), Mock())
        self.assertEqual(400, response["statusCode"])
        self.assertIn("Invalid NINO", response["body"])

    def test_provider_auth_failure_is_not_exposed_as_client_auth_failure(self):
        api._CLIENT = Mock()
        api._CLIENT.assess.return_value = api.ProviderResponse(401, {})
        self.assertEqual(502, api.lambda_handler(event(), Mock())["statusCode"])

    def test_provider_timeout_returns_504(self):
        api._CLIENT = Mock()
        api._CLIENT.assess.side_effect = api.ProviderTimeout()
        self.assertEqual(504, api.lambda_handler(event(), Mock())["statusCode"])

    def test_provider_unavailable_returns_502(self):
        api._CLIENT = Mock()
        api._CLIENT.assess.side_effect = api.ProviderUnavailable()
        self.assertEqual(502, api.lambda_handler(event(), Mock())["statusCode"])


class CredentialTests(unittest.TestCase):
    def test_secret_is_loaded_and_cached(self):
        secrets = Mock()
        secrets.get_secret_value.return_value = {"SecretString": '{"username":"user","password":"pass"}'}
        provider = api.SecretCredentialProvider("secret-id", secrets_client=secrets)
        self.assertEqual(api.Credentials("user", "pass"), provider.get())
        provider.get()
        secrets.get_secret_value.assert_called_once()

    def test_invalid_secret_raises_configuration_error(self):
        secrets = Mock()
        secrets.get_secret_value.return_value = {"SecretString": "{}"}
        with self.assertRaises(api.ConfigurationError):
            api.SecretCredentialProvider("secret-id", secrets_client=secrets).get()


class ClientTests(unittest.TestCase):
    def test_client_sets_basic_auth_and_correlation_id(self):
        response = Mock(status=201)
        response.read.return_value = b'{"assessmentId":"bca_123"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(return_value=response)
        credentials = Mock()
        credentials.get.return_value = api.Credentials("user", "pass")
        client = api.BenefitCheckerClient("https://provider.example", credentials, opener=opener)
        result = client.assess(PAYLOAD, "correlation-1")
        request = opener.call_args.args[0]
        self.assertEqual(201, result.status)
        self.assertEqual("Basic dXNlcjpwYXNz", request.get_header("Authorization"))
        self.assertEqual("correlation-1", request.get_header("X-correlation-id"))

    def test_client_refreshes_credentials_after_401(self):
        unauthorized = urllib.error.HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"{}"))
        success = Mock(status=201)
        success.read.return_value = b"{}"
        success.__enter__ = Mock(return_value=success)
        success.__exit__ = Mock(return_value=False)
        credentials = Mock()
        credentials.get.return_value = api.Credentials("user", "pass")
        client = api.BenefitCheckerClient("https://provider.example", credentials, opener=Mock(side_effect=[unauthorized, success]))
        self.assertEqual(201, client.assess(PAYLOAD, "request").status)
        credentials.invalidate.assert_called_once()

    def test_socket_timeout_is_translated(self):
        credentials = Mock()
        credentials.get.return_value = api.Credentials("user", "pass")
        client = api.BenefitCheckerClient("https://provider.example", credentials, opener=Mock(side_effect=socket.timeout()))
        with self.assertRaises(api.ProviderTimeout):
            client.assess(PAYLOAD, "request")


if __name__ == "__main__":
    unittest.main()
