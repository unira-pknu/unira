from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.preprocessing.extractors.common import normalize_text

SPREADSHEET_POLICY_VERSION = "spreadsheet-rows-v1"
_HEADER_NAMES = {"학년도", "학기", "교과목명", "과목명", "강좌명", "학수번호", "분반", "담당교수", "교수명", "성명",
                 "이름", "연락처", "전화번호", "부서", "구분", "대상", "내용", "비고", "번호", "학번", "학점", "시간표",
                 "소속대학", "소속학과", "캠퍼스", "수업방식", "이수구분", "평가방법", "과정", "학위과정"}


def _column_name(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference)
    result = 0
    for letter in letters[0].upper() if letters else "A":
        result = result * 26 + ord(letter) - 64
    return result - 1


def _header_key(text: str) -> str:
    return re.sub(r"[\s()/·ㆍ_-]+", "", text)


def _spreadsheet_blocks(sheet: str, rows: dict[int, dict[int, dict]], merges: list[tuple[int, int, int, int]], style: str) -> list[dict]:
    """Keep physical column positions, empty cells and merged-cell origins."""
    width = max((col + 1 for cells in rows.values() for col, cell in cells.items() if cell.get("value") or cell.get("formula")), default=0)
    if not width:
        return []

    def cells_at(row: int) -> list[dict]:
        result = []
        row_merges = [region for region in merges if region[1] <= row <= region[3]]
        for col in range(width):
            cell = {"column": _column_name(col), "coordinate": f"{_column_name(col)}{row}", "value": rows[row].get(col, {}).get("value", "")}
            if rows[row].get(col, {}).get("formula"):
                cell["formula"] = rows[row][col]["formula"]
            for left, top, right, bottom in row_merges:
                if left <= col <= right and (top, left) != (row, col):
                    cell["merged_from"] = f"{_column_name(left)}{top}"
                    cell["effective_value"] = rows.get(top, {}).get(left, {}).get("value", "")
                    break
            result.append(cell)
        return result

    initial = {row: cells_at(row) for row in sorted(rows)[:30]}
    scores = {row: sum(_header_key(str(c.get("effective_value", c["value"]))) in _HEADER_NAMES for c in cells) for row, cells in initial.items()}
    best = max(scores, key=lambda row: scores[row]) if scores else None
    header_rows = []
    if best is not None and scores[best] >= 2:
        header_rows = [best]
        for row in range(max(1, best - 2), best + 4):
            if row not in initial or row == best:
                continue
            values = [str(c.get("effective_value", c["value"])) for c in initial[row] if c.get("effective_value", c["value"])]
            merged_header = any(top <= best <= bottom and top <= row <= bottom for _, top, _, bottom in merges)
            looks_header = scores[row] >= 2 and values and not any(re.fullmatch(r"\d+(?:\.\d+)?", v) for v in values)
            if merged_header or looks_header:
                header_rows.append(row)
        header_rows.sort()
    headers = []
    for col in range(width):
        labels = list(dict.fromkeys(str(initial[row][col].get("effective_value", initial[row][col]["value"])) for row in header_rows if initial[row][col].get("effective_value", initial[row][col]["value"])))
        headers.append(" / ".join(labels) or _column_name(col))

    blocks = []
    for row in sorted(rows):
        if not any(c.get("value") or c.get("formula") for c in rows[row].values()):
            continue
        cells = cells_at(row)
        columns = [{"column": _column_name(col), "header": headers[col]} for col in range(width)]
        fields = {}
        if row not in header_rows:
            for col, cell in enumerate(cells):
                header = _header_key(headers[col])
                value = str(cell.get("effective_value", cell["value"]))
                key = {"학년도": "year", "학기": "term", "교과목명": "course_name", "과목명": "course_name",
                       "강좌명": "course_name", "학수번호": "course_no", "분반": "section", "과정": "level", "학위과정": "level",
                       "담당교수": "staff_name", "교수명": "staff_name", "담당교원": "staff_name"}.get(header)
                if "담당교수" in header and header.endswith("성명"):
                    key = "staff_name"
                if header.startswith("개설") and header.endswith("학부과전공"):
                    key = "department"
                if key and value:
                    fields[key] = value
        table = {"kind": "spreadsheet", "policy_version": SPREADSHEET_POLICY_VERSION, "sheet": sheet,
                 "header_rows": header_rows, "header_status": "detected" if header_rows else "unverified",
                 "columns": columns, "rows": [{"source_row": row, "cells": cells, "fields": fields,
                                                "row_kind": "header" if row in header_rows else "data"}]}
        text = f"시트: {sheet} | 원본 행: {row}\n" + "\n".join(
            f"{headers[i]}[{c['column']}]: {c.get('effective_value', c['value'])}" for i, c in enumerate(cells) if c.get("effective_value", c["value"]) or c.get("formula"))
        blocks.append({"type": "table_row", "style": style, "sheet": sheet, "row": row, "text": text, "structured_table": table})
    return blocks


