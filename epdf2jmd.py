from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import quote

import pymupdf


# ============================================================
# 設定
# ============================================================

# Codex CLI
CODEX_MODEL = "gpt-5.6-luna"
CODEX_REASONING_EFFORT = "low"
CODEX_TIMEOUT_SECONDS = 1800
SKIP_CODEX = False
PROMPT_FILENAME = "prompt.md"

# 画像抽出
EXTRACT_IMAGES = True
MIN_IMAGE_WIDTH = 160
MIN_IMAGE_HEIGHT = 100
MIN_IMAGE_AREA = 30_000
IMAGE_RENDER_SCALE = 2.0

# 表抽出
EXTRACT_TABLES = True
SAVE_TABLE_SOURCE_IMAGES = True
EMBED_STRUCTURED_TABLE_IMAGES = False
TABLE_RENDER_SCALE = 3.0
TABLE_MIN_ROWS = 2
TABLE_MIN_COLUMNS = 2
TABLE_CAPTION_TO_RULE_MAX_GAP = 80.0
TABLE_MAX_HEIGHT = 240.0
TABLE_RULE_MIN_WIDTH = 60.0
TABLE_RULE_MAX_GAP = 80.0

# 数式候補判定
MAX_MATH_CANDIDATE_LENGTH = 500
HIGH_MATH_CONFIDENCE_SCORE = 5
MEDIUM_MATH_CONFIDENCE_SCORE = 2

MATH_OPERATOR_PATTERN = re.compile(
    r"[=<>≤≥≠∈∉⊂⊆⊃⊇∪∩∑∏√∞≈±×÷→←↔∅]"
)
GREEK_OR_LATEX_PATTERN = re.compile(
    r"[Α-Ωα-ω]|"
    r"\\(?:frac|dfrac|tfrac|sum|prod|int|sqrt|argmin|argmax|"
    r"mathrm|mathbf|mathbb|mathcal|left|right|begin|end|"
    r"cup|cap|subset|in|cdot|times|theta|alpha|beta|gamma|lambda|sigma)"
)
EQUATION_NUMBER_PATTERN = re.compile(
    r"(?:\(|\[)(\d{1,3})(?:\)|\])\s*[.,;:]?\s*$"
)
REFERENCE_ENTRY_PATTERN = re.compile(r"^\s*\[\d+\]\s+")
MATH_CANDIDATE_MARKER_PATTERN = re.compile(
    r"<!--\s*/?math-candidate\b[^>]*-->\s*",
    flags=re.IGNORECASE,
)
TABLE_MARKER_PATTERN = re.compile(
    r"<!--\s*/?extracted-table\b[^>]*-->\s*",
    flags=re.IGNORECASE,
)
TABLE_NUMBER_PATTERN = re.compile(r"^(?:[IVXLCDM]+|\d+)[.:]?$", flags=re.IGNORECASE)


# ============================================================
# データ型
# ============================================================


@dataclass(frozen=True)
class ExtractedImage:
    page_number: int
    filename: str
    relative_obsidian_path: str
    y0: float


@dataclass(frozen=True)
class MathCandidate:
    page_number: int
    block_index: int
    raw_text: str
    confidence: str
    equation_number: str | None


@dataclass(frozen=True)
class ExtractedTable:
    page_number: int
    table_index: int
    caption: str
    data_bbox: tuple[float, float, float, float]
    markdown: str
    relative_obsidian_path: str
    structured: bool


@dataclass(frozen=True)
class PageElement:
    y0: float
    x0: float
    content: str


# ============================================================
# GUI
# ============================================================


def select_pdf_file() -> Path | None:
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()

        dialog_options = {
            "title": "翻訳する英語PDFを選択してください",
            "filetypes": [("PDFファイル", "*.pdf")],
        }
        if sys.platform != "darwin":
            dialog_options["parent"] = root

        selected = filedialog.askopenfilename(**dialog_options)
    finally:
        root.destroy()

    return Path(selected) if selected else None


def show_error(message: str) -> None:
    root = tk.Tk()
    try:
        root.withdraw()
        messagebox.showerror("エラー", message)
    finally:
        root.destroy()


def show_success(paths: list[Path]) -> None:
    root = tk.Tk()
    try:
        root.withdraw()
        saved = "\n".join(str(path) for path in paths)
        messagebox.showinfo("処理完了", f"処理が完了しました。\n\n保存先:\n{saved}")
    finally:
        root.destroy()


# ============================================================
# 数式候補の判定・マーカー
# ============================================================


def is_balanced_math_candidate(value: str) -> bool:
    """括弧の個数だけを確認する簡易検査。数学的な正しさは判定しない。"""
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    return all(value.count(opening) == value.count(closing) for opening, closing in pairs)


