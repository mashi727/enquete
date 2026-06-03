# enquete — アンケート読み込み支援アプリ

紙のアンケート（PDF スキャン）の集計・電子化を支援する PySide6 製デスクトップアプリです。
サムネイル一覧・PDF 表示・入力フォームの 3 ペイン構成で、チェックボックスや丸囲み選択の
自動判定、自由記述欄の OCR、結果のサイドカー JSON 保存までを一気通貫で行います。

## 主な機能

- **3 ペイン UI**: サムネイル / PDF プレビュー / 校正フォーム
- **選択式の自動判定**: チェックボックス（□）・番号丸囲み（①②…）・丸文字選択をインク密度／リング解析で検出
- **差分検出**: クリーンな台紙（先頭ページ）を基準に記入ページとの差分でチェックを判定
- **自由記述の OCR**: 領域を切り出して OCR（macOS=Apple Vision、Windows/Linux=Chrome screen-ai、Windows 標準 OCR にも対応）
- **フォーム自動生成**: 台紙 PDF から設問・選択肢フォームを生成（ベクタ／スキャンの両経路）
- **傾き補正モード**: グリッドを見ながらドラッグで回転し、確定で PDF を上書き保存（原本は `.bak` へ退避、Esc でキャンセル）
- **全面オートセーブ**: 校正結果は編集の都度サイドカー JSON へ自動保存
- **分割 / マージ**: 手分け作業のための PDF 分割と結果の統合
- **ドラッグ＆ドロップ**: サムネイル領域へ PDF を投入してページ挿入・別名保存

## 動作環境

- Python **3.12 以上**
- 依存: PySide6（LGPL）, pypdfium2（BSD）, opencv-python-headless, numpy
- OCR バックエンド（任意・OS により選択）
  - macOS: `ocrmac`（Apple Vision）
  - Windows / Linux: `locro`（Chrome screen-ai）— `screenai` extra
  - Windows: `winsdk`（Windows.Media.Ocr）— `winocr` extra

## セットアップ

[uv](https://docs.astral.sh/uv/) を推奨します。

```bash
uv sync                      # 基本依存のみ
uv sync --extra screenai     # Chrome screen-ai OCR を併用する場合（Windows/Linux）
uv sync --extra winocr       # Windows 標準 OCR を併用する場合（Windows）
```

pip の場合:

```bash
pip install -e .
```

> **screen-ai について**: `locro` は PyPI 未公開のソース配布で、エンジン本体／モデルは
> 非再配布です。導入後に `locro download` で手元の Chrome からコピーする必要があります。
> 詳細は [`docs/ocr_windows.md`](docs/ocr_windows.md) を参照してください。

## 実行

```bash
uv run python -m enquete
```

## 使い方（標準フロー）

下段アクションバーが作業順に並んでいます。

1. **① 開く** — アンケート PDF を開く（サムネイル領域への D&D も可）
2. **② フォーム ▾** — 台紙からフォームを作成 / 既存フォームを読込 / 編集
3. **傾き補正モード** — 必要に応じて傾きを補正（確定＝上書き保存、Esc＝キャンセル）
4. **③ 電子化** — 全ページを一括で自動判定・OCR
5. **◀ / ▶ ・ ✓ 確認済みにする** — ページを送りながら結果を校正・確認

校正結果は編集の都度、PDF と同名・同ディレクトリの `*.json`（サイドカー）へ自動保存されます。

## ライセンス

[MIT License](LICENSE)。

なお依存ライブラリは各自のライセンスに従います（PySide6 は LGPL、pypdfium2 は BSD など）。

## 備考

- 本リポジトリには実アンケート（個人情報を含む）・処理ログ・作業メモは含めていません
  （`resources/`・`worklog/` は `.gitignore` 済み）。動作確認には任意の PDF を用意してください。
