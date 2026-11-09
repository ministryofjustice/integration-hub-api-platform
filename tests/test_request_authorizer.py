import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("request_authorizer", Path(__file__).parents[1] / "lambda/request_authorizer/lambda_function.py")
authorizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(authorizer)


class RetiredAuthorizerTests(unittest.TestCase):
    def test_shared_credentials_no_longer_authorise_requests(self):
        for header in ["Basic dXNlcjpwYXNz", "Bearer system.token", "Bearer jwt", ""]:
            with self.subTest(header=header):
                self.assertFalse(authorizer.lambda_handler({"headers": {"authorization": header}}, None)["isAuthorized"])
