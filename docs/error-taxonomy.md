# BHM error taxonomy

BHM keeps native HTTP and JSON-RPC envelopes for compatibility, while sharing
the semantic contract `bhm.error-taxonomy.v1` across REST and MCP.

## REST

Structured responses use `detail.code` or `detail.error` as the canonical
operation-specific value. Legacy string details are classified by HTTP status
(`http_<status>` when no bounded class exists). The public and admin OpenAPI
documents publish the mapping under `x-bhm-error-taxonomy` and expose the
`BhmRestErrorDetail` schema.

| HTTP | Class |
| ---: | --- |
| 400 | `invalid_request` |
| 401 | `auth_required` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict` |
| 422 | `validation_failed` |
| 429 | `rate_limited` |
| 503 | `not_ready` |
| 504 | `timeout` |

## MCP

JSON-RPC keeps its integer `error.code` and adds bounded metadata:

```json
{
  "data": {
    "bhm_error_code": "invalid_params",
    "schema_version": "bhm.error-taxonomy.v1"
  }
}
```

The same taxonomy is returned by the MCP protocol contract snapshot. Error
messages remain redacted and are not a stable machine-readable interface.
