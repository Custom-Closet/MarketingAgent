"""Instagram Graph API client (Instagram professional account linked to a Facebook Page).

Uses only official, ToS-compliant endpoints:

* Business Discovery - read public profile data of other *business/creator* accounts.
* Hashtag Search     - find recent public posts for a hashtag (max 30 unique hashtags / 7 days).
* Messaging          - list inbound DM conversations and reply. Meta only allows replies
                       within 24 hours of the user's last message; businesses cannot open
                       a conversation, so cold outreach goes through the assisted queue.
                       Works with an Instagram Login token ("IG...") or a Facebook Page token ("EAA...").

Calls are throttled and stop early when Meta's usage headers near the rate limit.

Required permissions: instagram_basic, instagram_manage_messages, pages_show_list,
pages_read_engagement, business_management.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{1,30})")
# Meta allows replies for 24h after the user's last message; keep a margin so late sends aren't rejected.
REPLY_WINDOW = timedelta(hours=23)
MIN_CALL_INTERVAL = 1.0      # seconds between Graph API calls
USAGE_LIMIT_PCT = 80         # stop once any usage header reports this much of the quota used
_RATE_LIMIT_CODES = {4, 17, 32, 613, 80002, 80006}
_TRANSIENT_CODES = {-2, 1, 2}    # Meta-side timeouts / temporary errors: retried with back-off
RETRY_DELAYS = (3, 9)            # seconds


class InstagramError(RuntimeError):
    pass


class InstagramRateLimited(InstagramError):
    pass


def _scrub_paging(value: Any) -> Any:
    """Drop paging next/previous URLs - Meta embeds the access token in them."""
    if isinstance(value, dict):
        return {k: _scrub_paging(v) for k, v in value.items() if k not in ("next", "previous")}
    if isinstance(value, list):
        return [_scrub_paging(v) for v in value]
    return value


def _usage_problem(headers: httpx.Headers) -> str | None:
    """Describe the rate-limit state if Meta's usage headers say we should stop, else None."""
    for name in ("x-business-use-case-usage", "x-app-usage"):
        raw = headers.get(name)
        if not raw:
            continue
        try:
            usage = json.loads(raw)
        except ValueError:
            continue
        entries = [e for group in usage.values() for e in group] if name == "x-business-use-case-usage" else [usage]
        for e in entries:
            wait = e.get("estimated_time_to_regain_access") or 0
            pct = max((v for k, v in e.items() if k in ("call_count", "total_cputime", "total_time")), default=0)
            if wait or pct >= USAGE_LIMIT_PCT:
                return f"{pct}% of quota used" + (f", retry in {wait} min" if wait else "")
    return None


@dataclass
class IGProfile:
    username: str
    name: str | None
    biography: str | None
    website: str | None
    followers_count: int | None
    media_count: int | None

    @property
    def domain(self) -> str | None:
        return domain_from_url(self.website)


@dataclass
class IGConversation:
    recipient_id: str        # IGSID of the other participant
    username: str | None
    last_inbound_text: str | None
    last_inbound_at: datetime | None

    @property
    def can_reply(self) -> bool:
        return bool(self.last_inbound_at) and datetime.now(timezone.utc) - self.last_inbound_at < REPLY_WINDOW


# Link-in-bio services that never point at the lead's own domain.
_LINK_HUBS = {"linktr.ee", "beacons.ai", "lnk.bio", "linkin.bio", "bio.link", "instagram.com", "facebook.com",
              "tiktok.com", "youtube.com", "wa.me", "bit.ly", "shopmy.us", "stan.store", "taplink.cc"}


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host or host in _LINK_HUBS:
        return None
    return host


def extract_mentions(text: str | None) -> set[str]:
    return {m.lower().rstrip(".") for m in MENTION_RE.findall(text or "")}


