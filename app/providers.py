import logging
import time
import re
from functools import lru_cache
from urllib.parse import urlparse
from .config import settings

log = logging.getLogger("telephony")


class FakeProvider:
    """No real calls. Use POST /dev/calls/{id}/simulate to finish a call."""
    name = "fake"

    def place_call(self, *, from_number: str, to_number: str, call_id: int) -> str:
        log.info("[fake] call %s -> %s", call_id, to_number)
        return f"fake-{call_id}"


def check_settings() -> list:
    """Plain-English problems with the Plivo settings (empty list = fine)."""
    problems = []
    url = settings.public_base_url
    p = urlparse(url)
    host = p.hostname or ""
    if p.scheme != "https":
        problems.append(f"PUBLIC_BASE_URL must start with https:// (it is '{url}')")
    elif "." not in host or host == "localhost" or re.fullmatch(r"[\d.]+", host):
        problems.append(f"PUBLIC_BASE_URL must be a public domain name, not localhost or an IP (it is '{url}')")
    if p.path not in ("", "/") or p.query:
        problems.append(f"PUBLIC_BASE_URL must have nothing after the domain: no /docs or other path (it is '{url}')")
    if re.search(r"\s|[\"'<>]", url):
        problems.append("PUBLIC_BASE_URL contains a space or quote character; remove it")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", settings.webhook_secret):
        problems.append("WEBHOOK_SECRET must be at least 16 characters, only letters, numbers, - or _ "
                        "(other characters break the webhook address)")
    return problems


class TunnelNotReady(Exception):
    """The public tunnel hostname can't be resolved yet, so Plivo would reject it."""


_ready_hosts: set = set()
_first_miss: dict = {}
GIVE_UP_AFTER = 150     # seconds of "not visible yet" before we let Plivo be the judge


def tunnel_ready(host: str) -> bool:
    """True once public DNS (IPv4 or IPv6, Cloudflare or Google) can resolve the tunnel hostname.
    Fails open: if the check can't run, or the name stays unresolved for 2.5 minutes, we try Plivo anyway."""
    if host in _ready_hosts:
        return True
    import httpx
    answered, responded, detail = False, False, []
    for url, headers in (("https://cloudflare-dns.com/dns-query", {"accept": "application/dns-json"}),
                         ("https://dns.google/resolve", {})):
        for rtype in ("A", "AAAA"):
            try:
                j = httpx.get(url, params={"name": host, "type": rtype}, headers=headers, timeout=5).json()
            except Exception:
                continue
            responded = True
            detail.append(f"{url.split('/')[2]} {rtype}: status {j.get('Status')}, {len(j.get('Answer') or [])} answers")
            answered = answered or bool(j.get("Answer"))
    if answered:
        _ready_hosts.add(host)
        return True
    if not responded:
        return True
    first = _first_miss.setdefault(host, time.time())
    if time.time() - first > GIVE_UP_AFTER:
        log.warning("%s still not found by public DNS after %ss; trying Plivo anyway (%s)", host, GIVE_UP_AFTER, "; ".join(detail))
        return True
    log.info("DNS check for %s: %s", host, "; ".join(detail))
    return False


class PlivoProvider:
    name = "plivo"

    def __init__(self):
        import plivo
        if not (settings.plivo_auth_id and settings.plivo_auth_token and settings.public_base_url):
            raise RuntimeError("Set PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN and PUBLIC_BASE_URL")
        if settings.webhook_secret in ("", "dev-secret", "change-me-to-a-long-random-string"):
            raise RuntimeError("Set WEBHOOK_SECRET to a long random string")
        problems = check_settings()
        if problems:
            raise RuntimeError("Fix these in .env: " + "; ".join(problems))
        self.client = plivo.RestClient(settings.plivo_auth_id, settings.plivo_auth_token)

    def _url(self, path: str, call_id: int) -> str:
        return f"{settings.public_base_url}{path}?call_id={call_id}&token={settings.webhook_secret}"

    def place_call(self, *, from_number: str, to_number: str, call_id: int) -> str:
        host = urlparse(settings.public_base_url).hostname or ""
        if not tunnel_ready(host):
            raise TunnelNotReady(f"{host} is not visible on public DNS yet")
        answer_url = self._url("/plivo/answer", call_id)
        try:
            resp = self.client.calls.create(
                from_=from_number.lstrip("+"),
                to_=to_number.lstrip("+"),
                answer_url=answer_url, answer_method="POST",
                hangup_url=self._url("/plivo/hangup", call_id), hangup_method="POST",
                ring_timeout=30,
            )
        except Exception as e:
            shown = answer_url.replace(settings.webhook_secret, "***")
            log.error("Plivo rejected the call request: %s | from=%s to=%s answer_url=%s",
                      e, from_number, to_number, shown)
            raise
        return resp.request_uuid if hasattr(resp, "request_uuid") else resp["request_uuid"]


@lru_cache
def get_provider():
    return PlivoProvider() if settings.provider == "plivo" else FakeProvider()
