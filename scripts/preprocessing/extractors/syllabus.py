"""Syllabus PDF tables with cell identity, blank cells and merged geometry."""
from __future__ import annotations

from pathlib import Path
import re

from scripts.preprocessing.text_cleaning import clean_extracted_text

TABLE_POLICY_VERSION = "syllabus-tables-v1"


def render_row(cells: list, headers: list[str]) -> str:
    values = []
    for index, cell in enumerate(cells):
        value = "[병합 셀]" if cell is None else clean_extracted_text(cell).replace("\n", " / ") or "[빈 셀]"
        values.append(f"{headers[index] if index < len(headers) else f'열{index + 1}'}: {value}")
    return " | ".join(values)


def extract_syllabus_blocks(path: Path) -> list[dict]:
    import fitz

    blocks = []
    with fitz.open(path) as document:
        for page_index, page in enumerate(document):
            tables = page.find_tables().tables
            boxes = [fitz.Rect(table.bbox) for table in tables]
            for item in page.get_text("blocks", sort=True):
                rect = fitz.Rect(item[:4])
                # Keep text outside detected tables; never duplicate a flattened
                # copy of a table beside its structured rows.
                if any(max(0, (rect & box).width) * max(0, (rect & box).height) >= rect.width * rect.height * 0.5 for box in boxes):
                    continue
                text = clean_extracted_text(item[4])
                if text:
                    blocks.append({"type": "paragraph", "page": page_index + 1,
                                   "bbox": list(rect), "text": text})
            for table_index, table in enumerate(tables):
                grid = table.extract()
                if not grid:
                    continue
                width = max(len(row) for row in grid)
                headers = [f"열{i + 1}" for i in range(width)]
                # The first row is often a merged title. Use the actual header
                # row for assessment/weekly tables, without guessing spans.
                for row in grid[:4]:
                    joined = " ".join(str(cell or "") for cell in row)
                    if re.search(r"중간고사|기말고사|강의주제|상세\s*강의내용|Midterm|Final|Week", joined, re.I):
                        headers = [clean_extracted_text(cell) or f"열{i + 1}" for i, cell in enumerate(row)]
                        break
                title = " ".join(clean_extracted_text(cell) for cell in grid[0] if cell).strip()
                table_id = f"p{page_index + 1}-t{table_index + 1}"
                for row_index, cells in enumerate(grid):
                    row = {"type": "syllabus_table_row", "page": page_index + 1,
                           "table_id": table_id, "row_index": row_index,
                           "table_headers": headers, "table_cells": cells,
                           "bbox": list(table.rows[row_index].bbox),
                           "text": f"{title}\n{render_row(cells, headers)}"}
                    if row_index == 0:
                        row["table_grid"] = grid
                        row["cell_bboxes"] = [list(cell) if cell else None for cell in table.cells]
                    blocks.append(row)
    return blocks
