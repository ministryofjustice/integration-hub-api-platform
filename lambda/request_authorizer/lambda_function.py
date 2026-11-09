"""Retired shared-credential authorizer; deny all requests.

Retained as a deployment artifact during the native JWT authorizer migration.
"""


def lambda_handler(_event, _context):
    return {"isAuthorized": False, "context": {}}


handler = lambda_handler
