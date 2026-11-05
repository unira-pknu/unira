"""Conservative OCR grouping for infographic cards and adjacent prose."""
from __future__ import annotations

import io
import re
import subprocess
from difflib import SequenceMatcher

import numpy as np
from PIL import Image
from scipy import ndimage


VERSION = "2"
SCALE = 4
MIN_FILL = 0.75
MIN_BLOCK_COVERAGE = 0.80


def _intersection_area(first: list[int], second: list[int]) -> int:
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1]))


def detect_filled_regions(image: Image.Image) -> list[dict]:
    """Find large near-uniform light regions; never infer a box from OCR alone."""
    width, height = image.size
    reduced = image.convert("RGB").resize(
        ((width + SCALE - 1) // SCALE, (height + SCALE - 1) // SCALE),
        Image.Resampling.NEAREST,
    )
    rgb = np.asarray(reduced, dtype=np.int16)
    mean = rgb.mean(axis=2)
    spread = rgb.max(axis=2) - rgb.min(axis=2)
    # Separate printed light-gray cards from the white page. The fill is
    # allowed to have small holes where text and icons sit.
    mask = (mean >= 230) & (mean <= 248) & (spread <= 10)
    labels, _ = ndimage.label(mask)
    counts = np.bincount(labels.ravel())
    shapes = []
    for component, slices in enumerate(ndimage.find_objects(labels), 1):
        if slices is None or component >= len(counts):
            continue
        vertical, horizontal = slices
        local_width = horizontal.stop - horizontal.start
        local_height = vertical.stop - vertical.start
        if (local_width * SCALE < max(90, width * .035)
                or local_height * SCALE < max(50, height * .007)):
            continue
        fill = int(counts[component]) / (local_width * local_height)
        if fill < MIN_FILL:
            continue
        bbox = [horizontal.start * SCALE, vertical.start * SCALE,
                min(width, horizontal.stop * SCALE), min(height, vertical.stop * SCALE)]
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area > width * height * .70:
            continue  # A page-wide background is not one content region.
        color = np.median(rgb[labels == component], axis=0).astype(int).tolist()
        shapes.append({"bbox": bbox, "fill_ratio": round(fill, 3),
                       "background_rgb": color, "area": area})
    # Bound work on unusually complex images without preferring tiny noise.
    shapes = sorted(shapes, key=lambda item: item["area"], reverse=True)[:100]
    return sorted(shapes, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def _crop_ocr(image: Image.Image, bbox: list[int], *, executable: str,
              language: str, tessdata: str | None, psm: int = 6,
              inset_pixels: int = 10) -> tuple[str, str]:
    x1, y1, x2, y2 = bbox
    inset = min(inset_pixels, max(1, (x2 - x1) // 20), max(1, (y2 - y1) // 20))
    crop = image.crop((x1 + inset, y1 + inset, x2 - inset, y2 - inset))
    payload = io.BytesIO()
    crop.save(payload, format="PNG")
    command = [str(executable), "stdin", "stdout", "-l", language, "--psm", str(psm)]
    if tessdata:
        command += ["--tessdata-dir", str(tessdata)]
    raw = subprocess.run(command, input=payload.getvalue(), capture_output=True,
                         timeout=60, check=True).stdout.decode("utf-8-sig").strip()
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        # Discard isolated icon glyphs. Keep the raw crop output for review.
        if (len(re.findall(r"[\uac00-\ud7a3]", line)) >= 2
                or re.search(r"[A-Za-z]{3,}", line)
                or re.search(r"\d[\d,.]*\s*(?:원|%|명|학년)", line)):
            lines.append(line)
    return raw, "\n".join(lines)


def _unboxed_prose_regions(image: Image.Image, blocks: list[dict],
                           unassigned: list[dict], shapes: list[dict],
                           *, executable: str | None, language: str,
                           tessdata: str | None) -> tuple[list[dict], set[int]]:
    """Group a question heading and aligned prose on an unfilled background.

    This deliberately requires multiple left-aligned body lines and free space
    before any detected card. Other unboxed text remains unassigned for review.
    """
    available = {item["block_index"] for item in unassigned}
    regions = []
    consumed: set[int] = set()
    for heading_index in sorted(available, key=lambda i: blocks[i].get("bbox", [0, 0])[1]):
        heading = blocks[heading_index]
        box = heading.get("bbox")
        title = str(heading.get("text") or "").strip()
        if (heading_index in consumed or not isinstance(box, list) or len(box) != 4
                or not title.endswith("?") or len(re.findall(r"[가-힣]", title)) < 3
                or heading.get("confidence", 0) < 70):
            continue
        left, top, right, bottom = box
        heading_height = bottom - top
        column_right = min(image.width, left + max(right-left+120, image.width * .40))
        max_bottom = bottom + max(210, heading_height * 2.5)
        candidates = []
        for index in available - consumed - {heading_index}:
            block = blocks[index]
            other = block.get("bbox")
            if (not isinstance(other, list) or len(other) != 4
                    or block.get("confidence", 0) < 50
                    or len(re.findall(r"[가-힣]", str(block.get("text") or ""))) < 1):
                continue
            if (other[0] < left - 50 or other[2] > column_right
                    or other[1] < bottom - 10 or other[3] > max_bottom):
                continue
            candidates.append(index)
        # A prose paragraph has at least two rows beginning near the title's
        # left edge. A diagram label or a lone caption must not pass this test.
        aligned = sorted((blocks[i]["bbox"] for i in candidates
                          if abs(blocks[i]["bbox"][0] - left) <= 75),
                         key=lambda item: item[1])
        rows = []
        for candidate in aligned:
            center = (candidate[1] + candidate[3]) / 2
            if not rows or center - rows[-1] > 30:
                rows.append(center)
        if len(rows) < 2:
            continue
        indices = [heading_index] + sorted(candidates, key=lambda i: (blocks[i]["bbox"][1],
                                                                        blocks[i]["bbox"][0]))
        x1 = max(0, min(blocks[i]["bbox"][0] for i in indices) - 22)
        y1 = max(0, min(blocks[i]["bbox"][1] for i in indices) - 20)
        x2 = min(image.width, max(blocks[i]["bbox"][2] for i in indices) + 29)
        y2 = min(image.height, max(blocks[i]["bbox"][3] for i in indices) + 19)
        region_box = [x1, y1, x2, y2]
        if any(_intersection_area(region_box, shape["bbox"]) > 0 for shape in shapes):
            continue
        source = "\n".join(str(blocks[i].get("text") or "").strip() for i in indices)
        region = {"kind": "unboxed_prose", "bbox": region_box,
                  "block_indices": indices, "title": title,
                  "text": source, "source_block_text": source,
                  "text_source": "source_blocks",
                  "confidence": round(sum(blocks[i].get("confidence", 0) for i in indices)
                                      / len(indices), 3),
                  "status": "needs_review",
                  "warnings": ["UNBOXED_PROSE_BOUNDARY_INFERRED"]}
        if executable:
            try:
                raw, refined = _crop_ocr(image, region_box, executable=executable,
                                          language=language, tessdata=tessdata, psm=4,
                                          inset_pixels=0)
                region["crop_ocr_raw_text"] = raw
                lines = refined.splitlines()
                if len(lines) >= 3 and lines[0].endswith("?"):
                    first_y = min(blocks[i]["bbox"][1] for i in candidates)
                    first_row = [blocks[i]["bbox"] for i in candidates
                                 if blocks[i]["bbox"][1] - first_y <= 15]
                    if first_row:
                        line_box = [
                            max(0, min(box[0] for box in first_row) - 14),
                            max(0, min(box[1] for box in first_row) - 14),
                            min(image.width, max(box[2] for box in first_row) + 22),
                            min(image.height, max(box[3] for box in first_row) + 11),
                        ]
                        try:
                            line_raw, line_refined = _crop_ocr(
                                image, line_box, executable=executable,
                                language=language, tessdata=tessdata, psm=7,
                                inset_pixels=0)
                            region["first_line_ocr_raw_text"] = line_raw
                            first_line = line_refined.replace("\n", " ").strip()
                            plain = lambda value: re.sub(r"[^가-힣A-Za-z0-9]", "", value)
                            similarity = SequenceMatcher(
                                None, plain(first_line), plain(lines[1])).ratio()
                            if similarity >= .85:
                                lines[1] = first_line
                                region["first_line_replaced"] = True
                        except (OSError, subprocess.SubprocessError, ValueError):
                            region["warnings"].append("REGION_LINE_OCR_FAILED")
                    # Middle dots in Korean compounds are often read as a
                    # colon, hyphen, or the wide arae-a mark by Tesseract.
                    joined = " ".join(lines)
                    normalized = re.sub(r"(?<=[가-힣])[:ㆍ](?=[가-힣])", "·", joined)
                    normalized = re.sub(
                        r"([가-힣]+)-([가-힣]+)",
                        lambda match: (f"{match[1]}·{match[2]}"
                                       if any(f"{match[1]}{mark}{match[2]}" in source
                                              for mark in (":", "ㆍ", "·"))
                                       else match[0]),
                        normalized)
                    if normalized != joined:
                        region["warnings"].append("OCR_PUNCTUATION_NORMALIZED")
                    region["title"] = lines[0]
                    region["body"] = normalized[len(lines[0]):].strip()
                    region["text"] = normalized
                    region["text_source"] = ("region_crop_psm4+line_psm7"
                                             if region.get("first_line_replaced")
                                             else "region_crop_psm4")
                    region["source_blocks_confidence"] = region["confidence"]
                    region["confidence"] = None
                    if re.sub(r"\s+", "", normalized) != re.sub(r"\s+", "", source):
                        region["warnings"].append("REGION_OCR_DISAGREEMENT")
                else:
                    region["warnings"].append("REGION_CROP_OCR_UNVERIFIED")
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                region["warnings"].append("REGION_CROP_OCR_FAILED")
                region["crop_ocr_error"] = f"{type(exc).__name__}: {exc}"
        regions.append(region)
        consumed.update(indices)
    return regions, consumed


def group_ocr_regions(image: Image.Image, blocks: list[dict], *, executable: str | None = None,
                      language: str = "kor+eng", tessdata: str | None = None) -> dict:
    shapes = detect_filled_regions(image)
    members: list[list[int]] = [[] for _ in shapes]
    unassigned = []
    for block_index, block in enumerate(blocks):
        bbox = block.get("bbox")
        if (not isinstance(bbox, list) or len(bbox) != 4
                or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]):
            unassigned.append({"block_index": block_index, "reason": "INVALID_OCR_BBOX"})
            continue
        block_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        candidates = []
        touches_shape = False
        for shape_index, shape in enumerate(shapes):
            region = shape["bbox"]
            overlap = _intersection_area(bbox, region)
            if not overlap:
                continue
            touches_shape = True
            if (overlap / block_area >= MIN_BLOCK_COVERAGE
                    and region[0] <= center[0] <= region[2]
                    and region[1] <= center[1] <= region[3]):
                candidates.append((shape["area"], shape_index))
        if candidates:
            members[min(candidates)[1]].append(block_index)
        else:
            unassigned.append({"block_index": block_index,
                               "reason": "CROSSES_REGION_BOUNDARY" if touches_shape
                               else "NO_VERIFIED_REGION"})
    regions = []
    for shape, indices in zip(shapes, members):
        if not indices:
            continue
        indices.sort(key=lambda index: (blocks[index]["bbox"][1], blocks[index]["bbox"][0]))
        relevant = [blocks[index] for index in indices if str(blocks[index].get("text") or "").strip()]
        if not relevant:
            continue
        low_confidence = [index for index in indices if blocks[index].get("confidence", 100) < 40]
        region = {"id": f"r{len(regions) + 1}", "kind": "filled_shape",
                        "bbox": shape["bbox"], "background_rgb": shape["background_rgb"],
                        "fill_ratio": shape["fill_ratio"], "block_indices": indices,
                        "text": "\n".join(str(block["text"]).strip() for block in relevant),
                        "confidence": round(sum(block.get("confidence", 0) for block in relevant)
                                            / len(relevant), 3),
                        "warnings": ["OCR_LOW_CONFIDENCE_IN_REGION"] if low_confidence else [],
                        "low_confidence_block_indices": low_confidence,
                        "status": "needs_review"}
        if executable and len(regions) < 30:
            try:
                raw, refined = _crop_ocr(image, shape["bbox"], executable=executable,
                                          language=language, tessdata=tessdata)
                region["source_block_text"] = region["text"]
                region["crop_ocr_raw_text"] = raw
                if refined:
                    region["text"] = refined
                    region["text_source"] = "region_crop_psm6"
                    region["source_blocks_confidence"] = region["confidence"]
                    region["confidence"] = None  # Crop text has no measured TSV confidence.
                    if re.sub(r"\s+", "", refined) != re.sub(r"\s+", "", region["source_block_text"]):
                        region["warnings"].append("REGION_OCR_DISAGREEMENT")
                else:
                    region["text_source"] = "source_blocks"
                    region["warnings"].append("REGION_CROP_OCR_EMPTY")
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                region["text_source"] = "source_blocks"
                region["warnings"].append("REGION_CROP_OCR_FAILED")
                region["crop_ocr_error"] = f"{type(exc).__name__}: {exc}"
        elif executable:
            region["text_source"] = "source_blocks"
            region["warnings"].append("REGION_CROP_OCR_LIMIT_REACHED")
        else:
            region["text_source"] = "source_blocks"
        regions.append(region)
    prose_regions, prose_indices = _unboxed_prose_regions(
        image, blocks, unassigned, shapes, executable=executable,
        language=language, tessdata=tessdata)
    regions.extend(prose_regions)
    regions.sort(key=lambda region: (region["bbox"][1], region["bbox"][0]))
    for index, region in enumerate(regions, 1):
        region["id"] = f"r{index}"
    unassigned = [item for item in unassigned if item["block_index"] not in prose_indices]
    warnings = ["OCR_BLOCKS_OUTSIDE_VERIFIED_REGIONS"] if unassigned else []
    if prose_regions:
        warnings.append("UNBOXED_PROSE_BOUNDARY_INFERRED")
    if any("OCR_PUNCTUATION_NORMALIZED" in region["warnings"] for region in prose_regions):
        warnings.append("OCR_PUNCTUATION_NORMALIZED")
    disagreements = sum("REGION_OCR_DISAGREEMENT" in region["warnings"] for region in regions)
    if disagreements:
        warnings.append("REGION_OCR_DISAGREEMENT")
    return {"version": VERSION, "status": "needs_review", "regions": regions,
            "detected_shape_count": len(shapes),
            "unboxed_prose_count": len(prose_regions),
            "unassigned_blocks": unassigned,
            "crop_refined_count": sum(region["text_source"].startswith("region_crop_psm")
                                      for region in regions),
            "disagreement_count": disagreements, "warnings": warnings}
