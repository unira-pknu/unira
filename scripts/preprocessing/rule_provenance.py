"""Restore rule identities only from recorded source links and explicit version fields."""
from __future__ import annotations

from datetime import date
import hashlib
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup

POLICY = "rule-provenance-v1"
# Observed by following both recorded PKNU wrapper iframe links on 2026-11-08.
# Only this verified document/version pair has an explicit URL override.
VERIFIED_VERSION_URLS = {
    ("2094548", "2200000110025"): "https://www.law.go.kr/LSW/schlPubRulInfoP.do?chrClsCd=010202&schlPubRulSeq=2200000110025&urlMode=schlPubRulLsInfoP",
}


def official_rule_url(value):
    parsed = urlsplit(str(value or ""))
    return (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
            and parsed.hostname in {"www.pknu.ac.kr", "pknu.ac.kr", "www.law.go.kr", "law.go.kr"})


def exact_date(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
    except ValueError:
        return None


def snapshot_identity(html):
    soup = BeautifulSoup(html, "html.parser")
    def field(name):
        tag = soup.find("input", id=name)
        return str(tag.get("value") or "").strip() if tag else ""
    document_id = field("schlPubRulId") or field("lsId")
    version_id = field("schlPubRulSeq") or field("lsiSeq")
    if not document_id.isdigit() or not version_id.isdigit():
        return {}
    title = field("schlPubRulNm") or field("lsNm")
    heading = soup.select_one("#conTop h2") or soup.find("h2")
    if heading and re.sub(r"\s+", "", heading.get_text()) != re.sub(r"\s+", "", title):
        raise ValueError("Rule hidden title and visible heading disagree")
    effective = exact_date(field("efYd"))
    summary = soup.select_one("#conTop .subtit1") or soup.select_one(".subtit1")
    display = summary.get_text(" ", strip=True) if summary else ""
    match = re.search(r"\[시행\s*(\d{4})\s*[.-]\s*(\d{1,2})\s*[.-]\s*(\d{1,2})", display)
    if match:
        shown = date(*map(int, match.groups())).isoformat()
        if effective and effective != shown:
            raise ValueError("Rule hidden effective date and displayed date disagree")
        effective = effective or shown
    root = soup.select_one("#conScroll") or soup.select_one("#contentBody") or soup.body or soup
    text = re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()
    return {"rule_document_id": document_id, "rule_version_id": version_id,
            "rule_official_title": title, "effective_at": effective + "T00:00:00+09:00" if effective else None,
            "rule_effective_status": "confirmed" if effective else "unconfirmed",
            "rule_promulgation_no": field("prmlNo"), "rule_promulgated_date": exact_date(field("prmlYd")),
            "rule_body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "rule_effective_evidence": {"efYd": field("efYd"), "display": display},
            "rule_valid_until": None}


def duplicate_version(left, right):
    keys = ("rule_document_id", "rule_version_id", "effective_at", "rule_body_sha256")
    return all(left.get(k) and left.get(k) == right.get(k) for k in keys)


def restore_from_snapshot(provenance, html, path):
    identity = snapshot_identity(html)
    if not identity:
        return dict(provenance)
    result = {**provenance, **identity, "rule_provenance_policy": POLICY,
              "rule_identity_evidence_path": path}
    if provenance.get("doc_title") != identity["rule_official_title"]:
        result["rule_collected_title"] = provenance.get("doc_title")
    result["doc_title"] = identity["rule_official_title"]
    # An ID does not authorize inventing a URL. Keep the observed public URL.
    if not official_rule_url(result.get("doc_url")):
        result["doc_url"] = ""
    confirmed_url = VERIFIED_VERSION_URLS.get((identity["rule_document_id"], identity["rule_version_id"]))
    if confirmed_url and official_rule_url(provenance.get("doc_url")):
        result["rule_source_aliases"] = list(dict.fromkeys((provenance["doc_url"], confirmed_url)))
        result["doc_url"] = confirmed_url
        result["rule_url_evidence"] = "Recorded PKNU wrapper iframe links; checked 2026-11-08"
    return result


def attachment_identity(provenance, document):
    result = dict(provenance)
    text = "\n".join(str(b.get("text") or "") for b in document.get("blocks", []))
    revision_dates = re.findall(r"(?:전부개정|개정|부\s*칙\s*\()\s*(\d{4})[.\s]+(\d{1,2})[.\s]+(\d{1,2})", text)
    dates = []
    for parts in revision_dates:
        try:
            dates.append(date(*map(int, parts)).isoformat())
        except ValueError:
            continue
    filename_years = set(re.findall(r"20\d{2}", document.get("source_file", "")))
    title_years = set(re.findall(r"20\d{2}", result.get("doc_title") or ""))
    result.update(rule_provenance_policy=POLICY, rule_revision_dates=sorted(set(dates)),
                  rule_filename_date_conflict=bool(filename_years and title_years and filename_years != title_years),
                  rule_effective_status="unconfirmed", rule_valid_until=None)
    # "공포한 날부터" is a condition, not proof of an actual promulgation date.
    # Board publication/revision dates and filename dates cannot fill this gap.
    result["effective_at"] = None
    result["rule_effective_note"] = ("공포한 날부터 시행: 공포일 별도 확인 필요" if "공포한 날부터" in text
                                     else "시행일 원문 확인 필요")
    result["rule_document_id"] = str(provenance.get("source_id") or provenance.get("document_id") or "")
    return result


def version_state(identity, observations):
    if not all(identity.get(key) for key in ("rule_document_id", "rule_version_id", "effective_at")):
        return {"rule_version_state": "unconfirmed", "rule_valid_until": None}
    newer = [item for item in observations if item.get("rule_document_id") == identity.get("rule_document_id")
             and item.get("rule_version_id") != identity.get("rule_version_id")
             and item.get("effective_at") and identity.get("effective_at")
             and item["effective_at"] > identity["effective_at"]]
    if not newer:
        return {"rule_version_state": "latest_observed", "rule_valid_until": None}
    newest = max(newer, key=lambda item: item["effective_at"])
    return {"rule_version_state": "historical", "rule_newer_observed_version": newest["rule_version_id"],
            "rule_valid_until": None}
