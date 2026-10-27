# Testing, authorization, previews and security history

## Integration tests

CI requires both test databases: a connection failure fails the job instead of silently skipping tests. The same rule is available locally with `REQUIRE_TEST_DATABASE=1`.

Run an isolated MariaDB-backed suite:

```sh
docker compose -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yml down -v
```

The test database lives in tmpfs. The suite drops tables in `biggy_test` and `biggy_test2`; never point it at production databases.

## Authorization and API routes

`app/access.py` owns form, table, field, row and attachment policies. `app/helpers.py` re-exports the existing permission helper names. Both staff and portal downloads and preview-link issuance use the same attachment resolver. Portal access additionally requires a catalog form and organization ownership, and respects hidden fields. Owner-restricted rows with no owner are inaccessible to ordinary users, consistently with list filtering.

Canonical record endpoints are `/api/v1/tables/{table}/records` and `/api/v1/tables/{table}/records/{key}`. Table metadata and bulk operations live at `/api/v1/tables/{table}/fields` and `/api/v1/tables/{table}/bulk`. This allows record keys such as `fields` and `bulk`. Keys containing `/` are not supported. Existing routes remain compatible; OpenAPI and new Location links advertise canonical routes.

## Isolated previews

Downloads always use attachment disposition. To enable previews:

1. Choose a separate HTTPS domain that receives none of the application's cookies, preferably on a different registrable domain. Do not set shared parent-domain cookies.
2. Generate a dedicated random `PREVIEW_SIGNING_KEY`, shared only between the web and preview services. Set `PREVIEW_BASE_URL` on the web service to the preview HTTPS origin, with no path.
3. Run `gunicorn 'app.previews:create_preview_app()'` with `UPLOAD_FOLDER` pointing to the same uploads, mounted read-only. The Compose `previews` profile provides this service on localhost port 8001. Put an HTTPS reverse proxy in front of it.
4. Configure the preview proxy to neither forward cookies nor add cookies, and redact `/preview/` URLs in access logs. Do not mount uploads as public static files.

The preview app has no database credentials, login handling or session middleware. It serves only signed links valid for 60 seconds, detects raster image/PDF types from file headers, and sends a sandbox CSP, `nosniff`, `no-store` and `no-referrer`. HTML/SVG and unsupported formats remain downloads. Files are not sanitized; isolation is the security boundary. A signed link is a bearer capability: anyone holding it can use it until expiration. Revoking access prevents new links immediately; an issued link can remain usable for up to 60 seconds, and expiration cannot retract already downloaded bytes. Rotate the preview key to invalidate all outstanding links.

Without preview configuration, downloads continue to work. Same-origin or cookie-domain-overlapping preview configurations fall back to downloads.

## Security history

Designers can open **Security history** to search actor, event or target and page through results. Account activation/role/company/MFA changes, form/field permissions, and token creation/revocation/deletion are recorded transactionally. Events contain only an explicit allowlist of non-secret fields, never token values, password hashes, MFA secrets or request query strings. Denied protected requests (401/403/404) are recorded independently, including ownership denials that intentionally return 404.

The new `app_security_event` table is created by the existing startup schema initialization. History has no edit/delete UI. Database administrators can still modify it; it is not a tamper-proof external audit store. Retention is currently operator-managed. Direct SQL changes bypass ORM auditing.
