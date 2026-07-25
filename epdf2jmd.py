from __future__ import annotations

import hashlib
from collections import Counter
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox

import pymupdf
from deep_translator import GoogleTranslator


# ============================================================
# 設定
# ============================================================

SOURCE_LANGUAGE = "en"
TARGET_LANGUAGE = "ja"

# PDF文字抽出・OCR
MIN_DIRECT_TEXT_LENGTH = 80
OCR_DPI = 300
TESSDATA_PATH: str | None = None

# Google翻訳
TRANSLATION_CHUNK_SIZE = 3500
MAX_TRANSLATION_RETRIES = 3
TRANSLATION_INTERVAL_SECONDS = 0.5

# Codex CLI
CODEX_MODEL = "gpt-5.6-luna"
CODEX_REASONING_EFFORT = "low"
CODEX_TIMEOUT_SECONDS = 1800
SKIP_CODEX = False

# Codex出力の簡易検査
MIN_OUTPUT_LENGTH_RATIO = 0.65
INVALID_OUTPUT_PATTERNS = (
    "okay, let's",
    "let's tackle",
    "i need to",
    "the user wants",
    "first, i'll",
    "let me analyze",
    "let me think",
    "editing task",
    "作業方針:",
    "入力を確認します",
    "編集タスク:",
)

# 画像抽出
EXTRACT_IMAGES = True
MIN_IMAGE_WIDTH = 160
MIN_IMAGE_HEIGHT = 100
MIN_IMAGE_AREA = 30_000
IMAGE_RENDER_SCALE = 2.0

# 数式プレースホルダー
MATH_TOKEN_PREFIX = "ZXQMATH"
MATH_TOKEN_SUFFIX = "QXZ"


# ============================================================
# データ型
# ============================================================


@dataclass(frozen=True)
class ExtractedImage:
    page_number: int
    filename: str
    relative_obsidian_path: str
    y0: float


# ============================================================
# GUI
# ============================================================


def select_pdf_file() -> Path | None:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    selected = filedialog.askopenfilename(
        title="翻訳する英語PDFを選択してください",
        filetypes=[("PDFファイル", "*.pdf")],
    )

    root.destroy()
    return Path(selected) if selected else None


def show_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("エラー", message)
    root.destroy()


def show_success(paths: list[Path]) -> None:
    root = tk.Tk()
    root.withdraw()
    saved = "\n".join(str(path) for path in paths)
    messagebox.showinfo("処理完了", f"処理が完了しました。\n\n保存先:\n{saved}")
    root.destroy()


# ============================================================
# PDF文字抽出・OCR
# ============================================================


def normalize_text_block(text: str) -> str:
    """1つのPDFテキストブロックを整形する。"""
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # trans-\nlation のような行末ハイフンを結合する。
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    if not lines:
        return ""

    # 数式らしい短い行は改行を維持する。
    if all(is_math_like_line(line) for line in lines):
        return "\n".join(lines)

    return " ".join(lines).strip()


def extract_text_from_blocks(page: pymupdf.Page) -> str:
    """PDFブロック境界を残してテキストを直接抽出する。"""
    blocks = page.get_text("blocks")
    blocks = sorted(blocks, key=lambda block: (round(block[1], 1), round(block[0], 1)))

    parts: list[str] = []
    for block in blocks:
        block_type = int(block[6]) if len(block) > 6 else 0
        if block_type != 0:
            continue

        normalized = normalize_text_block(str(block[4]))
        if normalized:
            parts.append(normalized)

    return "\n\n".join(parts)


def extract_page_text(page: pymupdf.Page, page_number: int) -> tuple[str, str]:
    direct_text = extract_text_from_blocks(page)
    if len(direct_text) >= MIN_DIRECT_TEXT_LENGTH:
        return direct_text, "直接抽出"

    print(f"  ページ {page_number}: 文字情報が少ないためOCRを実行します。")

    try:
        text_page = page.get_textpage_ocr(
            language="eng",
            dpi=OCR_DPI,
            full=True,
            tessdata=TESSDATA_PATH,
        )
        ocr_text = page.get_text("text", textpage=text_page)
        ocr_text = "\n\n".join(
            part for part in (normalize_text_block(block) for block in re.split(r"\n\s*\n", ocr_text))
            if part
        )
        return (ocr_text, "OCR") if ocr_text else (direct_text, "抽出結果なし")
    except Exception as exc:
        raise RuntimeError(
            f"{page_number}ページ目のOCRに失敗しました。\n"
            "Tesseract OCRが正しくインストールされているか確認してください。\n\n"
            f"詳細:\n{exc}"
        ) from exc


