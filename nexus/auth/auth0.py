"""Auth0 JWT validation for the API boundary."""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient


class Auth0Error(RuntimeError):
    pass


@dataclass(frozen=True)
class Auth0Claims:
    sub: str
    email: str
    name: str
    raw: dict


class Auth0Verifier:
    def __init__(
        self,
        *,
        domain: str | None = None,
        audience: str | None = None,
        issuer: str | None = None,
    ):
        self.domain = (domain or os.getenv("AUTH0_DOMAIN") or "").strip().removeprefix(
            "https://"
        ).rstrip("/")
        self.audience = (audience or os.getenv("AUTH0_AUDIENCE") or "").strip()
        raw_issuer = (issuer or os.getenv("AUTH0_ISSUER") or "").strip()
        if raw_issuer:
            self.issuer = raw_issuer.rstrip("/") + "/"
        elif self.domain:
            self.issuer = f"https://{self.domain}/"
        else:
            self.issuer = ""
        if not self.domain or not self.audience or not self.issuer:
            raise Auth0Error("AUTH0_DOMAIN, AUTH0_AUDIENCE, and AUTH0_ISSUER are required")
        self._jwks = PyJWKClient(f"{self.issuer}.well-known/jwks.json")

    def verify(self, token: str) -> Auth0Claims:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except Exception as e:
            raise Auth0Error("invalid Auth0 access token") from e

        sub = str(claims.get("sub") or "")
        email = str(claims.get("email") or claims.get("https://nexus/email") or "")
        name = str(claims.get("name") or claims.get("nickname") or email)
        if not sub:
            raise Auth0Error("Auth0 token missing subject")
        if not email:
            raise Auth0Error("Auth0 token missing email claim")
        return Auth0Claims(sub=sub, email=email.strip().lower(), name=name, raw=claims)
