"""Render layout-classification JSONL as a browsable, linked HTML report."""
from __future__ import annotations

import html
import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAYOUT_LABELS = {
    "prose": "문단", "table": "표", "diagram": "도식", "mixed": "혼합",
    "no_text": "텍스트 없음", "unknown": "검토 필요",
}
DEFAULT_LABELS = PROJECT_ROOT / "files" / "_reviewed" / "body_image_ocr" / "layout_labels.jsonl"


def _read_labels(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    labels: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.is_file():
        return labels
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            key = (str(row.get("source_path", "")), str(row.get("image_sha256", "")))
            if all(key):
                labels[key] = row
    return labels


def _local_href(target: Path, report_dir: Path) -> str:
    relative = os.path.relpath(target, report_dir).replace(os.sep, "/")
    return quote(relative, safe="/:.-_")


def _resolve_source(row: dict[str, Any], source_cache: dict[str, tuple[Path | None, dict[str, Any]]]) -> tuple[Path | None, dict[str, Any]]:
    raw = row.get("source_path")
    if not isinstance(raw, str):
        return None, {}
    if raw in source_cache:
        return source_cache[raw]
    requested = PROJECT_ROOT / Path(raw)
    resolved = requested if requested.is_file() else None
    if resolved is None:
        dataset = row.get("dataset")
        basename = Path(raw).name
        from scripts.crawlers.common.storage import get_dataset_paths
        candidate_root = get_dataset_paths(PROJECT_ROOT, str(dataset)).json
        if candidate_root.is_dir():
            matches = list(candidate_root.rglob(basename))
            if len(matches) == 1:
                resolved = matches[0]
    metadata: dict[str, Any] = {}
    if resolved is not None:
        try:
            metadata = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
    source_cache[raw] = (resolved, metadata)
    return resolved, metadata


def render_report(rows: list[dict[str, Any]], report_path: Path, *, input_path: Path | None = None,
                  labels_path: Path = DEFAULT_LABELS) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir = report_path.parent
    source_cache: dict[str, tuple[Path | None, dict[str, Any]]] = {}
    saved_labels = _read_labels(labels_path)
    cards: list[str] = []
    for index, row in enumerate(rows, start=1):
        layout = str(row.get("layout_type", "unknown"))
        confidence = float(row.get("layout_confidence", 0) or 0)
        source_file, metadata = _resolve_source(row, source_cache)
        saved_path = row.get("saved_path")
        image_file = PROJECT_ROOT / Path(saved_path) if isinstance(saved_path, str) else None
        source_path = str(row.get("source_path", ""))
        source_name = Path(source_path).name
        title = str(metadata.get("title") or Path(str(row.get("source_path", "문서"))).stem)
        display_title = source_name if "?" in title else title
        title_note = '<span class="muted">문서 제목 글자가 손상되어 파일명으로 표시</span>' if "?" in title else ""
        article_url = metadata.get("url")
        if source_file is not None:
            source_link = f'<a class="button" href="{html.escape(_local_href(source_file, report_dir), quote=True)}" target="_blank">원본 JSON 열기</a>'
        else:
            source_link = '<span class="muted">원본 파일 경로 확인 필요</span>'
        page_link = (f'<a class="button secondary" href="{html.escape(str(article_url), quote=True)}" target="_blank" rel="noreferrer">게시글 열기</a>'
                     if isinstance(article_url, str) and article_url.startswith(("http://", "https://")) else "")
        if image_file is not None and image_file.is_file():
            preview = (f'<a href="{html.escape(_local_href(image_file, report_dir), quote=True)}" target="_blank" title="원본 크기 이미지 열기">'
                       f'<img loading="lazy" src="{html.escape(_local_href(image_file, report_dir), quote=True)}" alt="본문 이미지 미리보기"></a>')
        else:
            preview = '<div class="no-preview">미리보기 없음</div>'
        evidence = row.get("classification_evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        warnings = row.get("warnings", row.get("current_warnings", []))
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        tags = " ".join(f'<span class="tag">{html.escape(str(item))}</span>' for item in warnings)
        prior = saved_labels.get((source_path, str(row.get("image_sha256", ""))), {})
        reviewer_type = str(prior.get("reviewer_layout_type") or "")
        reviewer_notes = str(prior.get("reviewer_notes") or "")
        expected = str(prior.get("expected_text_or_structure") or "")
        options = '<option value="">미검토</option>' + "".join(
            f'<option value="{key}"{" selected" if reviewer_type == key else ""}>{html.escape(label)}</option>'
            for key, label in LAYOUT_LABELS.items())
        cards.append(f"""
        <article class="card" data-layout="{html.escape(layout, quote=True)}" data-confidence="{confidence:.3f}" data-search="{html.escape((title + ' ' + source_name + ' ' + source_path).casefold(), quote=True)}" data-source-path="{html.escape(source_path, quote=True)}" data-image-sha256="{html.escape(str(row.get('image_sha256', '')), quote=True)}" data-saved-path="{html.escape(str(saved_path or ''), quote=True)}" data-image-url="{html.escape(str(row.get('source_url') or ''), quote=True)}" data-current-warnings="{html.escape(json.dumps(row.get('current_warnings', []), ensure_ascii=False), quote=True)}">
          <div class="preview">{preview}</div>
          <div class="details">
            <div class="eyebrow">#{index} · {html.escape(str(row.get('dataset', '')))} · {html.escape(source_name)}</div>
            <h2>{html.escape(display_title)}</h2>{title_note}
            <div class="badges"><span class="layout {html.escape(layout, quote=True)}">{html.escape(LAYOUT_LABELS.get(layout, layout))}</span><span class="confidence">confidence {confidence:.2f}</span></div>
            <div class="links">{source_link}{page_link}</div>
            <div class="tags">{tags}</div>
            <details class="review"><summary>사람 검토 라벨 입력</summary>
              <label>확정 분류<select data-field="reviewer_layout_type">{options}</select></label>
              <label>검토 메모<textarea data-field="reviewer_notes" rows="2" placeholder="예: 상단 안내 문단과 하단 격자 표가 함께 있음">{html.escape(reviewer_notes)}</textarea></label>
              <label>보존할 텍스트·구조<textarea data-field="expected_text_or_structure" rows="2" placeholder="예: 표의 행·열, 병합 셀, 도식의 노드와 연결 관계">{html.escape(expected)}</textarea></label>
              <span class="save-state" aria-live="polite">이 브라우저에 임시 저장됩니다.</span>
            </details>
            <details><summary>판정 근거와 경로</summary>
              <ul>{''.join(f'<li>{html.escape(str(item))}</li>' for item in evidence)}</ul>
              <div class="path">게시글 파일: {html.escape(source_path)}</div>
              <div class="path">이미지 파일: {html.escape(str(saved_path or ''))}</div>
              <div class="hash">SHA-256: {html.escape(str(row.get('image_sha256', '')))}</div>
            </details>
          </div>
        </article>""")

    summary_counts = {key: sum(1 for row in rows if row.get("layout_type") == key) for key in LAYOUT_LABELS}
    count_cards = "".join(f'<div class="metric"><strong>{value:,}</strong><span>{html.escape(LAYOUT_LABELS[key])}</span></div>'
                          for key, value in summary_counts.items())
    source_label = input_path.name if input_path else "분류 결과"
    storage_key = json.dumps("body-image-layout-reviews:" + str(input_path.resolve() if input_path else report_path.resolve()))
    download_name = json.dumps((input_path.stem if input_path else report_path.stem) + ".reviewed.jsonl")
    merge_command = ".venv\\Scripts\\python.exe -m scripts.preprocessing.apply_body_image_layout_labels --input files/_reviewed/body_image_ocr/" + (input_path.stem if input_path else report_path.stem) + ".reviewed.jsonl"
    review_script = r"""
const storageKey=__STORAGE_KEY__, downloadName=__DOWNLOAD_NAME__;
let drafts={}; try { drafts=JSON.parse(localStorage.getItem(storageKey)||'{}'); } catch {}
for (const card of document.querySelectorAll('.card')) {
  const key=card.dataset.sourcePath+'#'+card.dataset.imageSha256;
  const controls=[...card.querySelectorAll('[data-field]')];
  if (drafts[key]) for (const control of controls) if (drafts[key][control.dataset.field]!==undefined) control.value=drafts[key][control.dataset.field];
  for (const control of controls) for (const eventName of ['input','change']) control.addEventListener(eventName,()=>{
    const fields={}; for (const item of controls) fields[item.dataset.field]=item.value;
    drafts[key]=fields; try { localStorage.setItem(storageKey,JSON.stringify(drafts)); } catch {}
    card.querySelector('.save-state').textContent='임시 저장됨';
  });
}
document.querySelector('#export').addEventListener('click',()=>{
  const output=[];
  for (const card of document.querySelectorAll('.card')) {
    const fields={}; for (const item of card.querySelectorAll('[data-field]')) fields[item.dataset.field]=item.value;
    if (!fields.reviewer_layout_type) continue;
    output.push({source_path:card.dataset.sourcePath,image_sha256:card.dataset.imageSha256,
      saved_path:card.dataset.savedPath,image_url:card.dataset.imageUrl||null,
      current_warnings:JSON.parse(card.dataset.currentWarnings||'[]'),
      suggested_layout_type:card.dataset.layout,reviewer_layout_type:fields.reviewer_layout_type,
      reviewer_notes:fields.reviewer_notes||'',expected_text_or_structure:fields.expected_text_or_structure||''});
  }
  if (!output.length) { alert('확정 분류를 선택한 카드가 없습니다.'); return; }
  const blob=new Blob([output.map(row=>JSON.stringify(row)).join('\n')+'\n'],{type:'application/x-ndjson;charset=utf-8'});
  const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download=downloadName; link.click(); URL.revokeObjectURL(link.href);
  alert(output.length+'개 검토 라벨을 내보냈습니다. 병합 명령은 화면 안내를 참고하세요.');
});
""".replace("__STORAGE_KEY__", storage_key).replace("__DOWNLOAD_NAME__", download_name)
    document = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>본문 이미지 분류 검토</title>
<style>
:root{{color-scheme:light;--ink:#1d2939;--muted:#667085;--line:#e4e7ec;--blue:#175cd3;--bg:#f7f8fa}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI","Malgun Gothic",sans-serif}}
header{{position:sticky;top:0;z-index:2;background:#fff;border-bottom:1px solid var(--line);padding:16px max(20px,calc((100vw - 1180px)/2));box-shadow:0 2px 8px #1018280d}}
h1{{font-size:20px;margin:0 0 3px}}.sub{{color:var(--muted);font-size:12px}}.metrics{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}}
.metric{{background:#f2f4f7;padding:7px 12px;border-radius:8px;min-width:92px;display:flex;gap:8px;align-items:baseline}}.metric strong{{font-size:16px}}.metric span{{color:var(--muted)}}
.controls{{display:flex;gap:8px;flex-wrap:wrap}}input,select{{padding:9px 11px;border:1px solid #d0d5dd;border-radius:7px;background:white;color:var(--ink)}}input{{min-width:260px;flex:1}}
main{{max-width:1180px;margin:18px auto;padding:0 16px}}.card{{display:grid;grid-template-columns:200px 1fr;gap:18px;background:white;border:1px solid var(--line);border-radius:11px;padding:14px;margin:12px 0;box-shadow:0 1px 2px #1018280a}}
.preview{{height:180px;background:#f2f4f7;border-radius:7px;display:flex;align-items:center;justify-content:center;overflow:hidden}}.preview img{{width:100%;height:100%;object-fit:contain}}.no-preview{{color:var(--muted)}}
.eyebrow,.muted,.path,.hash{{font-size:12px;color:var(--muted);overflow-wrap:anywhere}}h2{{font-size:16px;margin:5px 0 8px}}.badges,.links,.tags{{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0}}.layout,.confidence,.tag{{padding:3px 8px;border-radius:20px;font-size:12px;background:#f2f4f7}}.layout.unknown{{background:#fff1f3;color:#c01048}}.layout.diagram{{background:#eff8ff;color:#175cd3}}.layout.table{{background:#ecfdf3;color:#027a48}}.layout.prose{{background:#f4f3ff;color:#5925dc}}.layout.mixed{{background:#fff6ed;color:#c4320a}}
.button{{display:inline-block;text-decoration:none;padding:7px 10px;border-radius:6px;background:var(--blue);color:white;font-weight:600}}.button.secondary{{background:#eef4ff;color:#1849a9}}details{{margin-top:7px}}summary{{cursor:pointer;color:#344054}}ul{{margin:7px 0;padding-left:20px}}li{{overflow-wrap:anywhere}}.path,.hash{{margin-top:5px}}
.export{{border:0;cursor:pointer;font:inherit;margin-left:auto}}.review label{{display:block;margin:9px 0 5px;font-weight:600;font-size:12px}}.review select,.review textarea{{display:block;width:100%;margin-top:4px;font:inherit;font-weight:400}}.review textarea{{resize:vertical;min-height:44px}}.save-state{{font-size:11px;color:#027a48}}
.exportbar{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}}.exportbar p{{margin:4px 0;color:var(--muted);font-size:12px;overflow-wrap:anywhere}}.exportbar code{{color:#1849a9;user-select:all}}
@media(max-width:650px){{header{{position:static;padding:14px}}main{{padding:0 10px}}.card{{grid-template-columns:1fr;gap:10px}}.preview{{height:220px}}}}
</style></head><body>
<header><h1>본문 이미지 레이아웃 검토</h1><div class="sub">{html.escape(source_label)} · {len(rows):,}개 이미지 · 카드에서 미리보기와 원본 문서에 바로 접근할 수 있습니다.</div>
<div class="metrics">{count_cards}</div><div class="controls"><input id="search" type="search" placeholder="제목, 파일명, 경로 검색"><select id="filter"><option value="">모든 분류</option>{''.join(f'<option value="{key}">{label}</option>' for key,label in LAYOUT_LABELS.items())}</select><select id="confidence"><option value="">모든 confidence</option><option value="low">낮음 (&lt; 0.60)</option><option value="medium">중간 (0.60–0.84)</option><option value="high">높음 (≥ 0.85)</option></select></div></header>
<main id="cards"><div class="exportbar"><button id="export" class="button export">검토 라벨 내보내기</button><p>분류를 선택한 카드만 내보냅니다. 선택과 메모는 이 브라우저에 임시 저장됩니다.</p><p>다운로드한 JSONL을 files/_reviewed/body_image_ocr/에 둔 뒤 실행: <code>{html.escape(merge_command)}</code></p></div>{''.join(cards)}</main>
<script>
const search=document.querySelector('#search'), filter=document.querySelector('#filter'), confidence=document.querySelector('#confidence');
function update(){{const q=search.value.trim().toLocaleLowerCase();for(const card of document.querySelectorAll('.card')){{const c=Number(card.dataset.confidence);const band=c<.6?'low':c<.85?'medium':'high';card.hidden=!!((filter.value&&card.dataset.layout!==filter.value)||(confidence.value&&band!==confidence.value)||(q&&!card.dataset.search.includes(q)));}}}}
search.addEventListener('input',update);filter.addEventListener('change',update);confidence.addEventListener('change',update);
{review_script}
</script></body></html>"""
    report_path.write_text(document, encoding="utf-8")


def render_jsonl(input_path: Path, report_path: Path, labels_path: Path = DEFAULT_LABELS) -> int:
    rows = []
    with input_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    render_report(rows, report_path, input_path=input_path, labels_path=labels_path)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Classification JSONL")
    parser.add_argument("--output", type=Path, help="HTML destination; defaults beside input")
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS, help="Label file used to restore prior human reviews")
    args = parser.parse_args()
    input_path = args.input if args.input.is_absolute() else PROJECT_ROOT / args.input
    output_path = args.output if args.output and args.output.is_absolute() else (
        PROJECT_ROOT / args.output if args.output else input_path.with_suffix(".html")
    )
    labels_path = args.labels if args.labels.is_absolute() else PROJECT_ROOT / args.labels
    count = render_jsonl(input_path, output_path, labels_path)
    print(json.dumps({"html": str(output_path), "images": count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
