from __future__ import annotations

import re
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import pymupdf
from deep_translator import GoogleTranslator


# =========================
# 設定
# =========================

SOURCE_LANGUAGE = "en"
TARGET_LANGUAGE = "ja"

# 文字抽出結果がこれ未満なら、そのページをOCRする
MIN_DIRECT_TEXT_LENGTH = 80

# OCR解像度。高いほど精度が上がりやすいが、処理時間も増える
OCR_DPI = 300

# Google翻訳へ一度に送る最大文字数。
# 上限ぎりぎりを避けるため、余裕を持たせている
TRANSLATION_CHUNK_SIZE = 3500

# 翻訳失敗時の再試行回数
MAX_TRANSLATION_RETRIES = 3

# WindowsでTesseractの言語データを自動認識できない場合に指定する。
# 例:
# TESSDATA_PATH = r"C:\Program Files\Tesseract-OCR\tessdata"
TESSDATA_PATH: str | None = None


def select_pdf_file() -> Path | None:
    """ファイル選択画面を開き、選択されたPDFのパスを返す。"""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    selected = filedialog.askopenfilename(
        title="翻訳する英語PDFを選択してください",
        filetypes=[("PDFファイル", "*.pdf")],
    )

    root.destroy()
    return Path(selected) if selected else None


def normalize_extracted_text(text: str) -> str:
    """
    PDFから抽出した英文を整える。

    - 行末ハイフンによる単語分割を結合
    - 段落内の不要な改行を空白へ変換
    - 連続する空白を整理
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 例: trans-\nlation -> translation
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)

    paragraphs: list[str] = []

    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        paragraph = " ".join(lines)
        paragraph = re.sub(r"[ \t]+", " ", paragraph).strip()

        if paragraph:
            paragraphs.append(paragraph)

    return "\n\n".join(paragraphs)


def extract_page_text(page: pymupdf.Page, page_number: int) -> tuple[str, str]:
    """
    1ページ分のテキストを取得する。

    まず通常の文字抽出を試し、文字数が少なければOCRを実行する。
    戻り値は (抽出テキスト, 抽出方法)。
    """
    direct_text = normalize_extracted_text(page.get_text("text"))

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
        ocr_text = normalize_extracted_text(
            page.get_text("text", textpage=text_page)
        )

        if ocr_text:
            return ocr_text, "OCR"

        return direct_text, "抽出結果なし"

    except Exception as exc:
        raise RuntimeError(
            f"{page_number}ページ目のOCRに失敗しました。\n"
            "Tesseract OCRが正しくインストールされているか確認してください。\n"
            f"詳細: {exc}"
        ) from exc


def extract_pdf_text(pdf_path: Path) -> list[str]:
    """PDF全ページから文字を抽出し、ページごとの文字列として返す。"""
    page_texts: list[str] = []

    with pymupdf.open(pdf_path) as document:
        total_pages = document.page_count

        for index, page in enumerate(document, start=1):
            print(f"[文字抽出] {index}/{total_pages}ページ")

            text, method = extract_page_text(page, index)
            print(f"  -> {method}: {len(text)}文字")

            page_texts.append(text)

    return page_texts


def split_long_text(text: str, max_length: int) -> list[str]:
    """
    長い文章を翻訳用の小さな単位へ分割する。

    段落、文末、空白の順で、できるだけ自然な位置を探して分割する。
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

        split_position = max(candidates)

        # 分割点が極端に前だと細切れになるため、その場合は強制分割
        if split_position < max_length // 2:
            split_position = max_length
        else:
            # 句読点を前半チャンクへ含める
            split_position += 1

        chunks.append(text[:split_position].strip())
        text = text[split_position:].strip()

    if text:
        chunks.append(text)

    return chunks


def translate_chunk(
    translator: GoogleTranslator,
    chunk: str,
    chunk_number: int,
    total_chunks: int,
) -> str:
    """1チャンクを翻訳し、失敗時は一定回数再試行する。"""
    for attempt in range(1, MAX_TRANSLATION_RETRIES + 1):
        try:
            print(
                f"[翻訳] {chunk_number}/{total_chunks} "
                f"({len(chunk)}文字、試行 {attempt}/{MAX_TRANSLATION_RETRIES})"
            )

            translated = translator.translate(text=chunk)

            if not translated:
                raise RuntimeError("翻訳結果が空でした。")

            return translated

        except Exception as exc:
            if attempt == MAX_TRANSLATION_RETRIES:
                raise RuntimeError(
                    f"翻訳チャンク {chunk_number} の処理に失敗しました。\n"
                    f"詳細: {exc}"
                ) from exc

            wait_seconds = 3 * attempt
            print(f"  翻訳に失敗したため、{wait_seconds}秒後に再試行します。")
            time.sleep(wait_seconds)

    raise RuntimeError("翻訳処理で予期しないエラーが発生しました。")


