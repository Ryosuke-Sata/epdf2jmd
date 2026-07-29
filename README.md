# epdf2jmd

英語PDFから文字と画像を抽出し、Codex CLIを使って日本語のObsidian向けMarkdownを生成するツールです。
文字の取得にはPyMuPDFの文字抽出を用います。

## 処理の流れ

1. PDFのテキストブロックを読み順に並べ、ページ単位で抽出する
2. PDF内の一定サイズ以上のラスター画像を`assets`へ保存する
3. `prompt.md`と抽出内容をCodex CLIへ渡し、日本語Markdownへ変換する
4. PDFと同じ分類フォルダの`papers/<PDF名>/`へ結果を保存する

主な出力は`[en]原文抽出.txt`、`[ja]翻訳.md`、画像がある場合の`assets/`です。

## Windowsでの準備

Python 3.10以降とCodex CLIをインストールし、コマンドプロンプトまたはPowerShellで次を実行します。

```bat
cd path\to\epdf2jmd
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
codex --version
```

Codex CLIへのサインインが済んでいない場合は、利用しているCodex CLIの案内に従って認証してください。

## 実行方法

ファイル選択画面からPDFを選ぶ場合は、引数を付けずに実行します。

```bat
.venv\Scripts\python epdf2jmd.py
```

PDFを直接指定する場合は次のとおりです。

```bat
.venv\Scripts\python epdf2jmd.py "C:\path\to\paper.pdf"
```

Codexを呼ばずに抽出だけ確認する場合:

```bat
.venv\Scripts\python epdf2jmd.py --skip-codex "C:\path\to\paper.pdf"
```

macOSやLinuxでは`python3 epdf2jmd.py`で実行できます。Windows、macOS、Linuxのいずれも、PDF引数を省略するとファイル選択画面が開きます。

## 注意事項

- スキャン画像だけで構成されたPDFからは文字を取得できません。
- `prompt.md`は`epdf2jmd.py`と同じフォルダに置いてください。
- Codex CLIがnpmによって`codex.cmd`として導入されているWindows環境にも対応しています。
