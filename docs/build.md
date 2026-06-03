# バイナリのビルド

スタンドアロン実行ファイルは [PyInstaller](https://pyinstaller.org/) で生成します。
設定は repo 直下の [`enquete.spec`](../enquete.spec) に集約しています。

## 自動ビルド（推奨）

GitHub Actions（[`.github/workflows/build.yml`](../.github/workflows/build.yml)）で
**macOS と Windows のバイナリを自動生成**します。

- **手動実行**: GitHub の *Actions* → *Build binaries* → *Run workflow*
  → 完了後、各ジョブの *Artifacts* から `enquete-macos.zip` / `enquete-windows.zip` を取得。
- **リリース**: `v*` タグを push すると同じ生成物が **GitHub Release に添付**されます。

```bash
git tag v0.1.0
git push origin v0.1.0   # → Actions が走り、Release に zip が付く
```

## ローカルビルド

CI と同じ Python 3.12 を推奨します（[uv](https://docs.astral.sh/uv/) 利用例）。

```bash
uv venv --python 3.12
uv pip install -e ".[build]"          # Windows で標準OCRも含めるなら ".[build,winocr]"
uv run pyinstaller enquete.spec --noconfirm
```

生成物:

- **macOS**: `dist/enquete.app`
- **Windows**: `dist/enquete/`（`enquete.exe` を含むフォルダ一式）

## 同梱方針

- 実行時に読む `src/enquete/ui/main_window.ui` と pypdfium2 のネイティブ
  ライブラリ（libpdfium）を同梱します。
- **macOS**: Apple Vision OCR（`ocrmac` + pyobjc）を同梱 → 追加導入なしで OCR 可能。
- **Windows**: Windows 標準 OCR（`winsdk`）を `winocr` extra として同梱可能。
- **同梱しない**: Chrome screen-ai（`locro`）とそのモデルは**非再配布**のため同梱しません。
  PyMuPDF（AGPL）も含めません。screen-ai を使う場合は別途導入が必要です
  （[`ocr_windows.md`](ocr_windows.md) 参照）。

## 配布時の注意（署名なし）

CI 生成物は**コード署名・公証なし**です。

- **macOS**: 初回起動は Gatekeeper に阻まれます。Finder で右クリック →「開く」、
  または隔離属性を外してください：
  ```bash
  xattr -dr com.apple.quarantine /Applications/enquete.app
  ```
- **Windows**: SmartScreen の警告が出る場合があります。「詳細情報」→「実行」で起動できます。