def translate_pages(page_texts: list[str]) -> list[str]:
    """ページ単位の英文を日本語へ翻訳する。"""
    translator = GoogleTranslator(
        source=SOURCE_LANGUAGE,
        target=TARGET_LANGUAGE,
    )

    page_chunks = [
        split_long_text(text, TRANSLATION_CHUNK_SIZE)
        for text in page_texts
    ]
    total_chunks = sum(len(chunks) for chunks in page_chunks)

    translated_pages: list[str] = []
    current_chunk_number = 0

    for page_number, chunks in enumerate(page_chunks, start=1):
        translated_chunks: list[str] = []

        if not chunks:
            translated_pages.append("")
            continue

        for chunk in chunks:
            current_chunk_number += 1
            translated = translate_chunk(
                translator,
                chunk,
                current_chunk_number,
                total_chunks,
            )
            translated_chunks.append(translated)

            # 非公式Google翻訳への連続アクセスを少し抑える
            time.sleep(0.5)

        translated_pages.append("\n\n".join(translated_chunks))
        print(f"  ページ {page_number} の翻訳が完了しました。")

    return translated_pages


def build_markdown(
    pdf_path: Path,
    original_pages: list[str],
    translated_pages: list[str],
) -> str:
    """翻訳結果をMarkdown形式へまとめる。"""
    lines = [
        f"# PDF翻訳: {pdf_path.name}",
        "",
        "> 自動文字抽出・OCRおよび機械翻訳による結果です。",
        "> 数式、表、脚注、段組みなどは正確に再現されない場合があります。",
        "",
    ]

    for page_number, (original, translated) in enumerate(
        zip(original_pages, translated_pages),
        start=1,
    ):
        lines.extend(
            [
                f"## ページ {page_number}",
                "",
                "### 日本語訳",
                "",
                translated or "（翻訳できる文章がありませんでした）",
                "",
                "<details>",
                "<summary>抽出した原文を表示</summary>",
                "",
                original or "（文章を抽出できませんでした）",
                "",
                "</details>",
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines)


def save_result(
    pdf_path: Path,
    original_pages: list[str],
    translated_pages: list[str],
) -> Path:
    """PDFと同じフォルダへMarkdownファイルを保存する。"""
    output_path = pdf_path.with_name(f"{pdf_path.stem}_翻訳.md")
    markdown = build_markdown(pdf_path, original_pages, translated_pages)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


def show_error(message: str) -> None:
    """エラーダイアログを表示する。"""
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("エラー", message)
    root.destroy()


def show_success(output_path: Path) -> None:
    """完了ダイアログを表示する。"""
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        "翻訳完了",
        f"翻訳が完了しました。\n\n保存先:\n{output_path}",
    )
    root.destroy()


def main() -> int:
    pdf_path = select_pdf_file()

    if pdf_path is None:
        print("PDFの選択がキャンセルされました。")
        return 0

    print("=" * 60)
    print(f"対象PDF: {pdf_path}")
    print("=" * 60)

    try:
        print("\n1/3 PDFから文章を取得しています。")
        original_pages = extract_pdf_text(pdf_path)

        if not any(text.strip() for text in original_pages):
            raise RuntimeError(
                "PDFから文章を取得できませんでした。\n"
                "PDFが破損していないか、Tesseract OCRが使用可能か確認してください。"
            )

        print("\n2/3 Google翻訳を実行しています。")
        translated_pages = translate_pages(original_pages)

        print("\n3/3 翻訳結果を保存しています。")
        output_path = save_result(
            pdf_path,
            original_pages,
            translated_pages,
        )

    except Exception as exc:
        error_message = str(exc)
        print(f"\n[エラー]\n{error_message}", file=sys.stderr)
        show_error(error_message)
        return 1

    print("\n" + "=" * 60)
    print("翻訳が完了しました。")
    print(f"保存先: {output_path}")
    print("=" * 60)

    show_success(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())