"""CORS origin policy (PRD v4 §9.10 X6). Pure + env-driven so it is testable without
importing the full app (which would run create_all on the dev DB)."""

import os


def cors_allowed_origins() -> list[str]:
    """Loopback CORS allowlist tied to SERVER_BIND_HOST.

    Defense-in-depth behind the real mitigations (loopback bind + no key read-back):
    a fixed loopback allowlist, NOT ``*`` (which is illegal with allow_credentials and
    would re-open in-browser CSRF/DNS-rebinding). ``CORS_ALLOWED_ORIGINS`` (comma-
    separated) overrides for a proxied/non-default frontend origin."""
    override = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
    if override:
        return [o.strip() for o in override.split(",") if o.strip()]
    host = os.environ.get("SERVER_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    origins = ["http://localhost:5173", "http://127.0.0.1:5173", f"http://{host}:5173"]
    return list(dict.fromkeys(origins))  # dedupe, preserve order
