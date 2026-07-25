from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import pymupdf
from deep_translator import GoogleTranslator


# ============================================================
# 設定
# ============================================================

SOURCE_LANGUAGE = "en"
TARGET_LANGUAGE = "ja"


# ------------------------------------------------------------
# PDF文字抽出・OCR
# ------------------------------------------------------------

# PDFから直接抽出できた文字数がこれ未満の場合、OCRを使用する。
MIN_DIRECT_TEXT_LENGTH = 80

# OCR解像度
OCR_DPI = 300

# 通常はNoneでよい。
# Tesseractの言語データが見つからない場合のみ設定する。
TESSDATA_PATH: str | None = None


# ------------------------------------------------------------
# Google翻訳
# ------------------------------------------------------------

# Google翻訳に一度に渡す最大文字数
TRANSLATION_CHUNK_SIZE = 3500

# 翻訳失敗時の再試行回数
MAX_TRANSLATION_RETRIES = 3

# 翻訳リクエスト間の待機時間
TRANSLATION_INTERVAL_SECONDS = 0.5


# ------------------------------------------------------------
# Codex CLI
# ------------------------------------------------------------

# Codex CLIのモデル選択画面に表示されたモデル名を指定する。
CODEX_MODEL = "gpt-5.6-luna"

# 日本語校正では通常lowで十分。
CODEX_REASONING_EFFORT = "low"

# Codex処理の最大待機時間（秒）
CODEX_TIMEOUT_SECONDS = 1800

# Trueにすると、Codex校正をせず機械翻訳Markdownまでで終了する。
SKIP_CODEX = False


# ------------------------------------------------------------
# Codex出力の簡易検査
# ------------------------------------------------------------

# Codex出力が機械翻訳結果の40%未満になった場合は、
# 要約や途中切れの可能性があるとして警告する。
MIN_OUTPUT_LENGTH_RATIO = 0.40

# Codexの作業メモとして疑わしい文字列
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


# ============================================================
# GUI
# ============================================================


def select_pdf_file() -> Path | None:
    """翻訳するPDFを選択する。"""

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
    """エラーダイアログを表示する。"""

    root = tk.Tk()
    root.withdraw()

    messagebox.showerror(
        "エラー",
        message,
    )

    root.destroy()


def show_success(paths: list[Path]) -> None:
    """処理完了ダイアログを表示する。"""

    root = tk.Tk()
    root.withdraw()

    saved_paths = "\n".join(str(path) for path in paths)

    messagebox.showinfo(
        "処理完了",
        f"処理が完了しました。\n\n保存先:\n{saved_paths}",
    )

    root.destroy()


# ============================================================
# PDF文字抽出・OCR
# ============================================================


def normalize_extracted_text(text: str) -> str:
    """
    PDFまたはOCRから取得した文章を整形する。

    行末ハイフンを結合し、PDF上の単なる折り返しを
    段落内の空白に変換する。
    """

    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 例:
    # trans-
    # lation
    #
    # を translation にする。
    text = re.sub(
        r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])",
        "",
        text,
    )

    paragraphs: list[str] = []

    for block in re.split(r"\n\s*\n", text):
        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip()
        ]

        if not lines:
            continue

        paragraph = re.sub(
            r"[ \t]+",
            " ",
            " ".join(lines),
        ).strip()

        if paragraph:
            paragraphs.append(paragraph)

    return "\n\n".join(paragraphs)


def extract_page_text(
    page: pymupdf.Page,
    page_number: int,
) -> tuple[str, str]:
    """
    1ページ分の文章を取得する。

    十分な埋め込み文字が存在する場合は直接抽出し、
    文字が少ない場合だけOCRを使用する。
    """

    direct_text = normalize_extracted_text(
        page.get_text("text")
    )

    if len(direct_text) >= MIN_DIRECT_TEXT_LENGTH:
        return direct_text, "直接抽出"

    print(
        f"  ページ {page_number}: "
        "文字情報が少ないためOCRを実行します。"
    )

    try:
        text_page = page.get_textpage_ocr(
            language="eng",
            dpi=OCR_DPI,
            full=True,
            tessdata=TESSDATA_PATH,
        )

        ocr_text = normalize_extracted_text(
            page.get_text(
                "text",
                textpage=text_page,
            )
        )

        if ocr_text:
            return ocr_text, "OCR"

        return direct_text, "抽出結果なし"

    except Exception as exc:
        raise RuntimeError(
            f"{page_number}ページ目のOCRに失敗しました。\n"
            "Tesseract OCRが正しくインストールされているか"
            "確認してください。\n\n"
            f"詳細:\n{exc}"
        ) from exc