def is_math_like_line(line: str) -> bool:
    """改行を残した方がよい短い数式行かを保守的に判定する。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 260:
        return False

    if re.search(r"(?:https?://|www\.|doi\b|arxiv\b)", stripped, flags=re.IGNORECASE):
        return False

    if REFERENCE_ENTRY_PATTERN.match(stripped):
        return False

    operator_count = len(MATH_OPERATOR_PATTERN.findall(stripped))
    has_math_command = bool(GREEK_OR_LATEX_PATTERN.search(stripped))
    has_sub_or_superscript = bool(re.search(r"[_^]", stripped))
    long_words = re.findall(r"\b[A-Za-z]{5,}\b", stripped)

    if len(long_words) > 8:
        return False

    return operator_count >= 1 or has_math_command or has_sub_or_superscript


def classify_math_block(text: str) -> tuple[str | None, str | None]:
    """
    PDFテキストブロックの数式らしさを high / medium / None で返す。

    この判定は数式を置換・削除するためには使わず、Codexへの補助コメントを
    付けるためだけに使う。誤判定しても元の文字列は必ず残る。
    """
    value = text.strip()
    if not value:
        return None, None

    equation_match = EQUATION_NUMBER_PATTERN.search(value)
    equation_number = equation_match.group(1) if equation_match else None

    if len(value) > MAX_MATH_CANDIDATE_LENGTH:
        return None, equation_number

    if re.search(r"(?:https?://|www\.|doi\b|arxiv\b)", value, flags=re.IGNORECASE):
        return None, equation_number

    if REFERENCE_ENTRY_PATTERN.match(value):
        return None, equation_number

    score = 0

    if equation_number is not None:
        score += 3

    operator_count = len(MATH_OPERATOR_PATTERN.findall(value))
    if operator_count >= 2:
        score += 2
    elif operator_count == 1:
        score += 1

    if GREEK_OR_LATEX_PATTERN.search(value):
        score += 2

    if re.search(r"[_^]", value):
        score += 1

    if re.search(r"[A-Za-z]\s*[\(\[]|[\)\]]\s*[A-Za-z]", value):
        score += 1

    if len(value) <= 240:
        score += 1

    long_words = re.findall(r"\b[A-Za-z]{5,}\b", value)
    if len(long_words) >= 10:
        score -= 3
    elif len(long_words) >= 6:
        score -= 1

    # 括弧不一致は壊れた数式の可能性があるため除外せず、信頼度だけ下げる。
    if not is_balanced_math_candidate(value):
        score -= 1

    if score >= HIGH_MATH_CONFIDENCE_SCORE:
        return "high", equation_number

    if score >= MEDIUM_MATH_CONFIDENCE_SCORE:
        return "medium", equation_number

    return None, equation_number


def mark_math_candidate(
    text: str,
    page_number: int,
    block_index: int,
    confidence: str,
    equation_number: str | None,
) -> str:
    """元の文字列を残したまま、Codex向けの数式候補コメントで囲む。"""
    attributes = [
        f'page="{page_number}"',
        f'block="{block_index}"',
        f'confidence="{confidence}"',
    ]

    if equation_number is not None:
        attributes.append(f'equation-number="{equation_number}"')

    attribute_text = " ".join(attributes)
    return (
        f"<!-- math-candidate {attribute_text} -->\n"
        f"{text}\n"
        f"<!-- /math-candidate -->"
    )


def remove_extraction_markers(text: str) -> str:
    """補助コメントだけを削除し、囲まれた本文・表・画像は残す。"""
    result = MATH_CANDIDATE_MARKER_PATTERN.sub("", text)
    result = TABLE_MARKER_PATTERN.sub("", result)
    return result.strip()


# ============================================================
# PDF表抽出
# ============================================================


def rect_overlap_ratio(first: pymupdf.Rect, second: pymupdf.Rect) -> float:
    """firstの面積に対してsecondと重なる割合を返す。"""
    intersection = first & second
    if intersection.is_empty:
        return 0.0

    first_area = first.width * first.height
    if first_area <= 0:
        return 0.0

    return (intersection.width * intersection.height) / first_area


def clean_table_cell(value: object) -> str:
    """PDF表セルをMarkdown表へ安全に入れられる文字列へ整形する。"""
    if value is None:
        return ""

    cell = str(value).replace("\r\n", "\n").replace("\r", "\n")
    cell = re.sub(r"\s*\n\s*", " ", cell)
    cell = re.sub(r"[ \t]+", " ", cell).strip()
    return cell.replace("|", r"\|")


def normalize_table_rows(rows: list[list[object]]) -> list[list[str]]:
    """空行を除去し、全行の列数を揃える。"""
    cleaned = [
        [clean_table_cell(cell) for cell in row]
        for row in rows
        if row is not None
    ]
    cleaned = [row for row in cleaned if any(cell for cell in row)]
    if not cleaned:
        return []

    column_count = max(len(row) for row in cleaned)
    return [row + [""] * (column_count - len(row)) for row in cleaned]


def rows_to_markdown_table(rows: list[list[object]]) -> str:
    normalized = normalize_table_rows(rows)
    if not normalized:
        return ""

    column_count = len(normalized[0])
    header = normalized[0]
    separator = ["---"] * column_count

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines.extend(
        "| " + " | ".join(row) + " |"
        for row in normalized[1:]
    )
    return "\n".join(lines)


def is_valid_structured_table(rows: list[list[object]]) -> bool:
    normalized = normalize_table_rows(rows)
    if len(normalized) < TABLE_MIN_ROWS:
        return False

    column_count = len(normalized[0]) if normalized else 0
    if column_count < TABLE_MIN_COLUMNS:
        return False

    nonempty_count = sum(bool(cell) for row in normalized for cell in row)
    if nonempty_count < max(4, column_count + 1):
        return False

    return True


def group_page_words_into_lines(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, list[str]]]:
    """PyMuPDFの単語情報を元のテキスト行単位へまとめる。"""
    grouped: dict[tuple[int, int], list[tuple]] = {}
    for word in page.get_text("words", sort=True):
        key = (int(word[5]), int(word[6]))
        grouped.setdefault(key, []).append(word)

    result: list[tuple[pymupdf.Rect, list[str]]] = []
    for words in grouped.values():
        words = sorted(words, key=lambda item: float(item[0]))
        if not words:
            continue

        bbox = pymupdf.Rect(words[0][:4])
        for word in words[1:]:
            bbox |= pymupdf.Rect(word[:4])

        result.append((bbox, [str(word[4]) for word in words]))

    return sorted(result, key=lambda item: (item[0].y0, item[0].x0))


def find_table_captions(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, str]]:
    """TABLE I、TABLE 2、表1などの表番号行を座標付きで探す。"""
    captions: list[tuple[pymupdf.Rect, str]] = []

    for bbox, words in group_page_words_into_lines(page):
        if len(words) < 2:
            continue

        first = words[0].strip()
        second = words[1].strip()
        is_english_caption = first == "TABLE" and TABLE_NUMBER_PATTERN.match(second)
        is_japanese_caption = first.startswith("表") and (
            first != "表" or TABLE_NUMBER_PATTERN.match(second)
        )

        if is_english_caption or is_japanese_caption:
            captions.append((bbox, " ".join(words)))

    return captions


def find_horizontal_table_rules(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """罫線として使われていそうな長い水平線を抽出する。"""
    rules: list[pymupdf.Rect] = []

    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing["rect"])
        item_count = len(drawing.get("items", []))

        if (
            rect.width >= TABLE_RULE_MIN_WIDTH
            and rect.height <= 1.2
            and item_count <= 3
        ):
            rules.append(rect)

    return sorted(rules, key=lambda rect: (rect.y0, rect.x0))


def horizontal_overlap_ratio(first: pymupdf.Rect, second: pymupdf.Rect) -> float:
    overlap = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
    return overlap / max(1.0, min(first.width, second.width))


def find_raster_image_after_caption(
    page: pymupdf.Page,
    caption_bbox: pymupdf.Rect,
    y_limit: float,
) -> pymupdf.Rect | None:
    """表がラスター画像の場合、キャプション直下の大きな画像領域を探す。"""
    candidates: list[pymupdf.Rect] = []

    for info in page.get_image_info(xrefs=True):
        bbox = pymupdf.Rect(info.get("bbox", (0, 0, 0, 0)))
        if bbox.is_empty or bbox.is_infinite:
            continue
        if bbox.width < 100 or bbox.height < 30:
            continue
        if bbox.y0 < caption_bbox.y1 - 2 or bbox.y0 >= y_limit:
            continue
        if bbox.y0 - caption_bbox.y1 > TABLE_CAPTION_TO_RULE_MAX_GAP:
            continue
        candidates.append(bbox)

    if not candidates:
        return None

    return min(candidates, key=lambda rect: (rect.y0 - caption_bbox.y1, -rect.width))


def detect_caption_table_regions(
    page: pymupdf.Page,
) -> list[tuple[str, pymupdf.Rect]]:
    """
    表番号と罫線から表本体の領域を推定する。

    find_tables()がセル抽出できない、文字がベクター化された表でも、
    ページ領域のPNG保存に利用できる。
    """
    captions = find_table_captions(page)
    rules = find_horizontal_table_rules(page)
    regions: list[tuple[str, pymupdf.Rect]] = []

    for caption_index, (caption_bbox, caption_text) in enumerate(captions):
        next_caption_y = (
            captions[caption_index + 1][0].y0
            if caption_index + 1 < len(captions)
            else page.rect.height - 10
        )

        nearby_rules = [
            rule
            for rule in rules
            if caption_bbox.y1 - 1 <= rule.y0
            <= min(
                next_caption_y,
                caption_bbox.y1 + TABLE_CAPTION_TO_RULE_MAX_GAP,
            )
        ]

        if nearby_rules:
            def top_rule_score(rule: pymupdf.Rect) -> tuple[int, float, float]:
                caption_near_rule = int(
                    not (
                        rule.x0 - 20 <= caption_bbox.x0 <= rule.x1 + 20
                        or rule.x0 - 20 <= caption_bbox.x1 <= rule.x1 + 20
                    )
                )
                return (
                    caption_near_rule,
                    rule.y0 - caption_bbox.y1,
                    -rule.width,
                )

            top_rule = min(nearby_rules, key=top_rule_score)
            related_rules = [
                rule
                for rule in rules
                if top_rule.y0 - 1 <= rule.y0 < next_caption_y
                and rule.y0 <= top_rule.y0 + TABLE_MAX_HEIGHT
                and horizontal_overlap_ratio(rule, top_rule) >= 0.75
            ]
            related_rules.sort(key=lambda rule: rule.y0)

            continuous_rules: list[pymupdf.Rect] = []
            previous_y: float | None = None
            for rule in related_rules:
                if previous_y is not None and rule.y0 - previous_y > TABLE_RULE_MAX_GAP:
                    break
                continuous_rules.append(rule)
                previous_y = rule.y0

            if len(continuous_rules) >= 2:
                x0 = min(rule.x0 for rule in continuous_rules)
                x1 = max(rule.x1 for rule in continuous_rules)
                y0 = top_rule.y0
                y1 = max(rule.y1 for rule in continuous_rules)
                regions.append((caption_text, pymupdf.Rect(x0, y0, x1, y1)))
                continue

        raster_bbox = find_raster_image_after_caption(
            page,
            caption_bbox,
            next_caption_y,
        )
        if raster_bbox is not None:
            regions.append((caption_text, raster_bbox))

    return regions


def try_find_structured_table(
    page: pymupdf.Page,
    clip: pymupdf.Rect,
) -> tuple[list[list[object]], pymupdf.Rect] | None:
    """find_tables()を複数設定で試し、妥当なセル配列だけを採用する。"""
    strategies = (
        ("lines", "lines"),
        ("lines_strict", "lines_strict"),
        ("text", "lines"),
        ("text", "lines_strict"),
    )

    best: tuple[list[list[object]], pymupdf.Rect] | None = None
    best_nonempty_count = -1

    for vertical_strategy, horizontal_strategy in strategies:
        try:
            finder = page.find_tables(
                clip=clip,
                vertical_strategy=vertical_strategy,
                horizontal_strategy=horizontal_strategy,
                min_words_vertical=2,
                min_words_horizontal=1,
                snap_tolerance=4,
                join_tolerance=4,
                intersection_tolerance=5,
                text_tolerance=3,
            )
        except Exception:
            continue

        for table in finder.tables:
            rows = table.extract()
            if not is_valid_structured_table(rows):
                continue

            normalized = normalize_table_rows(rows)
            nonempty_count = sum(bool(cell) for row in normalized for cell in row)
            if nonempty_count <= best_nonempty_count:
                continue

            best_nonempty_count = nonempty_count
            best = (rows, pymupdf.Rect(table.bbox))

    return best


def render_table_source_image(
    page: pymupdf.Page,
    table_bbox: pymupdf.Rect,
    output_path: Path,
) -> None:
    """表のページ領域を見た目どおりPNGへレンダリングする。"""
    padding = 3.0
    clip = pymupdf.Rect(
        max(page.rect.x0, table_bbox.x0 - padding),
        max(page.rect.y0, table_bbox.y0 - padding),
        min(page.rect.x1, table_bbox.x1 + padding),
        min(page.rect.y1, table_bbox.y1 + padding),
    )

    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(TABLE_RENDER_SCALE, TABLE_RENDER_SCALE),
        clip=clip,
        alpha=False,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(pixmap.tobytes("png"))


def table_regions_overlap(first: pymupdf.Rect, second: pymupdf.Rect) -> bool:
    return (
        rect_overlap_ratio(first, second) >= 0.5
        or rect_overlap_ratio(second, first) >= 0.5
    )


def extract_tables_from_page(
    page: pymupdf.Page,
    page_number: int,
    tables_dir: Path,
) -> list[ExtractedTable]:
    """構造化表を優先し、失敗時は表領域の画像を必ず残す。"""
    if not EXTRACT_TABLES:
        return []

    regions = detect_caption_table_regions(page)
    extracted: list[ExtractedTable] = []

    for table_index, (caption, detected_bbox) in enumerate(regions, start=1):
        expanded_clip = pymupdf.Rect(
            max(page.rect.x0, detected_bbox.x0 - 5),
            max(page.rect.y0, detected_bbox.y0 - 5),
            min(page.rect.x1, detected_bbox.x1 + 5),
            min(page.rect.y1, detected_bbox.y1 + 5),
        )

        structured_result = try_find_structured_table(page, expanded_clip)
        if structured_result is not None:
            rows, structured_bbox = structured_result
            markdown = rows_to_markdown_table(rows)
            table_bbox = structured_bbox
            structured = True
        else:
            markdown = ""
            table_bbox = detected_bbox
            structured = False

        filename = f"page_{page_number:03d}_table_{table_index:02d}.png"
        relative_path = f"assets/tables/{filename}"
        output_path = tables_dir / filename

        if SAVE_TABLE_SOURCE_IMAGES or not structured:
            render_table_source_image(page, table_bbox, output_path)

        extracted.append(
            ExtractedTable(
                page_number=page_number,
                table_index=table_index,
                caption=caption,
                data_bbox=tuple(float(value) for value in table_bbox),
                markdown=markdown,
                relative_obsidian_path=relative_path,
                structured=structured,
            )
        )

    return extracted


def extract_pdf_tables(
    pdf_path: Path,
    assets_dir: Path,
) -> dict[int, list[ExtractedTable]]:
    """PDF全ページから表を抽出し、元表画像をassets/tablesへ保存する。"""
    by_page: dict[int, list[ExtractedTable]] = {}
    if not EXTRACT_TABLES:
        return by_page

    tables_dir = assets_dir / "tables"

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            tables = extract_tables_from_page(page, page_number, tables_dir)
            if tables:
                by_page[page_number] = tables

            structured_count = sum(table.structured for table in tables)
            fallback_count = len(tables) - structured_count
            print(
                f"[表抽出] {page_number}/{document.page_count}ページ: "
                f"{len(tables)}個 "
                f"(Markdown={structured_count}, 画像={fallback_count})"
            )

    if not any(by_page.values()):
        try:
            tables_dir.rmdir()
        except OSError:
            pass

    return by_page


def table_to_raw_content(table: ExtractedTable) -> str:
    if table.structured and table.markdown:
        return table.markdown
    return f"[TABLE IMAGE: {table.relative_obsidian_path}]"


def table_to_codex_content(table: ExtractedTable) -> str:
    structured_value = "true" if table.structured else "false"
    marker = (
        f'<!-- extracted-table page="{table.page_number}" '
        f'index="{table.table_index}" structured="{structured_value}" '
        f'source-image="{table.relative_obsidian_path}" -->'
    )

    contents: list[str] = [marker]

    if table.structured and table.markdown:
        contents.append(table.markdown)
        if EMBED_STRUCTURED_TABLE_IMAGES and table.relative_obsidian_path:
            contents.append(f"![[{table.relative_obsidian_path}]]")
    else:
        contents.append(f"![[{table.relative_obsidian_path}]]")

    contents.append("<!-- /extracted-table -->")
    return "\n".join(contents)


# ============================================================
# PDF文字抽出
# ============================================================


def normalize_text_block(text: str) -> str:
    """1つのPDFテキストブロックを整形する。"""
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    if not lines:
        return ""

    if all(is_math_like_line(line) for line in lines):
        return "\n".join(lines)

    return " ".join(lines).strip()


def extract_text_from_blocks(
    page: pymupdf.Page,
    page_number: int,
    tables: list[ExtractedTable],
) -> tuple[str, str, list[MathCandidate]]:
    """本文と表を座標順にまとめ、保存用・Codex用の2種類を返す。"""
    blocks = page.get_text("blocks")
    table_rects = [pymupdf.Rect(table.data_bbox) for table in tables]

    raw_elements: list[PageElement] = []
    codex_elements: list[PageElement] = []
    candidates: list[MathCandidate] = []

    for block_index, block in enumerate(blocks, start=1):
        block_type = int(block[6]) if len(block) > 6 else 0
        if block_type != 0:
            continue

        block_rect = pymupdf.Rect(block[0], block[1], block[2], block[3])
        if any(rect_overlap_ratio(block_rect, table_rect) >= 0.5 for table_rect in table_rects):
            continue

        normalized = normalize_text_block(str(block[4]))
        if not normalized:
            continue

        raw_elements.append(
            PageElement(
                y0=float(block_rect.y0),
                x0=float(block_rect.x0),
                content=normalized,
            )
        )

        confidence, equation_number = classify_math_block(normalized)
        if confidence is None:
            codex_content = normalized
        else:
            candidate = MathCandidate(
                page_number=page_number,
                block_index=block_index,
                raw_text=normalized,
                confidence=confidence,
                equation_number=equation_number,
            )
            candidates.append(candidate)
            codex_content = mark_math_candidate(
                text=normalized,
                page_number=page_number,
                block_index=block_index,
                confidence=confidence,
                equation_number=equation_number,
            )

        codex_elements.append(
            PageElement(
                y0=float(block_rect.y0),
                x0=float(block_rect.x0),
                content=codex_content,
            )
        )

    for table in tables:
        table_rect = pymupdf.Rect(table.data_bbox)
        raw_elements.append(
            PageElement(
                y0=float(table_rect.y0),
                x0=float(table_rect.x0),
                content=table_to_raw_content(table),
            )
        )
        codex_elements.append(
            PageElement(
                y0=float(table_rect.y0),
                x0=float(table_rect.x0),
                content=table_to_codex_content(table),
            )
        )

    raw_elements.sort(key=lambda element: (round(element.y0, 1), round(element.x0, 1)))
    codex_elements.sort(key=lambda element: (round(element.y0, 1), round(element.x0, 1)))

    return (
        "\n\n".join(element.content for element in raw_elements),
        "\n\n".join(element.content for element in codex_elements),
        candidates,
    )


def extract_pdf_text(
    pdf_path: Path,
    tables_by_page: dict[int, list[ExtractedTable]],
) -> tuple[list[str], list[str], list[MathCandidate]]:
    """保存用原文、Codex用原文、数式候補一覧をページ単位で取得する。"""
    raw_pages: list[str] = []
    codex_pages: list[str] = []
    all_candidates: list[MathCandidate] = []

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            print(f"[文字抽出] {page_number}/{document.page_count}ページ")

            raw_text, codex_text, candidates = extract_text_from_blocks(
                page,
                page_number,
                tables_by_page.get(page_number, []),
            )

            method = "直接抽出" if raw_text else "抽出結果なし"
            print(f"  -> {method}: {len(raw_text)}文字")
            print(f"  -> 数式候補: {len(candidates)}件")

            raw_pages.append(raw_text)
            codex_pages.append(codex_text)
            all_candidates.extend(candidates)

    return raw_pages, codex_pages, all_candidates


# ============================================================
# PDF画像抽出（Obsidian向け）
# ============================================================


def extract_pdf_images(
    pdf_path: Path,
    assets_dir: Path,
    tables_by_page: dict[int, list[ExtractedTable]],
) -> dict[int, list[ExtractedImage]]:
    """
    PDF内のラスター画像をPNGとして抽出する。

    表として抽出済みの領域と重なる画像は、図としての重複保存を避ける。
    """
    by_page: dict[int, list[ExtractedImage]] = {}
    if not EXTRACT_IMAGES:
        return by_page

    assets_dir.mkdir(parents=True, exist_ok=True)
    seen_hashes: dict[str, str] = {}

    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, start=1):
            page_items: list[ExtractedImage] = []
            image_infos = page.get_image_info(xrefs=True)
            table_rects = [
                pymupdf.Rect(table.data_bbox)
                for table in tables_by_page.get(page_number, [])
            ]

            for image_index, info in enumerate(image_infos, start=1):
                bbox = pymupdf.Rect(info.get("bbox", (0, 0, 0, 0)))
                width = int(info.get("width", 0) or 0)
                height = int(info.get("height", 0) or 0)

                if (
                    width < MIN_IMAGE_WIDTH
                    or height < MIN_IMAGE_HEIGHT
                    or width * height < MIN_IMAGE_AREA
                    or bbox.is_empty
                    or bbox.is_infinite
                ):
                    continue

                if any(
                    rect_overlap_ratio(bbox, table_rect) >= 0.6
                    for table_rect in table_rects
                ):
                    continue

                matrix = pymupdf.Matrix(IMAGE_RENDER_SCALE, IMAGE_RENDER_SCALE)
                pixmap = page.get_pixmap(matrix=matrix, clip=bbox, alpha=False)
                png_bytes = pixmap.tobytes("png")
                digest = hashlib.sha256(png_bytes).hexdigest()

                if digest in seen_hashes:
                    filename = seen_hashes[digest]
                else:
                    filename = f"page_{page_number:03d}_image_{image_index:02d}.png"
                    (assets_dir / filename).write_bytes(png_bytes)
                    seen_hashes[digest] = filename

                relative_path = f"assets/{filename}"
                page_items.append(
                    ExtractedImage(
                        page_number=page_number,
                        filename=filename,
                        relative_obsidian_path=relative_path,
                        y0=float(bbox.y0),
                    )
                )

            if page_items:
                unique: dict[str, ExtractedImage] = {}
                for item in page_items:
                    unique[item.relative_obsidian_path] = item
                by_page[page_number] = sorted(unique.values(), key=lambda item: item.y0)

            print(
                f"[画像抽出] {page_number}/{document.page_count}ページ: "
                f"{len(by_page.get(page_number, []))}枚"
            )

    if not any(by_page.values()) and not any(tables_by_page.values()):
        try:
            assets_dir.rmdir()
        except OSError:
            pass

    return by_page


# ============================================================
# Markdown生成
# ============================================================


def insert_images_near_captions(
    page_text: str,
    images: list[ExtractedImage],
) -> str:
    """同一ページの画像を図キャプション付近へ挿入する。"""
    if not images:
        return page_text

    paragraphs = re.split(r"(\n\s*\n)", page_text.strip())
    caption_indexes: list[int] = []

    caption_pattern = re.compile(
        r"^\s*(?:図|Fig\.?|Figure)\s*\d+[A-Za-z.．:：-]*",
        flags=re.IGNORECASE,
    )

    for index in range(0, len(paragraphs), 2):
        if caption_pattern.search(paragraphs[index]):
            caption_indexes.append(index)

    paired_count = min(len(images), len(caption_indexes))
    for pair_index in range(paired_count - 1, -1, -1):
        image = images[pair_index]
        caption_index = caption_indexes[pair_index]
        embed = f"![[{image.relative_obsidian_path}]]\n\n"
        paragraphs.insert(caption_index, embed)

    result = "".join(paragraphs).strip()

    if len(images) > paired_count:
        remaining = "\n\n".join(
            f"![[{image.relative_obsidian_path}]]"
            for image in images[paired_count:]
        )
        result = f"{result}\n\n{remaining}".strip()

    return result


def build_source_markdown(
    pdf_link_line: str,
    original_pages: list[str],
    images_by_page: dict[int, list[ExtractedImage]],
) -> str:
    """Codexへ渡す英語原文Markdownを作る。"""
    lines = [
        pdf_link_line,
        "",
        "# Extracted English Source",
        "",
        "> This text was automatically extracted from the PDF.",
        "> Reading order, equations, headings, and line breaks may be broken. Tables may be structured Markdown or source-image fallbacks.",
        "",
    ]

    for page_number, text in enumerate(original_pages, start=1):
        page_content = text or "(No extractable text was found on this page.)"
        page_content = insert_images_near_captions(
            page_content,
            images_by_page.get(page_number, []),
        )
        lines.extend(
            [
                f"<!-- page: {page_number} -->",
                "",
                page_content,
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


def get_prompt_path() -> Path:
    """実行中のPythonファイルと同じ階層にあるprompt.mdを返す。"""
    return Path(__file__).resolve().parent / PROMPT_FILENAME


def load_codex_prompt_template() -> str:
    """Codexへ渡す指示文をprompt.mdから読み込む。"""
    prompt_path = get_prompt_path()

    if not prompt_path.exists():
        raise RuntimeError(
            f"プロンプトファイルが見つかりません。\n\n"
            f"次の場所に {PROMPT_FILENAME} を置いてください。\n"
            f"{prompt_path}"
        )

    template = prompt_path.read_text(encoding="utf-8").strip()
    if not template:
        raise RuntimeError(f"{prompt_path} が空です。")

    return template


def build_codex_prompt(input_markdown: str) -> str:
    """prompt.mdの指示文と英語原文Markdownを結合する。"""
    template = load_codex_prompt_template()
    return f"{template}\n\n---\n\n# 翻訳対象の英語Markdown\n\n{input_markdown}"


# ============================================================
# Codex CLI
# ============================================================


def find_codex_cli() -> str:
    """PATHからCodex CLIを探す。Windowsではnpmのラッパーも対象にする。"""
    candidates = (
        ("codex.exe", "codex.cmd", "codex.bat", "codex")
        if os.name == "nt"
        else ("codex",)
    )

    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is not None:
            return executable

    raise RuntimeError(
        "Codex CLIが見つかりません。\n\n"
        "コマンドプロンプトまたはターミナルで codex --version を確認してください。"
    )


def codex_command(executable: str, *arguments: str) -> list[str]:
    """Codexの実行コマンドをOSに合わせて組み立てる。"""
    command = [executable, *arguments]

    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        command_line = subprocess.list2cmdline(command)
        command_prompt = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        return [command_prompt, "/d", "/s", "/c", command_line]

    return command


def check_codex_cli() -> None:
    executable = find_codex_cli()

    result = subprocess.run(
        codex_command(executable, "--version"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Codex CLIを起動できませんでした。\n\n詳細:\n{details}")

    print(f"  Codex CLI: {result.stdout.strip()}")


def run_codex_polish(input_markdown: str) -> str:
    executable = find_codex_cli()
    prompt = build_codex_prompt(input_markdown)

    with tempfile.TemporaryDirectory(prefix="paper_codex_full_") as temp_name:
        temp_dir = Path(temp_name)
        output_path = temp_dir / "codex_result.md"

        arguments = [
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--model",
            CODEX_MODEL,
            "-c",
            f"model_reasoning_effort={CODEX_REASONING_EFFORT}",
            "--output-last-message",
            str(output_path),
            "-",
        ]
        command = codex_command(executable, *arguments)

        print(f"  英語Markdown全文 {len(prompt):,}文字をCodexへ1回だけ送信します。")

        try:
            result = subprocess.run(
                command,
                input=prompt,
                cwd=temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CODEX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Codex処理が{CODEX_TIMEOUT_SECONDS}秒以内に完了しませんでした。"
            ) from exc

        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Codex CLI処理に失敗しました。\n\n詳細:\n{details}")

        if not output_path.exists():
            raise RuntimeError("Codex出力ファイルが作成されませんでした。")

        output = output_path.read_text(encoding="utf-8").strip()
        return clean_codex_output(output)


def clean_codex_output(text: str) -> str:
    text = text.strip()

    fence = re.fullmatch(
        r"```(?:markdown|md)?\s*(.*?)\s*```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence:
        text = fence.group(1).strip()

    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()

    return text


# ============================================================
# Codex出力の数式検査
# ============================================================


def extract_latex_math(text: str) -> list[str]:
    """Markdownから独立数式と行内数式の中身を抽出する。"""
    fragments: list[str] = []

    display_pattern = re.compile(r"\$\$(.+?)\$\$", flags=re.DOTALL)
    fragments.extend(match.group(1) for match in display_pattern.finditer(text))

    without_display = display_pattern.sub("", text)
    inline_pattern = re.compile(
        r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)",
        flags=re.DOTALL,
    )
    fragments.extend(match.group(1) for match in inline_pattern.finditer(without_display))

    return fragments


def inspect_latex_fragment(fragment: str) -> list[str]:
    """Obsidianでの表示を妨げそうな形式上の問題だけを検査する。"""
    warnings: list[str] = []

    for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
        if fragment.count(opening) != fragment.count(closing):
            warnings.append(f"{opening}{closing} の個数が一致しません")

    begin_environments = Counter(re.findall(r"\\begin\{([^}]+)\}", fragment))
    end_environments = Counter(re.findall(r"\\end\{([^}]+)\}", fragment))
    if begin_environments != end_environments:
        warnings.append(r"\begin と \end が対応していません")

    if fragment.count(r"\left") != fragment.count(r"\right"):
        warnings.append(r"\left と \right の個数が一致しません")

    return warnings


def inspect_math_output(
    source_candidates: list[MathCandidate],
    output_markdown: str,
) -> list[str]:
    """数式出力を検査する。警告があっても翻訳結果は不採用にしない。"""
    warnings: list[str] = []
    formulas = extract_latex_math(output_markdown)

    high_candidates = [
        candidate for candidate in source_candidates if candidate.confidence == "high"
    ]
    if high_candidates and not formulas:
        warnings.append(
            "高信頼度の数式候補が存在しましたが、出力にLaTeX数式が見つかりません。"
        )

    for index, formula in enumerate(formulas, start=1):
        for problem in inspect_latex_fragment(formula):
            warnings.append(f"出力数式{index}: {problem}")

    source_numbers = {
        candidate.equation_number
        for candidate in source_candidates
        if candidate.equation_number is not None
    }

    output_numbers = set(re.findall(r"\\tag\{(\d{1,3})\}", output_markdown))
    output_numbers.update(
        re.findall(r"(?m)^\s*\((\d{1,3})\)\s*$", output_markdown)
    )

    missing_numbers = sorted(
        source_numbers - output_numbers,
        key=lambda value: int(value),
    )
    if missing_numbers:
        preview = ", ".join(missing_numbers[:10])
        suffix = " ..." if len(missing_numbers) > 10 else ""
        warnings.append(
            f"原文候補にあった式番号が出力で確認できません: {preview}{suffix}"
        )

    return warnings


# ============================================================
# Codex出力の表検査
# ============================================================


def extract_markdown_tables(text: str) -> list[list[str]]:
    tables: list[list[str]] = []
    current: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            current.append(stripped)
            continue

        if len(current) >= 2 and re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", current[1]):
            tables.append(current)
        current = []

    if len(current) >= 2 and re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", current[1]):
        tables.append(current)

    return tables


def extract_numeric_tokens(text: str) -> Counter[str]:
    values = re.findall(
        r"(?<![A-Za-z])\d+(?:,\d{3})*(?:\.\d+)?(?:%|ms|MB|GB|kB|m²)?",
        text,
    )
    return Counter(values)


def inspect_table_output(
    tables_by_page: dict[int, list[ExtractedTable]],
    output_markdown: str,
) -> list[str]:
    """表の欠落を検査する。問題があっても翻訳結果は採用する。"""
    warnings: list[str] = []
    tables = [table for page_tables in tables_by_page.values() for table in page_tables]
    structured_tables = [table for table in tables if table.structured and table.markdown]
    image_tables = [table for table in tables if not table.structured]

    markdown_tables = extract_markdown_tables(output_markdown)
    if structured_tables and len(markdown_tables) < len(structured_tables):
        warnings.append(
            f"構造化表{len(structured_tables)}個に対し、出力のMarkdown表は"
            f"{len(markdown_tables)}個でした。"
        )

    missing_images = [
        table.relative_obsidian_path
        for table in image_tables
        if table.relative_obsidian_path not in output_markdown
    ]
    if missing_images:
        preview = ", ".join(missing_images[:5])
        suffix = " ..." if len(missing_images) > 5 else ""
        warnings.append(f"表の元画像リンクが出力から欠落しています: {preview}{suffix}")

    if structured_tables:
        source_numbers = Counter()
        for table in structured_tables:
            source_numbers.update(extract_numeric_tokens(table.markdown))

        output_numbers = extract_numeric_tokens(output_markdown)
        missing_numbers = list((source_numbers - output_numbers).elements())
        if missing_numbers:
            preview = ", ".join(missing_numbers[:15])
            suffix = " ..." if len(missing_numbers) > 15 else ""
            warnings.append(f"原表にある数値が出力で確認できません: {preview}{suffix}")

    return warnings


# ============================================================
# 出力先・リンク
# ============================================================


def safe_folder_name(name: str, max_length: int = 72) -> str:
    """PDF名からWindowsでも使用できる短いフォルダ名を作る。"""
    cleaned = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:max_length].rstrip(" .")

    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if cleaned.split(".", 1)[0].upper() in reserved_names:
        cleaned = f"_{cleaned}"

    return cleaned or "paper"


def create_output_layout(pdf_path: Path) -> tuple[Path, Path]:
    """分類/pdf/論文.pdf から 分類/papers/論文名/ を作る。"""
    pdf_dir = pdf_path.parent
    category_dir = pdf_dir.parent if pdf_dir.name.lower() == "pdf" else pdf_dir
    papers_dir = category_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    base_name = safe_folder_name(pdf_path.stem)
    paper_dir = papers_dir / base_name
    paper_dir.mkdir(parents=True, exist_ok=True)

    assets_dir = paper_dir / "assets"
    return paper_dir, assets_dir


def build_pdf_link(pdf_path: Path, markdown_dir: Path) -> str:
    """最終Markdown先頭に置く原PDFへの相対Markdownリンク。"""
    relative = Path(os.path.relpath(pdf_path, markdown_dir)).as_posix()
    encoded = "/".join(quote(part) for part in relative.split("/"))
    return f"[原文PDF]({encoded})"


# ============================================================
# 保存
# ============================================================


def save_original_text(path: Path, pages: list[str]) -> Path:
    content = "\n\n".join(
        f"===== PAGE {number} =====\n{text}"
        for number, text in enumerate(pages, start=1)
    )
    path.write_text(content, encoding="utf-8")
    return path


# ============================================================
# メイン処理
# ============================================================


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="英語PDFを抽出し、Codex CLIで日本語Markdownへ変換します。",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        type=Path,
        help="処理するPDF。省略するとファイル選択画面を表示します。",
    )
    parser.add_argument(
        "--skip-codex",
        action="store_true",
        help="Codexによる翻訳を行わず、抽出結果をMarkdownとして保存します。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    interactive = args.pdf is None
    pdf_path = select_pdf_file() if interactive else args.pdf.expanduser().resolve()

    if pdf_path is None:
        print("PDFの選択がキャンセルされました。")
        return 0

    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        message = f"PDFファイルが見つかりません。\n\n{pdf_path}"
        print(f"\n[エラー]\n{message}", file=sys.stderr)
        if interactive:
            show_error(message)
        return 1

    paper_dir, assets_dir = create_output_layout(pdf_path)
    original_path = paper_dir / "[en]原文抽出.txt"
    final_path = paper_dir / "[ja]翻訳.md"
    pdf_link_line = build_pdf_link(pdf_path, paper_dir)

    print("=" * 60)
    print(f"対象PDF: {pdf_path}")
    print(f"出力先: {paper_dir}")
    print("=" * 60)

    try:
        skip_codex = SKIP_CODEX or args.skip_codex

        if not skip_codex:
            print("\n1/5 Codex CLIを確認しています。")
            check_codex_cli()
        else:
            print("\n1/5 Codex CLI確認をスキップします。")

        print("\n2/5 PDFから表と文章を取得しています。")
        tables_by_page = extract_pdf_tables(pdf_path, assets_dir)
        original_pages, codex_pages, math_candidates = extract_pdf_text(
            pdf_path,
            tables_by_page,
        )

        if not any(text.strip() for text in original_pages):
            raise RuntimeError("PDFから文章を取得できませんでした。")

        save_original_text(original_path, original_pages)
        high_count = sum(
            candidate.confidence == "high" for candidate in math_candidates
        )
        medium_count = sum(
            candidate.confidence == "medium" for candidate in math_candidates
        )
        print(
            f"  数式候補合計: {len(math_candidates)}件 "
            f"(high={high_count}, medium={medium_count})"
        )

        table_count = sum(len(items) for items in tables_by_page.values())
        structured_table_count = sum(
            table.structured
            for items in tables_by_page.values()
            for table in items
        )
        print(
            f"  抽出した表: {table_count}個 "
            f"(Markdown={structured_table_count}, "
            f"画像={table_count - structured_table_count})"
        )

        print("\n3/5 PDF画像を抽出しています。")
        images_by_page = extract_pdf_images(
            pdf_path,
            assets_dir,
            tables_by_page,
        )
        image_count = sum(len(items) for items in images_by_page.values())
        print(f"  抽出した図・写真: {image_count}枚")

        source_markdown = build_source_markdown(
            pdf_link_line=pdf_link_line,
            original_pages=codex_pages,
            images_by_page=images_by_page,
        )

        if skip_codex:
            print("\n4/5 Codex処理は設定によりスキップされました。")
            final_text = remove_extraction_markers(source_markdown)
        else:
            print("\n4/5 Codex CLIで英語原文を日本語へ直接翻訳・整形しています。")
            print(f"  使用モデル: {CODEX_MODEL}")
            print(f"  プロンプト: {get_prompt_path()}")
            print(f"  推論量: {CODEX_REASONING_EFFORT}")
            print("  Google翻訳は使用しません。")
            print(
                "  抽出した英語全文を渡し、日本語翻訳・構造復元・数式整形を1回で行います。"
            )

            try:
                candidate = run_codex_polish(source_markdown)
                if not candidate.strip():
                    raise RuntimeError("Codexの出力が空でした。")

                final_text = remove_extraction_markers(candidate)

                math_warnings = inspect_math_output(math_candidates, final_text)
                if math_warnings:
                    print("  [数式確認]")
                    for warning in math_warnings:
                        print(f"    - {warning}")

                table_warnings = inspect_table_output(tables_by_page, final_text)
                if table_warnings:
                    print("  [表確認]")
                    for warning in table_warnings:
                        print(f"    - {warning}")

                print("  Codex翻訳版を最終結果として採用します。")
            except Exception as exc:
                # 翻訳が失敗しても抽出内容を失わない。
                final_text = remove_extraction_markers(source_markdown)
                print(
                    "  [警告] Codex処理に失敗したため英語抽出版を保存します: "
                    f"{exc}"
                )

        print("\n5/5 最終ファイルを保存しています。")
        final_path.write_text(final_text.rstrip() + "\n", encoding="utf-8")

    except Exception as exc:
        message = str(exc)
        print(f"\n[エラー]\n{message}", file=sys.stderr)
        if interactive:
            show_error(message)
        return 1

    paths = [original_path, final_path]
    if assets_dir.exists():
        paths.append(assets_dir)

    print("\n" + "=" * 60)
    print("処理が完了しました。")
    for path in paths:
        print(f"- {path}")
    print("=" * 60)

    if interactive:
        show_success(paths)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
