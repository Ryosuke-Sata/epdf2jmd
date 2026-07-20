import os
import tkinter as tk
from tkinter import filedialog
from pypdf import PdfReader
from deep_translator import GoogleTranslator
import ollama

# 【設定】使用するOllamaのモデル名
OLLAMA_MODEL = "qwen3:8b" 
# 【設定】プロンプトを記述したファイル名
PROMPT_FILE = "prompt.md"

def load_prompt():
    """外部ファイル（prompt.md）から指示を読み込む関数"""
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    else:
        # ファイルが見つからない場合用の予備プロンプト
        print(f"\n[警告] '{PROMPT_FILE}' が見つかりません。デフォルトの指示を使用します。")
        return "英語の論文を機械翻訳したテキストです。意味を変えずに自然な「〜である」調の日本語に整え、Markdownで構造化して出力してください。"

def select_pdf_file():
    root = tk.Tk()
    root.withdraw()
    print("フォルダ画面を開きました。翻訳したいPDFを選んでください...")
    file_path = filedialog.askopenfilename(
        title="翻訳するPDFファイルを選択してください",
        filetypes=[("PDFファイル", "*.pdf"), ("すべてのファイル", "*.*")]
    )
    return file_path

def clean_text(text):
    lines = text.split('\n')
    cleaned_paragraphs = []
    current_paragraph = []
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_paragraph:
                cleaned_paragraphs.append(" ".join(current_paragraph))
                current_paragraph = []
        else:
            if stripped.endswith('-') and not stripped.endswith(' -'):
                current_paragraph.append(stripped[:-1])
            else:
                current_paragraph.append(stripped)
                
    if current_paragraph:
        cleaned_paragraphs.append(" ".join(current_paragraph))
        
    final_paragraphs = []
    for p in cleaned_paragraphs:
        while len(p) > 3000:
            split_idx = p.rfind(' ', 0, 3000)
            if split_idx == -1: split_idx = 3000
            final_paragraphs.append(p[:split_idx])
            p = p[split_idx:].strip()
        if p:
            final_paragraphs.append(p)
            
    return final_paragraphs

def polish_with_ollama(japanese_text, base_prompt):
    """
    読み込んだプロンプトと、翻訳テキストを合体させてOllamaに投げる
    """
    # 外部ファイルの指示＋対象テキストを組み立てる
    full_prompt = f"{base_prompt}\n\n【対象テキスト】\n{japanese_text}"
    
    try:
        response = ollama.chat(model=OLLAMA_MODEL, messages=[
            {'role': 'user', 'content': full_prompt}
        ])
        return response['message']['content'].strip()
    except Exception as e:
        print(f"\n[Ollamaエラー] 整形に失敗しました: {e}")
        return japanese_text

def main():
    pdf_path = select_pdf_file()
    if not pdf_path:
        print("キャンセルされました。処理を終了します。")
        return

    base_name = os.path.splitext(pdf_path)[0]
    output_path = f"{base_name}_AI整形翻訳.md"

    # 1. まずプロンプトファイルを読み込む
    base_prompt = load_prompt()

    print(f"\n[{os.path.basename(pdf_path)}] の翻訳と整形を開始します！")

    print("1/5: PDFの文字を読み取っています...")
    reader = PdfReader(pdf_path)
    all_text = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            all_text += text + "\n\n"

    print("2/5: 文章を綺麗に整形しています...")
    paragraphs = clean_text(all_text)

    print(f"3/5: Google翻訳を実行中...（全 {len(paragraphs)} 段落）")
    print("進捗: ", end="", flush=True)
    
    translator = GoogleTranslator(source='en', target='ja')
    translated_chunks = []
    
    chunk = []
    chunk_len = 0
    for p in paragraphs:
        if not p.strip(): continue
        if chunk_len + len(p) > 3000:
            try:
                translated_chunks.append(translator.translate("\n\n".join(chunk)))
                print("■", end="", flush=True)
            except: pass
            chunk = [p]
            chunk_len = len(p)
        else:
            chunk.append(p)
            chunk_len += len(p) + 2
            
    if chunk:
        try:
            translated_chunks.append(translator.translate("\n\n".join(chunk)))
            print("■", end="", flush=True)
        except: pass

    # --- Ollamaの処理 ---
    print(f"\n4/5: '{PROMPT_FILE}' の指示を元に Ollama ({OLLAMA_MODEL}) で綺麗に整えています...")
    print("AI校正進捗: ", end="", flush=True)
    
    polished_results = []
    for t_chunk in translated_chunks:
        if not t_chunk: continue
        # 読み込んでおいた指示(base_prompt)を渡す
        polished = polish_with_ollama(t_chunk, base_prompt)
        polished_results.append(polished)
        print("★", end="", flush=True)

    print("\n5/5: ファイルを保存しています...")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"# AI整形翻訳: {os.path.basename(pdf_path)}\n\n")
        for result in polished_results:
            if result:
                f.write(result + "\n\n---\n\n")

    print("--------------------------------------------------")
    print(f"★ 完了しました！ 保存先:\n{output_path}")
    print("--------------------------------------------------")

if __name__ == "__main__":
    main()