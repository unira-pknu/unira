"""Spreadsheet row/column boundaries must survive preprocessing and indexing."""
from __future__ import annotations

import json
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

from scripts.preprocessing.extractors.xlsx import SPREADSHEET_POLICY_VERSION, extract_xlsx_blocks
from scripts.preprocessing.file_preprocessing import chunk_blocks, ensure_output_path, is_preprocessed_current, save_preprocessed_file
from scripts.vectorization.vectorization import extract_chunk_records


def workbook(path: Path, rows: dict[int, dict[str, object]], merges=()):
    root = ET.Element("worksheet")
    data = ET.SubElement(root, "sheetData")
    for number, values in rows.items():
        row = ET.SubElement(data, "row", r=str(number))
        for col, value in values.items():
            if value is None:
                continue
            cell = ET.SubElement(row, "c", r=col + str(number), t="inlineStr")
            ET.SubElement(ET.SubElement(cell, "is"), "t").text = str(value)
    regions = ET.SubElement(root, "mergeCells")
    for ref in merges:
        ET.SubElement(regions, "mergeCell", ref=ref)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", ET.tostring(root, encoding="unicode"))
        z.writestr("xl/workbook.xml", '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="교내" r:id="r1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')


def course_workbook(path: Path, description=""):
    rows = {1: {"A": "학년도", "B": "학기", "C": "교과목명", "D": "학수번호", "E": "분반", "F": "담당교수", "H": "내용"},
            2: {"F": "소속학과", "G": "성명"},
            378: {"A": 2026, "B": 2, "C": "소프트웨어공학", "D": "102961", "E": "104", "F": None, "G": "유승호", "H": description},
            379: {"A": 2026, "B": 2, "C": "수리학및실험", "D": "102997", "E": "101", "F": "토목공학전공", "G": "오정은"}}
    workbook(path, rows, ("A1:A2", "B1:B2", "C1:C2", "D1:D2", "E1:E2", "F1:G1", "H1:H2"))


def test_merged_headers_sparse_cells_and_source_coordinates_are_preserved(tmp_path):
    path = tmp_path / "courses.xlsx"
    course_workbook(path)
    blocks = extract_xlsx_blocks(path)
    row = next(b for b in blocks if b["row"] == 378)
    table = row["structured_table"]
    assert table["header_rows"] == [1, 2] and table["sheet"] == "교내"
    assert table["columns"][6] == {"column": "G", "header": "담당교수 / 성명"}
    assert table["rows"][0]["cells"][5]["value"] == ""
    assert table["rows"][0]["cells"][6]["coordinate"] == "G378"
    assert table["rows"][0]["fields"]["staff_name"] == "유승호"
    assert next(b for b in blocks if b["row"] == 2)["structured_table"]["rows"][0]["cells"][0]["merged_from"] == "A1"


def test_neighboring_course_rows_never_share_a_chunk_even_with_overlap(tmp_path):
    path = tmp_path / "courses.xlsx"
    course_workbook(path)
    blocks = [b for b in extract_xlsx_blocks(path) if b["row"] >= 378]
    chunks = chunk_blocks(blocks, chunk_size=1200, overlap=200)
    assert len(chunks) == 2
    assert all(len(c["structured_table"]["rows"]) == 1 for c in chunks)
    assert not any("유승호" in c["text"] and "수리학및실험" in c["text"] for c in chunks)
    assert "원본 행: 378" in chunks[0]["text"] and "소프트웨어공학" in chunks[0]["text"]


def test_long_row_fragments_keep_the_same_course_instructor_and_cell_label(tmp_path):
    path = tmp_path / "courses.xlsx"
    description = "주차별 수업 내용과 상세 설명 " * 150
    course_workbook(path, description)
    block = next(b for b in extract_xlsx_blocks(path) if b["row"] == 378)
    chunks = chunk_blocks([block], chunk_size=400, overlap=200)
    assert len(chunks) > 1
    assert all("소프트웨어공학" in c["text"] and "유승호" in c["text"] and "원본 행: 378" in c["text"] for c in chunks)
    assert all(c["num_chars"] <= 400 for c in chunks)
    pieces = [line.split("내용[H]: ", 1)[1] for c in chunks for line in c["text"].splitlines() if line.startswith("내용[H]: ")]
    assert "".join(pieces) == description.strip()


def test_unknown_header_uses_column_letters_without_inventing_course_fields(tmp_path):
    path = tmp_path / "unknown.xlsx"
    workbook(path, {5: {"A": "alpha1", "C": "value3"}})
    table = extract_xlsx_blocks(path)[0]["structured_table"]
    assert table["header_status"] == "unverified"
    assert table["rows"][0]["cells"][1]["value"] == ""
    assert table["columns"][2]["header"] == "C"
    assert not table["rows"][0]["fields"]


