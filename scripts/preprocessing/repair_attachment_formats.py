"""Inspect attachment signatures/download history and recover into an isolated run.

No canonical files, crawler state, vectors, or database rows are modified.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
from urllib.parse import urljoin, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.preprocessing.collected import scopes, source_files
from scripts.preprocessing.file_formats import FileFormat, detect_file_format, extract_formatted_blocks
from scripts.preprocessing.file_preprocessing import (
    MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_MEMBER_BYTES, MAX_ARCHIVE_TOTAL_BYTES,
    chunk_blocks, decoded_archive_member_name, is_safe_archive_member, make_slug,
    validate_archive_member_targets,
)
from scripts.recovery.common import atomic, digest, read, event, receipt, inside as resolve_inside
from scripts.artifact_paths import reference_path
from scripts.preprocessing.text_cleaning import clean_extracted_text, has_searchable_body

POLICY = "attachment-signature-v1"
BINARY_EXTS = {".pdf", ".zip", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rar", ".7z"}
IMAGE_KINDS = {"jpeg", "png", "gif", "bmp", "tiff", "webp"}
STATUS_LABELS = {"processed": "처리 성공", "body_extraction_failed": "본문 추출 실패", "unrecoverable": "복구 불가"}


def inside(path, boundary=None):
    return resolve_inside(path, root=ROOT, boundary=boundary)


def relative(path):
    return reference_path(inside(path), ROOT)


def history_index():
    """Bind download records by saved path, retaining every conflicting receipt."""
    histories, candidates, issues = defaultdict(list), {}, []
    for dataset, source, _ in sorted(scopes(ROOT), key=lambda row: row[0]):
        for path in source_files(source):
            if path.suffix.lower() in BINARY_EXTS:
                candidates.setdefault(relative(path), dataset)
            if path.suffix.lower() != ".json":
                continue
            try:
                payload = read(path)
                if not isinstance(payload, dict) or not isinstance(payload.get("attachments"), list):
                    continue
                for attachment in payload["attachments"]:
                    if not isinstance(attachment, dict) or not attachment.get("saved_path"):
                        continue
                    saved = relative(attachment["saved_path"])
                    checksum = str(attachment.get("sha256") or "").lower()
                    histories[saved].append({
                        "history_path": relative(path), "saved_path": saved, "dataset": dataset,
                        "source_dataset": payload.get("source_dataset") or dataset,
                        "source_page_url": payload.get("url"), "doc_title": payload.get("title"),
                        "attachment_id": attachment.get("id"), "attachment_name": attachment.get("name"),
                        "url": attachment.get("url"), "final_url": attachment.get("final_url") or attachment.get("downloaded_from_url"),
                        "content_type": attachment.get("content_type"), "downloaded": attachment.get("downloaded"),
                        "expected_sha256": checksum if re.fullmatch(r"[0-9a-f]{64}", checksum) else None,
                    })
                    candidates.setdefault(saved, dataset)
            except (OSError, ValueError, TypeError) as error:
                issues.append({"history_path": relative(path), "error": str(error)})
    # Syllabus source_url is a lookup screen, never a stable PDF download URL.
    try:
        from scripts.preprocessing.syllabus_provenance import load_syllabus_index
        for saved, provenance in load_syllabus_index(ROOT).items():
            histories[saved].append({"history_path": provenance["download_receipt_path"],
                "dataset": "pknu_syllabus", "source_dataset": "pknu_syllabus",
                "source_page_url": provenance["doc_url"], "doc_title": provenance["doc_title"],
                "attachment_name": provenance["attachment_name"], "expected_sha256": provenance["attachment_sha256"],
                "source_link_kind": provenance["source_link_kind"], "task_id": provenance["syllabus_task_id"], "downloaded": True})
            candidates.setdefault(saved, "pknu_syllabus")
    except (OSError, ValueError, KeyError) as error:
        issues.append({"history_path": "files/pknu_syllabus/_state/downloads.json", "error": str(error)})
    return histories, candidates, issues


def scan(run, extensions=None):
    if (run / "plan.json").exists():
        raise ValueError("Plan already exists; reuse this run or choose a new --run directory")
    histories, candidates, issues = history_index()
    by_hash = defaultdict(list)
    for saved, entries in histories.items():
        for entry in entries:
            if entry.get("expected_sha256"):
                by_hash[entry["expected_sha256"]].append({**entry, "saved_path": saved})
    selected, checked, verified_copies = [], 0, []
    for saved, dataset in sorted(candidates.items()):
        path = inside(saved)
        if extensions and path.suffix.lower() not in extensions:
            continue
        # Never select a standalone body image for OCR from the raw image tree.
        if path.suffix.lower() in {".html", ".htm", ".json"} and saved not in histories:
            continue
        try:
            before = (path.stat().st_size, path.stat().st_mtime_ns)
            detected = detect_file_format(path)
            checksum = digest(path)
            if before != (path.stat().st_size, path.stat().st_mtime_ns):
                raise RuntimeError("Attachment changed during inspection; retry after its download finishes")
        except OSError as error:
            detected, checksum = FileFormat("unknown", None, "missing", str(error)), None
        checked += 1
        history = histories.get(saved, [])
        binding = "saved_path" if history else None
        if not history and checksum in by_hash:
            history = by_hash[checksum]
            binding = "sha256"
        if checksum in by_hash:
            verified_copies.append({"path": saved, "sha256": checksum})
        expected = {h["expected_sha256"] for h in history if h.get("expected_sha256")}
        reasons = []
        if detected.extension and not detected.matches(path.suffix):
            reasons.append("extension_mismatch")
        if detected.state != "valid":
            reasons.append("signature_" + detected.state)
        if expected and checksum not in expected:
            reasons.append("download_hash_mismatch")
        if len(expected) > 1:
            reasons.append("conflicting_download_hashes")
        if not reasons:
            continue
        selected.append({"source_path": saved, "dataset": dataset, "declared_ext": path.suffix.lower(),
                         "original_sha256": checksum, "detected": detected.to_dict(), "reasons": reasons,
                         "history_binding": binding, "download_history": history})
        event("selected", source_path=saved, detected=detected.to_dict(), reasons=reasons)
    atomic(run / "plan.json", {"policy_version": POLICY, "checked": checked, "selected": selected,
                              "verified_copies": verified_copies, "history_issues": issues})
    receipt(run, status="planned", checked=checked, selected=len(selected), history_issues=issues)
    event("planned", checked=checked, selected=len(selected), history_issues=len(issues))


class RecoveryError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def public_url(url):
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or not (host == "pknu.ac.kr" or host.endswith(".pknu.ac.kr"))):
        raise RecoveryError("SOURCE_DEFERRED", "Source is outside the configured public university scope")
    if re.search(r"/(?:login|signin|sso)(?:[/?.]|$)", parsed.path, re.I):
        raise RecoveryError("LOGIN_REQUIRED", "Source requires an interactive login")
    return str(url)


def fetch_file(session, url, target, *, source_page=False):
    """Bounded public GETs; validate every redirect before following it."""
    import requests
    from scripts.crawlers.departments.tls import mount_verified_pknu_tls
    if isinstance(session, requests.Session) and not getattr(session, "_pknu_tls_mounted", False):
        mount_verified_pknu_tls(session)
    current = public_url(url)
    limit = 2 * 1024 * 1024 if source_page else MAX_ARCHIVE_MEMBER_BYTES
    for _ in range(6):
        response = session.get(current, timeout=(15, 60), stream=True, allow_redirects=False)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise RecoveryError("BAD_REDIRECT", "Redirect has no Location")
                current = public_url(urljoin(current, location))
                continue
            if response.status_code in {401, 403}:
                raise RecoveryError("ACCESS_DEFERRED", f"Source access denied: HTTP {response.status_code}")
            if response.status_code != 200:
                raise RecoveryError("HTTP_ERROR", f"HTTP {response.status_code}")
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > limit:
                raise RecoveryError("TOO_LARGE", "Response exceeds the download limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > limit:
                        raise RecoveryError("TOO_LARGE", "Response exceeds the download limit")
                    handle.write(chunk)
            if declared and total != declared and not response.headers.get("Content-Encoding"):
                raise RecoveryError("INCOMPLETE_RESPONSE", "Received byte count differs from Content-Length")
            return {"requested_url": url, "final_url": current, "content_type": response.headers.get("Content-Type"),
                    "content_disposition": response.headers.get("Content-Disposition"), "bytes": total}
        finally:
            response.close()
    raise RecoveryError("REDIRECT_LIMIT", "Too many redirects")


def public_page_error(raw, final_url):
    """Public board pages can contain hidden comment-password dialogs."""
    from bs4 import BeautifulSoup
    from scripts.crawlers.departments.access import detect_access_block
    html = raw.decode("utf-8", errors="replace")
    evidence = detect_access_block(final_url=final_url, html=html)
    if evidence.blocked:
        return "LOGIN_REQUIRED" if "login_required" in evidence.reasons else "ACCESS_BLOCKED"
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('input[type="password"]'):
        body = soup.select_one(".bdvEdit, article, .view-content, .view_cont, .view-con, .board_view, .view_content, .board-view")
        if not body or len(body.get_text(" ", strip=True)) < 40:
            return "LOGIN_REQUIRED"
    return None


def refreshed_urls(session, history, folder):
    from bs4 import BeautifulSoup
    from scripts.crawlers.departments.attachments import attachment_candidate, html_download_error
    urls, evidence = [], []
    for entry in history:
        if not entry.get("source_page_url") or entry.get("source_link_kind") == "syllabus_search_page":
            continue
        try:
            target = folder / (hashlib.sha256(entry["source_page_url"].encode()).hexdigest() + ".html")
            fetched = fetch_file(session, entry["source_page_url"], target, source_page=True)
            raw = target.read_bytes()
            failure = public_page_error(raw, fetched["final_url"])
            if failure:
                raise RecoveryError(failure, "Source page is an error or login page")
            soup = BeautifulSoup(raw, "html.parser")
            name = re.sub(r"\s+", "", str(entry.get("attachment_name") or ""))
            if not name:
                continue
            matches = set()
            for link in soup.select("a[href]"):
                label = re.sub(r"\s+", "", link.get_text(" ", strip=True))
                if label != name:
                    continue
                candidate = urljoin(fetched["final_url"], link["href"])
                if attachment_candidate(candidate, entry["attachment_name"]):
                    matches.add(public_url(candidate))
            if len(matches) == 1:
                urls.extend(matches)
            evidence.append({**fetched, "matching_links": len(matches), "snapshot": relative(target)})
        except Exception as error:
            evidence.append({"source_page_url": entry["source_page_url"], "code": getattr(error, "code", "SOURCE_PAGE_FAILED"), "error": str(error)})
    return list(dict.fromkeys(urls)), evidence


def recover(entry, run, histories, *, network=False, cached=True):
    """Try a verified cached copy first, then recorded URLs and exact-name public links."""
    folder = run / "recovered" / hashlib.sha256(entry["source_path"].encode()).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    history = entry["download_history"]
    expected = {h["expected_sha256"] for h in history if h.get("expected_sha256")}
    attempts = []
    if len(expected) > 1:
        raise RecoveryError("AMBIGUOUS_HISTORY", "Conflicting download hashes need source review")
    for path, receipts in histories.items():
        if not cached:
            break
        if path == entry["source_path"] or not expected.intersection(h.get("expected_sha256") for h in receipts):
            continue
        candidate = inside(path)
        if candidate.is_file() and digest(candidate) in expected:
            detected = detect_file_format(candidate)
            if detected.state == "valid" and detected.kind != "html":
                target = folder / ("cached" + (detected.extension or candidate.suffix))
                shutil.copy2(candidate, target)
                if digest(target) not in expected:
                    target.unlink()
                    continue
                return target, detect_file_format(target), {"status": "verified_cached_copy", "cached_source_path": path, "attempts": attempts}
    if not network:
        raise RecoveryError("RECOVERY_NOT_REQUESTED", "Re-run process with --recover to attempt public source downloads")
    if not history:
        raise RecoveryError("NO_DOWNLOAD_HISTORY", "No original download URL is recorded")
    if all(h.get("source_link_kind") == "syllabus_search_page" for h in history):
        raise RecoveryError("SESSION_DOWNLOAD_REQUIRED", "Syllabus receipts identify a lookup screen, not a public PDF URL; use the recorded task IDs in the syllabus download workflow")
    origins = {h.get("source_page_url") for h in history if h.get("source_page_url")}
    if len(origins) > 1:
        raise RecoveryError("AMBIGUOUS_HISTORY", "Multiple parent source pages need source review")
    import requests
    from scripts.crawlers.departments.attachments import html_download_error
    with requests.Session() as session:
        session.trust_env = False  # No ambient netrc credentials or authenticated browser state.
        queue = list(dict.fromkeys(u for h in history for u in (h.get("url"), h.get("final_url")) if u))
        refreshed = False
        while True:
            for url in queue:
                temporary = folder / ("download.part" + Path(entry["source_path"]).suffix)
                try:
                    downloaded = fetch_file(session, url, temporary)
                    detected = detect_file_format(temporary)
                    if detected.kind == "html":
                        failure = html_download_error(temporary.read_bytes()[:2 * 1024 * 1024], downloaded["final_url"])
                        raise RecoveryError(failure or "HTML_RESPONSE", "Download returned HTML instead of an attachment")
                    if detected.state != "valid":
                        raise RecoveryError("INVALID_DOWNLOAD", detected.evidence)
                    checksum = digest(temporary)
                    target = folder / ("recovered" + (detected.extension or ".bin"))
                    temporary.replace(target)
                    return target, detected, {**downloaded, "status": "downloaded_valid",
                        "sha256": checksum, "matches_historical_hash": checksum in expected if expected else None,
                        "requires_source_review": bool(expected and checksum not in expected), "attempts": attempts}
                except Exception as error:
                    attempts.append({"url": url, "code": getattr(error, "code", "DOWNLOAD_FAILED"), "error": str(error)})
                finally:
                    temporary.unlink(missing_ok=True)
            if refreshed:
                break
            queue, page_evidence = refreshed_urls(session, history, folder)
            attempts.extend(page_evidence)
            refreshed = True
        atomic(folder / "attempts.json", attempts)
    raise RecoveryError("SOURCE_RECOVERY_FAILED", "No valid public attachment recovered",
                        {"attempts_path": relative(folder / "attempts.json"), "attempts": attempts})


def make_document(entry, source, detected, blocks, run, recovery, *, member=None):
    source_path = entry["source_path"] + ("!/" + member if member else "")
    cleaned = [{**b, "text": clean_extracted_text(b.get("text", ""))} for b in blocks]
    cleaned = [b for b in cleaned if b["text"] and b.get("type") not in {"metadata", "metadata_fallback", "attachments", "title"}]
    chunks = chunk_blocks(cleaned, chunk_size=900, overlap=120)
    if not chunks:
        raise ValueError("No body text extracted; metadata is not counted as successful extraction")
    history = entry["download_history"]
    urls = {h.get("source_page_url") for h in history if h.get("source_page_url")}
    attachment_urls = {h.get("final_url") or h.get("url") for h in history if h.get("final_url") or h.get("url")}
    names = PurePosixPath(member or Path(entry["source_path"]).name)
    key = hashlib.sha256(entry["source_path"].encode()).hexdigest()
    target = run / "preprocessed" / entry["dataset"] / key / Path(*names.parts)
    target = target.with_name(target.name + ".json")
    provenance = {"source_dataset": (history[0].get("source_dataset") if history else None) or entry["dataset"],
        "source_kind": "archive_member" if member else "attachment", "doc_url": next(iter(urls)) if len(urls) == 1 else "",
        "source_page_url": next(iter(urls)) if len(urls) == 1 else "",
        "attachment_url": next(iter(attachment_urls)) if len(attachment_urls) == 1 else "",
        "source_attachment_path": entry["source_path"],
        "doc_title": history[0].get("doc_title") if len(urls) == 1 else "",
        "attachment_name": history[0].get("attachment_name") if history else Path(entry["source_path"]).name,
        "format_policy_version": POLICY, "actual_file_format": detected.kind, "actual_source_ext": detected.extension,
        "original_sha256": entry["original_sha256"], "processed_sha256": digest(source),
        "download_history_paths": sorted({h["history_path"] for h in history}),
        "format_repair_receipt": relative(run / "results.json"), "recovery_status": recovery["status"],
        "requires_source_review": recovery.get("requires_source_review", False),
        "ocr_review_required": detected.kind in IMAGE_KINDS}
    if member:
        provenance.update(archive_path=entry["source_path"], archive_member_path=member,
                          archive_member_ext=PurePosixPath(member).suffix, archive_member_sha256=digest(source))
    doc = {"slug": make_slug(source_path), "source_path": source_path, "source_file": names.name,
           "source_ext": PurePosixPath(member).suffix.lower() if member else entry["declared_ext"],
           "processed_at": datetime.now(timezone.utc).isoformat(), "num_blocks": len(cleaned),
           "num_chunks": len(chunks), "total_chars": sum(len(b["text"]) for b in cleaned),
           "used_metadata_fallback": False, "provenance": provenance, "blocks": cleaned, "chunks": chunks}
    atomic(target, doc)
    return {"source_path": source_path, "preprocessed_path": relative(target), "chunks": len(chunks), "chars": doc["total_chars"]}


def extract_entry(entry, source, detected, run, recovery):
    confirmed_attachment = (any(h.get("downloaded") for h in entry["download_history"])
                            or bool(entry["download_history"]) and recovery["status"] in {"downloaded_valid", "verified_cached_copy"})
    if detected.kind != "zip":
        blocks = extract_formatted_blocks(source, detected, attachment_image=confirmed_attachment)
        return [make_document(entry, source, detected, blocks, run, recovery)], []
    documents, failures = [], []
    with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory(prefix="attachment_repair_zip_") as temporary:
        members = [(info, decoded_archive_member_name(info).replace("\\", "/")) for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_ARCHIVE_MEMBERS or sum(i.file_size for i, _ in members) > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("ZIP exceeds extraction limits")
        validate_archive_member_targets([name for _, name in members])
        for info, name in members:
            try:
                if not is_safe_archive_member(name) or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError("Unsafe or oversized ZIP member")
                path = Path(temporary) / Path(*PurePosixPath(name).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                member_format = detect_file_format(path)
                blocks = extract_formatted_blocks(path, member_format, attachment_image=confirmed_attachment)
                documents.append(make_document(entry, path, member_format, blocks, run, recovery, member=name))
            except Exception as error:
                failures.append({"member": name, "error": f"{type(error).__name__}: {error}"})
    return documents, failures


def previous_results(run, plan):
    selected = {e["source_path"]: e["original_sha256"] for e in plan["selected"]}
    result = {}
    paths = sorted(run.glob("results_before_retry_*.json"))
    if (run / "results.json").exists():
        paths.append(run / "results.json")
    for path in paths:
        for item in read(path).get("results", []):
            source = item.get("source_path")
            if source in selected and item.get("original_sha256") == selected[source]:
                result[source] = item
    return result


def successful_result_is_current(result, entry, run):
    if (result.get("status") != "processed" or result.get("original_sha256") != entry["original_sha256"]
            or not result.get("documents")):
        return False
    try:
        source = inside(entry["source_path"])
        artifact = inside(result["processed_artifact"], run)
        if not source.is_file() or digest(source) != entry["original_sha256"]:
            return False
        if not artifact.is_file() or digest(artifact) != result.get("processed_sha256"):
            return False
        for declared in result["documents"]:
            document = read(inside(declared["preprocessed_path"], run))
            if (document.get("source_path") != declared.get("source_path")
                    or not has_searchable_body(document)
                    or len(document.get("chunks", [])) != declared.get("chunks")):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def process(run, network=False, retry_failed=False):
    plan = read(run / "plan.json")
    if plan.get("policy_version", POLICY) != POLICY:
        raise ValueError("This directory contains a different recovery job")
    receipt(run, status="processing")
    histories = {}
    for candidate in plan.get("verified_copies", []):
        histories.setdefault(candidate["path"], []).append({"expected_sha256": candidate["sha256"]})
    prior = {}
    if retry_failed:
        # A partial retry may not yet include successful files from the original
        # completed run. Reconcile that history before deciding what to repeat.
        prior = previous_results(run, plan)
    if retry_failed and (run / "results.json").exists():
        backup = run / ("results_before_retry_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
        shutil.copy2(run / "results.json", backup)
    results = []
    for entry in plan["selected"]:
        path = inside(entry["source_path"])
        old = prior.get(entry["source_path"], {})
        if successful_result_is_current(old, entry, run):
            results.append(old)
            continue
        result = {**entry, "status": "unrecoverable", "documents": []}
        try:
            if (digest(path) if path.is_file() else None) != entry["original_sha256"]:
                raise RecoveryError("INPUT_CHANGED", "Original attachment changed since scan; create a new plan")
            # Keep the immutable scan evidence, but use the current detector on
            # the same verified original bytes when a decoder policy improves.
            detected = detect_file_format(path) if retry_failed else FileFormat(**entry["detected"])
            parent_urls = {h.get("source_page_url") for h in entry["download_history"] if h.get("source_page_url")}
            recovery = {"status": "original_bytes", "requires_source_review":
                        "conflicting_download_hashes" in entry["reasons"] or len(parent_urls) > 1}
            if (detected.state in {"damaged", "missing", "unknown"} or detected.kind == "html"
                    or "download_hash_mismatch" in entry["reasons"]):
                path, detected, recovery = recover(entry, run, histories, network=network)
            else:
                # Snapshot before parsing so an independent downloader cannot
                # change the bytes between signature validation and extraction.
                snapshot = run / "artifacts" / hashlib.sha256(entry["source_path"].encode()).hexdigest() / path.name
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, snapshot)
                if digest(snapshot) != entry["original_sha256"]:
                    raise RecoveryError("INPUT_CHANGED", "Attachment changed while snapshotting; create a new plan")
                path = snapshot
            result.update(processed_artifact=relative(path), processed_sha256=digest(path), actual_format=detected.to_dict(), recovery=recovery)
            result["status"] = "body_extraction_failed"
            if detected.state == "protected":
                raise RecoveryError("PROTECTED_DOCUMENT", "Original is present but protected; no password/login bypass attempted")
            if detected.kind == "html":
                raise RecoveryError("HTML_RESPONSE", "Original download is HTML, possibly an error or login page")
            docs, failures, body_error = [], [], None
            try:
                docs, failures = extract_entry(entry, path, detected, run, recovery)
            except Exception as error:
                body_error = f"{type(error).__name__}: {error}"
            # A readable container can still contain corrupt document streams.
            # Try the public source once when body parsing fails, but do not
            # redownload a blank image or try to fix missing local dependencies.
            dependency_error = body_error and any(marker in body_error.lower() for marker in
                ("no module named", "not installed", "requires pywin32", "command not found", "import failed", "required",
                 "no enabled body extractor", "cannot extract unvalidated"))
            if ((body_error or failures or not docs) and network and entry["download_history"]
                    and detected.state == "valid" and detected.kind not in IMAGE_KINDS
                    and recovery["status"] == "original_bytes" and not dependency_error):
                result.update(documents=docs, original_body_error=body_error or failures or "No body text extracted")
                path, detected, recovery = recover(entry, run, histories, network=True, cached=False)
                result.update(processed_artifact=relative(path), processed_sha256=digest(path), actual_format=detected.to_dict(), recovery=recovery)
                docs, failures = extract_entry(entry, path, detected, run, recovery)
                body_error = None
            if body_error:
                raise ValueError(body_error)
            result.update(documents=docs, member_failures=failures)
            if docs and not failures:
                result["status"] = "processed"
            elif not docs:
                result["error"] = "No body text extracted"
        except RecoveryError as error:
            status = "body_extraction_failed" if result.get("original_body_error") or result["status"] == "body_extraction_failed" else "unrecoverable"
            result.update(status=status, reason=error.code, error=str(error), recovery_attempts=error.details)
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        results.append(result)
        summary = {status: sum(r["status"] == status for r in results) for status in STATUS_LABELS}
        atomic(run / "results.json", {"status": "running", "summary": summary, "results": results})
        event("attachment_result", source_path=entry["source_path"], status=STATUS_LABELS[result["status"]],
              documents=len(result["documents"]), reason=result.get("reason") or result.get("error"))
    summary = {status: sum(r["status"] == status for r in results) for status in STATUS_LABELS}
    atomic(run / "results.json", {"status": "complete", "summary": summary, "results": results, "history_issues": plan["history_issues"]})
    receipt(run, status="complete", summary=summary)
    event("complete", **{STATUS_LABELS[k]: v for k, v in summary.items()})


def main(argv=None):
    from scripts.recovery.cli import legacy_main
    return legacy_main("formats", argv)


if __name__ == "__main__":
    raise SystemExit(main())
