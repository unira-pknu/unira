'Main website common http.'

from __future__ import annotations
from requests.adapters import HTTPAdapter
from scripts.crawlers.common.logging import log_event
from scripts.crawlers.main.common.storage import BASE_URL, GUIDE_URL, log
from urllib.parse import urljoin
import logging
import requests
import ssl
import time
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


REQUEST_DELAY = 1.0


REQUEST_TIMEOUT = 60


class LegacySSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        proxy_kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def build_session(referer: str = GUIDE_URL) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.verify = False
    session.mount("https://", LegacySSLAdapter())
    session.mount("http://", LegacySSLAdapter())
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": referer,
        }
    )
    return session


def fetch(
    session: requests.Session,
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    stream: bool = False,
    allow_redirects: bool = True,
) -> requests.Response:
    log_event(log, logging.INFO, "request_started", url=url, method=method)
    time.sleep(REQUEST_DELAY)
    try:
        resp = session.request(
            method, url, data=data, timeout=REQUEST_TIMEOUT, verify=False,
            stream=stream, allow_redirects=allow_redirects,
        )
    except requests.RequestException as exc:
        log_event(log, logging.ERROR, "request_failed", url=url, method=method, error=exc, retryable=True)
        raise
    if resp.status_code >= 400:
        log_event(log, logging.ERROR, "request_failed", url=url, method=method, status_code=resp.status_code, retryable=False)
    resp.encoding = resp.encoding or "utf-8"
    return resp


def resolve_media_pdf_url(session: requests.Session, media_id: str) -> str:
    resp = fetch(session, f"{BASE_URL}/common/getMdaId.do", method="POST", data={"no": media_id})
    resp.raise_for_status()
    payload = resp.json()
    path = str(payload.get("response") or "").strip()
    if not path:
        raise ValueError(f"empty media path for id={media_id}")
    return urljoin(BASE_URL, "/upload/" + path.lstrip("/"))


def is_html_response(resp: requests.Response, expected_url: str) -> bool:
    mime = resp.headers.get("Content-Type", "").lower()
    return (resp.status_code == 200 and resp.url == expected_url
            and ("text/html" in mime or "application/xhtml+xml" in mime))