def test_format_only_rows_and_uncached_formulas_keep_data_coordinates(tmp_path):
    path = tmp_path / "formatted.xlsx"
    workbook(path, {1: {"A": "교과목명", "B": "담당교수"}, 2: {"A": "소프트웨어공학", "B": "유승호"}})
    with zipfile.ZipFile(path) as z:
        members = {name: z.read(name) for name in z.namelist()}
    root = ET.fromstring(members["xl/worksheets/sheet1.xml"])
    data = root.find("sheetData")
    row = data.findall("row")[1]
    ET.SubElement(ET.SubElement(row, "c", r="D2"), "f").text = "SUM(C2:C3)"
    empty = ET.SubElement(data, "row", r="1048576")
    ET.SubElement(empty, "c", r="XFD1048576", s="1")
    members["xl/worksheets/sheet1.xml"] = ET.tostring(root, encoding="utf-8")
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    blocks = extract_xlsx_blocks(path)
    assert [b["row"] for b in blocks] == [1, 2]
    cells = blocks[1]["structured_table"]["rows"][0]["cells"]
    assert len(cells) == 4 and cells[2]["coordinate"] == "C2" and cells[2]["value"] == ""
    assert cells[3]["coordinate"] == "D2" and cells[3]["formula"] == "SUM(C2:C3)"
    assert cells[3]["value"] == ""


def test_repeated_headers_remain_headers_instead_of_course_records(tmp_path):
    path = tmp_path / "repeat.xlsx"
    workbook(path, {1: {"A": "교과목명", "B": "담당교수"}, 2: {"A": "소프트웨어공학", "B": "유승호"},
                    100: {"A": "교과목명", "B": "담당교수"}, 101: {"A": "엣지컴퓨팅", "B": "유승호"}})
    row = next(b for b in extract_xlsx_blocks(path) if b["row"] == 100)["structured_table"]["rows"][0]
    assert row["row_kind"] == "header" and not row["fields"]


def test_headers_are_not_indexed_as_facts_but_remain_in_data_metadata(tmp_path):
    path = tmp_path / "courses.xlsx"
    course_workbook(path)
    chunks = chunk_blocks(extract_xlsx_blocks(path))
    records = extract_chunk_records({"slug": "courses", "chunks": chunks,
        "provenance": {"source_dataset": "ce"}}, tmp_path / "courses.json", tmp_path)
    assert len(records) == 2
    assert all(r["metadata"]["structured_table"]["header_rows"] == [1, 2] for r in records)
    assert all(r["metadata"]["structured_table"]["rows"][0]["row_kind"] == "data" for r in records)


def test_data_merge_keeps_blank_raw_cell_and_explicit_master_reference(tmp_path):
    path = tmp_path / "merged.xlsx"
    workbook(path, {1: {"A": "교과목명", "B": "담당교수"}, 2: {"A": "소프트웨어공학", "B": "유승호"},
                    3: {"A": "엣지컴퓨팅"}}, ("B2:B3",))
    row = next(b for b in extract_xlsx_blocks(path) if b["row"] == 3)["structured_table"]["rows"][0]
    assert row["cells"][1]["value"] == "" and row["cells"][1]["merged_from"] == "B2"
    assert row["fields"]["staff_name"] == "유승호"


def test_row_fields_and_headers_reach_vector_and_ingestion_metadata(tmp_path, monkeypatch):
    from scripts.vectorization import load_to_supabase as loader
    path = tmp_path / "courses.xlsx"
    course_workbook(path)
    chunks = chunk_blocks([b for b in extract_xlsx_blocks(path) if b["row"] == 378])
    doc = {"slug": "courses", "source_file": path.name, "chunks": chunks,
           "provenance": {"source_dataset": "ce", "source_kind": "attachment", "spreadsheet_policy_version": SPREADSHEET_POLICY_VERSION}}
    preprocessed = tmp_path / "preprocessed/ce/courses.json"
    preprocessed.parent.mkdir(parents=True)
    preprocessed.write_text(json.dumps(doc), encoding="utf-8")
    records = extract_chunk_records(doc, preprocessed, tmp_path)
    assert records
    record = records[0]
    body = record["text"].split("본문: ", 1)[1]
    assert body == chunks[0]["text"]
    assert "\n교과목명[C]: 소프트웨어공학" in body
    assert "\n담당교수 / 성명[G]: 유승호" in body
    assert record["metadata"]["spreadsheet_policy_version"] == SPREADSHEET_POLICY_VERSION
    record["embedding"] = [0.1] * 384
    monkeypatch.setattr(loader, "PROJECT_ROOT", tmp_path)
    row = loader.prepare_row(record, loader.DEFAULT_EMBEDDING_MODEL, "ce")
    assert "structured_table" not in row["source"]["metadata"].obj
    table = row["chunk"]["metadata"].obj["structured_table"]
    assert table["rows"][0]["fields"]["course_name"] == "소프트웨어공학"
    assert table["columns"][6]["header"] == "담당교수 / 성명"


def test_previous_preprocessing_version_is_stale_and_new_output_is_current(tmp_path):
    source = tmp_path / "courses.xlsx"
    course_workbook(source)
    output = tmp_path / "processed"
    target = ensure_output_path(source, tmp_path, output, layout="by_ext")
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"provenance": {}}), encoding="utf-8")
    assert not is_preprocessed_current(source, tmp_path, output, tmp_path, {}, layout="by_ext")
    ok, _ = save_preprocessed_file(source, tmp_path, output, tmp_path, {}, 1200, 200, "off", "kor+eng", 200)
    assert ok
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result["provenance"]["spreadsheet_policy_version"] == SPREADSHEET_POLICY_VERSION
    assert is_preprocessed_current(source, tmp_path, output, tmp_path, {}, layout="by_ext")
