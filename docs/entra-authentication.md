# Calling the pilot API with an Entra token

This change replaces caller-to-pilot Basic and shared-token authentication.
The pilot-to-provider connection still uses its separate Basic credential.

## Registration

Create a dedicated single-tenant benefit-checker API registration in DEVL, using
v2 access tokens and the optional `idtyp` access-token claim. Expose an application
role named `BenefitChecker.Assess`, allowed for applications. Require assignment
on the API enterprise application. Assign the role to the approved consumer with
admin consent. No delegated Microsoft Graph permission or browser callback is
needed for this machine-to-machine flow.

Record the tenant ID, API client ID, Application ID URI and approved consumer
client IDs. Use the API client ID as the v2 token audience and request
`<API Application ID URI>/.default` as the token scope. Do not reuse the
file-transfer API's audience or upload role.

For unattended clients, prefer certificates or workload identity. A local Bruno
demo can use a development consumer secret if that credential type is approved;
keep it in Bruno's secret variables, not the collection or Git.

## Deployment

The matching Terraform change is in the benefit-checker-api component of
modernisation-platform-environments. Supply the dedicated registration IDs in its
`entra` configuration. The plan deliberately fails while these are missing.

Coordinate a development maintenance window before merging this application PR:
its merge triggers deployment and immediately denies the old caller credentials.
Apply the matching gateway/environment configuration and deploy the application
in the same window, then verify the assessment flow. Updating one side alone can
cause temporary 401/403/503 responses. The retired authorizer package denies all
requests and is retained only for the existing deployment workflow.

The upstream mock client currently needs a matching token-acquisition change.
Bruno can test this API directly while that client work is pending.

## Bruno

1. Create a POST token request to
   `https://login.microsoftonline.com/{{entra_tenant_id}}/oauth2/v2.0/token`.
2. Use a form URL-encoded body with `grant_type=client_credentials`,
   `client_id={{entra_client_id}}`, `client_secret={{entra_client_secret}}` and
   `scope={{entra_api_uri}}/.default`. Store the secret as a secret variable.
3. Capture `access_token` from the response as a runtime variable.
4. POST the normal assessment JSON to
   `{{pilot_api_url}}/v1/benefit-checks/assessments`, with Bearer authentication
   using that token and `Content-Type: application/json`.

A successful assessment returns 201. API Gateway returns 401 for missing, expired,
invalid-signature or wrong-audience tokens. The Lambda returns 403 for a verified
token with a missing role, delegated user identity or unapproved consumer, and 503
when its authentication configuration is incomplete. Gateway errors do not use
the application's error envelope.

## Verification

Unit tests cover the application claim policy and ensure denied calls never
reach the provider. They use synthetic gateway claims and do not prove signature
or expiry checks. Those are API Gateway responsibilities and need live tests:

- A real approved application token gets an assessment (201).
- Missing, malformed, expired, wrong-audience and wrong-tenant tokens get 401.
- A valid token without the assessment role or from an unapproved application
  gets 403; a file-transfer access token must not grant access.
- Health stays public; request IDs and downstream error mapping still work.

Do not publish access tokens, secret values or assessment personal data in logs
or test reports. Live testing is pending registration, apply and deployment.

The design follows [AWS JWT authorizer validation](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html)
and [Microsoft application claim validation](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation).