def extract_pdf_text(pdf_path: Path) -> list[str]:
    """PDF全ページから文章を取得する。"""

    page_texts: list[str] = []

    with pymupdf.open(pdf_path) as document:
        total_pages = document.page_count

        for index, page in enumerate(document, start=1):
            print(
                f"[文字抽出] {index}/{total_pages}ページ"
            )

            text, method = extract_page_text(
                page,
                index,
            )

            print(
                f"  -> {method}: {len(text)}文字"
            )

            page_texts.append(text)

    return page_texts


# ============================================================
# Google翻訳
# ============================================================


def split_long_text(
    text: str,
    max_length: int,
) -> list[str]:
    """
    長い英文をGoogle翻訳へ送れる大きさに分割する。

    可能な限り、段落末・文末・空白の位置で分割する。
    """

    text = text.strip()

    if not text:
        return []

    chunks: list[str] = []

    while len(text) > max_length:
        search_area = text[:max_length]

        candidates = [
            search_area.rfind("\n\n"),
            search_area.rfind(". "),
            search_area.rfind("? "),
            search_area.rfind("! "),
            search_area.rfind("; "),
            search_area.rfind(", "),
            search_area.rfind(" "),
        ]

        split_at = max(candidates)

        # あまりにも前方で分割される場合は、
        # 最大文字数で強制的に区切る。
        if split_at < max_length // 2:
            split_at = max_length
        else:
            split_at += 1

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
    """1つの文章チャンクをGoogle翻訳する。"""

    for attempt in range(
        1,
        MAX_TRANSLATION_RETRIES + 1,
    ):
        try:
            print(
                f"[翻訳] {number}/{total} "
                f"({len(chunk)}文字)"
            )

            translated = translator.translate(
                text=chunk
            )

            if not translated:
                raise RuntimeError(
                    "翻訳結果が空でした。"
                )

            return translated

        except Exception as exc:
            if attempt >= MAX_TRANSLATION_RETRIES:
                raise RuntimeError(
                    f"翻訳チャンク {number} の処理に"
                    "失敗しました。\n\n"
                    f"詳細:\n{exc}"
                ) from exc

            wait_seconds = 3 * attempt

            print(
                "  翻訳に失敗したため、"
                f"{wait_seconds}秒後に再試行します。"
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        "翻訳処理で予期しないエラーが発生しました。"
    )


def translate_pages(
    page_texts: list[str],
) -> list[str]:
    """PDFから取得した文章をページごとに翻訳する。"""

    translator = GoogleTranslator(
        source=SOURCE_LANGUAGE,
        target=TARGET_LANGUAGE,
    )

    page_chunks = [
        split_long_text(
            text,
            TRANSLATION_CHUNK_SIZE,
        )
        for text in page_texts
    ]

    total_chunks = sum(
        len(chunks)
        for chunks in page_chunks
    )

    translated_pages: list[str] = []
    current_number = 0

    for page_number, chunks in enumerate(
        page_chunks,
        start=1,
    ):
        translated_chunks: list[str] = []

        for chunk in chunks:
            current_number += 1

            translated = translate_chunk(
                translator=translator,
                chunk=chunk,
                number=current_number,
                total=total_chunks,
            )

            translated_chunks.append(translated)

            time.sleep(
                TRANSLATION_INTERVAL_SECONDS
            )

        translated_page = "\n\n".join(
            translated_chunks
        )

        translated_pages.append(
            translated_page
        )

        print(
            f"  ページ {page_number} の翻訳が"
            "完了しました。"
        )

    return translated_pages


