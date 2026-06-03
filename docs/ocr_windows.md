# Windows / Linux での OCR（Chrome screen-ai / locro）

enquete の OCR（スキャンからのフォーム作成・将来の手書き領域読取）は、
プラットフォームごとに別バックエンドへ自動で切り替わる。

| OS | バックエンド | 既定の入手性 |
|----|-------------|-------------|
| macOS | Apple Vision（`ocrmac`） | 標準依存に含まれ、追加作業なしで動作 |
| Windows / Linux | Chrome screen-ai（`locro`） | 下記セットアップが必要 |

バックエンドの選択は `enquete.ocr.get_default_backend()` が行い、
利用可能な最優先のものを返す（mac では文字単位 box が取れる Vision を優先）。

## なぜ追加セットアップが要るのか

`clv-locro`（<https://github.com/sergiocorreia/clv-locro>, MIT）は Chrome の
`screen-ai` エンジンを ctypes 経由で呼ぶラッパだが、

- **PyPI 未公開**（Git からのソース導入）。
- **エンジン本体・モデルが非再配布**（`chrome_screen_ai.dll` /
  `libchromescreenai.so` と重みファイル）。アプリには同梱できず、利用者が
  自分の Chrome から実行時にコピーする。

このため Windows/Linux ではオプション依存として明示的に導入する。

## セットアップ手順（Windows / Linux）

```bash
# 1) オプション依存 screenai を導入(locro + pillow)
#    ※ locro は Git ソース導入。ビルドに Rust/C ツールチェーンは不要。
uv sync --extra screenai
#   もしくは: uv pip install -e '.[screenai]'

# 2) Chrome から screen-ai のライブラリ＋モデルを一度だけコピー
#    (Google Chrome / Chromium がインストール済みである必要がある)
uv run locro download

# 3) 動作確認(任意): 画像/PDF をOCRしてみる
uv run locro ocr path/to/sample.png --text
```

セットアップ後にアプリを起動すれば、スキャン PDF に対する
「ツール → フォーム作成」が screen-ai 経由で動作する。

## 配布バイナリ（exe / .app）で screen-ai を有効化する

スタンドアロン配布版には **`locro`（MIT のラッパ）は同梱**していますが、
**screen-ai のライブラリ本体・モデル（非再配布）は含めていません**。
アプリ内から一度だけ取得して有効化します（ソース環境・`uv`/`locro` コマンド不要）。

1. アプリのメニュー **ツール → OCRエンジン → 「screen-ai OCR を有効化（コンポーネント取得）…」**
2. 確認のうえ実行すると、バックグラウンドで以下を**自動フォールバック**で取得します:
   - インストール済み **Chrome** の screen_ai コンポーネントをコピー
   - 無ければ **Dropbox** の zip（`<Dropbox>/bin/screen-ai-<platform>.zip`）
   - 無ければ **Google サーバ**から取得（サーバ制限で失敗する場合あり）
3. 取得に成功すると `%LOCALAPPDATA%\locro`（Windows）/ `~/Library/Application Support/locro`（macOS）
   へ配置され、OCRエンジンが自動的に screen-ai に切り替わります。

> Chrome 経由が最も確実です。事前に Chrome の `chrome://components` で
> 「Screen AI」コンポーネントを更新しておくと、ローカルコピーで取得できます。

## 既知の制約

- screen-ai は **平文テキストには高精度だが、表・フォームのような複雑な
  レイアウトはやや苦手**（作者明記）。番号マーカーのクラスタリングは
  ROI オーバーレイ＋人手校正で補正する前提。
- locro は **行・語レベルの bounding box** のみ提供（文字単位は無し）。
  本アプリの `box_for_range` は語 box の和集合で近似するため、Apple Vision
  より個別マーカーの矩形が粗くなることがある。
- 日本語**手書き**の認識精度は未実測（実機 Windows での検証が必要）。

## 実装メモ

- バックエンド実装: `src/enquete/ocr/screenai_backend.py`（`ScreenAiBackend`）。
  `available()` は `locro` の import とモデルのロード可否を遅延チェックする
  （未導入・モデル未取得なら `False` を返し、アプリ自体は通常どおり動く）。
- locro の box はピクセル・左上原点。`_norm_box()` で正規化(0..1)左上原点
  Rect=(x0,y0,x1,y1) に変換している。
