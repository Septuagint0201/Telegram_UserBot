import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = json.loads((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))


@pytest.mark.unit
def test_caddy_only_routes_key_webapp_and_credential_api() -> None:
    caddyfile = (ROOT / "deploy/caddy/Caddyfile").read_text(encoding="utf-8")
    assert "admin off" in caddyfile
    assert "auto_https disable_redirects" in caddyfile
    assert "https_port 8443" in caddyfile
    assert "https://{$PUBLIC_HOST}:8443" in caddyfile
    assert "protocols h1 h2" in caddyfile
    assert "h3" not in caddyfile
    assert "path /webapp/* /api/v1/model-keys/*" in caddyfile
    assert "reverse_proxy control:8080" in caddyfile
    assert 'respond "Not Found" 404' in caddyfile
    for forbidden in ("/health", "/metrics", "postgres", "redis", "app:", "worker:"):
        assert forbidden not in caddyfile


@pytest.mark.unit
def test_caddy_liveness_is_a_loopback_only_real_tls_request() -> None:
    caddyfile = (ROOT / "deploy/caddy/Caddyfile").read_text(encoding="utf-8")
    public_block, internal_block = caddyfile.split("https://127.0.0.1:9443", maxsplit=1)
    assert "/internal/alive" not in public_block
    assert "bind 127.0.0.1" in internal_block
    assert "tls internal" in internal_block
    assert "@alive path /internal/alive" in internal_block
    assert "respond @alive 204" in internal_block
    assert 'respond "Not Found" 404' in internal_block

    gateway = COMPOSE["services"]["https-gateway"]
    assert gateway["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-T",
        "2",
        "--spider",
        "--no-check-certificate",
        "https://127.0.0.1:9443/internal/alive",
    ]
    assert all(port["target"] != 9443 for port in gateway["ports"])
    assert "9443" not in gateway.get("expose", [])
    assert "caddy validate" not in json.dumps(gateway["healthcheck"])
    assert "curl" not in json.dumps(gateway["healthcheck"])

    dockerfile = (ROOT / "deploy/caddy/Dockerfile").read_text(encoding="utf-8")
    assert "command -v wget >/dev/null" in dockerfile
    assert "chown -R 1000:1000 /data /config" in dockerfile
    assert "chmod 0750 /data /config" in dockerfile
    assert "apk add" not in dockerfile


@pytest.mark.unit
def test_caddy_has_security_headers_body_limit_and_no_sensitive_access_log() -> None:
    caddyfile = (ROOT / "deploy/caddy/Caddyfile").read_text(encoding="utf-8")
    assert "header {\n\t\tdefer" in caddyfile
    security_headers = (
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "Referrer-Policy",
        "X-Content-Type-Options",
        "Cache-Control",
        "Permissions-Policy",
    )
    for header in security_headers:
        assert header in caddyfile
    assert "handle_errors {" in caddyfile
    error_block = caddyfile.split("handle_errors {", maxsplit=1)[1].split("\n\t}", maxsplit=1)[0]
    for header in security_headers:
        assert header in error_block
    assert "-X-Powered-By" in error_block
    assert "-Server" in error_block
    assert 'respond "" {err.status_code}' in error_block
    assert "max_size 16KB" in caddyfile
    assert "output discard" in caddyfile
    for sensitive in ("initData", "launch-token", "Authorization", "Cookie"):
        assert sensitive not in caddyfile
    assert "rate_limit" not in caddyfile
    boundary = (ROOT / "deploy/caddy/README.md").read_text(encoding="utf-8")
    assert "No third-party Caddy module" in boundary
    assert "trusted-proxy boundary" in boundary
    assert "liveness evidence only" in boundary
    assert "does not prove public DNS" in boundary


@pytest.mark.unit
def test_webapp_assets_stay_inside_the_only_public_webapp_prefix() -> None:
    source = (ROOT / "src/telegram_userbot/adapters/webapp/app.py").read_text(encoding="utf-8")
    assert 'href="/webapp/model-key.css"' in source
    assert 'src="/webapp/model-key.js"' in source
    assert 'Route("/webapp/model-key.js"' in source
    assert 'Route("/webapp/model-key.css"' in source
    assert 'Route("/model-key.js"' not in source
    assert 'Route("/model-key.css"' not in source