def extract_pdf_text(pdf_path: Path) -> list[str]:
    page_texts: list[str] = []

    with pymupdf.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            print(f"[文字抽出] {index}/{document.page_count}ページ")
            text, method = extract_page_text(page, index)
            print(f"  -> {method}: {len(text)}文字")
            page_texts.append(text)

    return page_texts


# ============================================================
# 数式の退避・復元
# ============================================================


def math_token(index: int) -> str:
    return f"{MATH_TOKEN_PREFIX}{index:06d}{MATH_TOKEN_SUFFIX}"


def is_balanced_math_candidate(value: str) -> bool:
    """途中で切れた数式断片を退避しないための簡易検査。"""
    pairs = (("(", ")"), ("[", "]"), ("{", "}"))
    for opening, closing in pairs:
        if value.count(opening) != value.count(closing):
            return False

    if re.search(r"(?:https?://|www\.|doi\b|arxiv\b|\bid\s*=)", value, flags=re.IGNORECASE):
        return False
    if re.search(r"[=,+\-*/∪∩]\s*$", value):
        return False
    if re.match(r"^\s*[=,+*/∪∩]", value):
        return False

    if "=" in value:
        left, right = value.split("=", 1)
        if not left.strip() or len(right.strip()) < 2:
            return False

    return True


def is_math_like_line(line: str) -> bool:
    """短い行が、完結した数式らしいかを保守的に判定する。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 260 or not is_balanced_math_candidate(stripped):
        return False

    if re.search(r"[=<>≤≥≠∈∉⊂⊆⊃⊇∪∩∑∏√∞≈±×÷→←↔∅εαβγδθλμσφω]", stripped):
        words = re.findall(r"[A-Za-z]{4,}", stripped)
        math_marks = re.findall(r"[_^{}()\[\]|,+\-*/=<>≤≥≠∈⊂⊆∪∩]", stripped)
        return len(math_marks) >= 1 and len(words) <= 8

    if re.search(r"\\(?:frac|sum|prod|arg|min|max|mathrm|mathbb|left|right|cup|cap|subset|in|cdot|quad)", stripped):
        return True

    return False


def normalize_math_for_markdown(raw: str, display: bool) -> str:
    """元の数式内容を変更せずMarkdown数式区切りだけ統一する。"""
    value = raw.strip()

    wrappers = (
        (r"^\$\$(.*)\$\$$", True),
        (r"^\\\[(.*)\\\]$", True),
        (r"^\$(.*)\$$", False),
        (r"^\\\((.*)\\\)$", False),
    )
    for pattern, wrapped_display in wrappers:
        match = re.match(pattern, value, flags=re.DOTALL)
        if match:
            value = match.group(1).strip()
            display = wrapped_display
            break

    if display:
        return f"$$\n{value}\n$$"
    return f"${value}$"


def protect_math_expressions(text: str, math_store: dict[str, str]) -> str:
    """
    数式をプレースホルダーへ置換する。

    既存のLaTeX区切りを最優先で保護し、その後、独立した数式らしい
    ブロックと、等号・包含記号などを含む短い行内式を保護する。
    """

    def save(raw: str, display: bool) -> str:
        token = math_token(len(math_store) + 1)
        math_store[token] = normalize_math_for_markdown(raw, display)
        return token

    protected = text

    # 既に区切られている数式。
    patterns = (
        (r"\$\$(.+?)\$\$", True),
        (r"\\\[(.+?)\\\]", True),
        (r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", False),
        (r"\\\((.+?)\\\)", False),
    )

    for pattern, display in patterns:
        protected = re.sub(
            pattern,
            lambda match, d=display: save(match.group(0), d),
            protected,
            flags=re.DOTALL,
        )

    # ブロック単位の数式候補。
    blocks = re.split(r"(\n\s*\n)", protected)
    for index in range(0, len(blocks), 2):
        block = blocks[index].strip()
        if not block or MATH_TOKEN_PREFIX in block:
            continue

        block_lines = [line.strip() for line in block.splitlines() if line.strip()]
        if block_lines and all(is_math_like_line(line) for line in block_lines):
            blocks[index] = save("\n".join(block_lines), True)

    protected = "".join(blocks)

    # 行内の短い数式候補。英語文章全体を巻き込まないよう、長さを制限する。
    inline_pattern = re.compile(
        r"(?<![A-Za-z0-9_])"
        r"([A-Za-z][A-Za-z0-9_{}^\\]*"
        r"(?:\s*[=<>≤≥≠∈⊂⊆]\s*)"
        r"[A-Za-z0-9_{}^\\()\[\].,+\-*/| ∪∩]+?)"
        r"(?=[,;:。．、]|\s(?:and|or|where|is|are|was|were)\b|$)",
        flags=re.MULTILINE,
    )

    def replace_inline(match: re.Match[str]) -> str:
        candidate = match.group(1).strip()
        if not is_balanced_math_candidate(candidate):
            return match.group(0)
        return save(candidate, False)

    protected = inline_pattern.sub(replace_inline, protected)

    return protected


def protect_math_pages(page_texts: list[str]) -> tuple[list[str], dict[str, str]]:
    math_store: dict[str, str] = {}
    protected_pages = [protect_math_expressions(text, math_store) for text in page_texts]
    return protected_pages, math_store


def restore_math_placeholders(text: str, math_store: dict[str, str]) -> str:
    result = text
    for token, math_text in math_store.items():
        result = result.replace(token, math_text)
    return result


def validate_math_placeholders(text: str, math_store: dict[str, str]) -> tuple[bool, str]:
    missing = [token for token in math_store if text.count(token) != 1]
    if missing:
        preview = ", ".join(missing[:8])
        return False, f"数式プレースホルダーが欠落・重複しています: {preview}"
    return True, ""


# ============================================================
# PDF画像抽出（Obsidian向け）
# ============================================================


def extract_pdf_images(
    pdf_path: Path,
    assets_dir: Path,
) -> dict[int, list[ExtractedImage]]:
    """
    PDF内のラスター画像をPNGとして抽出する。

    Obsidianから参照しやすいよう、Markdownと同じ階層にassetsフォルダを作る。
    小さなロゴ・アイコン・マスク画像は寸法で除外する。
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

                # ページ上の表示領域をレンダリングすることで、マスク付き画像にも対応する。
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
                # 同じページに同一画像が複数回検出された場合を除外する。
                unique: dict[str, ExtractedImage] = {}
                for item in page_items:
                    unique[item.relative_obsidian_path] = item
                by_page[page_number] = sorted(unique.values(), key=lambda item: item.y0)

            print(f"[画像抽出] {page_number}/{document.page_count}ページ: {len(by_page.get(page_number, []))}枚")

    # 何も抽出されなかった場合は空フォルダを削除する。
    if not any(by_page.values()):
        try:
            assets_dir.rmdir()
        except OSError:
            pass

    return by_page


