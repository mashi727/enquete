# enquete — アンケート読み込み支援アプリ

紙のアンケート（PDF スキャン）の集計・電子化を支援する PySide6 製デスクトップアプリです。
サムネイル一覧・PDF 表示・入力フォームの 3 ペイン構成で、チェックボックスや丸囲み選択の
自動判定、自由記述欄の OCR、結果のサイドカー JSON 保存までを一気通貫で行います。

## 主な機能

- **3 ペイン UI**: サムネイル / PDF プレビュー / 校正フォーム
- **開くだけでフォーム自動作成**: サイドカー JSON が無い PDF を開くと、操作なしで設問・選択肢フォームを自動生成（テキスト層あり＝ベクタ抽出／スキャン＝OCR）。「その他（　）」のような空欄括弧は自由入力欄として生成
- **選択式の自動判定**: チェックボックス（□）・番号丸囲み（①②…）・丸文字選択をインク密度／リング解析で検出。**読み取りは「③ 電子化」を押したときにまとめて実行**（開いた直後は検出しません）
- **差分検出（既定 ON）**: 1 枚目をクリーンな原紙（基準）とし、2 枚目以降との差分でチェックを判定。**原紙は集計結果に含めません**。原紙はサムネイルの表示/非表示をトグルで切替可能
- **自由記述の OCR**: 領域を切り出して OCR（macOS=Apple Vision、Windows/Linux=Chrome screen-ai、Windows 標準 OCR にも対応）
- **傾き補正モード**: グリッドを見ながらドラッグで回転し、確定で PDF を上書き保存（原本は `.bak` へ退避、Esc でキャンセル）
- **全面オートセーブ**: 校正結果は編集の都度サイドカー JSON へ自動保存（明示保存は不要）
- **ROI オーバーレイ**: 検出枠と「選択済み（集計対象）」マークを PDF に重畳（既定 ON）。インク比率の数値は検出チューニング用に任意表示
- **分割 / マージ**: 手分け作業のための PDF 分割と結果の統合
- **ドラッグ＆ドロップ**: サムネイル領域へ PDF を投入してページ挿入・別名保存

## ダウンロード（ビルド済みバイナリ）

macOS / Windows 向けのスタンドアロン版を配布します。

- **リリース**: [Releases](https://github.com/mashi727/enquete/releases) から取得（`v*` タグごとに自動添付）。
  - **Windows**: `enquete-windows.exe`（単一ファイル。ダウンロードしてダブルクリックで起動）
  - **macOS**: `enquete-macos.zip`（展開して `enquete.app` を起動）
- **最新ビルド**: GitHub の *Actions* → *Build binaries* の成果物（Artifacts）からも取得可能。

> ⚠️ バイナリは**コード署名・公証なし**です。
> - macOS: 初回は Finder で右クリック →「開く」、または `xattr -dr com.apple.quarantine enquete.app`
> - Windows: SmartScreen の「詳細情報」→「実行」（単一 exe は初回起動の自己展開でやや時間がかかります）
>
> ビルド方法・同梱方針は [`docs/build.md`](docs/build.md) を参照してください。

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
> 配布バイナリ（exe / .app）では `locro` を同梱しており、メニューの
> **ツール → OCRエンジン → 「screen-ai OCR を有効化…」** から一度だけ取得すれば
> 高精度な screen-ai OCR を利用できます。詳細は
> [`docs/ocr_windows.md`](docs/ocr_windows.md) を参照してください。

## 実行

```bash
uv run python -m enquete
```

## 使い方（標準フロー）

想定する運用は「**1 枚目＝クリーンな原紙、2 枚目以降＝記入済みスキャン**」を
1 つの複数ページ PDF にまとめたものです（1 ページ＝1 回答）。
下段アクションバーが作業順に並んでいます。

1. **① 開く** — アンケート PDF を開く（サムネイル領域への D&D も可）。
   サイドカー JSON が無ければ、**この時点でフォームが自動作成**されます（差分認識 ON）。
2. **傾き補正モード**（必要時）— トグル式。入る→グリッドを見ながらドラッグで回転→
   もう一度押すと保存（PDF 上書き・原本は `.bak` へ）。`Esc` で破棄してキャンセル
3. **③ 電子化** — ここで初めて**全ページのチェックを一括判定・自由記述を OCR**します
   （開いた直後やページ送りでは検出しません）。1 枚目の原紙は対象外
4. **◀ / ▶ ・ ✓ 確認済みにする** — ページを送りながら結果を校正・確認。
   PDF 上のチェック位置をダブルクリックして直接トグルもできます（単一選択は排他）
5. **原紙を隠す** — 1 枚目のクリーン原紙をサムネイルから隠せます（原紙は集計対象外）

> - フォームを作り直す／設問・選択肢を手で直すときは **② フォーム ▾**（作成・編集）。
> - 「**モデル: …**」ドロップダウンで OCR エンジン（自動 / Apple Vision /
>   Chrome screen-ai / Windows 標準）を切り替えられます（ツール → OCRエンジンと同期）。
> - 拡大縮小は **Ctrl ＋ マウスホイール**（Windows）／ピンチ（macOS トラックパッド）。

校正結果は編集の都度、PDF と同名・同ディレクトリの `*.json`（サイドカー）へ
**自動保存**されます（明示的な保存操作は不要）。傾き補正だけは破壊的操作のため、
モードを抜けたときに確定保存します。

## ライセンス

[MIT License](LICENSE)。

なお依存ライブラリは各自のライセンスに従います（PySide6 は LGPL、pypdfium2 は BSD など）。

## 備考

- 本リポジトリには実アンケート（個人情報を含む）・処理ログ・作業メモは含めていません
  （`resources/`・`worklog/` は `.gitignore` 済み）。動作確認には任意の PDF を用意してください。
