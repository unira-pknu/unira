import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.crawlers.common.reader import read_document
from scripts.crawlers.common.storage import get_dataset_paths
paths = get_dataset_paths(PROJECT_ROOT, "pknu_student_life")
ebook_url = "https://www.pknu.ac.kr/ebook/col_life/kor/index.html"

text_pdfs = []
image_pdfs = []
total_chars = 0
ebook = None

for p in paths.iter_document_json():
    doc = read_document(
        json.loads(p.read_text(encoding="utf-8")),
        dataset="pknu_student_life", project_root=PROJECT_ROOT,
    )
    if doc.get("url") == ebook_url:
        ebook = doc
        continue
    n = len(doc.get("content") or "")
    total_chars += n
    entry = {
        "slug": doc.get("slug"),
        "title": doc.get("title", "")[:60],
        "subcategory": doc.get("subcategory"),
        "year": doc.get("year"),
        "chars": n,
        "skipped": bool(doc.get("pdf_text_skipped")),
    }
    if doc.get("pdf_text_skipped") or n < 80:
        image_pdfs.append(entry)
    else:
        text_pdfs.append(entry)

n_guide = len(text_pdfs) + len(image_pdfs)
pct = (len(image_pdfs) / n_guide * 100) if n_guide else 0

print("=== GUIDE", n_guide, "건 ===")
print("text_type:", len(text_pdfs))
print("image_type:", len(image_pdfs))
print("image_pct:", round(pct, 1))
print("total_content_chars:", total_chars)
print()
for e in sorted(text_pdfs + image_pdfs, key=lambda x: x["chars"], reverse=True):
    kind = "IMAGE" if e["skipped"] or e["chars"] < 80 else "TEXT"
    print(f"  [{kind}] {e['chars']:>7} | y={e.get('year')} | {e['title']}")
print()
print("=== E-하나로 ===")
if ebook:
    print("pdf_not_found:", ebook.get("pdf_not_found"))
    print("content_len:", len(ebook.get("content") or ""))
