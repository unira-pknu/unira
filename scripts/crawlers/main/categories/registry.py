"""Main-site navigation labels and their collection entry points."""

from importlib import import_module


# Slash-separated navigation labels use a single safe storage component.
SITE_CATEGORIES = {
    "대학/대학원": {"directory": "대학_대학원", "module": None},
    "연구/산학": {"directory": "연구_산학", "module": None},
    "대학생활": {"directory": "대학생활", "module": "student_life"},
    "입학/취업": {"directory": "입학_취업", "module": None},
    "커뮤니티": {"directory": "커뮤니티", "module": "community"},
    "대학소개": {"directory": "대학소개", "module": "university_intro"},
    "개교 80주년": {"directory": "개교_80주년", "module": None},
}


def category_label(directory: str) -> str:
    for label, settings in SITE_CATEGORIES.items():
        if settings["directory"] == directory:
            return label
    raise ValueError(f"unknown main website category: {directory}")


def category_executor(directory: str):
    settings = SITE_CATEGORIES[category_label(directory)]
    module = ("scripts.crawlers.main.categories." + settings["module"] if settings["module"]
              else "scripts.crawlers.main.common.pages")
    return import_module(module).execute
