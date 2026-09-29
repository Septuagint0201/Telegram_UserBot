# Gateway contract

The production gateway publishes host TCP 443 only and maps it to Caddy's unprivileged container
port 8443; the non-root process never binds a privileged container port. It proxies exactly `/webapp/*` and
`/api/v1/model-keys/*`. It does not expose HTTP 80, HTTP/3 UDP, health, metrics, Bot handlers,
database, Redis, app, or worker routes. TLS-ALPN-01 is the selected production certificate path;
the validation override uses Caddy's internal issuer on loopback port 18443 and cannot be cited as
public TLS evidence.

No third-party Caddy module is compiled into the gateway. PostgreSQL-backed administrator/role
rate limits remain authoritative. The additional client-network dimension is intentionally pending
until the application has an acceptance-tested trusted-proxy boundary; it must not trust arbitrary
client-supplied forwarding headers.

Request access logging is disabled in this slice so custom initData, launch-token, Cookie,
Authorization, query, and body fields cannot leak. A later content-free formatter may enable only
reviewed route-template/status/latency fields.

Handler failures, including an unavailable `control` upstream, are rendered by a content-free
error route that preserves the generated status code, reapplies the public security headers, and
removes `Server` and `X-Powered-By`. Synthetic gateway evidence must exercise this error path;
checking only the local 404 response is insufficient.

Container liveness uses BusyBox wget against
`https://127.0.0.1:9443/internal/alive`. That listener uses Caddy's internal issuer, binds only
container loopback, has no Compose port or network exposure, and returns 404 for every other path.
It is liveness evidence only: it does not prove public DNS, ACME, the public certificate, the
`control` backend, or credential readiness.
