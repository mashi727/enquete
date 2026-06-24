"""メインウィンドウ。Qt Designer の .ui を実行時に QUiLoader で読み込み、
PDF 表示まわりのロジックを MainWindowController に実装する。

.ui を編集したら再実行するだけで反映される(ビルド工程なし)。
Designer での編集:  uv run pyside6-designer src/enquete/ui/main_window.ui
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
from pathlib import Path

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QObject,
    QPointF,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QNativeGestureEvent,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTransform,
    QWheelEvent,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from enquete._log import log
from enquete.detect import QuestionResult, detect_checkboxes, qimage_to_gray
from enquete.detection_settings_dialog import DetectionSettingsDialog
from enquete.form_editor_dialog import FormEditorDialog
from enquete.form_pane import FormPane
from enquete.formgen import generate_survey_from_pdf, has_text_layer
from enquete.align import write_aligned_pdf
from enquete.deskew import write_deskewed_pdf
from enquete.merge import insert_pdf
from enquete.digitize import digitize_page, merge_results, recognize_region
from enquete.formgen_ocr import generate_survey_from_scans
from enquete.merge_dialog import MergeDialog, SplitDialog
from enquete.ocr import (
    BACKEND_AUTO,
    BACKEND_SCREENAI,
    backend_choices,
    get_backend,
)
from enquete.pdf import PdfDocument
from enquete.region_edit_item import RegionEditItem
from enquete.schema import (
    FREE_TEXT,
    MARKER_BOX,
    MULTI_CHOICE,
    SINGLE_CHOICE,
    CheckboxDetection,
    Rect,
    Survey,
    survey_to_dict,
)
from enquete import pdf_embed
from enquete.store import SurveyDocument

UI_PATH = Path(__file__).resolve().parent / "ui" / "main_window.ui"

# レンダリング倍率(scale=1.0 が 72dpi 相当)。
# サムネイルは列幅いっぱいに拡大表示するため、上限幅(maximumSize=360px)を
# 上回る解像度で描画しておき、表示側で縮小して鮮明さを保つ。
THUMB_SCALE = 0.7

# メイン表示のラスタライズ倍率(px/pt)。表示倍率(×devicePixelRatio)に追従して
# 元PDFから再描画し、表示解像度を下回らないようにする。
# 上限は対象スキャンのネイティブ(≈2.78px/pt)を上回る 4.0(≈288dpi)に設定。
RENDER_MIN_SCALE = 1.0
RENDER_MAX_SCALE = 4.0
# 再描画頻度を抑えるための余裕係数(必要倍率を少し上回って描画)
RENDER_HEADROOM = 1.1
# 一括電子化(データ化)時のラスタライズ倍率。検出比率は正規化で倍率非依存だが、
# 自由記述OCRの精度のため本表示より高めに描く。
DIGITIZE_RENDER_SCALE = 3.0
# 縦横比のフォールバック(A4縦 595:841)。実ページ取得後に上書きする。
DEFAULT_PAGE_ASPECT = 595.0 / 841.0

# PDF 表示のフィットモード
FIT_WIDTH = "width"
FIT_HEIGHT = "height"

# 入力フォームの既定フォントサイズ(pt)と可変範囲
DEFAULT_FORM_FONT_PT = 14
MIN_FORM_FONT_PT = 8
MAX_FORM_FONT_PT = 28


class _ScreenAiSetupThread(QThread):
    """screen-ai コンポーネント取得をバックグラウンドで実行するワーカー。

    locro.download_component() は Chrome コピー/zip 展開/サーバDLを行い時間を
    要するため、UI スレッドを塞がないよう別スレッドで走らせ、結果を done で返す。
    """

    done = Signal(bool, str)  # (成功か, 成功時はパス / 失敗時はエラーメッセージ)

    def run(self) -> None:  # QThread のエントリ(別スレッドで実行)
        try:
            from enquete.ocr.screenai_backend import setup_component

            path = setup_component()
            self.done.emit(True, path)
        except Exception as exc:  # noqa: BLE001  取得失敗を UI へ伝える
            self.done.emit(False, str(exc))


def load_main_window() -> QMainWindow:
    loader = QUiLoader()
    window = loader.load(str(UI_PATH))
    assert isinstance(window, QMainWindow)

    # 領域比率: 左(サムネイル+PDF)=6 / 右(アンケート入力)=4。
    splitter = window.findChild(QSplitter, "splitter")
    if splitter is not None:
        splitter.setStretchFactor(0, 6)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([600, 400])

    # 左領域内: サムネイル列は控えめな初期幅、残りを PDF 表示に。
    # ウィンドウ拡大時は PDF 側が伸びるよう stretch を設定。
    left_splitter = window.findChild(QSplitter, "leftSplitter")
    if left_splitter is not None:
        left_splitter.setStretchFactor(0, 0)
        left_splitter.setStretchFactor(1, 1)
        left_splitter.setSizes([200, 560])

    return window


class MainWindowController(QObject):
    """ロジック担当。シグナル受信のため寿命を保つ必要があり、
    呼び出し側(__main__)が参照を保持する。"""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.doc: PdfDocument | None = None
        # サムネイル縦横比(幅/高さ)。ページ読み込み時に実値へ更新。
        self._thumb_aspect = DEFAULT_PAGE_ASPECT
        # PDF 表示のフィットモード(既定: 横幅いっぱい)
        self._fit_mode = FIT_WIDTH
        # フィット倍率に対するズーム係数(1.0=フィット)。これを保持することで
        # ウィンドウのリサイズ時も「フィット倍率 × 係数」で表示が連動する。
        self._zoom_factor = 1.0
        # 現在表示中のページと、そのラスタライズ倍率
        self._page_index = -1
        self._render_scale = 0.0
        # サムネイルの元ピクスマップ(確認済みハッチ合成の再生成用)
        self._thumb_base: list[QPixmap] = []
        self._base_title = "アンケート読み込み支援"
        # 全面オートセーブ: 校正・フォーム編集は記入済みPDFへ即時埋め込み保存する。
        # 自由記述はキー入力ごとに発火するため、短いデバウンスで書込を間引く。
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(500)
        self._autosave_timer.timeout.connect(self._save_store)
        # ページごとの傾き補正角(度・反時計回り正)。in-memory のみ。
        # 確定時は補正済みPDF(別ファイル)として書き出す(サイドカーには持たない)。
        self._skew: dict[int, float] = {}
        # 傾き補正モード(グリッド表示＋ドラッグ回転)の状態
        self._skew_mode = False
        self._skew_action: QAction | None = None
        self._page_item = None  # 表示中ページの QGraphicsPixmapItem
        self._grid_items: list[QGraphicsItem] = []
        self._skew_preview_deg = 0.0  # 現在のプレビュー回転(Qt時計回り正)
        self._skew_drag = False
        self._skew_angle0 = 0.0
        self._skew_base_deg = 0.0
        # 差分検出の基準＝別PDFで指定する原紙。開いた文書(記入済み)とは別扱い。
        self._reference_path: str | None = None
        self._reference_doc: PdfDocument | None = None
        # 差分基準(原紙)のグレースケールを (scale, ndarray) でキャッシュ
        self._clean_cache: tuple[float, object] | None = None
        # 操作フローのアクションバーのページ表示ラベル
        self._page_label: QLabel | None = None
        # 操作フローのアクションバーの使用モデル(OCRエンジン)切替ボタン
        self._model_btn: QToolButton | None = None
        # 操作フローのアクションバーの「確認済み」トグルボタン
        self._review_btn: QToolButton | None = None
        # 現在ページの「確認済み時点の結果」スナップショット(編集差分の検知・Esc復帰用)。
        # None=現在ページは未確認(復帰先なし)。
        self._confirmed_results: dict | None = None
        # ズーム処理中フラグ。スクロールバー方針変更が誘発する Resize で
        # _apply_view が割り込み、増分スケールと二重適用されるのを防ぐ。
        self._zooming = False
        # _apply_view 再入ガード。スクロールバー方針変更が同期 Resize を誘発し
        # _apply_view が自己再帰するのを防ぐ。
        self._applying = False
        # サムネイル幅調整の再入ガード(setIconSize が誘発する Resize 対策)。
        self._sizing_thumbs = False

        # 主要ウィジェット参照(.ui 由来。取得失敗は起動時に検出する)
        thumbnail_list = window.findChild(QListWidget, "thumbnailList")
        pdf_view = window.findChild(QGraphicsView, "pdfView")
        form_scroll = window.findChild(QScrollArea, "formScrollArea")
        assert thumbnail_list and pdf_view and form_scroll, (
            "main_window.ui の必須ウィジェットが見つかりません"
        )
        self.thumbnail_list = thumbnail_list
        self.pdf_view = pdf_view
        # スプリッタ参照(分割比の永続化に使用)
        self._splitter = window.findChild(QSplitter, "splitter")
        self._left_splitter = window.findChild(QSplitter, "leftSplitter")
        # PDF表示とフォーム(右ペイン)のスクロールを相互連動させる際の再帰防止。
        self._scroll_sync_guard = False

        # アンケート設問定義と入力フォーム(右ペインへ注入)
        # フォーム定義は内蔵せず、原紙PDFの指定(=フォーム作成)で与える。
        self._form_scroll = form_scroll
        self._default_survey: Survey | None = None  # 読み込み済みフォーム(新規PDFの既定)
        self._survey: Survey | None = None  # 現在アクティブなフォーム
        # 原紙でフォーム編集モード中の情報({"filled","genshi"})。None=非編集中。
        self._genshi_edit: dict | None = None
        self._digitize_btn: QPushButton | None = None  # ③電子化ボタン(モードで表示切替)
        self._struct_btn: QPushButton | None = None  # 構造編集ボタン(原紙モードで表示)
        self._vector_check: QCheckBox | None = None  # 原紙=電子PDF(原紙モードで表示)
        # 作業モード。True=校正モード(確認済み✓を保護) / False=自動認識(全上書き)。
        self._proof_mode = False
        self._mode_btn: QToolButton | None = None
        self._act_mode_auto: QAction | None = None
        self._act_mode_proof: QAction | None = None
        self._settings_btn: QPushButton | None = None  # 検出設定ボタン(校正で不活性)
        self._submit_btn: QPushButton | None = None  # 提出用JSON書き出し(全✓で有効)
        self._genshi_status_label: QLabel | None = None  # 原紙の読込状態(ステータスバー)
        # 起動時はPDF未読込のためフォームは出さず、プレースホルダのみ表示。
        self.form_pane: FormPane | None = None
        self._show_form_placeholder()
        # PDF表示と右ペイン(読み取り結果)のスクロールを相互連動させる。
        self.pdf_view.verticalScrollBar().valueChanged.connect(self._on_pdf_scrolled)
        self._form_scroll.verticalScrollBar().valueChanged.connect(
            self._on_form_scrolled
        )

        # サイドカー(校正結果の保存・再開)
        self._doc_store: SurveyDocument | None = None

        # 検出結果・ROIオーバーレイ・設定ダイアログの状態
        self._last_results: dict[str, QuestionResult] = {}
        # ROIオーバーレイは既定で表示(QSettings 未設定時の初期値)。下で設定から復元。
        self._overlay_enabled = True
        self._overlay_items: list[QGraphicsItem] = []
        # 直近にオーバーレイへ描いた選択状態(自由記述入力での無駄な再描画を抑止)
        self._overlay_selection_cache: dict[str, set[str]] = {}
        self._settings_dialog: DetectionSettingsDialog | None = None
        self._editor_dialog: FormEditorDialog | None = None
        self._highlight_item: QGraphicsItem | None = None
        # 自由記述の範囲指定(ラバーバンド)モードの状態
        # 自由記述の記入領域編集(ハンドル付き矩形)の状態
        self._region_edit_item: RegionEditItem | None = None
        self._region_edit_qid: str | None = None
        # フォームのフォントサイズ(pt)。既定14。表示メニューで変更・永続化。
        self._form_font_pt = DEFAULT_FORM_FONT_PT
        # OCRバックエンドの選好(auto/ocrmac/screenai)。ツールメニューで選択・永続化。
        self._ocr_pref = BACKEND_AUTO
        self._ocr_actions: dict[str, QAction] = {}
        # screen-ai コンポーネント取得(バックグラウンド)用のワーカー/進捗ダイアログ。
        self._screenai_thread: _ScreenAiSetupThread | None = None
        self._screenai_progress: QProgressDialog | None = None

        # ウィンドウ状態の保存先(組織名/アプリ名は __main__ で設定済み)。
        # 保存は __main__ が QApplication.aboutToQuit に save_window_state を
        # 接続して行う(破棄中の Close イベントを拾う方式より堅牢)。
        self._settings = QSettings()
        self._ocr_pref = str(self._settings.value("ocr/backend", BACKEND_AUTO))
        # ROIオーバーレイ表示は既定 ON。ユーザーが切り替えたら記憶する。
        self._overlay_enabled = bool(
            self._settings.value("overlay/enabled", True, type=bool)
        )
        # インク比率の数値表示は既定 OFF(検出チューニング時のみ。視認性のため)。
        self._overlay_ratios = bool(
            self._settings.value("overlay/ratios", False, type=bool)
        )

        # PDF 表示用シーン
        self._scene = QGraphicsScene(self)
        self.pdf_view.setScene(self._scene)
        # スムーズな縮小・拡大表示(ジャギー/モアレを抑制)
        self.pdf_view.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        # 表示倍率に追従した再ラスタライズをデバウンスするタイマ
        self._rerender_timer = QTimer(self)
        self._rerender_timer.setSingleShot(True)
        self._rerender_timer.setInterval(120)
        self._rerender_timer.timeout.connect(self._ensure_render_resolution)
        # 拡大中心をマウス位置にする(ホイール/ピンチ時の基準)
        self.pdf_view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.pdf_view.setResizeAnchor(
            QGraphicsView.ViewportAnchor.AnchorViewCenter
        )
        # リサイズ・ホイール・ネイティブピンチを拾うためのイベントフィルタ
        # (ホイール/リサイズは viewport、ネイティブジェスチャは view 本体へ届く)
        self.pdf_view.viewport().installEventFilter(self)
        self.pdf_view.installEventFilter(self)
        self.thumbnail_list.viewport().installEventFilter(self)
        # サムネイル領域への PDF ドラッグ&ドロップで開く
        self.thumbnail_list.installEventFilter(self)
        self.thumbnail_list.setAcceptDrops(True)
        self.thumbnail_list.viewport().setAcceptDrops(True)
        # Esc の一元処理: 傾き補正→原紙編集→確認済み編集の取消(復帰) の優先順で。
        # フォーカス位置によらず拾えるよう、ウィンドウのショートカットにする。
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.window)
        self._esc_shortcut.activated.connect(self._on_escape)

        self._connect()
        # 既定モード(横幅いっぱい)のスクロールバー方針を適用
        self.set_fit_mode(self._fit_mode)

    # ------------------------------------------------------------------ wiring
    def _connect(self) -> None:
        action_open = self.window.findChild(QAction, "actionOpenPdf")
        if action_open is not None:
            action_open.triggered.connect(self.open_pdf)

        action_quit = self.window.findChild(QAction, "actionQuit")
        if action_quit is not None:
            action_quit.triggered.connect(self.window.close)
        # 校正結果は全面オートセーブ(編集の都度サイドカーへ書込)のため、
        # 明示的な「結果を保存」操作・ショートカットは設けない。

        # 表示フィットモード(横幅 / 高さ)を排他選択にする
        self._action_fit_width = self.window.findChild(QAction, "actionFitWidth")
        self._action_fit_height = self.window.findChild(QAction, "actionFitHeight")
        if self._action_fit_width is not None and self._action_fit_height is not None:
            group = QActionGroup(self)
            group.setExclusive(True)
            group.addAction(self._action_fit_width)
            group.addAction(self._action_fit_height)
            self._action_fit_width.triggered.connect(
                lambda: self.set_fit_mode(FIT_WIDTH)
            )
            self._action_fit_height.triggered.connect(
                lambda: self.set_fit_mode(FIT_HEIGHT)
            )

        action_settings = self.window.findChild(QAction, "actionDetectionSettings")
        if action_settings is not None:
            action_settings.triggered.connect(self.open_detection_settings)

        # フォーム作成/読込/編集/保存のメニューは廃止。フォーム編集は「③電子化→
        # 原紙を指定」の原紙編集モード(構造編集ボタン＋右ペインの範囲指定)に集約する。
        for name in (
            "actionCreateForm", "actionLoadForm", "actionEditForm", "actionSaveForm",
        ):
            act = self.window.findChild(QAction, name)
            if act is not None:
                act.setVisible(False)

        self._build_ocr_menu()
        self._build_merge_menu()
        # 傾き補正は「電子化→原紙を指定」の位置補正(自動・水平化＋平行移動)に統合
        # したため、手動の傾き補正モード(トグル/ボタン)は設けない。
        self._build_action_bar()

        for name, delta in (("actionFontLarger", +1), ("actionFontSmaller", -1)):
            act = self.window.findChild(QAction, name)
            if act is not None:
                act.triggered.connect(lambda _=False, d=delta: self._adjust_form_font(d))
        act_reset = self.window.findChild(QAction, "actionFontReset")
        if act_reset is not None:
            act_reset.triggered.connect(
                lambda: self._set_form_font(DEFAULT_FORM_FONT_PT)
            )

        self.thumbnail_list.currentRowChanged.connect(self.show_page)

    # --------------------------------------------------------- OCRエンジン選択
    def _build_ocr_menu(self) -> None:
        """ツールメニューに「OCRエンジン」サブメニュー(排他選択)を構築する。"""
        tools = self.window.findChild(QMenu, "menuTools")
        if tools is None:
            return
        submenu = QMenu("OCRエンジン", tools)
        group = QActionGroup(self)
        group.setExclusive(True)
        for key, label, _available in backend_choices():
            act = QAction(label, self.window)
            act.setCheckable(True)
            act.setData(key)
            act.setChecked(key == self._ocr_pref)
            act.triggered.connect(lambda _=False, k=key: self._set_ocr_pref(k))
            group.addAction(act)
            submenu.addAction(act)
            self._ocr_actions[key] = act
        # メニューを開くたびに利用可否ラベルを更新(locro 導入後にも追従)
        submenu.aboutToShow.connect(self._refresh_ocr_menu)
        # 検出設定の前にサブメニューを差し込む
        tools.addSeparator()
        tools.addMenu(submenu)
        # screen-ai(Chrome の高精度OCR)の有効化アクション
        act_setup = QAction("screen-ai OCR を有効化（コンポーネント取得）…", self.window)
        act_setup.triggered.connect(self.enable_screenai)
        submenu.addSeparator()
        submenu.addAction(act_setup)

    def _refresh_ocr_menu(self) -> None:
        """各エンジンの利用可否をラベル末尾に反映する。"""
        for key, label, available in backend_choices():
            act = self._ocr_actions.get(key)
            if act is None:
                continue
            suffix = "" if available else "（この環境では利用不可）"
            act.setText(f"{label}{suffix}")

    def _set_ocr_pref(self, key: str) -> None:
        """OCRバックエンドの選好を更新・永続化する。"""
        self._ocr_pref = key
        self._settings.setValue("ocr/backend", key)
        self._settings.sync()
        act = self._ocr_actions.get(key)
        if act is not None:
            act.setChecked(True)
        self._update_model_label()
        backend = get_backend(key)
        sb = self.window.statusBar()
        if sb is not None:
            if backend is not None:
                sb.showMessage(f"OCRエンジン: {backend.name}", 4000)
            else:
                sb.showMessage(
                    "選択したOCRエンジンはこの環境では利用できません"
                    "（スキャンからのフォーム作成は不可。編集モードのみ）。",
                    6000,
                )

    def _current_backend(self):
        """現在の選好に基づく OCR バックエンド(無ければ None)。"""
        return get_backend(self._ocr_pref)

    def _update_model_label(self) -> None:
        """アクションバーの使用モデル表示を現在のOCRエンジンに合わせて更新する。"""
        if self._model_btn is None:
            return
        backend = self._current_backend()
        if backend is not None:
            self._model_btn.setText(f"モデル: {backend.name} ▾")
        else:
            self._model_btn.setText("モデル: 利用不可 ▾")

    # ----------------------------------------- screen-ai(Chrome高精度OCR)の有効化
    def enable_screenai(self) -> None:
        """screen-ai のコンポーネントを取得して OCR エンジンを screen-ai に切替。

        DLL/モデルは非再配布のため exe に同梱せず、利用者環境(Chrome/Dropbox/
        Google サーバ)から取得する。取得は時間を要するので別スレッドで実行する。
        """
        import importlib.util

        if importlib.util.find_spec("locro") is None:
            QMessageBox.information(
                self.window,
                "screen-ai OCR",
                "このビルドには screen-ai(locro)が含まれていないため有効化できません。",
            )
            return
        # 既に取得済みなら取得をスキップして切替のみ
        if get_backend(BACKEND_SCREENAI) is not None:
            self._set_ocr_pref(BACKEND_SCREENAI)
            self._refresh_ocr_menu()
            QMessageBox.information(
                self.window,
                "screen-ai OCR",
                "screen-ai は既に利用可能です。OCR エンジンを screen-ai に切り替えました。",
            )
            return
        if self._screenai_thread is not None:
            return  # 取得処理が進行中
        ret = QMessageBox.question(
            self.window,
            "screen-ai OCR を有効化",
            "Chrome の高精度 OCR コンポーネント(ライブラリ＋モデル)を取得します。\n\n"
            "・インストール済み Chrome があればそこからコピー\n"
            "・無ければ Dropbox の zip / Google サーバからの取得を試みます\n\n"
            "数十MB のダウンロードが発生する場合があります。続行しますか？",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        dlg = QProgressDialog(
            "screen-ai コンポーネントを取得中…\n(完了まで数分かかる場合があります)",
            "", 0, 0, self.window,
        )
        dlg.setWindowTitle("screen-ai OCR を有効化")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setCancelButton(None)  # 取得処理は中断できないため無効
        dlg.setMinimumDuration(0)
        self._center_dialog(dlg)
        dlg.show()
        self._screenai_progress = dlg
        thread = _ScreenAiSetupThread(self)
        thread.done.connect(self._on_screenai_setup_done)
        self._screenai_thread = thread
        thread.start()

    def _on_screenai_setup_done(self, ok: bool, info: str) -> None:
        """screen-ai 取得スレッドの完了処理(UI スレッドで実行される)。"""
        if self._screenai_progress is not None:
            self._screenai_progress.close()
            self._screenai_progress = None
        self._screenai_thread = None
        if ok:
            self._set_ocr_pref(BACKEND_SCREENAI)
            self._refresh_ocr_menu()
            QMessageBox.information(
                self.window,
                "screen-ai OCR",
                "有効化しました。以後、電子化の OCR に screen-ai を使用します。\n\n"
                f"コンポーネント: {info}",
            )
        else:
            QMessageBox.warning(
                self.window,
                "screen-ai OCR",
                "コンポーネントの取得に失敗しました。\n\n"
                f"詳細: {info}\n\n"
                "対処:\n"
                "・Google Chrome をインストールし、chrome://components で\n"
                "  「Optimization Guide On Device Model / Screen AI」を更新してから\n"
                "  再実行してください。",
            )

    # ------------------------------------------------- 操作フローのアクションバー
    def _build_action_bar(self) -> None:
        """最下段に作業順のボタンバー(4つ目の領域)を設置する。

        左→右が一連の作業: 開く → 電子化(原紙指定＝フォーム/位置補正) →
        校正(ページ送り/確認) → 分割/マージ。
        """
        bar = QWidget(self.window)
        bar.setObjectName("actionBar")
        bar.setStyleSheet(
            """
            #actionBar { background: #f4f6f9; border-top: 1px solid #d7dbe0; }
            #actionBar QPushButton, #actionBar QToolButton {
                background: #ffffff; border: 1px solid #c7ccd4; border-radius: 7px;
                padding: 6px 14px; color: #2b2f36; font-size: 16pt;
            }
            #actionBar QPushButton:hover, #actionBar QToolButton:hover {
                background: #eef3ff; border-color: #5b8def;
            }
            #actionBar QPushButton:pressed, #actionBar QToolButton:pressed {
                background: #dde7ff;
            }
            #actionBar QToolButton:checked {
                background: #1a7f37; border-color: #15692e; color: white; font-weight: bold;
            }
            #actionBar QPushButton:disabled { color: #aab0b8; background: #f0f1f3; }
            #actionBar QToolButton::menu-indicator { image: none; width: 0; }
            #actionBar QFrame { color: #d7dbe0; }
            #actionBar QLabel { color: #6b7280; }
            """
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(8)

        def btn(text: str, slot, tip: str = "") -> QPushButton:
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        def sep() -> QFrame:
            f = QFrame()
            f.setFrameShape(QFrame.Shape.VLine)
            f.setFrameShadow(QFrame.Shadow.Sunken)
            return f

        h.addStretch(1)  # 右寄せ
        # 1) 開く
        h.addWidget(btn("開く", self.open_pdf, "PDFを開く（ドラッグ&ドロップも可）"))
        # 開くの次に、使用中のモデル(OCRエンジン)を表示・切替するドロップダウン
        model_btn = QToolButton()
        model_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        model_btn.setToolTip("電子化に使用するOCRエンジンを切り替えます")
        mmenu = QMenu(model_btn)
        # ツールメニューと同じ QAction を共有して選択状態を同期させる
        for key, _label, _available in backend_choices():
            act = self._ocr_actions.get(key)
            if act is not None:
                mmenu.addAction(act)
        mmenu.aboutToShow.connect(self._refresh_ocr_menu)
        model_btn.setMenu(mmenu)
        model_btn.setEnabled(not mmenu.isEmpty())
        self._model_btn = model_btn
        h.addWidget(model_btn)
        self._update_model_label()
        # フォーム編集は「③電子化→原紙を指定」の原紙編集モードに集約。構造編集
        # (設問・選択肢)ボタンはそのモード中だけ表示する。
        self._struct_btn = btn(
            "設問・選択肢を編集…", self.open_form_editor,
            "原紙のフォーム構造（設問・選択肢・種別）を編集します",
        )
        self._struct_btn.setVisible(False)
        h.addWidget(self._struct_btn)
        # 原紙=電子データ出力PDF(スキャンでない)か。位置補正で原紙の水平化を省く。
        self._vector_check = QCheckBox("原紙は電子PDF")
        self._vector_check.setToolTip(
            "原紙がスキャンではなく電子データから出力したPDFのとき ON。"
            "位置補正で原紙の水平化を省き、原紙を厳密な基準にします（自動判定が初期値）。"
        )
        self._vector_check.setVisible(False)
        self._vector_check.toggled.connect(self._on_vector_check_toggled)
        h.addWidget(self._vector_check)
        h.addWidget(sep())
        # 作業モード: 自動認識(全上書き) / 校正(確認済み✓ページを保護)。サイドカー
        # がある(電子化済み)PDFを開くと校正モードを既定にする。
        mode_btn = QToolButton()
        mode_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        mmode = QMenu(mode_btn)
        self._act_mode_auto = mmode.addAction(
            "自動認識モード（全ページ上書き）",
            lambda: self._set_proof_mode(False),
        )
        self._act_mode_proof = mmode.addAction(
            "校正モード（確認済み✓を保護）",
            lambda: self._set_proof_mode(True),
        )
        for a in (self._act_mode_auto, self._act_mode_proof):
            a.setCheckable(True)
        mode_btn.setMenu(mmode)
        mode_btn.setToolTip(
            "電子化・再電子化の対象。自動認識=全ページ上書き / "
            "校正=確認済み(✓)ページを上書きしない"
        )
        self._mode_btn = mode_btn
        h.addWidget(mode_btn)
        # 電子化（原紙編集モード中は「✓ この原紙で電子化」に表示が変わる）
        self._digitize_btn = btn("電子化", self.run_digitize_all, "全ページを一括電子化")
        h.addWidget(self._digitize_btn)
        # 検出設定（閾値・判定範囲などの調整／「適用（再電子化）」もここから）
        self._settings_btn = btn(
            "検出設定", self.open_detection_settings,
            "チェック検出の閾値・判定範囲などを調整（適用で全ページ再電子化）",
        )
        h.addWidget(self._settings_btn)
        # 既定モードを反映(校正モードなら電子化・検出設定を不活性)。PDFを開くと
        # サイドカー有無で切り替わる。ボタン生成後に呼ぶ必要がある。
        self._set_proof_mode(False)
        h.addWidget(sep())
        # 5) 校正: ページ送り＋確認済みトグル(保存は全面オートセーブ)
        h.addWidget(btn("◀", self._go_prev_page, "前のページ"))
        self._page_label = QLabel("—")
        self._page_label.setMinimumWidth(64)
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._page_label)
        h.addWidget(btn("▶", self._go_next_page, "次のページ"))
        # このページを確認済みにする(トグル)。フォームのチェックと同期。
        review_btn = QToolButton()
        review_btn.setCheckable(True)
        review_btn.setText("✓ 確認済みにする")
        review_btn.setToolTip("このページを確認済みにする（もう一度押すと解除）")
        review_btn.setEnabled(False)
        review_btn.toggled.connect(self._on_review_btn_toggled)
        self._review_btn = review_btn
        h.addWidget(review_btn)
        h.addWidget(sep())
        # 提出用JSONの書き出し。全ページを確認済みにすると有効になる。
        self._submit_btn = btn(
            "提出用に書き出す", self.export_submission_json,
            "全ページを確認済みにすると有効。読み取り結果を提出用 JSON に書き出します。",
        )
        self._submit_btn.setEnabled(False)
        h.addWidget(self._submit_btn)
        # 校正結果は全面オートセーブのため、明示保存ボタンは置かない。
        # 分割/マージは頻度が低いため、バーには出さずツールメニューに残す

        # 中央ウィジェットを「上=既存スプリッタ / 下=アクションバー」の縦並びに再構成
        central = self.window.centralWidget()
        if central is not None and self._splitter is not None:
            old = central.layout()
            if old is not None:
                old.removeWidget(self._splitter)
                QWidget().setLayout(old)  # 旧レイアウトを central から切り離す
            v = QVBoxLayout(central)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            v.addWidget(self._splitter, 1)
            v.addWidget(bar, 0)

    def _go_prev_page(self) -> None:
        r = self.thumbnail_list.currentRow()
        if r > 0:
            self.thumbnail_list.setCurrentRow(r - 1)

    def _go_next_page(self) -> None:
        r = self.thumbnail_list.currentRow()
        if 0 <= r < self.thumbnail_list.count() - 1:
            self.thumbnail_list.setCurrentRow(r + 1)

    def _update_page_label(self) -> None:
        if self._page_label is None:
            return
        if self.doc is not None and self._page_index >= 0:
            self._page_label.setText(f"P {self._page_index + 1}/{len(self.doc)}")
        else:
            self._page_label.setText("—")

    # ----------------------------------------------------------- 傾き補正
    def _build_deskew_toolbar(self) -> None:
        """傾き補正モードのトグル用 QAction だけを用意する。

        操作はすべて下段アクションバーの「傾き補正モード」ボタンから行うため、
        上部ツールバーは設置しない(モードへ入る/保存して戻るの2状態で完結)。
        """
        act_mode = QAction("傾き補正モード", self.window)
        act_mode.setCheckable(True)
        act_mode.toggled.connect(self._toggle_skew_mode)
        self._skew_action = act_mode

    def _rotate_qimage(self, image: QImage, deg: float) -> QImage:
        """QImage を中心まわりに deg(反時計回り正)回転(同サイズ・白背景)。"""
        if not deg:
            return image
        w, h = image.width(), image.height()
        out = QImage(w, h, QImage.Format.Format_RGB888)
        out.fill(Qt.GlobalColor.white)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(w / 2.0, h / 2.0)
        painter.rotate(-deg)  # 補正角は反時計回り正、QPainter は時計回り正
        painter.translate(-w / 2.0, -h / 2.0)
        painter.drawImage(0, 0, image)
        painter.end()
        return out

    def _render_image(self, index: int, scale: float) -> QImage:
        """ページを描画し、そのページの傾き補正角を適用した QImage を返す。

        検出・OCR・通常表示で使う(常に補正を焼き込む)。傾き補正モードのプレビュー
        だけは生画像＋アイテム回転で行うため、_render_page 側で分岐する。
        """
        assert self.doc is not None
        image = self.doc.render(index, scale=scale)
        return self._rotate_qimage(image, self._skew.get(index, 0.0))

    # ----------------------------------------------------------- 差分検出
    def _has_baseline(self) -> bool:
        """差分の基準(原紙)が使えるか。原紙セッション中の原紙PDF、または
        PDFに埋め込まれた基準画像のどちらか。"""
        return self._reference_doc is not None or (
            self._doc_store is not None and self._doc_store.baseline_png is not None
        )

    def _use_diff_for(self, index: int) -> bool:
        """このページで差分検出を使うか。

        差分の基準は原紙。原紙PDF(指定セッション中)か、記入済みPDFに埋め込まれた
        基準画像があり、設定が差分ONなら、全ページを原紙との差分で判定する。
        """
        return (
            self._survey is not None
            and self._survey.detection.use_diff
            and self._has_baseline()
            and self.doc is not None
        )

    def _clean_gray(self, scale: float):
        """差分の基準(原紙)をグレースケールで返す(scale毎にキャッシュ)。

        原紙セッション中は原紙PDFの1ページ目を直接レンダリング。それ以外は記入済み
        PDFに埋め込まれた基準画像を、現ページのレンダリング寸法へ合わせて復元する。
        """
        if self._clean_cache is not None and abs(self._clean_cache[0] - scale) < 1e-9:
            return self._clean_cache[1]
        gray = None
        if self._reference_doc is not None:
            gray = qimage_to_gray(self._reference_doc.render(0, scale=scale))
        elif (
            self._doc_store is not None
            and self._doc_store.baseline_png is not None
            and self.doc is not None
        ):
            # 埋め込み基準画像(PNG)をページ0のレンダリング寸法へ合わせて復元。
            base = QImage()
            base.loadFromData(QByteArray(self._doc_store.baseline_png), "PNG")
            if not base.isNull():
                ref = self.doc.render(0, scale=scale)
                base = base.scaled(
                    ref.width(), ref.height(),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                gray = qimage_to_gray(base)
        if gray is None:
            return None
        self._clean_cache = (scale, gray)
        return gray

    def _render_reference_baseline_png(self) -> bytes | None:
        """原紙PDF(差分基準)の1ページ目をPNGバイト列にして返す(埋め込み用)。"""
        if self._reference_doc is None:
            return None
        img = self._reference_doc.render(0, scale=DIGITIZE_RENDER_SCALE)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return ba.data()

    # --- 傾き補正モード(グリッド＋ドラッグ回転) -------------------------------
    def _toggle_skew_mode(self, on: bool) -> None:
        if on and self.doc is not None and self._page_index >= 0:
            self._skew_mode = True
            self.pdf_view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.pdf_view.setFocus()  # Esc キャンセルを受け取れるよう明示フォーカス
            # モード中はボタンを「保存して戻る」として機能させる
            if self._skew_action is not None:
                self._skew_action.setText("傾き補正を保存して戻る")
            self._render_page(self._desired_render_scale())
            self._apply_view()
            sb = self.window.statusBar()
            if sb is not None:
                sb.showMessage(
                    "傾き補正モード: グリッドに合わせて画面をドラッグで回転。"
                    "もう一度押すと補正を保存（PDF上書き）して戻ります。"
                    "Esc で破棄してキャンセル。",
                    0,
                )
        else:
            self._skew_mode = False
            self._skew_drag = False
            self.pdf_view.viewport().unsetCursor()
            self._clear_skew_grid()
            if self._skew_action is not None:
                self._skew_action.setText("傾き補正モード")
            # モードから戻る＝保存: 補正角があれば PDF を上書き保存(原本は .bak へ)。
            # export_deskewed_pdf 内で PDF を開き直すため、通常描画はそちらに委ねる。
            if any(abs(a) >= 0.05 for a in self._skew.values()):
                self.export_deskewed_pdf()
                return
            self._render_page(self._desired_render_scale())
            self._apply_view()
            sb = self.window.statusBar()
            if sb is not None:
                sb.clearMessage()

    def _reset_current_skew(self) -> None:
        if self.doc is None or self._page_index < 0:
            return
        self._skew[self._page_index] = 0.0
        if self._skew_mode and self._page_item is not None:
            self._apply_skew_transform(0.0)
        else:
            self._render_page(self._desired_render_scale())
            self._apply_view()

    def _cancel_skew_mode(self) -> None:
        """傾き補正モードを保存せずに抜ける(Esc)。保留中の補正角は全て破棄。

        補正角を空にしてから抜けるため、退出時の自動保存(上書き)は走らない。
        """
        if not self._skew_mode:
            return
        self._skew = {}
        self._skew_drag = False
        if self._skew_action is not None:
            # setChecked(False) → _toggle_skew_mode(False)。補正角ゼロなので保存されない。
            self._skew_action.setChecked(False)
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage("傾き補正をキャンセルしました（保存していません）。", 4000)

    def _clear_skew_grid(self) -> None:
        for it in self._grid_items:
            if it.scene() is self._scene:
                self._scene.removeItem(it)
        self._grid_items = []

    def _draw_skew_grid(self) -> None:
        """正立判断用のグリッド(水平/垂直線)をシーンに描く(回転しない固定基準)。"""
        self._clear_skew_grid()
        sr = self._scene.sceneRect()
        w, h = sr.width(), sr.height()
        if w <= 0 or h <= 0:
            return
        pen = QPen(QColor(220, 60, 60, 150))
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        cols, rows = 12, 16
        for i in range(1, cols):
            x = w * i / cols
            line = self._scene.addLine(x, 0, x, h, pen)
            line.setZValue(30)
            self._grid_items.append(line)
        for j in range(1, rows):
            y = h * j / rows
            line = self._scene.addLine(0, y, w, y, pen)
            line.setZValue(30)
            self._grid_items.append(line)

    def _apply_skew_transform(self, deg: float) -> None:
        """表示中ページアイテムを中心回転(deg=Qt時計回り正)＋px→pt縮尺で配置。"""
        if self._page_item is None or self._render_scale <= 0:
            return
        pm = self._page_item.pixmap()
        s = 1.0 / self._render_scale
        cx, cy = pm.width() / 2.0, pm.height() / 2.0
        t = QTransform()
        t.scale(s, s)
        t.translate(cx, cy)
        t.rotate(deg)
        t.translate(-cx, -cy)
        self._page_item.setTransform(t)
        self._skew_preview_deg = deg
        # 注意: ここでオーバーレイは回さない。_render_page では scene.clear() 直後で
        # _overlay_items に破棄済み参照が残る恐れがあるため。オーバーレイ/ハイライトは
        # 再構築後に各所(_refresh_overlay/_highlight_rect/ドラッグ処理)で回転させる。
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage(f"傾き補正角: {(-deg):+.1f}°（ドラッグで調整／PDF書き出しで反映）", 0)

    def _apply_overlay_skew(self, deg: float) -> None:
        """ROIオーバーレイ・ハイライト枠をシーン中心まわりに deg 回転し、ページに追随。

        ItemIgnoresTransformations を持つ要素(比率ラベル等)は画面整列のまま回さない。
        """
        c = self._scene.sceneRect().center()
        t = QTransform()
        t.translate(c.x(), c.y())
        t.rotate(deg)
        t.translate(-c.x(), -c.y())
        ignore = QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        items = list(self._overlay_items)
        if self._highlight_item is not None:
            items.append(self._highlight_item)
        for it in items:
            if it.flags() & ignore:
                continue
            it.setTransform(t)

    def _mouse_angle(self, event: QMouseEvent) -> float:
        """ビューポート上のマウス位置とページ中心がなす角(度)。"""
        scene_pt = self.pdf_view.mapToScene(event.position().toPoint())
        c = self._scene.sceneRect().center()
        return math.degrees(
            math.atan2(scene_pt.y() - c.y(), scene_pt.x() - c.x())
        )

    @staticmethod
    def _norm_angle(deg: float) -> float:
        while deg > 180.0:
            deg -= 360.0
        while deg <= -180.0:
            deg += 360.0
        return deg

    def export_deskewed_pdf(self) -> None:
        """各ページを補正角で正立化し、元PDFを上書きする(原本は .bak.pdf へ退避)。

        確認ダイアログは出さない(原本退避があるため)。上書きは一時ファイルへ
        書いてから原本を置換する(読み書き衝突・ファイルロックを避ける)。
        """
        if self.doc is None:
            QMessageBox.information(self.window, "傾き補正", "PDFが開かれていません。")
            return
        if not any(abs(a) >= 0.05 for a in self._skew.values()):
            QMessageBox.information(
                self.window,
                "傾き補正",
                "補正する傾きが設定されていません。\n"
                "「傾き補正モード」でグリッドに合わせて回転してから実行してください。",
            )
            return
        src = Path(self.doc.path)
        bak = src.with_name(f"{src.stem}.bak.pdf")
        tmp = src.with_name(f"{src.stem}.deskew_tmp.pdf")
        # 現在の校正を確定保存(サイドカーは同名のまま維持される)
        self._capture_current_page()
        self._save_store()

        total = len(self.doc)
        progress = QProgressDialog(
            "傾き補正して上書き中…", "中断", 0, total, self.window
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        def on_page(done: int, n: int) -> None:
            progress.setValue(done)

        try:
            write_deskewed_pdf(
                src, dict(self._skew), tmp, render_scale=3.0, progress=on_page
            )
        except (OSError, ValueError) as e:
            progress.setValue(total)
            tmp.unlink(missing_ok=True)
            QMessageBox.critical(self.window, "傾き補正", f"書き出しに失敗しました:\n{e}")
            return
        progress.setValue(total)

        # 原本を .bak へ退避(初回のみ。既存 .bak は最古の原本として保持)
        backed = False
        if not bak.exists():
            try:
                shutil.copy2(src, bak)
                backed = True
            except OSError as e:
                tmp.unlink(missing_ok=True)
                QMessageBox.critical(
                    self.window, "傾き補正", f"バックアップに失敗しました:\n{e}"
                )
                return

        # 開いている文書を閉じてから原本を置換し、開き直す(サイドカーは同名で保持)
        page_index = self._page_index
        self.doc.close()
        self.doc = None
        try:
            os.replace(tmp, src)
        except OSError as e:
            QMessageBox.critical(self.window, "傾き補正", f"上書きに失敗しました:\n{e}")
            self.load_pdf(str(src))  # 元の状態で開き直す
            return
        self.load_pdf(str(src))
        if self.doc is not None and 0 <= page_index < len(self.doc):
            self.thumbnail_list.setCurrentRow(page_index)
        sb = self.window.statusBar()
        if sb is not None:
            note = (
                f"（原本を {bak.name} に退避）"
                if backed
                else f"（退避 {bak.name} は既存を保持）"
            )
            sb.showMessage(f"傾き補正で「{src.name}」を上書きしました{note}", 6000)

    # ----------------------------------------------------------- 分割/マージ
    def _build_merge_menu(self) -> None:
        """ツールメニューに「分割」「マージ」アクションを追加する。"""
        tools = self.window.findChild(QMenu, "menuTools")
        if tools is None:
            return
        tools.addSeparator()
        # 原紙の指定は「③電子化」の中で尋ねる(独立メニューは設けない)。
        act_digitize = QAction("アンケートを電子化（一括）…", self.window)
        act_digitize.triggered.connect(self.run_digitize_all)
        tools.addAction(act_digitize)
        tools.addSeparator()
        act_split = QAction("アンケートを分割…", self.window)
        act_split.triggered.connect(self.open_split_dialog)
        act_merge = QAction("アンケートをマージ…", self.window)
        act_merge.triggered.connect(self.open_merge_dialog)
        tools.addAction(act_split)
        tools.addAction(act_merge)

    def _center_dialog(self, dlg) -> None:
        """子ダイアログをメインウィンドウの中心に配置する(表示直前に呼ぶ)。"""
        dlg.adjustSize()
        geo = dlg.frameGeometry()
        geo.moveCenter(self.window.frameGeometry().center())
        dlg.move(geo.topLeft())

    # ----------------------------------------------------------- 一括電子化
    def _ask_digitize_options(
        self, total: int, backend
    ) -> tuple[bool, bool] | None:
        """電子化の実行前確認ダイアログ。(確認済みも対象, 上書き) を返す。"""
        dlg = QDialog(self.window)
        dlg.setWindowTitle("アンケートを電子化")
        engine = backend.name if backend is not None else "（OCR利用不可）"
        info = QLabel(
            f"全 {total} ページを電子化します。\n"
            f"選択肢は画像解析、自由記述は OCR（{engine}）で読み取ります。\n"
            + (
                ""
                if backend is not None
                else "※OCRが使えないため、自由記述は読み取らず選択肢のみ処理します。\n"
            )
        )
        info.setWordWrap(True)
        cb_reviewed = QCheckBox("確認済みのページも対象にする")
        # 校正モードは確認済み✓を保護する方針なので既定でOFF、自動認識はON。
        cb_reviewed.setChecked(not self._proof_mode)
        cb_overwrite = QCheckBox("既存の入力も上書きする（既定は空欄のみ補完）")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setText("電子化を実行")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addWidget(info)
        lay.addWidget(cb_reviewed)
        lay.addWidget(cb_overwrite)
        lay.addWidget(buttons)
        self._center_dialog(dlg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return cb_reviewed.isChecked(), cb_overwrite.isChecked()

    def run_digitize_all(self) -> None:
        """「電子化」ボタンの本体。状況に応じて自動で動作を切り替える。

        - 原紙フォーム編集中: 「✓ この原紙で電子化」＝編集を確定して読み取り。
        - 原紙が未指定(フォーム無し): 原紙PDFを指定→編集モードへ。
        - 原紙が指定済み(フォームあり): 検出設定の「適用（再電子化）」と同じ＝全ページを
          現在の検出設定・差分基準で再検出する(原紙の再指定は尋ねない)。
        """
        # 原紙フォーム編集中に押された場合 = 「確定して電子化」。
        if self._genshi_edit is not None:
            self._finish_genshi_edit()
            return
        if self.doc is None:
            QMessageBox.information(
                self.window, "電子化",
                "電子化するには、まず「開く」で記入済みアンケートPDFを開いてください。",
            )
            return
        # 原紙指定済み(フォームあり) → 再検出(検出設定の「適用」と同じ挙動)。
        if self._survey is not None and self._doc_store is not None:
            self.reapply_detection()
            return
        # 原紙が未指定 → 原紙PDFを指定して編集モードへ。
        path = self._pick_reference_pdf()
        if path is None:
            return
        survey = self._obtain_reference_survey(path)
        if survey is None:
            return
        self._enter_genshi_edit(str(self.doc.path), path, survey)

    def _pick_reference_pdf(self) -> str | None:
        """原紙PDFを選ぶ。複数ページなら警告して選び直しを促す(原紙は1ページ前提)。"""
        start = self._reference_path or str(Path.cwd())
        while True:
            path, _ = QFileDialog.getOpenFileName(
                self.window, "原紙（クリーンなアンケート用紙・1ページ）PDFを選択",
                start, "PDF Files (*.pdf *.PDF)",
            )
            if not path:
                return None
            try:
                doc = PdfDocument(path)
                n = len(doc)
                doc.close()
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self.window, "原紙を指定", f"PDFを開けませんでした:\n{e}")
                start = str(Path(path).parent)
                continue
            if n != 1:
                QMessageBox.warning(
                    self.window, "原紙を指定",
                    f"原紙は1ページのPDFを指定してください（選択したPDFは {n} ページあります）。\n"
                    "記入済みアンケートやページ数の多いPDFを誤って選んでいないかご確認ください。",
                )
                start = str(Path(path).parent)
                continue
            return path

    def _digitize_now(self) -> None:
        """現在のフォーム・差分基準で全ページを一括電子化し、PDFへ埋め込み保存する。"""
        if self.doc is None or self._survey is None or self._doc_store is None:
            return
        opts = self._ask_digitize_options(len(self.doc), self._current_backend())
        if opts is None:
            return
        self._run_digitize(*opts)

    def reapply_detection(self) -> None:
        """検出設定だけ変えて再電子化する(原紙再指定なし・既存結果は上書き)。

        検出設定ダイアログの「適用（再電子化）」から呼ぶ。確認ダイアログは出さない。
        校正モードでは確認済み(✓)ページをスキップして手校正を守り、自動認識モードでは
        確認済みも含め全ページを上書きする。
        """
        if self.doc is None or self._survey is None or self._doc_store is None:
            QMessageBox.information(
                self.window, "再電子化",
                "再電子化するには、PDFを開いて電子化（原紙指定）を済ませてください。",
            )
            return
        self._run_digitize(include_reviewed=not self._proof_mode, overwrite=True)

    def _run_digitize(self, include_reviewed: bool, overwrite: bool) -> None:
        """全ページを現在のフォーム・差分基準・検出設定で電子化し保存する(共通処理)。"""
        if self.doc is None or self._survey is None or self._doc_store is None:
            return
        total = len(self.doc)
        backend = self._current_backend()

        # 編集中の現ページを確定保存してから一括処理に入る
        self._capture_current_page()

        progress = QProgressDialog("電子化中…", "中断", 0, total, self.window)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        processed = skipped = 0
        # 差分の基準は別指定の原紙PDF。指定があれば全ページを原紙との差分で判定。
        use_diff = self._use_diff_for(0)
        clean = self._clean_gray(DIGITIZE_RENDER_SCALE) if use_diff else None
        for index in range(total):
            progress.setValue(index)
            if progress.wasCanceled():
                break
            if not include_reviewed and self._doc_store.is_reviewed(index):
                skipped += 1
                continue
            image = self._render_image(index, DIGITIZE_RENDER_SCALE)
            new = digitize_page(
                image, self._survey, backend, clean=clean,
            )
            existing = self._doc_store.get_results(index) or {}
            merged = merge_results(existing, new, overwrite=overwrite)
            self._doc_store.set_results(index, merged)
            processed += 1
        progress.setValue(total)

        self._save_store()
        # 現在表示中ページのフォームを保存結果で再表示
        if self._page_index >= 0:
            self._load_page_into_form(self._page_index)
        QMessageBox.information(
            self.window,
            "電子化完了",
            f"{processed} ページを電子化しました"
            + (f"（確認済み {skipped} ページはスキップ）" if skipped else "")
            + f"。\n結果は PDF（{self._doc_store.pdf_path.name}）に埋め込み保存しました。",
        )

    def _obtain_reference_survey(self, genshi_path: str) -> Survey | None:
        """原紙のフォーム定義(初期値)を得る。編集はこの後の原紙表示モードで行う。

        原紙PDFに埋め込み定義があればそれを返す。無ければ原紙を読み取って生成する。
        原紙が読み取れず OCR も使えない場合は、空フォームを返して手編集に委ねる。
        """
        # 原紙が電子データ出力PDF(テキスト層あり=スキャンでない)かを自動判定し、
        # 位置補正での原紙水平化スキップの初期値にする(原紙編集モードで上書き可)。
        is_vector = has_text_layer(genshi_path)
        store = SurveyDocument.load(genshi_path, None)  # 埋め込み定義があれば読む
        if store.survey is not None:
            return store.survey  # 既存定義の reference_is_vector を尊重(上書きしない)
        survey = self._build_survey_from(genshi_path)
        if survey is not None:
            survey.detection.reference_is_vector = is_vector
            return survey
        # 自動生成できない: 空フォームから手で組み立ててもらう。
        QMessageBox.information(
            self.window, "原紙の読み取り",
            "原紙からフォームを自動作成できませんでした"
            "（スキャン画像で OCR が使えない等）。\n"
            "空のフォームを開きます。「設問・選択肢を編集…」で設問・選択肢を"
            "追加し、自由記述は範囲指定してください。",
        )
        det = CheckboxDetection()
        det.reference_is_vector = is_vector
        return Survey(
            survey_id=Path(genshi_path).stem,
            title=Path(genshi_path).stem,
            questions=[],
            detection=det,
            source_pdf=Path(genshi_path).name,
        )

    def _enter_genshi_edit(
        self, filled_path: str, genshi_path: str, survey: Survey
    ) -> None:
        """原紙PDFを表示し、構造編集＋自由記述の範囲指定を行う編集モードへ入る。

        確定は「✓ この原紙で電子化」(=③電子化ボタン)、取消は Esc。原紙には回答が
        無いためサイドカーは作らない(_doc_store=None)。差分基準は別インスタンスで保持。
        """
        # 差分基準(原紙)を設定。表示用 self.doc とは別インスタンスにする。
        if self._reference_doc is not None:
            self._reference_doc.close()
        self._reference_doc = PdfDocument(genshi_path)
        self._reference_path = genshi_path
        self._clean_cache = None
        survey.detection.use_diff = True
        self._genshi_edit = {"filled": filled_path, "genshi": genshi_path}

        if self.doc is not None:
            self.doc.close()
        self.doc = PdfDocument(genshi_path)
        self._doc_store = None
        self._survey = survey
        self._default_survey = survey
        self._page_index = -1
        self._skew = {}
        if self._skew_action is not None and self._skew_action.isChecked():
            self._skew_action.setChecked(False)
        self._skew_mode = False
        if self._settings_dialog is not None:
            self._settings_dialog.close()
            self._settings_dialog = None
        self._install_form_pane(survey)
        self._set_digitize_mode(True)
        # 「原紙は電子PDF」チェックを現在の判定値で初期化(toggleハンドラは抑止)。
        if self._vector_check is not None:
            self._vector_check.blockSignals(True)
            self._vector_check.setChecked(survey.detection.reference_is_vector)
            self._vector_check.blockSignals(False)
        self._base_title = (
            f"アンケート読み込み支援 — {Path(genshi_path).name}（原紙でフォーム編集）"
        )
        self._update_title()
        self._build_thumbnails()
        if len(self.doc) > 0:
            self.thumbnail_list.setCurrentRow(0)
        QMessageBox.information(
            self.window, "原紙でフォームを編集",
            "原紙を表示しました。フォームを確認・修正してください。\n\n"
            "・設問／選択肢の構造: 「② フォーム ▾ → フォーム編集…」\n"
            "・自由記述の記入範囲: 右ペインの各自由記述欄の「範囲指定」ボタンを押し、"
            "原紙の上でドラッグして指定\n\n"
            "編集が済んだら「✓ この原紙で電子化」を押すと、原紙フォームを保存して"
            "記入済みPDFの読み取りを開始します（Esc で取消）。",
        )

    def _finish_genshi_edit(self) -> None:
        """原紙フォーム編集を確定: 位置補正→記入済みPDFを電子化し、結果＋原紙基準画像を
        記入済みPDFへ埋め込む(外部に原紙.json等は作らない)。"""
        info = self._genshi_edit
        if info is None:
            return
        self._finish_region_edit()
        survey = self._survey
        genshi_path = info["genshi"]
        filled_path = info["filled"]
        # 原紙基準画像(差分検出・再電子化用)を、原紙PDFを閉じる前に取得しておく。
        baseline_png = self._render_reference_baseline_png()
        self._genshi_edit = None
        self._set_digitize_mode(False)
        # 原紙編集表示を閉じてから、記入済みPDFを原紙基準で位置補正(上書き・.bak退避)。
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        level_ref = not (survey is not None and survey.detection.reference_is_vector)
        self._align_filled_to_reference(filled_path, genshi_path, level_ref)
        # 記入済み(補正後)を開き、原紙フォーム＋差分基準を適用してから電子化。
        self.load_pdf(filled_path)
        if survey is not None and self.doc is not None:
            survey.detection.use_diff = True
            self._apply_survey_to_doc(survey)
            if self._doc_store is not None:
                # 原紙基準画像を埋め込み対象に。以後この記入済みPDFだけで再電子化できる。
                self._doc_store.set_baseline(baseline_png)
        self._digitize_now()

    def _align_filled_to_reference(
        self, filled_path: str, genshi_path: str, level_reference: bool = True
    ) -> None:
        """記入済みPDFを原紙基準で位置補正(水平化＋平行移動)して上書きする。

        各ページを Hough で絶対水平へ正立化し、原紙との位相相関で平行移動して原紙と
        同位置へ揃える。level_reference=False(原紙が電子PDF)なら原紙は水平化せず厳密な
        基準として使う。元PDFは初回のみ .bak.pdf へ退避。失敗しても電子化は続行する。
        """
        src = Path(filled_path)
        bak = src.with_name(f"{src.stem}.bak.pdf")
        tmp = src.with_name(f"{src.stem}.align_tmp.pdf")
        progress = QProgressDialog(
            "原紙に合わせて位置補正中…", None, 0, 0, self.window
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        def on_page(done: int, n: int) -> None:
            if progress.maximum() != n:
                progress.setMaximum(n)
            progress.setValue(done)

        try:
            report = write_aligned_pdf(
                src, genshi_path, tmp, render_scale=3.0,
                level_reference=level_reference, progress=on_page,
            )
        except (OSError, ValueError, RuntimeError) as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            progress.close()
            QMessageBox.warning(
                self.window, "位置補正",
                f"位置補正に失敗しました:\n{e}\n補正せずに電子化します。",
            )
            return
        progress.close()
        if not bak.exists():
            try:
                shutil.copy2(src, bak)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                QMessageBox.warning(
                    self.window, "位置補正",
                    f"バックアップに失敗しました:\n{e}\n補正せずに電子化します。",
                )
                return
        try:
            os.replace(tmp, src)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            QMessageBox.warning(self.window, "位置補正", f"上書きに失敗しました:\n{e}")
            return
        moved = sum(
            1 for r in report if r.get("dx") or r.get("dy") or r.get("deg")
        )
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage(
                f"位置補正: 全{len(report)}ページ中 {moved}ページを補正"
                f"（原本は {bak.name} に退避）",
                6000,
            )

    def _cancel_genshi_edit(self) -> None:
        """原紙フォーム編集を保存せず取消し、記入済みPDFへ戻る(Esc)。"""
        info = self._genshi_edit
        if info is None:
            return
        self._finish_region_edit()
        filled_path = info["filled"]
        self._genshi_edit = None
        self._set_digitize_mode(False)
        self.load_pdf(filled_path)
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage("原紙フォームの編集をキャンセルしました（電子化していません）。", 5000)

    def _on_vector_check_toggled(self, checked: bool) -> None:
        """「原紙は電子PDF」チェックの操作 → 現在のフォーム定義へ反映。"""
        if self._survey is not None:
            self._survey.detection.reference_is_vector = checked

    def _set_proof_mode(self, proof: bool) -> None:
        """作業モードを設定(True=校正/False=自動認識)し、UIを同期する。

        校正モードは「校正作業だけ」を想定し、検出系の「電子化」「検出設定」を不活性
        化する(誤って再検出・原紙再指定しないため)。原紙編集モード中は「電子化」が
        確定ボタンを兼ねるので不活性化しない。
        """
        self._proof_mode = proof
        if self._mode_btn is not None:
            self._mode_btn.setText("モード: 校正 ▾" if proof else "モード: 自動認識 ▾")
        if self._act_mode_proof is not None:
            self._act_mode_proof.setChecked(proof)
        if self._act_mode_auto is not None:
            self._act_mode_auto.setChecked(not proof)
        enabled = not proof
        if self._digitize_btn is not None and self._genshi_edit is None:
            self._digitize_btn.setEnabled(enabled)
        if self._settings_btn is not None:
            self._settings_btn.setEnabled(enabled)
        # 校正モードでは自由記述の範囲編集(フォーム設定作業)も無効化する。
        if self.form_pane is not None:
            self.form_pane.set_region_edit_enabled(enabled)

    def _set_digitize_mode(self, editing: bool) -> None:
        """原紙編集モードに合わせて、電子化ボタン表示と構造編集ボタンの表示を切替。"""
        if self._struct_btn is not None:
            self._struct_btn.setVisible(editing)
        if self._vector_check is not None:
            self._vector_check.setVisible(editing)
        if self._digitize_btn is None:
            return
        if editing:
            self._digitize_btn.setText("✓ この原紙で電子化")
            self._digitize_btn.setEnabled(True)  # 確定ボタンを兼ねるので常に有効
            self._digitize_btn.setToolTip(
                "原紙フォームを保存して記入済みPDFを読み取る（Esc で取消）"
            )
        else:
            self._digitize_btn.setText("電子化")
            self._digitize_btn.setToolTip("全ページを一括電子化")

    def open_split_dialog(self) -> None:
        """電子化済みの「記入済みPDF＋読み取り結果」を N 分割して配布用に書き出す。"""
        if self.doc is None or self._survey is None or self._doc_store is None:
            QMessageBox.information(
                self.window,
                "分割",
                "分割するには、PDFを開いてフォームを用意してください。",
            )
            return
        # 編集中の内容を確定保存してから分割(最新の結果を配布)
        self._capture_current_page()
        self._save_store()
        dlg = SplitDialog(
            Path(self.doc.path), self._survey, len(self.doc), self.window,
            pages=self._doc_store.pages_dict(),
            baseline_png=self._doc_store.baseline_png,
        )
        self._center_dialog(dlg)
        dlg.exec()

    def open_merge_dialog(self) -> None:
        """チャンク(PDF or 提出JSON)を集めて統合結果を埋め込んだ自己完結PDFを書き出す。"""
        start = str(Path(self.doc.path).parent) if self.doc is not None else None
        dlg = MergeDialog(self.window, start_dir=start)
        self._center_dialog(dlg)
        dlg.exec()
        if dlg.merged_pdf is not None:
            ret = QMessageBox.question(
                self.window,
                "マージ完了",
                f"統合結果を開きますか？\n{dlg.merged_pdf.name}",
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.load_pdf(str(dlg.merged_pdf))

    # --------------------------------------------------------- detection UI
    def open_detection_settings(self) -> None:
        """検出パラメータ調整ダイアログを開く(非モーダル)。"""
        if self._survey is None:
            QMessageBox.information(
                self.window,
                "検出設定",
                "フォームが読み込まれていません。\n"
                "PDFを開くと原紙PDFを尋ねてフォームを作成します。作成されない場合は"
                "ツール→「原紙を指定してフォーム作成…」/「フォーム編集」をご利用ください。",
            )
            return
        dlg = self._settings_dialog
        if dlg is None:
            dlg = DetectionSettingsDialog(
                self._survey.detection, self._overlay_enabled, self.window,
                show_ratios=self._overlay_ratios,
            )
            dlg.valuesChanged.connect(self._on_detection_params_changed)
            dlg.overlayToggled.connect(self._set_overlay_enabled)
            dlg.ratiosToggled.connect(self._set_overlay_ratios)
            dlg.applyRequested.connect(self.reapply_detection)
            self._settings_dialog = dlg
        self._center_dialog(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # --------------------------------------------------------- フォーム適用/編集
    def _apply_survey_to_doc(self, survey: Survey) -> None:
        """survey を現在の文書に適用する(校正結果 pages は保持)。

        以後の新規PDFの既定にもする。開いている文書があれば、ストアの定義を
        差し替え→フォーム再構築→再検出→サイドカー保存まで行う。
        """
        self._default_survey = survey
        if self.doc is None:
            return
        if self._genshi_edit is not None:
            # 原紙フォーム編集モード: サイドカーは作らず(原紙に回答は無い)、フォーム
            # 定義だけ差し替えて再表示する。原紙.json は「✓ この原紙で電子化」で保存。
            self._survey = survey
            self._install_form_pane(survey)
            if self._page_index >= 0:
                self._load_page_into_form(self._page_index)
            return
        store = self._doc_store
        if store is None:
            store = SurveyDocument(self.doc.path, survey)
            self._doc_store = store
        else:
            store.survey = survey
        self._survey = survey
        self._install_form_pane(survey)
        if self._page_index >= 0:
            self._load_page_into_form(self._page_index)  # 作成直後は検出せず空表示
        self._save_store()
        self._update_genshi_status()

    def _build_survey_from(self, path: str) -> Survey | None:
        """指定PDFからフォーム定義を生成する。

        テキスト層があればベクタ抽出、無ければ選択中の OCR で生成。OCR 不可や
        失敗時は None を返す(呼び出し側がメッセージを出す)。
        """
        try:
            if has_text_layer(path):
                return generate_survey_from_pdf(path)
            backend = self._current_backend()
            if backend is None:
                return None
            sb = self.window.statusBar()
            if sb is not None:
                sb.showMessage("OCRでフォームを作成中…（数十秒かかる場合があります）", 0)
            survey = generate_survey_from_scans([path], backend)
            if sb is not None:
                sb.clearMessage()
            return survey
        except Exception:  # noqa: BLE001  失敗時は None(編集モードへフォールバック)
            return None

    def open_form_editor(self) -> None:
        """構造エディタでフォーム定義(設問・選択肢)を編集する(非モーダル)。"""
        if self._survey is None:
            QMessageBox.information(
                self.window,
                "フォーム編集",
                "編集するフォームがありません。\n"
                "先にPDFを開いてフォームを作成または読み込んでください。",
            )
            return
        dlg = FormEditorDialog(self._survey, self.window)
        dlg.applied.connect(self._apply_survey_to_doc)
        self._editor_dialog = dlg  # 寿命保持
        self._center_dialog(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_detection_params_changed(self) -> None:
        """パラメータ変更 → 現在ページを再検出してフォーム・オーバーレイを更新。"""
        if self.doc is not None and self._page_index >= 0:
            self._detect_and_fill(self._page_index)

    def _set_overlay_enabled(self, enabled: bool) -> None:
        self._overlay_enabled = enabled
        self._settings.setValue("overlay/enabled", enabled)  # 次回起動へ記憶
        self._refresh_overlay()

    def _set_overlay_ratios(self, show: bool) -> None:
        self._overlay_ratios = show
        self._settings.setValue("overlay/ratios", show)  # 次回起動へ記憶
        self._refresh_overlay()

    # ----------------------------------------------------------- persistence
    def restore_window_state(self) -> None:
        """保存済みのウィンドウ状態(ジオメトリ/分割比/フィットモード)を復元する。

        load_main_window() が設定した .ui 既定値・初期分割比の後に呼ぶことで、
        保存値があればそれで上書きする(無ければ既定のまま)。
        """
        geometry = self._settings.value("window/geometry")
        if geometry is not None:
            self.window.restoreGeometry(geometry)

        if self._splitter is not None:
            state = self._settings.value("window/splitter")
            if state is not None:
                self._splitter.restoreState(state)
        if self._left_splitter is not None:
            state = self._settings.value("window/leftSplitter")
            if state is not None:
                self._left_splitter.restoreState(state)

        # フィットモード(既定: 横幅)。アクションのチェックも同期させる。
        mode = self._settings.value("view/fitMode", FIT_WIDTH)
        if mode == FIT_HEIGHT and self._action_fit_height is not None:
            self._action_fit_height.setChecked(True)
        elif self._action_fit_width is not None:
            self._action_fit_width.setChecked(True)
        self.set_fit_mode(mode if mode in (FIT_WIDTH, FIT_HEIGHT) else FIT_WIDTH)

        # フォームのフォントサイズ(既定14pt)
        try:
            self._form_font_pt = int(
                self._settings.value("view/formFontPt", DEFAULT_FORM_FONT_PT)
            )
        except (TypeError, ValueError):
            self._form_font_pt = DEFAULT_FORM_FONT_PT

    def save_window_state(self) -> None:
        """ウィンドウ状態と校正結果を保存する(QApplication.aboutToQuit で呼ぶ)。"""
        # 終了時の自動保存: 現在ページの校正内容をサイドカーへ
        self._capture_current_page()
        self._save_store()
        self._settings.setValue("window/geometry", self.window.saveGeometry())
        if self._splitter is not None:
            self._settings.setValue("window/splitter", self._splitter.saveState())
        if self._left_splitter is not None:
            self._settings.setValue(
                "window/leftSplitter", self._left_splitter.saveState()
            )
        self._settings.setValue("view/fitMode", self._fit_mode)
        self._settings.setValue("view/formFontPt", self._form_font_pt)
        # 即時終了(os._exit)でも確実に書き出されるよう明示的に flush
        self._settings.sync()

    # --------------------------------------------------------------- form pane
    # ----------------------------------------------------- フォントサイズ(表示)
    def _set_form_font(self, pt: int) -> None:
        """フォームのフォントサイズ(pt)を設定・適用・保存する。"""
        pt = max(MIN_FORM_FONT_PT, min(MAX_FORM_FONT_PT, int(pt)))
        self._form_font_pt = pt
        if self.form_pane is not None:
            self.form_pane.setStyleSheet(f"font-size: {pt}pt;")
        self._settings.setValue("view/formFontPt", pt)
        self._settings.sync()
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage(f"フォームのフォントサイズ: {pt}pt", 3000)

    def _adjust_form_font(self, delta: int) -> None:
        self._set_form_font(self._form_font_pt + delta)

    def _show_form_placeholder(self) -> None:
        """PDF未読込時の右ペイン表示(フォームは出さない)。"""
        label = QLabel("PDFを開くと入力フォームが表示されます")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet("color: #888;")
        self._form_scroll.setWidget(label)  # 既存フォームがあれば破棄される
        self.form_pane = None

    def _install_form_pane(self, survey: Survey) -> None:
        """survey からフォームを生成して右ペインへ注入し、シグナルを結線する。"""
        pane = FormPane(survey)
        pane.resultsChanged.connect(self._on_form_changed)
        pane.reviewedToggled.connect(self._on_reviewed_toggled)
        pane.optionHovered.connect(self._highlight_option)
        pane.optionHoverEnded.connect(self._clear_highlight)
        pane.regionHovered.connect(self._highlight_region)
        pane.regionRequested.connect(self.start_region_capture)
        self.form_pane = pane
        pane.setStyleSheet(f"font-size: {self._form_font_pt}pt;")
        # 校正モードでは範囲編集ボタンを無効化(現在のモードを反映)。
        pane.set_region_edit_enabled(not self._proof_mode)
        self._form_scroll.setWidget(pane)

    # --------------------------------------------------- 校正結果の永続化
    def _capture_current_page(self) -> None:
        """現在ページのフォーム内容を(メモリ上の)サイドカーへ取り込む。

        原紙は別PDFなので、開いている記入済みPDFの全ページが回答(=保存対象)。
        """
        if (
            self._doc_store is not None
            and self._page_index >= 0
            and self.form_pane is not None
        ):
            self._doc_store.set_results(
                self._page_index, self.form_pane.get_results()
            )

    def _save_store(self) -> None:
        """結果を作業 PDF へ埋め込み保存する(存在する場合)。保留中のオートセーブも消化。

        保存に失敗しても例外を上げない。以前は show_page の途中(描画前)で保存が
        例外を投げると _render_page に到達せず本表示が真っ白になっていた(Windows で
        ファイルロックにより os.replace が失敗するケース)。失敗はログとステータスバーへ
        通知し、描画・操作は継続させる。
        """
        self._autosave_timer.stop()
        if self._doc_store is None:
            return
        try:
            self._doc_store.save()
        except Exception:
            log.exception("結果の埋め込み保存に失敗")
            sb = self.window.statusBar()
            if sb is not None:
                sb.showMessage("結果の保存に失敗しました(ファイルが開かれている可能性)", 8000)

    def _ensure_original_backup(self, path: str) -> None:
        """電子化前の生スキャン原本を初回オープン時に <stem>.bak.pdf へ退避する。

        既に結果が埋め込まれた PDF(配布されたもの等)は退避しない＝`.bak` は「まだ電子化
        していない生の原本」だけを保持する。`.bak.pdf` が既にあれば何もしない。
        """
        src = Path(path)
        bak = src.with_name(f"{src.stem}.bak.pdf")
        if not src.exists() or bak.exists():
            return
        if pdf_embed.has_embedded_data(src):
            return  # 埋め込み済み(処理後)のファイルは退避しない
        try:
            shutil.copy2(src, bak)
        except OSError:
            pass  # 退避に失敗しても読み込みは続行

    # ----------------------------------------------- 提出用JSONの書き出し
    def _all_pages_reviewed(self) -> bool:
        """全ページが確認済み(✓)か。1ページ以上あり、すべて reviewed のとき True。"""
        if self.doc is None or self._doc_store is None or self._survey is None:
            return False
        total = len(self.doc)
        return total > 0 and all(
            self._doc_store.is_reviewed(i) for i in range(total)
        )

    def _update_submit_btn(self) -> None:
        """提出ボタンの有効/無効を、全ページ確認済みかで更新する。"""
        if self._submit_btn is not None:
            self._submit_btn.setEnabled(self._all_pages_reviewed())

    def _update_genshi_status(self) -> None:
        """原紙(フォーム定義／差分基準)の読込状態をステータスバーに常時表示する。"""
        sb = self.window.statusBar()
        if sb is None:
            return
        if self._genshi_status_label is None:
            self._genshi_status_label = QLabel()
            sb.addPermanentWidget(self._genshi_status_label)
        loaded = self._survey is not None
        has_base = (
            self._doc_store is not None and self._doc_store.baseline_png is not None
        )
        if loaded:
            self._genshi_status_label.setText(
                "原紙: 読込済み" + ("（差分・再電子化可）" if has_base else "")
            )
            self._genshi_status_label.setStyleSheet("color:#1a7f37; padding:0 8px;")
        else:
            self._genshi_status_label.setText("原紙: 未指定")
            self._genshi_status_label.setStyleSheet("color:#888; padding:0 8px;")

    def export_submission_json(self) -> None:
        """読み取り結果を提出用 JSON として作業PDFと同じフォルダへ書き出す。

        全ページ確認済みのときだけ実行できる(ボタンはそれ以外では無効)。内容は
        {source_pdf, survey, pages} で、こちら側の「アンケートをマージ」と互換。
        書き出した JSON は再オープン時には読み込まれない(読込は常に埋め込み優先)。
        """
        if (
            self.doc is None
            or self._doc_store is None
            or self._survey is None
            or not self._all_pages_reviewed()
        ):
            return
        self._capture_current_page()
        self._save_store()  # 念のため最新を埋め込みへ確定してから書き出す
        out = Path(self.doc.path).with_suffix(".json")
        data = {
            "source_pdf": Path(self.doc.path).name,
            "survey": survey_to_dict(self._survey),
            "pages": self._doc_store.pages_dict(),
        }
        try:
            out.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as e:
            QMessageBox.warning(self.window, "提出用に書き出す", f"書き出しに失敗しました:\n{e}")
            return
        QMessageBox.information(
            self.window, "提出用に書き出す",
            f"提出用 JSON を書き出しました:\n{out}\n\nこの JSON を担当の方へ返送してください。",
        )

    def _update_title(self) -> None:
        """ウィンドウタイトルを設定する(全面オートセーブのため未保存印は無し)。"""
        self.window.setWindowTitle(self._base_title)

    def _on_form_changed(self) -> None:
        """フォーム編集 → メモリへ反映し、デバウンス付きで即時オートセーブする。"""
        self._capture_current_page()
        if self._doc_store is not None:
            self._autosave_timer.start()  # 連続入力をまとめて書込(500ms)
            self._track_reviewed_edit()
        # 選択が変わった時だけオーバーレイを更新(自由記述のキー入力では再描画しない)
        if self._overlay_enabled and self._current_selection() != self._overlay_selection_cache:
            self._refresh_overlay()

    def _track_reviewed_edit(self) -> None:
        """確認済みページの編集を監視し、確認済み時点との差分で確認状態を更新する。

        確認済み時点のスナップショットから変化したら確認済みを解除(ボタンは
        「✓ 確認済みにする」に戻る／Esc で復帰可能)。元に戻せば自動で確認済みへ復帰。
        """
        if (
            self._doc_store is None
            or self._page_index < 0
            or self._confirmed_results is None
            or self.form_pane is None
        ):
            return
        diverged = self.form_pane.get_results() != self._confirmed_results
        reviewed = self._doc_store.is_reviewed(self._page_index)
        if diverged and reviewed:
            # 確認済みページを編集 → 確認解除(スナップショットは復帰用に保持)
            self._doc_store.set_reviewed(self._page_index, False)
            self._refresh_thumb(self._page_index)
            self._sync_review_btn(False)
        elif not diverged and not reviewed:
            # 編集を元に戻した → 確認済みへ復帰
            self._doc_store.set_reviewed(self._page_index, True)
            self._refresh_thumb(self._page_index)
            self._sync_review_btn(True)

    def _on_reviewed_toggled(self, reviewed: bool) -> None:
        """フォームの確認済みチェック操作 → 記録・ボタン同期。"""
        self._apply_reviewed(reviewed, sync_form=False)

    def _on_review_btn_toggled(self, reviewed: bool) -> None:
        """アクションバーの確認済みボタン操作 → 記録・フォーム同期。

        確認済みにした(reviewed=True)ときは、続けて未確認の次ページへ自動で進む
        (校正を素早く回せるように)。解除時はそのページに留まる。
        """
        self._apply_reviewed(reviewed, sync_form=True)
        if reviewed:
            self._goto_next_unreviewed()

    def _goto_next_unreviewed(self) -> None:
        """現在ページより後ろの最初の未確認ページへ移動する(無ければ前方へ回り込む)。

        全ページ確認済みなら移動せず、その旨を知らせる。
        """
        if self.doc is None or self._doc_store is None:
            return
        total = len(self.doc)
        cur = self._page_index
        # 後ろ(cur+1..末尾) → 回り込み(先頭..cur) の順で最初の未確認を探す。
        order = list(range(cur + 1, total)) + list(range(0, cur + 1))
        for i in order:
            if not self._doc_store.is_reviewed(i):
                self.thumbnail_list.setCurrentRow(i)  # → show_page(i)
                return
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage("全ページを確認済みにしました。", 4000)

    def _apply_reviewed(self, reviewed: bool, *, sync_form: bool) -> None:
        """確認済み状態を記録し、サムネイル印・即保存・両UIの同期を行う。

        確認時は現在の結果をスナップショット(確認済み時点)として記録する。
        確認解除時はスナップショットを破棄する(編集差分の復帰先が無くなる)。
        """
        if self._doc_store is None or self._page_index < 0:
            return
        self._capture_current_page()
        self._doc_store.set_reviewed(self._page_index, reviewed)
        if reviewed and self.form_pane is not None:
            self._confirmed_results = copy.deepcopy(self.form_pane.get_results())
        elif not reviewed:
            self._confirmed_results = None
        self._refresh_thumb(self._page_index)
        self._save_store()
        if sync_form and self.form_pane is not None:
            self.form_pane.set_reviewed(reviewed)  # 内部でシグナル抑止
        self._sync_review_btn(reviewed)

    def _on_escape(self) -> None:
        """Esc の一元処理。傾き補正→原紙編集→確認済み編集の取消、の優先順。"""
        if self._skew_mode:
            self._cancel_skew_mode()
        elif self._genshi_edit is not None:
            self._cancel_genshi_edit()
        else:
            self._revert_reviewed_edit()

    def _revert_reviewed_edit(self) -> bool:
        """確認済みページの編集を破棄し、確認済み時点の状態へ戻す(Esc)。

        確認済みを編集して差分が出ている(=確認解除されスナップショットがある)ときだけ
        働く。スナップショットをフォーム・ストアへ復元し、確認済みに戻す。
        """
        if (
            self._doc_store is None
            or self._page_index < 0
            or self._confirmed_results is None
            or self._doc_store.is_reviewed(self._page_index)  # 差分なし=何もしない
            or self.form_pane is None
        ):
            return False
        snap = copy.deepcopy(self._confirmed_results)
        self.form_pane.blockSignals(True)
        self.form_pane.set_results_dict(snap)
        self.form_pane.blockSignals(False)
        self._doc_store.set_results(self._page_index, copy.deepcopy(snap))
        self._doc_store.set_reviewed(self._page_index, True)
        self._refresh_thumb(self._page_index)
        self._save_store()
        self._sync_review_btn(True)
        self._refresh_overlay()
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage("編集を破棄し、確認済みの状態に戻しました。", 4000)
        return True

    def _sync_review_btn(self, reviewed: bool) -> None:
        """確認済みボタンのチェック状態・文言を同期し、提出ボタンの有効状態も更新する。"""
        if self._review_btn is not None:
            self._review_btn.blockSignals(True)
            self._review_btn.setChecked(reviewed)
            self._review_btn.blockSignals(False)
            self._review_btn.setText("✓ 確認済み" if reviewed else "✓ 確認済みにする")
        # 確認状態の変化・ページ読み込みの度に、全ページ✓かで提出ボタンを更新。
        self._update_submit_btn()

    # ------------------------------------------------------------------ actions
    def open_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "PDFを開く",
            str(Path.cwd()),
            "PDF Files (*.pdf *.PDF)",
        )
        if not path:
            return
        self.load_pdf(path)

    def load_pdf(self, path: str) -> None:
        """指定パスの PDF を開く(埋め込み結果があれば復元)。"""
        # 直前の文書の現在ページを保存してから切り替える
        self._capture_current_page()
        self._save_store()
        # 配布時点の原本を初回オープン時に退避(以後この PDF はその場上書きされる)。
        # `.bak.pdf` が既にあれば触らない＝最初に開いた状態を恒久保存し、いつでも戻せる。
        self._ensure_original_backup(path)

        if self.doc is not None:
            self.doc.close()
        self.doc = PdfDocument(path)
        self._page_index = -1
        self._skew = {}  # 新しい文書: 傾き補正角をリセット
        self._clean_cache = None  # 差分基準ページのキャッシュを破棄
        if self._skew_action is not None and self._skew_action.isChecked():
            self._skew_action.setChecked(False)  # → _toggle_skew_mode(False)
        self._skew_mode = False
        # 検出設定ダイアログは破棄(新しい survey.detection に張り直す)
        if self._settings_dialog is not None:
            self._settings_dialog.close()
            self._settings_dialog = None

        # フォームの決定: サイドカーがあれば復元。無ければフォーム未設定のまま開き、
        # 「③電子化」で原紙を指定した時点でフォームを用意する。
        store = SurveyDocument.load(path, None)
        if store.survey is not None:
            self._doc_store = store
            self._survey = store.survey
            self._install_form_pane(self._survey)
            # 既に電子化済み(サイドカーあり)＝校正段階とみなし校正モードを既定にする。
            self._set_proof_mode(True)
        else:
            # サイドカー無し: フォーム未設定で開く(電子化時に原紙から用意)。新規＝
            # まず検出を作る段階なので自動認識モードを既定にする。
            self._doc_store = None
            self._survey = None
            self._set_proof_mode(False)
            self._show_form_placeholder()
            sb = self.window.statusBar()
            if sb is not None:
                sb.showMessage(
                    "「電子化」を押すと、原紙PDFを指定してフォームを用意し読み取ります。",
                    0,
                )

        self._base_title = f"アンケート読み込み支援 — {Path(path).name}"
        self._update_title()
        self._build_thumbnails()
        self._update_submit_btn()  # 新しい文書に合わせて提出ボタンを再評価
        self._update_genshi_status()

        if len(self.doc) > 0:
            self.thumbnail_list.setCurrentRow(0)

    # ------------------------------------------------------------------ rendering
    def _thumb_text(self, index: int) -> str:
        reviewed = self._doc_store is not None and self._doc_store.is_reviewed(index)
        return f"✓ P{index + 1}" if reviewed else f"P{index + 1}"

    def _is_reviewed(self, index: int) -> bool:
        return self._doc_store is not None and self._doc_store.is_reviewed(index)

    def _compose_thumb(self, base: QPixmap, reviewed: bool) -> QPixmap:
        """確認済みなら元サムネイルにグレーのハッチを重ねて返す。"""
        if not reviewed:
            return base
        pm = QPixmap(base)
        painter = QPainter(pm)
        # 斜線ハッチ＋薄いグレーで「処理済み」を視覚化
        painter.fillRect(
            pm.rect(), QBrush(QColor(110, 110, 110, 110), Qt.BrushStyle.BDiagPattern)
        )
        painter.fillRect(pm.rect(), QColor(110, 110, 110, 50))
        painter.end()
        return pm

    def _refresh_thumb(self, index: int) -> None:
        """1ページのサムネイル表示(アイコン＋テキスト)を現在の状態に合わせ直す。"""
        item = self.thumbnail_list.item(index)
        if item is None or not (0 <= index < len(self._thumb_base)):
            return
        item.setIcon(
            QIcon(self._compose_thumb(self._thumb_base[index], self._is_reviewed(index)))
        )
        item.setText(self._thumb_text(index))

    def _build_thumbnails(self) -> None:
        assert self.doc is not None
        lst = self.thumbnail_list
        lst.blockSignals(True)
        lst.clear()
        self._thumb_base = []
        statusbar = self.window.statusBar()
        for i in range(len(self.doc)):
            image = self.doc.render(i, scale=THUMB_SCALE)
            if i == 0 and image.height() > 0:
                self._thumb_aspect = image.width() / image.height()
            base = QPixmap.fromImage(image)
            self._thumb_base.append(base)
            item = QListWidgetItem(
                QIcon(self._compose_thumb(base, self._is_reviewed(i))),
                self._thumb_text(i),
            )
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            lst.addItem(item)
            if statusbar is not None:
                statusbar.showMessage(f"サムネイル生成中… {i + 1}/{len(self.doc)}")
        lst.blockSignals(False)
        self._update_thumbnail_size()
        if statusbar is not None:
            statusbar.showMessage(f"{len(self.doc)} ページを読み込みました", 5000)

    def _update_thumbnail_size(self) -> None:
        """サムネイルのアイコン幅をリスト領域の幅いっぱいに合わせる。"""
        if self._sizing_thumbs:
            return
        lst = self.thumbnail_list
        margin = 2 * lst.spacing() + 4  # spacing + 余白
        width = max(1, lst.viewport().width() - margin)
        height = max(1, round(width / self._thumb_aspect))
        self._sizing_thumbs = True
        try:
            lst.setIconSize(QSize(width, height))
        finally:
            self._sizing_thumbs = False

    def show_page(self, index: int) -> None:
        if self.doc is None or index < 0 or index >= len(self.doc):
            return
        # 移動前に現在ページの校正内容を保存(自動保存: ページ切替時)
        self._capture_current_page()
        self._save_store()

        self._page_index = index
        # 先に点空間のシーン矩形を確定(フィット倍率計算に必要)
        w_pt, h_pt = self.doc.page_size(index)
        self._scene.setSceneRect(0.0, 0.0, w_pt, h_pt)
        # 現在の表示倍率に見合う解像度で初回ラスタライズ
        self._render_scale = 0.0
        self._render_page(self._desired_render_scale())
        # ズーム係数を保ったままページに合わせて再表示
        self._apply_view()
        # 表示時は検出しない。保存済み結果(無ければ空)を反映するだけ。
        # チェックの読み取りは「③電子化」で実行する。
        self._load_page_into_form(index)
        self._update_page_label()

    def _load_page_into_form(self, index: int) -> None:
        """検出は行わず、保存済み校正結果(無ければ空)をフォームへ反映する。

        読み込み・ページ送りの時点では自動検出しない方針。チェックの読み取りは
        「③電子化」を押したときにまとめて実行する。ここではサイドカーに保存済みの
        結果があれば復元し、無ければ空のフォームを表示する。
        """
        if self.doc is None or self.form_pane is None or self._survey is None:
            return
        self._last_results = {}  # 未検出: オーバーレイにインク比率は出さない
        saved = self._doc_store.get_results(index) if self._doc_store else None
        self.form_pane.blockSignals(True)
        self.form_pane.set_results_dict(saved or {})  # 空 dict で全項目クリア
        reviewed_now = self._doc_store.is_reviewed(index) if self._doc_store else False
        self.form_pane.set_reviewed(reviewed_now)
        self.form_pane.blockSignals(False)
        # 確認済みページは「確認済み時点」のスナップショットを保持(編集差分・Esc復帰用)。
        self._confirmed_results = (
            copy.deepcopy(self.form_pane.get_results()) if reviewed_now else None
        )
        if self._review_btn is not None:
            self._review_btn.setEnabled(self._doc_store is not None)
        self._sync_review_btn(reviewed_now)
        self._refresh_overlay()

    def _detect_and_fill(self, index: int) -> None:
        """検出を実行し、保存済み校正結果があれば優先してフォームへ反映する。

        検出設定のプレビュー(パラメータ変更時)で使う。フォームの中身は、保存済みの
        校正結果があればそれを復元し、無ければ検出結果を初期値として表示する。
        """
        if self.doc is None or self.form_pane is None or self._survey is None:
            return
        survey = self._survey
        scale = survey.detection.render_scale
        image = self._render_image(index, scale)
        gray = qimage_to_gray(image)
        clean = self._clean_gray(scale) if self._use_diff_for(index) else None
        self._last_results = detect_checkboxes(survey, gray, clean)

        saved = self._doc_store.get_results(index) if self._doc_store else None
        self.form_pane.blockSignals(True)
        if saved is not None:
            self.form_pane.set_results_dict(saved)
        else:
            self.form_pane.set_detection(self._last_results)
        reviewed_now = self._doc_store.is_reviewed(index) if self._doc_store else False
        self.form_pane.set_reviewed(reviewed_now)
        self.form_pane.blockSignals(False)
        self._confirmed_results = (
            copy.deepcopy(self.form_pane.get_results()) if reviewed_now else None
        )
        if self._review_btn is not None:
            self._review_btn.setEnabled(self._doc_store is not None)
        self._sync_review_btn(reviewed_now)
        self._refresh_overlay()

    # ---------------------------------------------------- ホバー連動ハイライト
    def _highlight_rect(
        self, rect_norm: Rect | None, color: QColor, filled: bool = True
    ) -> None:
        """正規化矩形を PDF 上で強調表示する(直前の強調は消す)。

        filled=False のときは塗りなしの枠だけ(自由記述など、中の文字を隠さない)。
        """
        self._clear_highlight()
        sr = self._scene.sceneRect()
        w, h = sr.width(), sr.height()
        if rect_norm is None or w <= 0 or h <= 0:
            return
        x0, y0, x1, y1 = rect_norm
        box = QRectF(x0 * w, y0 * h, (x1 - x0) * w, (y1 - y0) * h)
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidthF(2.5)
        if filled:
            fill = QColor(color)
            fill.setAlpha(60)
            brush = QBrush(fill)
        else:
            brush = QBrush(Qt.BrushStyle.NoBrush)
        self._highlight_item = self._scene.addRect(box, pen, brush)
        self._highlight_item.setZValue(20)
        # 傾き補正モード中はフォーカス枠もページと一緒に回転させる
        if self._skew_mode:
            self._apply_overlay_skew(self._skew_preview_deg)
        # 拡大表示で枠が画面外なら最小限スクロールして見せる。これはフォーム操作
        # 起因のPDFスクロールなので、相互連動でフォームを巻き戻さないよう抑止する。
        self._scroll_sync_guard = True
        try:
            self.pdf_view.ensureVisible(box, 40, 40)
        finally:
            self._scroll_sync_guard = False

    # ----------------------------------------------- PDFダブルクリックでトグル
    @staticmethod
    def _rect_distance(px: float, py: float, rect: Rect) -> float:
        """点(px,py)と矩形の距離(内側なら0)。正規化座標。"""
        x0, y0, x1, y1 = rect
        dx = max(x0 - px, 0.0, px - x1)
        dy = max(y0 - py, 0.0, py - y1)
        return (dx * dx + dy * dy) ** 0.5

    def _option_marker_rect(self, opt) -> Rect:
        """当たり判定/表示用のマーカー矩形(box は box_expand で拡大した範囲)。"""
        if opt.marker_type != MARKER_BOX or self._survey is None:
            return opt.checkbox
        be = self._survey.detection.box_expand
        if not be:
            return opt.checkbox
        x0, y0, x1, y1 = opt.checkbox
        mx, my = (x1 - x0) * be, (y1 - y0) * be
        return (max(0.0, x0 - mx), max(0.0, y0 - my),
                min(1.0, x1 + mx), min(1.0, y1 + my))

    def _option_at(self, nx: float, ny: float, tol: float = 0.025):
        """正規化点に対応する選択肢を返す。許容外なら None。

        (1) マーカー(box は box_expand 拡大後)への近接ヒット、(2) 該当テキストの
        クリック=同じ行でクリックより左にある最も近いチェックボックス(ラベルは□の
        右側に並ぶ前提。右隣の□より手前のクリックのみ)で判定する。
        """
        if self._survey is None:
            return None
        opts: list[tuple[str, str, Rect]] = []  # (qid, value, checkbox)
        direct: tuple[str, str] | None = None
        best_d = tol
        for q in self._survey.questions:
            if q.type not in (SINGLE_CHOICE, MULTI_CHOICE):
                continue
            for opt in q.options:
                opts.append((q.id, opt.value, opt.checkbox))
                # (1) マーカー近接(拡大box含む)
                d = self._rect_distance(nx, ny, self._option_marker_rect(opt))
                if d < best_d:
                    best_d = d
                    direct = (q.id, opt.value)
        if direct is not None:
            return direct
        # (2) ラベルクリック: 同じ行でクリックの左にある最も近い□。隣の選択肢を
        # 拾わないよう、縦は箱高さ±25%、横はラベル相当(□右〜次の□までの隙間の
        # 左寄り60%・上限0.35)に絞る。
        pad_y = 0.25  # 行の縦許容(箱高さ比)
        gap_frac = 0.6  # 次の□までの隙間のうちラベルとみなす割合(左寄り)
        max_label_w = 0.35  # ラベル到達幅の上限(行末選択肢用・ページ幅比)

        def on_row(y0: float, y1: float) -> bool:
            bh = y1 - y0
            return y0 - pad_y * bh <= ny <= y1 + pad_y * bh

        cand: tuple[str, str, Rect] | None = None
        for qid, value, (x0, y0, x1, y1) in opts:
            if not on_row(y0, y1):
                continue  # 同じ行(縦バンド)でない
            if nx < x0:
                continue  # クリックは□より左(ラベルは右側のはず)
            if cand is None or x0 > cand[2][0]:
                cand = (qid, value, (x0, y0, x1, y1))
        if cand is None:
            return None
        cx0, _, cx1, _ = cand[2]
        # 右隣の同行□の左端(無ければページ右端)
        next_x0 = 1.0
        for _qid, _value, (x0, y0, x1, y1) in opts:
            if on_row(y0, y1) and x0 > cx0:
                next_x0 = min(next_x0, x0)
        # ラベル到達点 = □右端から、隙間の一部 or 上限幅の小さい方まで
        reach = cx1 + min(gap_frac * max(0.0, next_x0 - cx1), max_label_w)
        if nx <= reach:
            return (cand[0], cand[1])
        return None

    def _toggle_option_at(self, view_pos) -> None:
        """ビューポート座標の点に対応する選択肢のチェックをトグルする。"""
        if self.form_pane is None:
            return
        sr = self._scene.sceneRect()
        w, h = sr.width(), sr.height()
        if w <= 0 or h <= 0:
            return
        sp = self.pdf_view.mapToScene(view_pos)
        hit = self._option_at(sp.x() / w, sp.y() / h)
        if hit is None:
            return
        qid, value = hit
        self.form_pane.toggle_option(qid, value)  # → resultsChanged(保存/未保存印)
        self._highlight_option(qid, value)  # どこを操作したか一瞬強調
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage(f"「{value}」を切り替えました", 3000)

    def _highlight_option(self, question_id: str, value: str) -> None:
        """フォーム選択肢に対応するマーカーを PDF 上で強調表示する(青)。"""
        if self._survey is None:
            self._clear_highlight()
            return
        rect_norm: Rect | None = None
        for q in self._survey.questions:
            if q.id == question_id:
                for opt in q.options:
                    if opt.value == value:
                        rect_norm = opt.checkbox
                break
        self._highlight_rect(rect_norm, QColor(20, 110, 220))

    def _highlight_region(self, question_id: str) -> None:
        """自由記述欄に対応する記入領域を PDF 上で強調表示する(緑)。"""
        if self._survey is None:
            self._clear_highlight()
            return
        rect_norm: Rect | None = None
        for q in self._survey.questions:
            if q.id == question_id:
                rect_norm = q.region
                break
        # 自由記述は枠だけ(塗りで記入文字を隠さない)
        self._highlight_rect(rect_norm, QColor(20, 140, 60), filled=False)

    def _clear_highlight(self) -> None:
        if (
            self._highlight_item is not None
            and self._highlight_item.scene() is self._scene
        ):
            self._scene.removeItem(self._highlight_item)
        self._highlight_item = None

    # ----------------------------------------- PDF↔フォームのスクロール相互連動
    def _on_form_scrolled(self, _value: int) -> None:
        """右ペイン(読み取り結果)のスクロールに合わせてPDF表示を追従させる。"""
        self._sync_scroll(
            self._form_scroll.verticalScrollBar(),
            self.pdf_view.verticalScrollBar(),
        )

    def _on_pdf_scrolled(self, _value: int) -> None:
        """PDF表示のスクロールに合わせて右ペイン(読み取り結果)を追従させる。"""
        self._sync_scroll(
            self.pdf_view.verticalScrollBar(),
            self._form_scroll.verticalScrollBar(),
        )

    def _sync_scroll(self, src, dst) -> None:
        """src のスクロール位置(比率)に dst を合わせる。再帰・誘発は抑止する。

        ページ送り・ズーム・フォーカス追従などプログラム起因のスクロールでは
        相手を動かさない(_scroll_sync_guard / _applying / _zooming を見る)。
        """
        if self._scroll_sync_guard or self._applying or self._zooming:
            return
        smax = src.maximum()
        dmax = dst.maximum()
        if smax <= 0 or dmax <= 0:
            return  # 一方がスクロール不能(全体表示など)なら連動しない
        frac = src.value() / smax
        self._scroll_sync_guard = True
        try:
            dst.setValue(round(frac * dmax))
        finally:
            self._scroll_sync_guard = False

    # --------------------------------------------- 自由記述の記入領域編集(手動)
    def _find_question(self, qid: str):
        if self._survey is None:
            return None
        for q in self._survey.questions:
            if q.id == qid:
                return q
        return None

    def _handle_scene_size(self) -> float:
        """ハンドル一辺(シーン単位)。画面上でほぼ一定px(約9px)になるよう算出。"""
        scale = self.pdf_view.transform().m11() or 1.0
        return 9.0 / scale

    def start_region_capture(self, question_id: str) -> None:
        """記入領域をハンドル付き矩形で編集する(同じ設問で再呼び出しなら確定)。"""
        if self.doc is None or self._page_index < 0 or self._survey is None:
            if self.form_pane is not None:
                self.form_pane.set_region_editing(None)
            return
        # 同じ設問を編集中ならトグルで確定(終了)→ 以降ページをこの欄だけ再認識
        if self._region_edit_item is not None and self._region_edit_qid == question_id:
            self._finish_region_edit()
            self._rerecognize_field(question_id)
            return
        self._finish_region_edit()

        q = self._find_question(question_id)
        if q is None:
            if self.form_pane is not None:
                self.form_pane.set_region_editing(None)
            return
        if q.region is None:
            q.region = (0.1, 0.45, 0.9, 0.6)  # 仮の初期領域(ドラッグで調整)
        sr = self._scene.sceneRect()
        w, h = sr.width(), sr.height()
        if w <= 0 or h <= 0:
            return
        x0, y0, x1, y1 = q.region
        box = QRectF(x0 * w, y0 * h, (x1 - x0) * w, (y1 - y0) * h)
        item = RegionEditItem(box, self._handle_scene_size())
        item.changed.connect(lambda qid=question_id: self._on_region_edited(qid))
        self._scene.addItem(item)
        self._region_edit_item = item
        self._region_edit_qid = question_id
        if self.form_pane is not None:
            self.form_pane.set_region_editing(question_id)
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage(
                f"「{question_id}」の記入範囲を編集中：辺/角=サイズ変更, 内側=移動。"
                "もう一度ボタンで確定すると、確認済み以外の全ページをこの欄だけ再認識します。",
                0,
            )

    def _on_region_edited(self, question_id: str) -> None:
        """ハンドル編集の確定値を正規化して region に保存する。"""
        if self._region_edit_item is None or self._survey is None:
            return
        sr = self._scene.sceneRect()
        w, h = sr.width(), sr.height()
        if w <= 0 or h <= 0:
            return
        r = self._region_edit_item.rect()
        region = (
            max(0.0, r.left() / w),
            max(0.0, r.top() / h),
            min(1.0, r.right() / w),
            min(1.0, r.bottom() / h),
        )
        q = self._find_question(question_id)
        if q is not None:
            q.region = region
            self._save_store()

    def _finish_region_edit(self) -> None:
        """編集中の矩形を取り除いてモードを終了する。"""
        item = self._region_edit_item
        self._region_edit_item = None
        self._region_edit_qid = None
        if item is not None and item.scene() is self._scene:
            self._scene.removeItem(item)
        if self.form_pane is not None:
            self.form_pane.set_region_editing(None)
        sb = self.window.statusBar()
        if sb is not None:
            sb.clearMessage()
        self._refresh_overlay()

    def _rerecognize_field(self, question_id: str) -> None:
        """編集確定した自由記述欄を再OCRする。

        対象は確認済み以外の全ページ(確認済みページは保護のため除外)。選択肢や
        他欄には触れず、この設問の region だけを上書きする。
        """
        q = self._find_question(question_id)
        if (
            self.doc is None
            or self._survey is None
            or self._doc_store is None
            or q is None
            or q.type != FREE_TEXT
            or q.region is None
        ):
            return
        backend = self._current_backend()
        if backend is None:
            QMessageBox.information(
                self.window,
                "再認識",
                "OCRエンジンが利用できないため、自由記述の再認識はできません。"
                "（範囲の変更は保存済みです。）",
            )
            return
        self._capture_current_page()
        # 確認済み以外の全ページを対象にする(確認済みは保護)。
        indices = list(range(len(self.doc)))
        skip_reviewed = True
        if not indices:
            return
        progress = QProgressDialog(
            f"「{q.label}」を再認識中…", "中断", 0, len(indices), self.window
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        done = skipped = 0
        for i, index in enumerate(indices):
            progress.setValue(i)
            if progress.wasCanceled():
                break
            if skip_reviewed and self._doc_store.is_reviewed(index):
                skipped += 1
                continue
            image = self._render_image(index, DIGITIZE_RENDER_SCALE)
            text = recognize_region(
                image, q.region, backend, single_line=not q.multiline
            )
            results = dict(self._doc_store.get_results(index) or {})
            results[question_id] = text
            self._doc_store.set_results(index, results)
            done += 1
        progress.setValue(len(indices))
        self._save_store()
        if self._page_index >= 0:
            self._load_page_into_form(self._page_index)
        sb = self.window.statusBar()
        if sb is not None:
            msg = f"「{q.label}」を {done} ページ再認識しました"
            if skipped:
                msg += f"（確認済み {skipped} ページは除外）"
            sb.showMessage(msg, 6000)

    def _current_selection(self) -> dict[str, set[str]]:
        """現在の校正値(フォーム状態)を 設問id -> 選択値集合 で返す。

        手修正(フォーム操作・PDFダブルクリック)を反映した「いま選択済みとして
        カウントされている」値。フォーム未構築なら空(=検出結果へフォールバック)。
        """
        out: dict[str, set[str]] = {}
        if self.form_pane is None:
            return out
        try:
            results = self.form_pane.get_results()
        except Exception:  # noqa: BLE001  フォーム未同期時は空で返す
            return out
        for qid, val in results.items():
            if isinstance(val, list):
                out[qid] = {str(v) for v in val}
            elif isinstance(val, str) and val:
                out[qid] = {val}
            else:
                out[qid] = set()
        return out

    def _refresh_overlay(self) -> None:
        """ROIオーバーレイ(□枠＋インク比率)をシーンに描き直す。

        「選択済み」判定は現在の校正値(フォーム状態=手修正を反映)に基づき、
        選択済みの枠は太い赤線＋半透明の赤塗りで「選択済としてカウント中」と
        分かるようにする。比率テキストは検出結果由来でズーム非依存に表示。
        再ラスタライズ(scene.clear)後にも呼ばれるため、まず既存要素を除いてから
        再構築する。
        """
        for item in self._overlay_items:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        self._overlay_items.clear()
        if not self._overlay_enabled or self._survey is None:
            return
        rect = self._scene.sceneRect()
        w, h = rect.width(), rect.height()
        if w <= 0 or h <= 0:
            return

        red = QColor(200, 40, 40)
        gray = QColor(140, 140, 140)
        green = QColor(20, 140, 60)
        red_fill = QColor(200, 40, 40, 70)  # 選択済みマーク(半透明の赤塗り)
        # 「選択済み」は現在の校正値(フォーム状態=保存/検出/手修正の確定値)で判定する。
        # これが実際に集計される値であり、ユーザーの修正が即マークへ反映される。
        selection = self._current_selection()
        self._overlay_selection_cache = selection  # 再描画判定の基準を更新
        for q in self._survey.questions:
            # 自由記述の記入領域(region)を緑枠で表示
            if q.region is not None:
                rx0, ry0, rx1, ry1 = q.region
                rbox = QRectF(rx0 * w, ry0 * h, (rx1 - rx0) * w, (ry1 - ry0) * h)
                rpen = QPen(green)
                rpen.setCosmetic(True)
                rpen.setWidthF(1.2)
                gitem = self._scene.addRect(rbox, rpen)
                gitem.setZValue(9)
                self._overlay_items.append(gitem)
            res = self._last_results.get(q.id)
            for opt in q.options:
                # box は box_expand だけ判定範囲を外側へ広げるので、赤/灰マーカーも
                # 同じ範囲(=当たり判定と同一)で描く。
                x0, y0, x1, y1 = self._option_marker_rect(opt)
                box = QRectF(x0 * w, y0 * h, (x1 - x0) * w, (y1 - y0) * h)
                # 選択済み判定はフォーム状態(=集計される確定値)に基づく
                checked = opt.value in selection.get(q.id, set())
                pen = QPen(red if checked else gray)
                pen.setCosmetic(True)  # ズームに依らず一定の線幅
                pen.setWidthF(3.2 if checked else 1.8)  # 太め(視認性優先)
                # 選択済みは半透明の赤で塗り、「選択済としてカウント中」を明示
                brush = QBrush(red_fill) if checked else QBrush(Qt.BrushStyle.NoBrush)
                ritem = self._scene.addRect(box, pen, brush)
                ritem.setZValue(10)
                self._overlay_items.append(ritem)
                # インク比率の数値表示は既定 OFF(検出チューニング時のみ)。
                # 太字＋白文字を下に重ねた縁取りで、背景に埋もれず読めるようにする。
                if self._overlay_ratios and res is not None and opt.value in res.ratios:
                    txt = f"{res.ratios[opt.value]:.2f}"
                    pos_x, pos_y = box.left(), box.bottom() + 1.0  # 四角の直下
                    ignore = QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
                    # 白い縁取り(下層): 太い白ペン＋白塗りで光彩のように見せる
                    halo = self._scene.addSimpleText(txt)
                    hfont = halo.font()
                    hfont.setBold(True)
                    hfont.setPointSizeF(hfont.pointSizeF() + 1.0)
                    halo.setFont(hfont)
                    hpen = QPen(QColor(255, 255, 255, 235))
                    hpen.setWidthF(3.0)
                    hpen.setCosmetic(True)
                    halo.setPen(hpen)
                    halo.setBrush(QColor(255, 255, 255, 235))
                    halo.setFlag(ignore)
                    halo.setZValue(10.9)
                    halo.setPos(pos_x, pos_y)
                    self._overlay_items.append(halo)
                    # 本体(上層): 検出色の太字数字(縁取り無し)
                    label = self._scene.addSimpleText(txt)
                    lfont = label.font()
                    lfont.setBold(True)
                    lfont.setPointSizeF(lfont.pointSizeF() + 1.0)
                    label.setFont(lfont)
                    label.setBrush(red if checked else gray)
                    label.setFlag(ignore)
                    label.setZValue(11)
                    label.setPos(pos_x, pos_y)
                    self._overlay_items.append(label)
        # 傾き補正モード中はオーバーレイもページと一緒に回転させる
        if self._skew_mode:
            self._apply_overlay_skew(self._skew_preview_deg)

    def set_fit_mode(self, mode: str) -> None:
        """PDF 表示のフィットモードを切り替える(ズームはフィットに戻す)。"""
        self._fit_mode = mode
        self._zoom_factor = 1.0
        self._apply_view()

    # 最大ズーム倍率(絶対スケールの上限)
    MAX_SCALE = 12.0

    def _fit_scale(self) -> float:
        """現在のフィットモードでページをビューポートに収める倍率。"""
        rect = self._scene.sceneRect()
        if rect.isEmpty():
            return 0.0
        viewport = self.pdf_view.viewport()
        if self._fit_mode == FIT_WIDTH:
            return viewport.width() / rect.width()
        return viewport.height() / rect.height()

    def _target_scale(self) -> float:
        """表示すべき絶対スケール = フィット倍率 × ズーム係数(範囲制限つき)。"""
        fit = self._fit_scale()
        if fit <= 0:
            return 0.0
        return max(fit, min(fit * self._zoom_factor, self.MAX_SCALE))

    # ------------------------------------------------------------- rasterize
    def _render_page(self, render_scale: float) -> None:
        """現在のページを render_scale(px/pt)で元PDFから描画し直す。

        シーン座標は点空間で固定し、ピクスマップは 1/render_scale に縮めて
        ページの実寸(pt)に一致させる。これにより再描画してもビュー変換は
        不変のまま、画素密度だけが変わる(表示位置・サイズは保たれる)。
        """
        if self.doc is None or self._page_index < 0:
            return
        render_scale = max(RENDER_MIN_SCALE, min(render_scale, RENDER_MAX_SCALE))
        try:
            # 傾き補正モードのプレビューは生画像＋アイテム回転、通常は補正を焼き込む
            if self._skew_mode:
                image = self.doc.render(self._page_index, scale=render_scale)
            else:
                image = self._render_image(self._page_index, render_scale)
            pixmap = QPixmap.fromImage(image)
        except Exception:
            # ここで失敗すると本表示が「真っ白」になる。Qt スロット内例外は標準
            # エラーに吐かれて GUI ビルドでは見えないため、明示的にログへ残す。
            log.exception(
                "本表示の描画に失敗 page=%s scale=%.3f", self._page_index, render_scale
            )
            return
        if image.isNull() or pixmap.isNull():
            log.error(
                "本表示の画像が空 page=%s scale=%.3f image_null=%s pixmap_null=%s size=%sx%s",
                self._page_index, render_scale, image.isNull(), pixmap.isNull(),
                image.width(), image.height(),
            )
            return
        log.debug(
            "本表示 描画 page=%s scale=%.3f size=%sx%s",
            self._page_index, render_scale, pixmap.width(), pixmap.height(),
        )
        self._scene.clear()
        self._grid_items = []  # scene.clear で消えたので参照も破棄
        item = self._scene.addPixmap(pixmap)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._page_item = item
        self._render_scale = render_scale
        self._scene.setSceneRect(
            0.0, 0.0, pixmap.width() / render_scale, pixmap.height() / render_scale
        )
        if self._skew_mode:
            # 中心回転(現在の補正角)＋px→pt縮尺。グリッドを重ねる。
            self._apply_skew_transform(-self._skew.get(self._page_index, 0.0))
            self._draw_skew_grid()
        else:
            item.setScale(1.0 / render_scale)  # ラスタpx -> ページpt
        # scene.clear() でオーバーレイ・ハイライト・編集矩形も消えるため整合させる
        self._overlay_items.clear()
        self._highlight_item = None
        if self._region_edit_item is not None or self._region_edit_qid is not None:
            self._region_edit_item = None
            self._region_edit_qid = None
            if self.form_pane is not None:
                self.form_pane.set_region_editing(None)
        if self._overlay_enabled:
            self._refresh_overlay()

    def _desired_render_scale(self) -> float:
        """現在の表示倍率と画面の devicePixelRatio に見合うラスタ倍率。"""
        disp = self._target_scale()
        if disp <= 0:
            return RENDER_MIN_SCALE
        dpr = self.pdf_view.devicePixelRatioF()
        needed = disp * dpr * RENDER_HEADROOM
        return max(RENDER_MIN_SCALE, min(needed, RENDER_MAX_SCALE))

    def _ensure_render_resolution(self) -> None:
        """表示倍率が現在のラスタ解像度を上回るなら、より高精細に描き直す。

        画質優先のため解像度を下げる再描画は行わない(上限到達で打ち切り)。
        ビュー変換は変えないので表示位置・サイズはそのまま、鮮明さだけ向上。
        """
        if self.doc is None or self._page_index < 0:
            return
        desired = self._desired_render_scale()
        if (
            desired > self._render_scale + 1e-3
            and self._render_scale < RENDER_MAX_SCALE - 1e-3
        ):
            self._render_page(desired)

    def _schedule_rerender(self) -> None:
        """ズーム/リサイズ確定後に高精細化を行うようデバウンス起動。"""
        self._rerender_timer.start()

    def _update_scrollbars(self) -> None:
        """ズーム状態に応じてスクロールバー方針を更新する。

        フィット表示(係数=1)では片軸だけ余るので一方を常時オフ。
        拡大時は両軸はみ出し得るため両方を AsNeeded にする。
        """
        zoomed = self._zoom_factor > 1.0 + 1e-6
        as_needed = Qt.ScrollBarPolicy.ScrollBarAsNeeded
        off = Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        if zoomed:
            self.pdf_view.setHorizontalScrollBarPolicy(as_needed)
            self.pdf_view.setVerticalScrollBarPolicy(as_needed)
        elif self._fit_mode == FIT_WIDTH:
            self.pdf_view.setHorizontalScrollBarPolicy(off)
            self.pdf_view.setVerticalScrollBarPolicy(as_needed)
        else:
            self.pdf_view.setHorizontalScrollBarPolicy(as_needed)
            self.pdf_view.setVerticalScrollBarPolicy(off)

    def _apply_view(self) -> None:
        """現在のフィットモード・ズーム係数・ビューポート寸法から
        絶対スケールを算出して適用する。リサイズ・ページ切替・モード変更
        の共通処理で、ウィンドウサイズに表示を連動させる。
        """
        if self._applying:
            return
        scale = self._target_scale()
        if scale <= 0:
            return
        self._applying = True
        try:
            self._update_scrollbars()
            # ビュー中心基準で再配置(リサイズ時に中心が保たれる)
            self.pdf_view.setTransformationAnchor(
                QGraphicsView.ViewportAnchor.NoAnchor
            )
            self.pdf_view.resetTransform()
            self.pdf_view.scale(scale, scale)
            self.pdf_view.setAlignment(
                (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                if self._fit_mode == FIT_WIDTH
                else (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            )
            # ホイール/ピンチ用にマウス基準アンカーへ戻す
            self.pdf_view.setTransformationAnchor(
                QGraphicsView.ViewportAnchor.AnchorUnderMouse
            )
        finally:
            self._applying = False
        # 表示倍率が上がっていれば、確定後に高精細化
        self._schedule_rerender()

    def _zoom(self, factor: float) -> None:
        """マウス位置を中心に倍率 factor で拡縮する。

        下限はフィット倍率(係数 1.0)、上限は MAX_SCALE。ズーム係数を更新し、
        以後のリサイズでも相対倍率が保たれる。
        """
        if factor <= 0:
            return
        fit = self._fit_scale()
        if fit <= 0:
            return
        current = self.pdf_view.transform().m11()
        target = max(fit, min(current * factor, self.MAX_SCALE))
        applied = target / current  # 実際に掛ける増分倍率
        if abs(applied - 1.0) < 1e-4:
            return
        self._zoom_factor = target / fit
        # スクロールバー方針変更が誘発する Resize で _apply_view が割り込まない
        # よう、増分スケール適用までをガードする。
        self._zooming = True
        try:
            self._update_scrollbars()
            # マウス位置を中心に増分スケール(なめらかなズーム)
            self.pdf_view.setTransformationAnchor(
                QGraphicsView.ViewportAnchor.AnchorUnderMouse
            )
            self.pdf_view.scale(applied, applied)
        finally:
            self._zooming = False
        # ズーム確定後に表示倍率相当の解像度へ描き直す
        self._schedule_rerender()

    # ------------------------------------------------------------------ events
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        try:
            return self._handle_event(watched, event)
        except RuntimeError:
            # アプリ終了時、破棄済み(C++ 削除済み)のウィジェットへイベントが
            # 届くことがある。安全に無視する。
            return False

    @staticmethod
    def _dropped_pdf(mime) -> str | None:
        """ドロップされた MIME から最初のローカル PDF パスを返す(無ければ None)。"""
        if mime is None or not mime.hasUrls():
            return None
        for url in mime.urls():
            p = url.toLocalFile()
            if p and p.lower().endswith(".pdf"):
                return p
        return None

    def _drop_index(self, watched: QObject, event: QDropEvent) -> int:
        """ドロップ位置から挿入先のページ番号(サムネイル間のギャップ)を求める。"""
        lst = self.thumbnail_list
        pos = event.position().toPoint()
        if watched is not lst.viewport():
            pos = lst.viewport().mapFrom(watched, pos)  # ビューポート座標へ
        idx = lst.indexAt(pos)
        if not idx.isValid():
            return lst.count()  # アイテム外(下方)→末尾に追記
        rect = lst.visualRect(idx)
        return idx.row() + (1 if pos.y() >= rect.center().y() else 0)

    def _insert_dropped_pdf(self, path: str, insert_at: int) -> None:
        """現在の文書の insert_at 位置にドロップPDFを挿入し、別名で保存して開く。

        保存先はダイアログで尋ねる(既定＝現在の文書のフォルダに、ドロップした
        ファイル名)。元の文書は変更しない。校正結果は挿入位置以降を挿入枚数ぶん
        ずらして、新ファイル自身へ埋め込む。
        """
        if self.doc is None:
            return
        cur = Path(self.doc.path)
        default_out = str(cur.with_name(Path(path).name))  # 現フォルダ＋ドロップ名
        out_str, _ = QFileDialog.getSaveFileName(
            self.window, "結合したPDFの保存先", default_out, "PDF Files (*.pdf)"
        )
        if not out_str:
            return
        if not out_str.lower().endswith(".pdf"):
            out_str += ".pdf"
        out_path = Path(out_str)
        self._capture_current_page()
        self._save_store()  # 現在の編集を元PDFへ埋め込み確定(元文書はそのまま維持)

        tmp = out_path.with_name(f"{out_path.stem}.insert_tmp.pdf")
        try:
            n = insert_pdf(cur, path, insert_at, tmp)
        except (OSError, ValueError) as e:
            tmp.unlink(missing_ok=True)
            QMessageBox.critical(self.window, "ページ挿入", f"挿入に失敗しました:\n{e}")
            return
        if n <= 0:
            tmp.unlink(missing_ok=True)
            return
        # 埋め込みは PDF 本体を out_path へ置換した後に行う(write_data は対象PDFを
        # 開いて上書きするため、実体が無い/置換前だと失敗・上書き消失する)。同一上書き
        # 時は _doc_store を閉じるので、シフト対象の結果を置換前に確保しておく。
        new_survey = self._survey
        new_pages = (
            dict(self._doc_store._pages) if self._doc_store is not None else None
        )
        # 出力先が現在開いているファイルと同一なら、閉じてから置換する
        same = out_path.resolve() == cur.resolve()
        if same:
            self.doc.close()
            self.doc = None
            self._doc_store = None
            self._clean_cache = None
        try:
            os.replace(tmp, out_path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            QMessageBox.critical(self.window, "ページ挿入", f"保存に失敗しました:\n{e}")
            if same:
                self.load_pdf(str(cur))
            return
        # PDF 本体が out_path に入った後で、フォーム＋(挿入位置以降をシフトした)校正
        # 結果を out_path 自身へ埋め込む。
        if new_survey is not None and new_pages is not None:
            newdoc = SurveyDocument(out_path, new_survey)
            newdoc._pages = new_pages
            newdoc.shift_pages(insert_at, n)
            newdoc.save()
        self.load_pdf(str(out_path))
        if self.doc is not None and 0 <= insert_at < len(self.doc):
            self.thumbnail_list.setCurrentRow(insert_at)
        sb = self.window.statusBar()
        if sb is not None:
            sb.showMessage(
                f"{n} ページを位置 {insert_at} に挿入し、{out_path.name} を作成しました",
                6000,
            )

    def _handle_event(self, watched: QObject, event: QEvent) -> bool:
        et = event.type()
        on_pdf = watched is self.pdf_view or watched is self.pdf_view.viewport()

        # サムネイル領域への PDF ドラッグ&ドロップで開く
        if watched is self.thumbnail_list or watched is self.thumbnail_list.viewport():
            if et in (QEvent.Type.DragEnter, QEvent.Type.DragMove) and isinstance(
                event, (QDragEnterEvent, QDragMoveEvent)
            ):
                if self._dropped_pdf(event.mimeData()):
                    event.acceptProposedAction()
                    return True
            elif et == QEvent.Type.Drop and isinstance(event, QDropEvent):
                path = self._dropped_pdf(event.mimeData())
                if path:
                    event.acceptProposedAction()
                    if self.doc is None:
                        self.load_pdf(path)  # 未読込なら開く
                    else:
                        # ドロップ位置(サムネイル間)にページを挿入してマージ
                        self._insert_dropped_pdf(path, self._drop_index(watched, event))
                    return True

        # Esc キーは _on_escape(ウィンドウのショートカット)で一元処理する。

        # 傾き補正モード: ビューポート上のドラッグでページを中心回転
        if (
            self._skew_mode
            and watched is self.pdf_view.viewport()
            and isinstance(event, QMouseEvent)
        ):
            if (
                et == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
            ):
                self._skew_drag = True
                self._skew_angle0 = self._mouse_angle(event)
                self._skew_base_deg = self._skew_preview_deg
                return True
            if et == QEvent.Type.MouseMove and self._skew_drag:
                delta = self._mouse_angle(event) - self._skew_angle0
                deg = self._skew_base_deg + delta
                self._apply_skew_transform(deg)
                self._apply_overlay_skew(deg)  # 既存の有効なオーバーレイを追随
                return True
            if et == QEvent.Type.MouseButtonRelease and self._skew_drag:
                self._skew_drag = False
                deg = self._norm_angle(self._skew_preview_deg)
                self._skew[self._page_index] = self._norm_angle(-deg)
                return True

        # PDF上のダブルクリックで、その位置の選択肢のチェックをトグル(校正用)
        if (
            not self._skew_mode
            and self._survey is not None
            and self.form_pane is not None
            and watched is self.pdf_view.viewport()
            and isinstance(event, QMouseEvent)
            and et == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._toggle_option_at(event.position().toPoint())
            return True

        if et == QEvent.Type.Resize:
            if watched is self.pdf_view.viewport():
                # ウィンドウ(ビューポート)サイズに表示を連動させる。
                # ズーム中も相対倍率(ズーム係数)を保ったまま拡縮する。
                # ただしズーム処理中の Resize は無視(二重適用防止)。
                if not self._zooming:
                    self._apply_view()
            elif watched is self.thumbnail_list.viewport():
                self._update_thumbnail_size()

        elif et == QEvent.Type.Wheel and on_pdf and isinstance(event, QWheelEvent):
            # Ctrl + ホイールでズーム。修飾なしは通常スクロールに委ねる。
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta != 0:
                    self._zoom(1.0015**delta)
                return True

        elif (
            et == QEvent.Type.NativeGesture
            and on_pdf
            and isinstance(event, QNativeGestureEvent)
        ):
            # Mac トラックパッドのピンチ(ズーム)
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self._zoom(1.0 + event.value())
                return True

        return super().eventFilter(watched, event)
