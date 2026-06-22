# バイナリのビルド

スタンドアロン実行ファイルは [PyInstaller](https://pyinstaller.org/) で生成します。
設定は repo 直下の [`enquete.spec`](../enquete.spec) に集約しています。

## 自動ビルド（推奨）

GitHub Actions（[`.github/workflows/build.yml`](../.github/workflows/build.yml)）で
**macOS と Windows のバイナリを自動生成**します。

- **手動実行**: GitHub の *Actions* → *Build binaries* → *Run workflow*
  → 完了後、各ジョブの *Artifacts* から `enquete-macos.zip` / `enquete-windows.exe` を取得。
- **リリース**: `v*` タグを push すると同じ生成物が **GitHub Release に添付**されます。

```bash
git tag v0.1.6
git push origin v0.1.6   # → Actions が走り、Release に成果物が付く
# あるいは: gh release create v0.1.6 --title "enquete v0.1.6" --notes "..."
```

## ローカルビルド

CI と同じ Python 3.12 を推奨します（[uv](https://docs.astral.sh/uv/) 利用例）。

```bash
uv venv --python 3.12
uv pip install -e ".[build]"          # Windows で標準OCRも含めるなら ".[build,winocr]"
# screen-ai を同梱する場合は locro も導入(AGPL の PyMuPDF を引かないよう --no-deps)
uv pip install --no-deps "git+https://github.com/sergiocorreia/clv-locro"
uv run pyinstaller enquete.spec --noconfirm
```

生成物（`enquete.spec` はプラットフォームで形態を切り替えます）:

- **macOS**: `dist/enquete.app`（onedir + .app バンドル）
- **Windows**: `dist/enquete.exe`（onefile・単一実行ファイル）

## 同梱方針

- 実行時に読む `src/enquete/ui/main_window.ui`・アプリアイコン、pypdfium2 の
  ネイティブライブラリ（libpdfium）、Pillow を同梱します。
- **macOS**: Apple Vision OCR（`ocrmac` + pyobjc）を同梱 → 追加導入なしで OCR 可能。
- **Windows**: Windows 標準 OCR（`winsdk`）を `winocr` extra として同梱。
- **screen-ai（`locro`・MIT ラッパ）は同梱**します（外部依存は Pillow のみ）。
  ただし **screen-ai のライブラリ本体・モデルは非再配布のため同梱しません** —
  アプリ内の「screen-ai OCR を有効化」から実行時に取得します
  （[`ocr_windows.md`](ocr_windows.md) 参照）。
- **同梱しない**: PyMuPDF（AGPL）・`typer`・`locro.cli` は除外します。

## 配布時の注意（署名なし）

CI 生成物は**コード署名・公証なし**です。

- **macOS**: 初回起動は Gatekeeper に阻まれます。Finder で右クリック →「開く」、
  または隔離属性を外してください：
  ```bash
  xattr -dr com.apple.quarantine /Applications/enquete.app
  ```
- **Windows**: SmartScreen の警告が出る場合があります。「詳細情報」→「実行」で起動できます。