# ============================================================
# Markdown中間ファイル
# ============================================================


def build_translation_markdown(
    pdf_path: Path,
    translated_pages: list[str],
) -> str:
    """
    Google翻訳結果をMarkdownにする。

    ページ境界コメントは中間ファイルでのみ、
    原文ページとの対応確認に使用する。
    """

    lines = [
        f"# 機械翻訳: {pdf_path.name}",
        "",
        "> PDFから自動抽出またはOCRし、"
        "Google翻訳した未校正の中間結果である。",
        "> `<!-- page: N -->` は原文ページとの"
        "対応を確認するためのコメントである。",
        "",
    ]

    for page_number, text in enumerate(
        translated_pages,
        start=1,
    ):
        lines.extend(
            [
                f"<!-- page: {page_number} -->",
                "",
                text or (
                    "（翻訳できる文章がありませんでした）"
                ),
                "",
            ]
        )

    return "\n".join(lines).strip() + "\n"


# ============================================================
# Codex用プロンプト
# ============================================================


def build_codex_prompt(
    input_markdown: str,
) -> str:
    """
    Google翻訳結果の全文と校正指示を、
    1つのプロンプトとして作成する。
    """

    return f"""次のMarkdownは、英語学術論文をPDFから抽出またはOCRし、Google翻訳した全文である。

この文章を日本語の学術論文として校正し、完成したMarkdown本文だけを出力せよ。

条件:
- 内容を要約、省略、追加しない。
- 原文に存在しない説明や推測を追加しない。
- 数値、単位、数式、変数名、引用番号を変更しない。
- 固有名詞および専門用語を不用意に変更しない。
- Google翻訳による不自然な日本語を自然な日本語へ修正する。
- 意味を変えず、必要最小限の校正にとどめる。
- 文体は原則として「である調」に統一する。
- 明確に判別できる既存の章、節、項をMarkdown見出しにする。
- 主な章は ##、節は ###、項は #### とする。
- 本文中では単独の # 見出しを使用しない。
- 章番号や節番号が不明な場合は新しく推測して作らない。
- ページ番号、著者名、論文名の繰り返し、ヘッダー、フッターを見出しにしない。
- 同じ見出しや目次を重複して生成しない。
- 参考文献の著者名、論文名、誌名、年、巻号、ページ、DOIをできるだけ維持する。
- `<!-- page: N -->` コメントは残しても削除してもよい。
- ページコメントを文章中へ移動させない。

数式の書式:
- 行内数式は必ず `$ ... $` で囲む。
- 独立した数式は必ず `$$ ... $$` で囲む。
- `\\( ... \\)` および `\\[ ... \\]` は使用しない。
- cur、goal、grid、desc、nextなど、単語を表す添字・上付き文字には `\\mathrm{{...}}` を使用する。
- 原文で文章中に置かれている短い数式を、不必要に独立した数式へ変更しない。
- 複数行数式が必要な場合は、`$$` 内で `aligned` 環境を使用する。
- 数式の意味、変数、添字、上付き文字を推測して変更しない。
- 数式を日本語へ翻訳しない。
- 数式内のアンダースコア `_`、波括弧、ギリシャ文字、演算子を保持する。

出力:
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
    """Codex CLIがインストールされているか確認する。"""

    executable = shutil.which("codex")

    if executable is None:
        raise RuntimeError(
            "Codex CLIが見つかりません。\n\n"
            "ターミナルで次を確認してください。\n"
            "codex --version"
        )

    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Codex CLIの確認がタイムアウトしました。"
        ) from exc

    if result.returncode != 0:
        details = (
            result.stderr.strip()
            or result.stdout.strip()
        )

        raise RuntimeError(
            "Codex CLIを起動できませんでした。\n\n"
            f"詳細:\n{details}"
        )

    print(
        f"  Codex CLI: {result.stdout.strip()}"
    )


def run_codex_polish(
    input_markdown: str,
) -> str:
    """
    翻訳済みMarkdown全文をCodex CLIへ1回で渡す。

    Codexにはファイルを直接読ませず、標準入力から
    校正指示と全文をまとめて送信する。
    """

    executable = shutil.which("codex")

    if executable is None:
        raise RuntimeError(
            "Codex CLIが見つかりません。"
        )

    prompt = build_codex_prompt(
        input_markdown
    )

    # 元PDFのフォルダではなく、
    # macOSの標準一時フォルダを使う。
    with tempfile.TemporaryDirectory(
        prefix="paper_codex_",
    ) as temp_name:
        temp_dir = Path(temp_name)

        output_path = (
            temp_dir / "codex_result.md"
        )

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
            (
                "model_reasoning_effort="
                f'"{CODEX_REASONING_EFFORT}"'
            ),
            "--output-last-message",
            str(output_path),
            "-",
        ]

        print(
            f"  使用モデル: {CODEX_MODEL}"
        )
        print(
            f"  推論量: {CODEX_REASONING_EFFORT}"
        )
        print(
            "  Google翻訳後のMarkdown全文を"
            "Codexへ1回だけ送信します。"
        )
        print(
            f"  入力文字数: {len(prompt):,}文字"
        )

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
                "Codex処理が"
                f"{CODEX_TIMEOUT_SECONDS}秒以内に"
                "完了しませんでした。"
            ) from exc

        if result.returncode != 0:
            details = (
                result.stderr.strip()
                or result.stdout.strip()
            )

            raise RuntimeError(
                "Codex CLIの処理に失敗しました。\n\n"
                f"使用モデル: {CODEX_MODEL}\n"
                f"推論量: {CODEX_REASONING_EFFORT}\n"
                f"終了コード: {result.returncode}\n\n"
                f"詳細:\n{details}"
            )

        if not output_path.exists():
            raise RuntimeError(
                "Codexは正常終了しましたが、"
                "出力ファイルが作成されませんでした。\n\n"
                f"標準出力:\n{result.stdout}\n\n"
                f"標準エラー:\n{result.stderr}"
            )

        output = output_path.read_text(
            encoding="utf-8"
        ).strip()

        return clean_codex_output(output)


def clean_codex_output(
    text: str,
) -> str:
    """Codex出力の不要な囲みを除去する。"""

    text = text.strip()

    # 出力全体がMarkdownコードフェンスで
    # 囲まれている場合だけ外す。
    fence_match = re.fullmatch(
        r"```(?:markdown|md)?\s*(.*?)\s*```",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    if fence_match:
        text = fence_match.group(1).strip()

    # 万一thinkタグが含まれていた場合は除去する。
    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()

    return text


# ============================================================
# 簡易安全検査
# ============================================================


def remove_nonvisible_comments(
    text: str,
) -> str:
    """文字数比較のためHTMLコメントを除外する。"""

    return re.sub(
        r"<!--.*?-->",
        "",
        text,
        flags=re.DOTALL,
    ).strip()


def validate_codex_output(
    source: str,
    result: str,
) -> tuple[bool, str]:
    """
    Codex出力を簡単に確認する。

    厳密な内容検査は行わない。
    次の明らかな異常だけを検出する。

    1. 出力が空
    2. 作業メモが混入
    3. 入力に比べて極端に短い
    """

    if not result.strip():
        return False, "Codexの出力が空です"

    lowered = result.lower()

    for pattern in INVALID_OUTPUT_PATTERNS:
        if pattern.lower() in lowered:
            return (
                False,
                "作業メモと思われる文字列を"
                f"検出しました: {pattern}",
            )

    source_visible = remove_nonvisible_comments(
        source
    )

    result_visible = remove_nonvisible_comments(
        result
    )

    if not source_visible:
        return True, ""

    length_ratio = (
        len(result_visible)
        / len(source_visible)
    )

    if length_ratio < MIN_OUTPUT_LENGTH_RATIO:
        return (
            False,
            "Codex出力が入力に比べて"
            "極端に短くなっています。\n"
            f"入力: {len(source_visible):,}文字\n"
            f"出力: {len(result_visible):,}文字\n"
            f"比率: {length_ratio:.1%}",
        )

    # ページ境界コメントは検査しない。
    # Markdown見出しや改行の追加による
    # 文字数増加も不採用理由にしない。

    return True, ""


# ============================================================
# 保存
# ============================================================


def save_original_text(
    pdf_path: Path,
    pages: list[str],
) -> Path:
    """PDFから取得した英語原文を保存する。"""

    path = pdf_path.with_name(
        f"{pdf_path.stem}_原文抽出.txt"
    )

    content = "\n\n".join(
        (
            f"===== PAGE {number} =====\n"
            f"{text}"
        )
        for number, text in enumerate(
            pages,
            start=1,
        )
    )

    path.write_text(
        content,
        encoding="utf-8",
    )

    return path


def save_rejected_output(
    pdf_path: Path,
    candidate: str,
) -> Path:
    """検査に失敗したCodex出力を確認用に保存する。"""

    rejected_path = pdf_path.with_name(
        f"{pdf_path.stem}_Codex不採用出力.md"
    )

    rejected_path.write_text(
        candidate.rstrip() + "\n",
        encoding="utf-8",
    )

    return rejected_path


# ============================================================
# メイン処理
# ============================================================


def main() -> int:
    pdf_path = select_pdf_file()

    if pdf_path is None:
        print(
            "PDFの選択がキャンセルされました。"
        )
        return 0

    original_path = pdf_path.with_name(
        f"{pdf_path.stem}_原文抽出.txt"
    )

    translation_path = pdf_path.with_name(
        f"{pdf_path.stem}_機械翻訳.md"
    )

    final_path = pdf_path.with_name(
        f"{pdf_path.stem}_Codex校正版.md"
    )

    print("=" * 60)
    print(f"対象PDF: {pdf_path}")
    print("=" * 60)

    try:
        if not SKIP_CODEX:
            print(
                "\n1/5 Codex CLIを確認しています。"
            )

            check_codex_cli()

        else:
            print(
                "\n1/5 Codex CLI確認を"
                "スキップします。"
            )

        print(
            "\n2/5 PDFから文章を取得しています。"
        )

        original_pages = extract_pdf_text(
            pdf_path
        )

        if not any(
            text.strip()
            for text in original_pages
        ):
            raise RuntimeError(
                "PDFから文章を取得できませんでした。"
            )

        original_path = save_original_text(
            pdf_path,
            original_pages,
        )

        print(
            "\n3/5 Google翻訳を実行しています。"
        )

        translated_pages = translate_pages(
            original_pages
        )

        translation_markdown = (
            build_translation_markdown(
                pdf_path,
                translated_pages,
            )
        )

        translation_path.write_text(
            translation_markdown,
            encoding="utf-8",
        )

        print(
            "  機械翻訳結果を保存しました:"
        )
        print(
            f"  {translation_path}"
        )

        if SKIP_CODEX:
            print(
                "\n4/5 Codex処理は設定により"
                "スキップされました。"
            )

            final_text = translation_markdown

        else:
            print(
                "\n4/5 Codex CLIで校正・"
                "Markdown整形しています。"
            )

            candidate = run_codex_polish(
                translation_markdown
            )

            valid, reason = validate_codex_output(
                translation_markdown,
                candidate,
            )

            if not valid:
                rejected_path = save_rejected_output(
                    pdf_path,
                    candidate,
                )

                raise RuntimeError(
                    "Codex出力の簡易検査に"
                    "失敗しました。\n\n"
                    f"理由:\n{reason}\n\n"
                    "確認用の出力を保存しました:\n"
                    f"{rejected_path}\n\n"
                    "機械翻訳ファイルは正常に"
                    "保存されています。"
                )

            final_text = candidate

        print(
            "\n5/5 最終ファイルを保存しています。"
        )

        final_path.write_text(
            final_text.rstrip() + "\n",
            encoding="utf-8",
        )

    except Exception as exc:
        message = str(exc)

        print(
            f"\n[エラー]\n{message}",
            file=sys.stderr,
        )

        show_error(message)

        return 1

    paths = [
        original_path,
        translation_path,
        final_path,
    ]

    print("\n" + "=" * 60)
    print("処理が完了しました。")

    for path in paths:
        print(f"- {path}")

    print("=" * 60)

    show_success(paths)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())