"""Identify attachment bytes before selecting an extractor; never rename originals."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import codecs
import json
import shutil
import tempfile
import zipfile


@dataclass(frozen=True)
class FileFormat:
    kind: str
    extension: str | None
    state: str
    evidence: str

    def to_dict(self):
        return asdict(self)

    def matches(self, declared: str) -> bool:
        aliases = {".jpeg": ".jpg", ".jpe": ".jpg", ".tiff": ".tif", ".htm": ".html"}
        return aliases.get(declared.lower(), declared.lower()) == self.extension


def detect_file_format(path: Path) -> FileFormat:
    """Validate containers/images; a signature alone does not prove readable body text."""
    path = Path(path)
    with path.open("rb") as handle:
        head = handle.read(8192)
    if not head:
        return FileFormat("empty", None, "damaged", "Zero-byte file")
    if head.startswith(b"Handysoft Approval Document File"):
        return FileFormat("handysoft", None, "unverified", "Handysoft Approval Document File header; no supported body decoder")
    if head.startswith(b"%PDF-"):
        try:
            import fitz
            with fitz.open(path) as document:
                if document.needs_pass:
                    return FileFormat("pdf", ".pdf", "protected", "Encrypted PDF")
                if document.page_count < 1:
                    raise ValueError("PDF has no pages")
            return FileFormat("pdf", ".pdf", "valid", "PDF header and readable page tree")
        except ImportError as error:
            return FileFormat("pdf", ".pdf", "unverified", str(error))
        except Exception as error:
            return FileFormat("pdf", ".pdf", "damaged", str(error))
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        try:
            import olefile
            with olefile.OleFileIO(path, raise_defects=olefile.DEFECT_INCORRECT) as ole:
                streams = {"/".join(parts) for parts in ole.listdir()}
                if {"EncryptionInfo", "EncryptedPackage"}.issubset(streams):
                    return FileFormat("encrypted_office", None, "protected", "OLE EncryptionInfo and EncryptedPackage streams")
                if "FileHeader" in streams and ole.openstream("FileHeader").read(32).startswith(b"HWP Document File"):
                    return FileFormat("hwp", ".hwp", "valid", "OLE FileHeader: HWP Document File")
                for stream, kind, extension in (("WordDocument", "doc", ".doc"), ("Workbook", "xls", ".xls"),
                                               ("Book", "xls", ".xls"), ("PowerPoint Document", "ppt", ".ppt")):
                    if stream in streams:
                        return FileFormat(kind, extension, "valid", "OLE stream: " + stream)
            return FileFormat("ole", None, "unknown", "Valid OLE container without a supported document stream")
        except ImportError as error:
            return FileFormat("ole", None, "unverified", str(error))
        except Exception as error:
            return FileFormat("ole", None, "damaged", str(error))
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")) or path.suffix.lower() in {".zip", ".docx", ".xlsx", ".pptx", ".hwpx"}:
        # A mislabeled JPEG/HWP must still reach their signature checks below.
        if head.startswith(b"PK") or zipfile.is_zipfile(path):
            kind, extension = "zip", ".zip"
            try:
                with zipfile.ZipFile(path) as archive:
                    infos = archive.infolist()
                    names = {i.filename.lower() for i in infos}
                    for marker, candidate, ext in (("word/document.xml", "docx", ".docx"),
                                                   ("xl/workbook.xml", "xlsx", ".xlsx"),
                                                   ("ppt/presentation.xml", "pptx", ".pptx")):
                        if marker in names and "[content_types].xml" in names:
                            kind, extension = candidate, ext
                            break
                    if "mimetype" in names:
                        mime_info = next(i for i in infos if i.filename.lower() == "mimetype")
                        if mime_info.file_size <= 256 and archive.read(mime_info).strip() == b"application/hwp+zip":
                            kind, extension = "hwpx", ".hwpx"
                            if not any(n.startswith("contents/section") and n.endswith(".xml") for n in names):
                                raise ValueError("HWPX has no section XML")
                    if any(i.flag_bits & 1 for i in infos):
                        return FileFormat(kind, extension, "protected", "Encrypted ZIP member")
                    if len(infos) > 10000 or sum(i.file_size for i in infos) > 500 * 1024 * 1024:
                        return FileFormat(kind, extension, "unverified", "Container exceeds validation limits")
                    bad = archive.testzip()
                    if bad is not None:
                        raise ValueError("ZIP CRC mismatch: " + bad)
                return FileFormat(kind, extension, "valid", "ZIP directory/CRC and document member signatures")
            except Exception as error:
                return FileFormat(kind, extension, "damaged", str(error))
    image_signatures = ((b"\xff\xd8\xff", "jpeg", ".jpg"), (b"\x89PNG\r\n\x1a\n", "png", ".png"),
                        (b"GIF8", "gif", ".gif"), (b"BM", "bmp", ".bmp"),
                        (b"II*\x00", "tiff", ".tif"), (b"MM\x00*", "tiff", ".tif"))
    image = next(((kind, ext) for signature, kind, ext in image_signatures if head.startswith(signature)), None)
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        image = ("webp", ".webp")
    if image:
        kind, extension = image
        try:
            from PIL import Image
            with Image.open(path) as picture:
                picture.verify()
            with Image.open(path) as picture:
                picture.load()
            return FileFormat(kind, extension, "valid", "Image signature and decoder validation")
        except ImportError as error:
            return FileFormat(kind, extension, "unverified", str(error))
        except Exception as error:
            return FileFormat(kind, extension, "damaged", str(error))
    stripped = head.lstrip(b"\xef\xbb\xbf\t\r\n ").lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<script")):
        return FileFormat("html", ".html", "valid", "HTML document, possibly an error/login response")
    if b"hwpml" in stripped and stripped.startswith((b"<?xml", b"<hwpml")):
        return FileFormat("hwpml", ".hwp", "valid", "HWPML XML signature")
    if head.startswith(b"Rar!\x1a\x07"):
        return FileFormat("rar", ".rar", "unverified", "RAR signature; no integrity validator")
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return FileFormat("7z", ".7z", "unverified", "7z signature; no integrity validator")
    if path.suffix.lower() == ".json":
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
            return FileFormat("json", ".json", "valid", "Parsed JSON")
        except (ValueError, UnicodeError):
            return FileFormat("json", ".json", "damaged", "Invalid JSON")
    if path.suffix.lower() in {".txt", ".csv"}:
        try:
            codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=False)
            if b"\0" not in head:
                return FileFormat(path.suffix[1:].lower(), path.suffix.lower(), "valid", "UTF-8 text without binary NULs")
        except UnicodeError:
            pass
    return FileFormat("unknown", None, "unknown", "No recognized file signature; suffix alone cannot establish format or damage")


def extract_formatted_blocks(path: Path, detected: FileFormat, *, attachment_image=False) -> list[dict]:
    """Dispatch using detected bytes, keeping the original path and extension untouched."""
    if detected.state != "valid":
        raise ValueError(f"Cannot extract unvalidated {detected.kind}: {detected.evidence}")
    if detected.kind in {"jpeg", "png", "gif", "bmp", "tiff", "webp"}:
        if not attachment_image:
            raise ValueError("OCR requires confirmed attachment provenance; body images are excluded")
        from scripts.preprocessing.extractors.image_ocr import extract_image
        executable = shutil.which("tesseract")
        if not executable:
            raise RuntimeError("Tesseract is not installed or is not on PATH")
        return extract_image(path, executable=executable, language="kor+eng")["blocks"]
    from scripts.preprocessing.extractors.common import extract_blocks, SUPPORTED_EXTS
    if detected.extension not in SUPPORTED_EXTS or detected.kind == "html":
        raise ValueError(f"No enabled body extractor for actual format: {detected.kind}")
    if path.suffix.lower() == detected.extension:
        return extract_blocks(path)
    with tempfile.TemporaryDirectory(prefix="attachment_format_") as directory:
        copy = Path(directory) / ("document" + detected.extension)
        shutil.copy2(path, copy)
        return extract_blocks(copy)
