import json
import os
import unittest
from unittest.mock import Mock, patch
from test_lambda_function import api, AUTH_ENV, event


class JwtAuthorisationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AUTH_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.provider = Mock()
        api._CLIENT = self.provider

    def test_invalid_claims_never_reach_provider(self):
        for key, value in [("tid", "wrong"), ("aud", "file-transfer-api"), ("iss", "wrong"),
                           ("azp", "unmapped"), ("idtyp", "user"), ("ver", "1.0"),
                           ("roles", []), ("roles", ["FileTransfer.Upload"]), ("scp", "assess")]:
            with self.subTest(key=key, value=value):
                request = event()
                request["requestContext"]["authorizer"]["jwt"]["claims"][key] = value
                self.assertEqual(403, api.lambda_handler(request, None)["statusCode"])
        self.provider.assess.assert_not_called()

    def test_header_and_legacy_authorizer_cannot_supply_claims(self):
        request = event()
        request["requestContext"]["authorizer"] = {"lambda": {"principalId": "old-client"}}
        request["headers"]["authorization"] = "Bearer forged.token"
        self.assertEqual(403, api.lambda_handler(request, None)["statusCode"])
        self.provider.assess.assert_not_called()

    def test_configuration_fails_closed(self):
        for settings in [{"ENTRA_ALLOWED_CLIENT_IDS": "{}"}, {"ENTRA_ALLOWED_CLIENT_IDS": "[]"},
                         {"ENTRA_TENANT_ID": ""}, {"ENTRA_ALLOWED_CLIENT_IDS": "invalid"}]:
            with self.subTest(settings=settings), patch.dict(os.environ, settings):
                self.assertEqual(503, api.lambda_handler(event(), None)["statusCode"])
        self.provider.assess.assert_not_called()

    def test_gateway_serialised_roles_are_supported(self):
        request = event()
        request["requestContext"]["authorizer"]["jwt"]["claims"]["roles"] = json.dumps(["BenefitChecker.Assess"])
        self.assertTrue(api._authorised_application(request)[0])