class InstagramClient:
    def __init__(self, access_token: str, ig_user_id: str, *, page_id: str = "",
                 api_version: str = "v23.0", client: httpx.Client | None = None):
        if not (access_token and ig_user_id):
            raise InstagramError("IG_ACCESS_TOKEN and IG_USER_ID must be set")
        self.token = access_token
        self.ig_user_id = ig_user_id
        self.page_id = page_id
        # Instagram Login tokens ("IG...") only work on graph.instagram.com; Facebook tokens ("EAA...") on graph.facebook.com.
        self.instagram_login = access_token.startswith("IG")
        host = "graph.instagram.com" if self.instagram_login else "graph.facebook.com"
        self.http = client or httpx.Client(base_url=f"https://{host}/{api_version}", timeout=60)
        self.min_interval = MIN_CALL_INTERVAL
        self.retry_delays = RETRY_DELAYS
        self._last_call = 0.0

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        params = {**kwargs.pop("params", {}), "access_token": self.token}
        for delay in (*self.retry_delays, None):
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.http.request(method, path, params=params, **kwargs)
            finally:
                self._last_call = time.monotonic()
            body = _scrub_paging(resp.json())
            if not (resp.is_error or "error" in body):
                break
            err = body.get("error", {})
            if err.get("code") in _TRANSIENT_CODES and delay is not None:
                time.sleep(delay)
                continue
            cls = InstagramRateLimited if err.get("code") in _RATE_LIMIT_CODES else InstagramError
            raise cls(f"Graph API {path}: {err.get('message', 'HTTP ' + str(resp.status_code))} (code {err.get('code')})")
        if problem := _usage_problem(resp.headers):
            raise InstagramRateLimited(f"Graph API rate limit close: {problem} - stopping early")
        return body

    # ---- discovery -------------------------------------------------------

    def business_discovery(self, username: str) -> IGProfile:
        """Public profile of a business/creator account. Personal accounts are not visible."""
        username = username.lstrip("@")
        fields = (f"business_discovery.username({username})"
                  "{username,name,biography,website,followers_count,media_count}")
        data = self._request("GET", f"/{self.ig_user_id}", params={"fields": fields})["business_discovery"]
        return IGProfile(
            username=data["username"],
            name=data.get("name"),
            biography=data.get("biography"),
            website=data.get("website"),
            followers_count=data.get("followers_count"),
            media_count=data.get("media_count"),
        )

    def hashtag_mentions(self, hashtag: str, *, limit: int = 50) -> set[str]:
        """Usernames @-mentioned in recent posts for a hashtag.

        The API does not expose who authored hashtag media, so captions' @mentions are the
        best signal for candidate accounts; each candidate is then enriched via business_discovery.
        """
        found = self._request("GET", "/ig_hashtag_search",
                              params={"user_id": self.ig_user_id, "q": hashtag.lstrip("#")})
        if not found.get("data"):
            return set()
        hashtag_id = found["data"][0]["id"]
        media = self._request("GET", f"/{hashtag_id}/recent_media",
                              params={"user_id": self.ig_user_id, "fields": "caption,permalink", "limit": limit})
        mentions: set[str] = set()
        for item in media.get("data", []):
            mentions |= extract_mentions(item.get("caption"))
        return mentions

    # ---- messaging -------------------------------------------------------

    def _messaging_node(self, action: str) -> str:
        """Instagram Login tokens message as the IG account; Facebook tokens go through the linked Page."""
        if self.instagram_login:
            return f"/{self.ig_user_id}"
        if not self.page_id:
            raise InstagramError(f"IG_PAGE_ID must be set to {action} with a Facebook Page token")
        return f"/{self.page_id}"

    def conversations(self, limit: int = 25) -> list[IGConversation]:
        """Conversations updated within the reply window, newest first.

        Meta rejects large conversation pages ("reduce the amount of data"), so this lists one
        conversation per call and stops at the first one too old to reply to.
        """
        path = f"{self._messaging_node('read Instagram conversations')}/conversations"
        # Only "id" here: asking for more per page makes Meta time out or reject the request.
        params: dict[str, Any] = {"platform": "instagram", "fields": "id", "limit": 1}
        cutoff = datetime.now(timezone.utc) - REPLY_WINDOW
        result = []
        while len(result) < limit:
            page = self._request("GET", path, params=params)
            if not page.get("data"):
                break
            convo = self._request("GET", f"/{page['data'][0]['id']}", params={
                "fields": "updated_time,participants,messages.limit(5){message,from,created_time}"})
            if datetime.strptime(convo["updated_time"], "%Y-%m-%dT%H:%M:%S%z") < cutoff:
                break
            if parsed := self._parse_conversation(convo):
                result.append(parsed)
            after = page.get("paging", {}).get("cursors", {}).get("after")
            if not after:
                break
            params["after"] = after
        return result

    def messages(self, conversation_id: str, max_messages: int = 20) -> list[dict[str, Any]]:
        """Full message history of one conversation, oldest first (Meta exposes the latest 20)."""
        params: dict[str, Any] = {"fields": "message,from,created_time", "limit": 5}
        found: list[dict[str, Any]] = []
        while len(found) < max_messages:
            page = self._request("GET", f"/{conversation_id}/messages", params=params)
            found += page.get("data", [])
            after = page.get("paging", {}).get("cursors", {}).get("after")
            if not page.get("data") or not after:
                break
            params["after"] = after
        return list(reversed(found[:max_messages]))

    def _parse_conversation(self, convo: dict[str, Any]) -> IGConversation | None:
        other = next((p for p in convo.get("participants", {}).get("data", [])
                      if p.get("id") != self.ig_user_id), None)
        if not other:
            return None
        inbound = [m for m in convo.get("messages", {}).get("data", [])
                   if m.get("from", {}).get("id") == other["id"]]
        last = inbound[0] if inbound else None  # API returns newest first
        return IGConversation(
            recipient_id=other["id"],
            username=other.get("username"),
            last_inbound_text=last.get("message") if last else None,
            last_inbound_at=datetime.strptime(last["created_time"], "%Y-%m-%dT%H:%M:%S%z") if last else None,
        )

    def send_reply(self, recipient_id: str, text: str) -> str:
        """Reply to a user who messaged us within the last 24 hours. Returns the message id."""
        body = self._request("POST", f"{self._messaging_node('send Instagram messages')}/messages",
                             json={"recipient": {"id": recipient_id}, "message": {"text": text}})
        return body.get("message_id", "")
