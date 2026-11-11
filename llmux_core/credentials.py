"""Authenticate the loopback proxy without persisting a second credential store.

One proxy port belongs to one profile and credential/configuration snapshot.
Different profiles or rotated keys fail closed rather than sharing credit.
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import asdict
from pathlib import Path

from .config import Settings, load_settings


def profile_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home().resolve()


def proxy_token(settings: Settings, *, home: str | Path | None = None) -> str:
    """Local capability derived from scoped high-entropy API keys, never logged."""
    if not (settings.surplus_api_key or settings.solid_api_key):
        raise ValueError("LLMUX needs a marketplace credential in this Hermes profile")
    material = json.dumps(
        {"domain": "llmux-loopback-profile-v1", "home": str(Path(home).resolve()) if home is not None else str(profile_home()),
         "settings": asdict(settings)},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


def local_headers(settings: Settings | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {proxy_token(settings or load_settings())}"}


def verify_proxy_identity(port: int, settings: Settings | None = None) -> None:
    """Refuse legacy, unauthenticated or foreign-profile listeners on the port."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/llmux/authcheck", headers=local_headers(settings),
    )
    try:
        # Do not follow a redirect with a local capability in the headers.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with urllib.request.build_opener(NoRedirect).open(request, timeout=3) as response:
            result = json.load(response)
        if result != {"ok": True, "service": "llmux", "auth": "profile-scoped-v1"}:
            raise ValueError("unexpected proxy identity")
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "LLMUX cannot authenticate this proxy. Its port may belong to another profile or an older release. "
            "Stop the old proxy safely or configure a separate profile port before retrying."
        ) from exc
