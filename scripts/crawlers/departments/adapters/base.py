"""Interface between CMS-specific HTML parsing and the common crawl runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
import re
from typing import Any

from bs4 import BeautifulSoup


class DepartmentCMSAdapter(ABC):
    name: str
    version: str = "1.0"
    # Only adapters that guarantee monotonically increasing numeric post
    # numbers may use last_no for incremental filtering.
    uses_numeric_post_order: bool = False

    def select_body_root(self, soup: BeautifulSoup, *, page_url: str = "",
                         base_url: str = "", detail: bool = False):
        from scripts.crawlers.common.body_images import select_body_root
        return select_body_root(soup)

    def static_detail_links(self, soup: BeautifulSoup, *, page_url: str) -> list[dict[str, Any]]:
        return []

    def is_profile_detail_url(self, url: str) -> bool:
        return False

    def profile_target_allowed(self, target: str, requested_url: str) -> bool:
        return False

    def parse_profile(self, soup: BeautifulSoup, *, fallback_title: str,
                      page_url: str, base_url: str, site_prefix: str) -> dict[str, Any] | None:
        return None

    @abstractmethod
    def discover_menus(self, html: str, base_url: str) -> list[dict[str, str]]: ...

    @abstractmethod
    def analyze_section(
        self, *, name: str, page_url: str, html: str, final_url: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse_list(self, soup: BeautifulSoup, board_url: str) -> list[dict[str, Any]]: ...

    def count_list_candidates(self, soup: BeautifulSoup, board_url: str) -> int:
        """Count apparent detail links independently from the strict parser.

        This deliberately uses broad URL/onclick evidence. A non-zero count
        paired with an empty parse result is a parser mismatch, not an empty
        board.
        """
        candidates: set[str] = set()
        for anchor in soup.select("a[href], a[onclick]"):
            href = str(anchor.get("href") or "").strip().lower()
            onclick = str(anchor.get("onclick") or "").strip().lower()
            if (
                "action=view" in href
                or "pgmode=view" in href
                or "mode=read" in href
                or ("mode=2" in href and re.search(r"(?:[?&]|^)no=", href) is not None)
                or "kind=view" in href
                or re.search(r"(?:[?&]|^)idx=", href) is not None
                or "moveview(" in onclick
            ):
                candidates.add(f"{href}|{onclick}")
        return len(candidates)

    def list_request(
        self, board_url: str, page: int, bbs_id: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Return the paginated list request while preserving the legacy default."""
        params: dict[str, Any] = {"pageIndex": page}
        if bbs_id:
            params["bbsId"] = bbs_id
        return board_url, params

    @abstractmethod
    def parse_detail(
        self, soup: BeautifulSoup, post_url: str, item: dict[str, Any],
        *, base_url: str, site_prefix: str,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def parse_static(
        self, soup: BeautifulSoup, *, fallback_title: str,
        page_url: str = "", base_url: str = "", site_prefix: str = "",
    ) -> dict[str, Any]: ...

    @abstractmethod
    def parse_attachments(
        self, content: BeautifulSoup | Any, *, page_url: str,
        base_url: str, site_prefix: str,
    ) -> list[dict[str, str]]: ...
