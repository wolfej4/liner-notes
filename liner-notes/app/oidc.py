"""Single sign-on with any OpenID Connect provider (Pocket ID, Authentik, Keycloak...)."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import config, db


class OIDCError(Exception):
    pass


_discovery: dict = {}


def enabled() -> bool:
    return bool(config.get("oidc_enabled") and config.get("oidc_issuer") and config.get("oidc_client_id"))


async def _config() -> dict:
    issuer = config.get("oidc_issuer").rstrip("/")
    if _discovery.get("issuer") == issuer and time.time() - _discovery.get("at", 0) < 3600:
        return _discovery["doc"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(issuer + "/.well-known/openid-configuration")
    except httpx.HTTPError as e:
        raise OIDCError(f"Couldn't reach the sign-in provider at {issuer} ({e.__class__.__name__}).")
    if r.status_code != 200:
        raise OIDCError(f"Couldn't read the sign-in provider's settings from {issuer} (status {r.status_code}).")
    doc = r.json()
    _discovery.update(issuer=issuer, doc=doc, at=time.time())
    return doc


async def login_url() -> str:
    doc = await _config()
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    db.run("INSERT INTO oauth_states(state, kind, data, created_at) VALUES(?,?,?,?)",
           (state, "oidc", json.dumps({"verifier": verifier}), time.time()))
    return doc["authorization_endpoint"] + "?" + urlencode({
        "response_type": "code", "client_id": config.get("oidc_client_id"), "redirect_uri": config.oidc_redirect(),
        "scope": "openid email profile", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


async def finish(code: str, state: str) -> dict:
    st = db.pop_state(state, "oidc")
    if not st or not code:
        raise OIDCError("This sign-in link expired. Go back and try again.")
    doc = await _config()
    verifier = json.loads(st["data"])["verifier"]
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            doc["token_endpoint"],
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": config.oidc_redirect(),
                  "code_verifier": verifier},
            auth=(config.get("oidc_client_id"), config.get("oidc_client_secret")),
        )
        if r.status_code != 200:
            raise OIDCError(f"The sign-in provider rejected the login (status {r.status_code}). "
                            "An admin should check the client ID and secret.")
        access = r.json().get("access_token")
        info_r = await client.get(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"})
    if info_r.status_code != 200:
        raise OIDCError("Couldn't read your profile from the sign-in provider.")
    info = info_r.json()
    if not info.get("sub"):
        raise OIDCError("The sign-in provider didn't say who you are.")
    return {
        "sub": str(info["sub"]),
        "email": (info.get("email") or "").strip(),
        "email_verified": info.get("email_verified", True) is not False,
        "name": info.get("name") or info.get("given_name") or info.get("preferred_username") or "",
    }
