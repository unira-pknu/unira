"""Category-first storage for the main website; logical dataset IDs stay stable."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from scripts.crawlers.common.storage import DatasetPaths, safe_component


STUDENT_CATEGORIES = {
    "학사안내_페이지": ("학사안내",), "학사안내_파일": ("학사안내",),
    "학사정보": ("학사정보",), "E-하나로": ("학사정보",),
    "교육과정": ("교육과정",), "학생생활": ("학생생활",),
    "수강신청": ("수강신청",),
    "학점교류_게시판": ("학사안내", "학점교류"),
    "교내_식당_주간식단표": ("학생생활", "교내_식당_주간식단표"),
    "이수_로드맵": ("교육과정", "이수_로드맵"),
    "예비부경인": ("학생생활", "대학생활_가이드", "예비부경인"),
    "슬기로운_대학생활": ("학생생활", "대학생활_가이드", "슬기로운_대학생활"),
}
ATTACHMENT_CATEGORIES = {"학사안내_파일", "교육과정", "이수_로드맵", "예비부경인", "슬기로운_대학생활", "E-하나로"}
POST_CATEGORIES = {"학점교류_게시판", "교내_식당_주간식단표"}


def main_root(project_root: Path) -> Path:
    return project_root.resolve() / "files" / "pknu_main"


def derived_root(project_root: Path, dataset: str) -> Path:
    return main_root(project_root) / "_derived" / safe_component(dataset, "dataset")


def asset_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"}:
        return "images"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".hwp", ".hwpx", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv"}:
        return "office"
    if suffix in {".zip", ".rar", ".7z"}:
        return "archives"
    if suffix in {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}:
        return "audio"
    return "other"


def attachment_path(directory: Path, filename: str, project_root: Path) -> Path:
    if directory.resolve().is_relative_to(main_root(project_root)):
        kind = asset_type(filename)
        return directory / filename if directory.name == kind else directory / kind / filename
    return directory / filename


def document_kind(doc: dict) -> str:
    if parse_qs(urlparse(str(doc.get("url") or "")).query).get("action") == ["view"]:
        return "posts"
    return "attachments" if doc.get("type") in {"guide", "file", "pdf", "ebook"} else "pages"


def is_crawler_document(path: Path) -> bool:
    """Distinguish ingestible documents from specialty collection summaries."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and bool(doc.get("type") and doc.get("url"))


def page_root(project_root: Path, page_id: int) -> Path:
    from scripts.crawlers.main.routes import PAGE_SETTINGS
    category_path = PAGE_SETTINGS.get(page_id, {}).get("category_path")
    if not category_path:
        return main_root(project_root) / "_discovery" / f"main_{page_id}"
    return main_root(project_root).joinpath(*category_path)


def page_json(project_root: Path, page_id: int, filename: str | None = None) -> Path:
    return page_root(project_root, page_id) / "json" / "pages" / (filename or f"main_{page_id}.json")


class MainDatasetPaths(DatasetPaths):
    def category_root(self, category: str) -> Path:
        safe_component(category, "category")
        if self.dataset == "pknu_notice":
            return page_root(self.project_root, 163) / category
        if category in STUDENT_CATEGORIES:
            return self.root / "대학생활" / Path(*STUDENT_CATEGORIES[category])
        from scripts.crawlers.main.routes import PAGE_SETTINGS
        configured = {tuple(row["category_path"]) for row in PAGE_SETTINGS.values()
                      if row.get("category_path") and row["category_path"][-1] == category}
        if len(configured) > 1:
            raise ValueError(f"ambiguous category: {category}")
        return self.root.joinpath(*next(iter(configured))) if configured else self.root / "대학생활" / category

    def category_json(self, category: str) -> Path:
        return self.category_root(category) / "json"

    def category_html(self, category: str) -> Path:
        return self.category_root(category) / "html"

    def document_json(self, category: str, slug: str, *, kind: str | None = None) -> Path:
        name = f"{safe_component(slug, 'slug')}.json"
        if kind is None:
            matches = sorted(self.category_json(category).glob(f"*/{name}"))
            if matches:
                return matches[0]
            kind = "posts" if self.dataset == "pknu_notice" or category in POST_CATEGORIES else "attachments" if category in ATTACHMENT_CATEGORIES else "pages"
        if kind not in {"pages", "posts", "attachments"}:
            raise ValueError(f"unknown document kind: {kind}")
        return self.category_json(category) / kind / name

    def document_html(self, category: str, slug: str) -> Path:
        return self.category_html(category) / f"{safe_component(slug, 'slug')}.html"

    def attachment_dir(self, category: str, slug: str) -> Path:
        return self.category_root(category) / "files" / safe_component(slug, "slug")

    def attachment_file(self, category: str, slug: str, filename: str) -> Path:
        return attachment_path(self.attachment_dir(category, slug), filename, self.project_root)

    def iter_document_json(self):
        if self.json.exists():
            for path in self.json.rglob("*.json"):
                if "json" in path.relative_to(self.json).parts and is_crawler_document(path):
                    yield path


def get_main_dataset_paths(project_root: Path, dataset: str) -> MainDatasetPaths:
    root = main_root(project_root)
    section = root / ("커뮤니티/공지사항" if dataset == "pknu_notice" else "대학생활")
    return MainDatasetPaths(project_root.resolve(), dataset, root,
                            root / "_state" / f"{dataset}.json", section,
                            section, section, section,
                            root / "_archive" / dataset / "deleted", root / "_runs" / dataset)