# ============================================================
# Google翻訳
# ============================================================


def split_long_text(text: str, max_length: int) -> list[str]:
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []

    while len(text) > max_length:
        area = text[:max_length]
        candidates = [
            area.rfind("\n\n"),
            area.rfind(". "),
            area.rfind("? "),
            area.rfind("! "),
            area.rfind("; "),
            area.rfind(", "),
            area.rfind(" "),
        ]
        split_at = max(candidates)
        split_at = max_length if split_at < max_length // 2 else split_at + 1

        chunk = text[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        text = text[split_at:].strip()

    if text:
        chunks.append(text)

    return chunks


def translate_chunk(
    translator: GoogleTranslator,
    chunk: str,
    number: int,
    total: int,
) -> str:
    for attempt in range(1, MAX_TRANSLATION_RETRIES + 1):
        try:
            print(f"[翻訳] {number}/{total} ({len(chunk)}文字)")
            translated = translator.translate(text=chunk)
            if not translated:
                raise RuntimeError("翻訳結果が空でした。")
            return translated
        except Exception as exc:
            if attempt >= MAX_TRANSLATION_RETRIES:
                raise RuntimeError(
                    f"翻訳チャンク {number} の処理に失敗しました。\n\n詳細:\n{exc}"
                ) from exc

            wait = 3 * attempt
            print(f"  翻訳に失敗したため、{wait}秒後に再試行します。")
            time.sleep(wait)

    raise RuntimeError("翻訳処理で予期しないエラーが発生しました。")


def translate_pages(page_texts: list[str]) -> list[str]:
    translator = GoogleTranslator(source=SOURCE_LANGUAGE, target=TARGET_LANGUAGE)

    page_chunks = [split_long_text(text, TRANSLATION_CHUNK_SIZE) for text in page_texts]
    total_chunks = sum(len(chunks) for chunks in page_chunks)

    translated_pages: list[str] = []
    current_number = 0

    for page_number, chunks in enumerate(page_chunks, start=1):
        translated_chunks: list[str] = []

        for chunk in chunks:
            current_number += 1
            translated_chunks.append(
                translate_chunk(translator, chunk, current_number, total_chunks)
            )
            time.sleep(TRANSLATION_INTERVAL_SECONDS)

        translated_page = "\n\n".join(translated_chunks)
        translated_pages.append(translated_page)
        print(f"  ページ {page_number} の翻訳が完了しました。")

    return translated_pages


# ============================================================
# Markdown生成
# ============================================================


def insert_images_near_captions(
    page_text: str,
    images: list[ExtractedImage],
) -> str:
    """画像を対応する図キャプションの直前へ挿入する。

    PDF抽出後は文章と画像の厳密な座標対応が失われるため、同じページ内の
    「図1」「Fig. 1」などのキャプションを上から順に探し、画像も上から順に
    対応させる。対応できない画像はページ末尾へ置く。
    """
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

    # 後ろから挿入してインデックスのずれを防ぐ。
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


def build_translation_markdown(
    pdf_link_line: str,
    translated_pages: list[str],
    images_by_page: dict[int, list[ExtractedImage]],
) -> str:
    """プレースホルダーを含むCodex入力用Markdownを作る。"""
    lines = [
        pdf_link_line,
        "",
        "# 機械翻訳",
        "",
        "> PDFから自動抽出またはOCRし、Google翻訳した未校正の中間結果である。",
        "> 数式は一時プレースホルダーとして保護されている。",
        "",
    ]

    for page_number, text in enumerate(translated_pages, start=1):
        page_content = text or "（翻訳できる文章がありませんでした）"
        page_content = insert_images_near_captions(
            page_content,
            images_by_page.get(page_number, []),
        )
        lines.extend([
            f"<!-- page: {page_number} -->",
            "",
            page_content,
            "",
        ])

    return "\n".join(lines).strip() + "\n"

def build_codex_prompt(input_markdown: str) -> str:
    return rf"""以下は、英語論文をPDFから抽出し、Google翻訳したMarkdownです。
日本語訳やMarkdown構造がかなり崩れているため、文脈を踏まえて読みやすい日本語の論文として整えてください。

最優先事項:
- 最後まで読める完成したMarkdownを必ず出力する。
- 元の章・節・内容・論旨・数値・引用番号をできる限り維持する。
- PDF抽出による改行、単語分断、ページをまたぐ文、見出し、箇条書き、表を自然に修正する。
- 不自然な機械翻訳は、前後の文脈から意味を推測して自然な学術日本語に直す。
- 数式が崩れている場合は、周辺の説明と数式断片から妥当なLaTeXを推測して修復してよい。
- 数式を修復できない場合も削除せず、最も妥当と思われる形で残す。
- 完璧な忠実性より、内容を欠落させず最後まで完成させることを優先する。

整形条件:
- 内容を要約しすぎたり、論文にない新しい主張や結論を追加したりしない。
- 明らかな重複ヘッダー、フッター、ページ番号、途中に混入した著者名や短縮タイトルは削除してよい。
- 見出し、箇条書き、表、図表キャプションをMarkdownとして読みやすく整える。
- 文体は原則として「である調」に統一する。
- 専門用語、固有名詞、引用番号、URL、数値、単位は可能な限り維持する。
- 1行目の原文PDFリンクは残す。
- `![[...]]` 形式のObsidian画像埋め込みは残し、対応する図キャプションの直前へ置く。
- `<!-- page: N -->` コメントは残しても削除してもよい。
- 思考過程、作業説明、前置き、後書きを出力しない。
- Markdown全体をコードフェンスで囲まない。
- 校正後のMarkdown本文だけを出力する。

入力Markdown:

{input_markdown}
"""


# ============================================================
# Codex CLI
# ============================================================


def check_codex_cli() -> None:
    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError(
            "Codex CLIが見つかりません。\n\n"
            "ターミナルで codex --version を確認してください。"
        )

    result = subprocess.run(
        [executable, "--version"],
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
    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError("Codex CLIが見つかりません。")

    prompt = build_codex_prompt(input_markdown)

    with tempfile.TemporaryDirectory(prefix="paper_codex_full_") as temp_name:
        temp_dir = Path(temp_name)
        output_path = temp_dir / "codex_result.md"

        command = [
            executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "--model",
            CODEX_MODEL,
            "-c",
            f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"',
            "--output-last-message",
            str(output_path),
            "-",
        ]

        print(f"  Markdown全文 {len(prompt):,}文字をCodexへ1回だけ送信します。")

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
            raise RuntimeError(
                f"Codex CLI処理に失敗しました。\n\n詳細:\n{details}"
            )

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


def remove_nonvisible_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()


def inspect_codex_output(source: str, result: str) -> list[str]:
    """Codex出力を採否判定せず、確認事項だけを返す。"""
    warnings: list[str] = []

    if not result.strip():
        return ["Codexの出力が空です。"]

    lowered = result.lower()
    for pattern in INVALID_OUTPUT_PATTERNS:
        if pattern.lower() in lowered:
            warnings.append(f"作業メモと思われる文字列を検出しました: {pattern}")
            break

    source_visible = remove_nonvisible_comments(source)
    result_visible = remove_nonvisible_comments(result)
    if source_visible:
        length_ratio = len(result_visible) / len(source_visible)
        if length_ratio < MIN_OUTPUT_LENGTH_RATIO:
            warnings.append(
                "出力が入力に比べて短めです。"
                f" 入力={len(source_visible):,}文字、出力={len(result_visible):,}文字、比率={length_ratio:.1%}"
            )

    source_images = re.findall(r"!\[\[[^\]]+\]\]", source)
    result_images = re.findall(r"!\[\[[^\]]+\]\]", result)
    if Counter(source_images) != Counter(result_images):
        warnings.append("画像リンクの個数または内容が変化しています。")

    pdf_link_match = re.match(r"^\s*(\[原文PDF\]\([^\n]+\))", source)
    if pdf_link_match and pdf_link_match.group(1) not in result:
        warnings.append("原文PDFリンクがCodex出力から欠落しています。")

    return warnings


def build_full_warning(path: Path, reason: str) -> None:
    lines = [
        "# Codex校正確認",
        "",
        "Codex出力は最終ファイルとして保存したが、次の確認事項がある。",
        "",
        f"- 理由: {reason}",
        "",
        "必要に応じて元PDFと照合すること。",
    ]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ============================================================
# 出力先・リンク
# ============================================================


def safe_folder_name(name: str, max_length: int = 72) -> str:
    """PDF名からファイルシステム用の短いフォルダ名を作る。"""
    cleaned = re.sub(r'[\/:*?"<>|]', '_', name).strip().strip('.')
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return (cleaned[:max_length].rstrip() or 'paper')


def create_output_layout(pdf_path: Path) -> tuple[Path, Path]:
    """分類/pdf/論文.pdf から 分類/papers/論文名/ を作る。"""
    pdf_dir = pdf_path.parent
    category_dir = pdf_dir.parent if pdf_dir.name.lower() == 'pdf' else pdf_dir
    papers_dir = category_dir / 'papers'
    papers_dir.mkdir(parents=True, exist_ok=True)

    base_name = safe_folder_name(pdf_path.stem)
    paper_dir = papers_dir / base_name

    # 同じPDFを再処理する場合は同じフォルダを更新する。
    paper_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = paper_dir / 'assets'
    return paper_dir, assets_dir


def build_pdf_link(pdf_path: Path, markdown_dir: Path) -> str:
    """最終Markdown先頭に置く原PDFへの相対Markdownリンク。"""
    relative = Path(os.path.relpath(pdf_path, markdown_dir)).as_posix()
    encoded = '/'.join(quote(part) for part in relative.split('/'))
    return f'[原文PDF]({encoded})'




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


def save_rejected_output(path: Path, candidate: str) -> Path:
    path.write_text(candidate.rstrip() + "\n", encoding="utf-8")
    return path


# ============================================================
# メイン処理
# ============================================================


def main() -> int:
    pdf_path = select_pdf_file()
    if pdf_path is None:
        print("PDFの選択がキャンセルされました。")
        return 0

    paper_dir, assets_dir = create_output_layout(pdf_path)
    original_path = paper_dir / "[en]原文抽出.txt"
    translation_path = paper_dir / "[mt]機械翻訳.md"
    final_path = paper_dir / "[ja]翻訳.md"
    codex_warning_path = paper_dir / "[warning]Codex全文確認.md"
    rejected_path = paper_dir / "[warning]Codex不採用出力.md"
    pdf_link_line = build_pdf_link(pdf_path, paper_dir)

    print("=" * 60)
    print(f"対象PDF: {pdf_path}")
    print(f"出力先: {paper_dir}")
    print("=" * 60)

    warning_paths: list[Path] = []

    try:
        if not SKIP_CODEX:
            print("\n1/6 Codex CLIを確認しています。")
            check_codex_cli()
        else:
            print("\n1/6 Codex CLI確認をスキップします。")

        print("\n2/6 PDFから文章を取得しています。")
        original_pages = extract_pdf_text(pdf_path)
        if not any(text.strip() for text in original_pages):
            raise RuntimeError("PDFから文章を取得できませんでした。")
        save_original_text(original_path, original_pages)

        print("\n3/6 数式を退避し、PDF画像を抽出しています。")
        protected_pages, math_store = protect_math_pages(original_pages)
        print(f"  退避した数式候補: {len(math_store)}個")

        images_by_page = extract_pdf_images(pdf_path, assets_dir)
        image_count = sum(len(items) for items in images_by_page.values())
        print(f"  抽出した画像: {image_count}枚")

        print("\n4/6 Google翻訳を実行しています。")
        translated_pages = translate_pages(protected_pages)

        protected_machine_markdown = build_translation_markdown(
            pdf_link_line=pdf_link_line,
            translated_pages=translated_pages,
            images_by_page=images_by_page,
        )
        readable_machine_markdown = restore_math_placeholders(
            protected_machine_markdown,
            math_store,
        )
        translation_path.write_text(
            readable_machine_markdown.rstrip() + "\n",
            encoding="utf-8",
        )
        print(f"  機械翻訳結果を保存しました: {translation_path}")

        # 前回実行時の警告ファイルが残っていると紛らわしいため削除する。
        for stale_path in (codex_warning_path, rejected_path):
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass

        if SKIP_CODEX:
            print("\n5/6 Codex処理は設定によりスキップされました。")
            final_text = readable_machine_markdown
        else:
            print("\n5/6 Codex CLIでMarkdown全文を一括校正しています。")
            print(f"  使用モデル: {CODEX_MODEL}")
            print(f"  推論量: {CODEX_REASONING_EFFORT}")
            print("  英語原文は送信しません。")
            print("  数式を含む機械翻訳全文を渡し、文脈からの修復も許可します。")
            print("  Codexが空出力にならない限り、校正版を最終結果として保存します。")

            try:
                candidate = run_codex_polish(readable_machine_markdown)
                if not candidate.strip():
                    raise RuntimeError("Codexの出力が空でした。")

                final_text = candidate
                notices = inspect_codex_output(readable_machine_markdown, candidate)
                if notices:
                    build_full_warning(codex_warning_path, " / ".join(notices))
                    warning_paths.append(codex_warning_path)
                    print("  [注意] 校正版は保存しますが、確認事項があります。")
                    for notice in notices:
                        print(f"    - {notice}")
                else:
                    print("  Codex校正版を最終結果として採用します。")
            except Exception as exc:
                # Codex自体が失敗した場合に限り、機械翻訳版へフォールバックする。
                final_text = readable_machine_markdown
                reason = str(exc)
                build_full_warning(codex_warning_path, f"Codex処理に失敗したため機械翻訳版を保存した: {reason}")
                warning_paths.append(codex_warning_path)
                print(f"  [警告] Codex処理に失敗したため機械翻訳版を使用します: {reason}")

        print("\n6/6 最終ファイルを保存しています。")
        final_path.write_text(final_text.rstrip() + "\n", encoding="utf-8")

    except Exception as exc:
        message = str(exc)
        print(f"\n[エラー]\n{message}", file=sys.stderr)
        show_error(message)
        return 1

    paths = [original_path, translation_path, final_path]
    if assets_dir.exists():
        paths.append(assets_dir)
    paths.extend(warning_paths)

    print("\n" + "=" * 60)
    print("処理が完了しました。")
    for path in paths:
        print(f"- {path}")
    print("=" * 60)

    show_success(paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())