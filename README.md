# epdf2jmd

英語論文のPDFからテキストと画像を抽出し、Codex CLIで日本語のObsidian向けMarkdownへ翻訳・整形するPythonスクリプトです。

PyMuPDFでページごとのテキストブロックとラスター画像を取得し、`prompt.md`の指示と抽出結果をCodexへ一括で渡します。Codexは翻訳に加え、二段組による読み順の乱れ、見出し、数式、表、参考文献などの復元を試みます。

## 主な機能

- PDFのテキストブロックを座標順に抽出し、行末ハイフンで分割された英単語を結合
- 一定サイズ以上のラスター画像をPNGで保存し、Obsidianの`![[assets/...]]`形式で原稿へ挿入
- 原文PDFへの相対リンクをMarkdownの先頭に追加
- Codex CLIによる英語から日本語への翻訳とMarkdown整形
- Codexを使用せず、抽出内容だけを確認できる`--skip-codex`オプション
- 引数なしで起動した場合のPDFファイル選択ダイアログ

OCRは実装していません。画像としてスキャンされた文字は抽出できません。

## 動作環境

- Python 3.10以降
- Codex CLI（翻訳を行う場合）
- PythonのTkサポート（ファイル選択ダイアログを使う場合）

Pythonパッケージの依存関係は`requirements.txt`に記載されています。

## セットアップ

### Windows

コマンドプロンプトまたはPowerShellで実行します。

```bat
cd path\to\epdf2jmd
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
codex --version
```

### macOS / Linux

```sh
cd /path/to/epdf2jmd
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
codex --version
```

Codexを使用する場合は、実行前にCodex CLIの認証を完了してください。`codex --version`が失敗する場合は、Codex CLIがインストールされ、`PATH`から実行できることを確認します。Windowsでは`codex.exe`、`codex.cmd`、`codex.bat`も自動検出します。

## 使い方

### PDFを指定して実行する

Windows:

```bat
.venv\Scripts\python epdf2jmd.py "C:\path\to\paper.pdf"
```

macOS / Linux:

```sh
.venv/bin/python epdf2jmd.py "/path/to/paper.pdf"
```

### ファイル選択ダイアログを使う

PDF引数を省略すると、PDFを選択するダイアログが開きます。

```sh
.venv/bin/python epdf2jmd.py
```

Windowsでは次のように実行します。

```bat
.venv\Scripts\python epdf2jmd.py
```

### Codexを使わずに抽出する

```sh
.venv/bin/python epdf2jmd.py --skip-codex "/path/to/paper.pdf"
```

この場合もテキストと画像は抽出されますが、`[ja]翻訳.md`には日本語訳ではなく、Codexへ渡す予定だった英語の抽出Markdownが保存されます。Codex CLIのインストールや認証は不要です。

利用可能な引数は次のコマンドでも確認できます。

```sh
.venv/bin/python epdf2jmd.py --help
```

## 出力先

出力先は入力PDFの位置から自動的に決まります。

- PDFが`<分類>/pdf/論文名.pdf`にある場合: `<分類>/papers/論文名/`
- それ以外の場所にある場合: `<PDFと同じフォルダ>/papers/論文名/`

たとえば、入力が次の場所にある場合:

```text
robotics/
└── pdf/
    └── sample-paper.pdf
```

出力は次の構成になります。

```text
robotics/
├── pdf/
│   └── sample-paper.pdf
└── papers/
    └── sample-paper/
        ├── [en]原文抽出.txt
        ├── [ja]翻訳.md
        └── assets/
            ├── page_001_image_01.png
            └── ...
```

`assets/`は対象となる画像が1枚もない場合には残りません。

| 出力 | 内容 |
| --- | --- |
| `[en]原文抽出.txt` | ページ区切り付きの英語テキスト抽出結果 |
| `[ja]翻訳.md` | 原文PDFへの相対リンクを含む、日本語翻訳・整形結果 |
| `assets/` | PDFから抽出した画像のPNGファイル |

同じPDFを再処理すると同じ出力フォルダを使用し、同名の出力ファイルと画像を上書きします。

## 処理の流れ

1. 入力PDFと出力先を決定する
2. Codexを使う場合はCLIを検出し、起動できることを確認する
3. PyMuPDFでページごとのテキストを抽出し、`[en]原文抽出.txt`へ保存する
4. 条件を満たすラスター画像をページ上の表示領域からPNGとして抽出する
5. 原文PDFリンク、ページコメント、本文、画像埋め込みを含む英語Markdownを組み立てる
6. `prompt.md`と英語Markdownの全文を1回のCodex CLI実行へ渡す
7. 結果を`[ja]翻訳.md`へ保存する

Codexの処理に失敗した場合も、抽出結果を失わないように英語の抽出Markdownを`[ja]翻訳.md`へ保存し、コンソールへ警告を表示します。

## 現在の設定

次の値はコマンドライン引数ではなく、`epdf2jmd.py`冒頭の定数で設定されています。

| 設定 | 現在値 | 内容 |
| --- | --- | --- |
| `CODEX_MODEL` | `gpt-5.6-luna` | Codexで使用するモデル |
| `CODEX_REASONING_EFFORT` | `low` | 推論量 |
| `CODEX_TIMEOUT_SECONDS` | `1800` | Codex処理のタイムアウト秒数 |
| `EXTRACT_IMAGES` | `True` | 画像抽出の有効・無効 |
| `MIN_IMAGE_WIDTH` | `160` | 抽出する画像の最小幅 |
| `MIN_IMAGE_HEIGHT` | `100` | 抽出する画像の最小高さ |
| `MIN_IMAGE_AREA` | `30000` | 抽出する画像の最小面積 |
| `IMAGE_RENDER_SCALE` | `2.0` | 画像領域をPNG化するときの倍率 |

翻訳・整形の方針を変更する場合は、`epdf2jmd.py`と同じフォルダにある`prompt.md`を編集してください。ファイルが存在しない、または空の場合はCodex処理を開始できません。

## 制限事項と注意

- OCRを行わないため、スキャン画像だけで構成されたPDFには対応していません。
- テキストはブロックの縦・横座標で並べます。複雑な二段組、表、数式の抽出順は崩れることがあり、その修復は主にCodexへの指示に依存します。
- 画像抽出の対象はPDF内で検出できるラスター画像です。ベクター図形やページ全体のレイアウトをそのまま画像化する機能ではありません。
- 画像とキャプションは、同一ページ内で上から順に対応付けます。正確に対応できない画像はそのページの末尾へ配置されます。
- 翻訳実行時は、抽出したPDF本文と`prompt.md`の内容がCodex CLIへ送信されます。機密文書を扱う場合は利用条件を確認するか、`--skip-codex`を使用してください。
- PDF名から作る出力フォルダ名は、Windowsで使用できない文字を`_`へ置換し、最大72文字に短縮します。
