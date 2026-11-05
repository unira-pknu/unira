from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.preprocessing.extractors.common import normalize_text


def _format_xls_value(cell: object, datemode: int) -> str:
    try:
        import xlrd  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"XLS parser import failed (xlrd required): {exc}") from exc

    cell_type = getattr(cell, "ctype", None)
    value = getattr(cell, "value", "")

    if cell_type in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return ""
    if cell_type == xlrd.XL_CELL_TEXT:
        return normalize_text(value)
    if cell_type == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if bool(value) else "FALSE"
    if cell_type == xlrd.XL_CELL_DATE:
        try:
            dt = xlrd.xldate.xldate_as_datetime(value, datemode)
        except Exception:
            return normalize_text(value)
        if dt.time().isoformat() == "00:00:00":
            return dt.date().isoformat()
        return dt.isoformat(sep=" ")
    if cell_type == xlrd.XL_CELL_ERROR:
        try:
            return xlrd.error_text_from_code.get(value, f"#ERR{value}")
        except Exception:
            return normalize_text(value)
    if cell_type == xlrd.XL_CELL_NUMBER:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return normalize_text(value)
    return normalize_text(value)


def extract_xls_blocks(path: Path) -> list[dict]:
    try:
        import xlrd  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"XLS parser import failed (xlrd required): {exc}") from exc

    workbook = xlrd.open_workbook(str(path), formatting_info=False)
    blocks: list[dict] = []
    for sheet in workbook.sheets():
        for row_idx in range(sheet.nrows):
            cells = []
            for col_idx in range(sheet.ncols):
                value = _format_xls_value(sheet.cell(row_idx, col_idx), workbook.datemode)
                if value:
                    cells.append(value)
            if not cells:
                continue
            blocks.append(
                {
                    "type": "table_row",
                    "style": "XLS",
                    "sheet": sheet.name,
                    "row": row_idx + 1,
                    "text": " | ".join(cells),
                }
            )
    return blocks


def _xlsx_text_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        parts = [node.text or "" for node in cell.findall(".//{*}t")]
        return normalize_text("".join(parts))

    value_node = cell.find("{*}v")
    if value_node is None or value_node.text is None:
        return ""

    value = value_node.text
    if cell_type == "s":
        try:
            return normalize_text(shared_strings[int(value)])
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return normalize_text(value)


def _load_xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        payload = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []

    root = ET.fromstring(payload)
    strings: list[str] = []
    for item in root.findall("{*}si"):
        parts = [node.text or "" for node in item.findall(".//{*}t")]
        strings.append(normalize_text("".join(parts)))
    return strings


def _load_xlsx_sheet_names(zf: zipfile.ZipFile) -> dict[str, str]:
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return {}

    rel_targets: dict[str, str] = {}
    for rel in rels.findall("{*}Relationship"):
        rel_id = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rel_id and target:
            rel_targets[rel_id] = target.replace("\\", "/").split("/")[-1]

    sheet_names: dict[str, str] = {}
    for sheet in workbook.findall(".//{*}sheet"):
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rel_targets.get(rel_id)
        if target:
            sheet_names[f"xl/worksheets/{target}"] = sheet.attrib.get("name", "") or target
    return sheet_names


def extract_xlsx_blocks(path: Path) -> list[dict]:
    blocks: list[dict] = []
    with zipfile.ZipFile(path) as zf:
        shared_strings = _load_xlsx_shared_strings(zf)
        sheet_names = _load_xlsx_sheet_names(zf)
        sheet_paths = sorted(name for name in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))

        for sheet_index, sheet_path in enumerate(sheet_paths, start=1):
            sheet_name = sheet_names.get(sheet_path, f"sheet{sheet_index}")
            root = ET.fromstring(zf.read(sheet_path))
            for row_idx, row in enumerate(root.findall(".//{*}sheetData/{*}row"), start=1):
                cells = []
                for cell in row.findall("{*}c"):
                    value = _xlsx_text_value(cell, shared_strings)
                    if value:
                        cells.append(value)
                if not cells:
                    continue
                blocks.append(
                    {
                        "type": "table_row",
                        "style": "XLSX",
                        "sheet": sheet_name,
                        "row": int(row.attrib.get("r", row_idx)),
                        "text": " | ".join(cells),
                    }
                )
    return blocks


__all__ = ["extract_xls_blocks", "extract_xlsx_blocks"]
