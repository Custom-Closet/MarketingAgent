"""Hunter.io API v2 client: find and verify business email addresses.

Docs: https://hunter.io/api-documentation/v2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

BASE_URL = "https://api.hunter.io/v2"


class HunterError(RuntimeError):
    pass


@dataclass
class HunterContact:
    email: str
    first_name: str | None
    last_name: str | None
    position: str | None
    confidence: int | None
    organization: str | None
    domain: str

    @property
    def name(self) -> str | None:
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return full or None


class HunterClient:
    def __init__(self, api_key: str, *, client: httpx.Client | None = None):
        if not api_key:
            raise HunterError("HUNTER_API_KEY is not set")
        self.api_key = api_key
        self.http = client or httpx.Client(base_url=BASE_URL, timeout=30)

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        resp = self.http.get(path, params={**params, "api_key": self.api_key})
        if resp.status_code == 429:
            raise HunterError("Hunter rate limit or monthly quota reached (HTTP 429)")
        body = resp.json()
        if resp.is_error:
            details = "; ".join(e.get("details", "") for e in body.get("errors", []))
            raise HunterError(f"Hunter {path} failed ({resp.status_code}): {details or resp.text}")
        return body["data"]

    def domain_search(self, domain: str, *, limit: int = 10, seniority: str | None = None,
                      department: str | None = None) -> list[HunterContact]:
        """Personal (non-generic) emails Hunter knows about for a company domain."""
        data = self._get("/domain-search", domain=domain, limit=limit, type="personal",
                         seniority=seniority, department=department)
        return [
            HunterContact(
                email=e["value"],
                first_name=e.get("first_name"),
                last_name=e.get("last_name"),
                position=e.get("position"),
                confidence=e.get("confidence"),
                organization=data.get("organization"),
                domain=data.get("domain", domain),
            )
            for e in data.get("emails", [])
        ]

    def email_finder(self, domain: str, first_name: str, last_name: str) -> HunterContact | None:
        """Most likely email for a named person at a domain."""
        data = self._get("/email-finder", domain=domain, first_name=first_name, last_name=last_name)
        if not data.get("email"):
            return None
        return HunterContact(
            email=data["email"],
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            position=data.get("position"),
            confidence=data.get("score"),
            organization=data.get("company"),
            domain=domain,
        )

    def verify(self, email: str) -> str:
        """Return Hunter's verdict: valid, invalid, accept_all, webmail, disposable or unknown."""
        return self._get("/email-verifier", email=email).get("status", "unknown")