def chunk_spreadsheet_row(block: dict, chunk_size: int) -> list[dict]:
    """Split within one row only, repeating its identity on every fragment."""
    table = block["structured_table"]
    row = table["rows"][0]
    prefix = f"시트: {table['sheet']} | 원본 행: {row['source_row']}\n"
    identity = " | ".join(f"{key}: {value}" for key, value in row["fields"].items() if len(str(value)) <= max(80, chunk_size // 4))
    if identity:
        prefix += identity + "\n"
    # Very small requested chunks still need a usable row/column identity.
    budget = max(32, chunk_size - len(prefix))
    parts = []
    for column, cell in zip(table["columns"], row["cells"]):
        value = str(cell.get("effective_value", cell["value"]))
        if not value and not cell.get("formula"):
            continue
        label = f"{column['header']}[{column['column']}]: "
        if not value:
            value = "(수식의 저장된 계산 결과 없음)"
        step = max(1, budget - len(label))
        parts.extend(label + value[start:start + step] for start in range(0, len(value), step))
    groups, current = [], []
    for part in parts:
        if current and len("\n".join([*current, part])) > budget:
            groups.append(current)
            current = []
        current.append(part)
    if current:
        groups.append(current)
    return [{"text": prefix + "\n".join(group), "num_lines": (prefix + "\n".join(group)).count("\n") + 1,
             "num_chars": len(prefix + "\n".join(group)), "structured_table": {**table, "fragment": index, "fragments": len(groups)}}
            for index, group in enumerate(groups, 1)]


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

    workbook = xlrd.open_workbook(str(path), formatting_info=True)
    blocks: list[dict] = []
    for sheet in workbook.sheets():
        rows = {row + 1: {col: {"value": _format_xls_value(sheet.cell(row, col), workbook.datemode)} for col in range(sheet.ncols)} for row in range(sheet.nrows)}
        merges = [(left, top + 1, right - 1, bottom) for top, bottom, left, right in sheet.merged_cells]
        blocks.extend(_spreadsheet_blocks(sheet.name, rows, merges, "XLS"))
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
            rows = {}
            for row_idx, row in enumerate(root.findall(".//{*}sheetData/{*}row"), start=1):
                cells = {}
                for col_idx, cell in enumerate(row.findall("{*}c")):
                    value = _xlsx_text_value(cell, shared_strings)
                    column = _column_index(cell.attrib["r"]) if cell.attrib.get("r") else col_idx
                    cells[column] = {"value": value}
                    formula = cell.find("{*}f")
                    if formula is not None:
                        cells[column]["formula"] = formula.text or ""
                rows[int(row.attrib.get("r", row_idx))] = cells
            merges = []
            for region in root.findall(".//{*}mergeCell"):
                bounds = region.attrib["ref"].split(":")
                start, end = bounds[0], bounds[-1]
                merges.append((_column_index(start), int(re.search(r"\d+", start)[0]), _column_index(end), int(re.search(r"\d+", end)[0])))
            blocks.extend(_spreadsheet_blocks(sheet_name, rows, merges, "XLSX"))
    return blocks


__all__ = ["extract_xls_blocks", "extract_xlsx_blocks", "chunk_spreadsheet_row", "SPREADSHEET_POLICY_VERSION"]
