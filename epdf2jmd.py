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


def remove_math_candidate_markers(text: str) -> str:
    """数式候補コメントだけを削除し、囲まれた本文は残す。"""
    return MATH_CANDIDATE_MARKER_PATTERN.sub("", text).strip()


# ============================================================
# PDF文字抽出
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

    # 数式らしい短い行だけで構成されるブロックは改行を維持する。
    if all(is_math_like_line(line) for line in lines):
        return "\n".join(lines)

    return " ".join(lines).strip()


def extract_text_from_blocks(
    page: pymupdf.Page,
    page_number: int,
) -> tuple[str, str, list[MathCandidate]]:
    """
    PDFブロック境界を残して文字を抽出する。

    raw_text:
        [en]原文抽出.txtへ保存する、補助コメントなしの文字列。

    codex_text:
        数式候補コメントを付けたCodex入力用文字列。
    """
    blocks = page.get_text("blocks")
    blocks = sorted(
        blocks,
        key=lambda block: (round(block[1], 1), round(block[0], 1)),
    )

    raw_parts: list[str] = []
    codex_parts: list[str] = []
    candidates: list[MathCandidate] = []

    for block_index, block in enumerate(blocks, start=1):
        block_type = int(block[6]) if len(block) > 6 else 0
        if block_type != 0:
            continue

        normalized = normalize_text_block(str(block[4]))
        if not normalized:
            continue

        raw_parts.append(normalized)

        confidence, equation_number = classify_math_block(normalized)
        if confidence is None:
            codex_parts.append(normalized)
            continue

        candidate = MathCandidate(
            page_number=page_number,
            block_index=block_index,
            raw_text=normalized,
            confidence=confidence,
            equation_number=equation_number,
        )
        candidates.append(candidate)

        codex_parts.append(
            mark_math_candidate(
                text=normalized,
                page_number=page_number,
                block_index=block_index,
                confidence=confidence,
                equation_number=equation_number,
            )
        )

    return (
        "\n\n".join(raw_parts),
        "\n\n".join(codex_parts),
        candidates,
    )


def extract_pdf_text(
    pdf_path: Path,
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

    if not any(by_page.values()):
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
        "> Reading order, equations, headings, tables, and line breaks may be broken.",
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

        print("\n2/5 PDFから文章を取得しています。")
        original_pages, codex_pages, math_candidates = extract_pdf_text(pdf_path)

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

        print("\n3/5 PDF画像を抽出しています。")
        images_by_page = extract_pdf_images(pdf_path, assets_dir)
        image_count = sum(len(items) for items in images_by_page.values())
        print(f"  抽出した画像: {image_count}枚")

        source_markdown = build_source_markdown(
            pdf_link_line=pdf_link_line,
            original_pages=codex_pages,
            images_by_page=images_by_page,
        )

        if skip_codex:
            print("\n4/5 Codex処理は設定によりスキップされました。")
            final_text = remove_math_candidate_markers(source_markdown)
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

                final_text = remove_math_candidate_markers(candidate)

                math_warnings = inspect_math_output(math_candidates, final_text)
                if math_warnings:
                    print("  [数式確認]")
                    for warning in math_warnings:
                        print(f"    - {warning}")

                print("  Codex翻訳版を最終結果として採用します。")
            except Exception as exc:
                # 翻訳が失敗しても抽出内容を失わない。
                final_text = remove_math_candidate_markers(source_markdown)
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
