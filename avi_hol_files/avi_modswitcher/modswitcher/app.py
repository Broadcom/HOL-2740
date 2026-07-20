# /// script
# requires-python = ">=3.10"
# dependencies = ["PySide6>=6.6", "PyYAML>=6.0"]
# ///
import shutil
import sys
from pathlib import Path

import yaml
from PySide6.QtCore import Qt, Signal, QProcess
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QPlainTextEdit, QDialog, QSizePolicy,
)

BASE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = BASE_DIR / "manifest.yaml"
LOGO_PATH = BASE_DIR / "hol-logo.png"


def resolve_ansible_playbook_bin(configured_python_bin):
    if configured_python_bin:
        alongside_configured = Path(configured_python_bin).parent / "ansible-playbook"
        if alongside_configured.exists():
            return str(alongside_configured)
    found_on_path = shutil.which("ansible-playbook")
    if found_on_path:
        return found_on_path
    return "ansible-playbook"

STYLESHEET = """
QWidget { font-family: -apple-system, "Segoe UI", "Helvetica Neue", sans-serif; }
QMainWindow, #Root { background: #0d1321; }
#Header { background: #0a0f1c; border-bottom: 1px solid #1c2740; }
#HeaderTitle { color: #f2f5fb; font-size: 21px; font-weight: 700; }
#HeaderSubtitle { color: #6ea8fe; font-size: 14px; font-weight: 700; }
QPushButton#BackButton {
    background: transparent; color: #a9bbe0; border: 1px solid #2a3757;
    border-radius: 6px; padding: 6px 16px; font-size: 12px;
}
QPushButton#BackButton:hover { background: #16213b; color: white; }
QLabel#SectionTitle { font-size: 21px; font-weight: 700; color: #f2f5fb; }
QLabel#SectionSubtitle { color: #8b96ab; font-size: 13px; margin-bottom: 4px; }
Card { background: #182338; border-radius: 12px; border: 1px solid #263351; }
Card:hover { border: 1px solid #3b82f6; }
QLabel[role="cardTitle"] { font-size: 14px; font-weight: 600; color: #e7ecf5; background: transparent; border: none; }
QLabel[role="cardSubtitle"] { font-size: 12px; color: #8b96ab; background: transparent; border: none; }
QLabel[role="cardBadge"] {
    font-size: 11px; font-weight: 600; color: #7cb0ff; background: #1c2c4d;
    border-radius: 9px; padding: 3px 10px;
}
QLabel[role="chevron"] { color: #4b5670; font-size: 16px; background: transparent; border: none; }
QPushButton#RunButton {
    background: #3b82f6; color: white; border-radius: 8px; padding: 8px 18px;
    font-size: 13px; font-weight: 600; border: none;
}
QPushButton#RunButton:hover { background: #2f6fed; }
QPushButton#RunButton:disabled { background: #2a3a5c; color: #6b7690; }
QDialog { background: #0d1321; }
QPushButton#CloseButton {
    background: #182338; color: #cbd5e1; border-radius: 8px; padding: 8px 18px;
    font-size: 13px; font-weight: 600; border: 1px solid #2a3757;
}
QPushButton#CloseButton:hover { background: #202f4f; }
QPlainTextEdit#Console {
    background: #060a13; color: #d7e3f4; border-radius: 8px; padding: 12px;
    font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px; border: 1px solid #1c2740;
}
QLabel#StatusLabel { font-size: 13px; font-weight: 600; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
"""


class Card(QFrame):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


def make_row_card(title, subtitle, badge_text=None, trailing_widget=None):
    card = Card()
    layout = QHBoxLayout(card)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(14)

    text_col = QVBoxLayout()
    text_col.setSpacing(3)
    title_label = QLabel(title)
    title_label.setProperty("role", "cardTitle")
    text_col.addWidget(title_label)
    if subtitle:
        subtitle_label = QLabel(subtitle)
        subtitle_label.setProperty("role", "cardSubtitle")
        subtitle_label.setWordWrap(True)
        text_col.addWidget(subtitle_label)
    layout.addLayout(text_col, stretch=1)

    if badge_text:
        badge = QLabel(badge_text)
        badge.setProperty("role", "cardBadge")
        layout.addWidget(badge)

    if trailing_widget:
        layout.addWidget(trailing_widget)
    else:
        chevron = QLabel("›")
        chevron.setProperty("role", "chevron")
        layout.addWidget(chevron)

    return card


class Header(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Header")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(64)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)

        if LOGO_PATH.exists():
            logo = QLabel()
            pix = QPixmap(str(LOGO_PATH)).scaledToHeight(36, Qt.SmoothTransformation)
            logo.setPixmap(pix)
            layout.addWidget(logo)
            layout.addSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.title_label = QLabel("Hands-on Labs Module Switcher")
        self.title_label.setObjectName("HeaderTitle")
        self.subtitle_label = QLabel("Select a lab to get started")
        self.subtitle_label.setObjectName("HeaderSubtitle")
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.subtitle_label)
        layout.addLayout(title_col)

        layout.addStretch()

        self.back_button = QPushButton("‹  All Labs")
        self.back_button.setObjectName("BackButton")
        self.back_button.hide()
        layout.addWidget(self.back_button)

    def set_subtitle(self, text):
        self.subtitle_label.setText(text)


class SkuListPage(QWidget):
    sku_selected = Signal(dict)

    def __init__(self, skus, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(4)

        outer.addWidget(self._section_title("Please select your lab"))
        outer.addWidget(self._section_subtitle(f"{len(skus)} labs available"))
        outer.addSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        col = QVBoxLayout(container)
        col.setSpacing(10)
        col.setContentsMargins(0, 0, 0, 0)

        for sku in skus:
            card = make_row_card(
                sku["title"],
                sku["id"],
                badge_text=f'{len(sku["modules"])} modules',
            )
            card.clicked.connect(lambda checked=False, s=sku: self.sku_selected.emit(s))
            col.addWidget(card)

        col.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

    @staticmethod
    def _section_title(text):
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    @staticmethod
    def _section_subtitle(text):
        label = QLabel(text)
        label.setObjectName("SectionSubtitle")
        return label


class ModuleListPage(QWidget):
    module_run_requested = Signal(dict, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 32, 40, 32)
        outer.setSpacing(4)

        self.title_label = QLabel("")
        self.title_label.setObjectName("SectionTitle")
        outer.addWidget(self.title_label)

        self.subtitle_label = QLabel("Pick a module to fast-forward to that point in the lab")
        self.subtitle_label.setObjectName("SectionSubtitle")
        outer.addWidget(self.subtitle_label)
        outer.addSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.col = QVBoxLayout(self.container)
        self.col.setSpacing(10)
        self.col.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self.container)
        outer.addWidget(scroll)

        self._current_sku = None

    def load_sku(self, sku):
        self._current_sku = sku
        self.title_label.setText(sku["title"])

        while self.col.count():
            item = self.col.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for module in sku["modules"]:
            run_button = QPushButton("Run ▸")
            run_button.setObjectName("RunButton")
            run_button.setFixedWidth(90)
            run_button.clicked.connect(
                lambda checked=False, m=module: self.module_run_requested.emit(sku, m)
            )
            card = make_row_card(module["name"], module["description"], trailing_widget=run_button)
            self.col.addWidget(card)

        self.col.addStretch()


class RunDialog(QDialog):
    def __init__(self, sku, module, python_bin, parent=None):
        super().__init__(parent)
        self.setWindowTitle(module["name"])
        self.resize(640, 440)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel(f'{sku["id"]} – {module["name"]}')
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #e7ecf5;")
        header_row.addWidget(title)
        header_row.addStretch()
        self.status_label = QLabel("● Running")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setStyleSheet("color: #fbbf24;")
        header_row.addWidget(self.status_label)
        layout.addLayout(header_row)

        self.console = QPlainTextEdit()
        self.console.setObjectName("Console")
        self.console.setReadOnly(True)
        layout.addWidget(self.console, stretch=1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.action_button = QPushButton("Cancel")
        self.action_button.setObjectName("CloseButton")
        self.action_button.clicked.connect(self._on_action)
        button_row.addWidget(self.action_button)
        layout.addLayout(button_row)

        script_path = BASE_DIR / module["playbook"]
        ansible_bin = resolve_ansible_playbook_bin(python_bin)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.setWorkingDirectory(str(script_path.parent))
        self.process.readyReadStandardOutput.connect(self._on_output)
        self.process.finished.connect(self._on_finished)
        self.process.start(ansible_bin, ["-e", "ANSIBLE_HOST_KEY_CHECKING=False", str(script_path), "-v"])

    def _on_output(self):
        text = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        self.console.appendPlainText(text.rstrip("\n"))

    def _on_finished(self, exit_code, _exit_status):
        if exit_code == 0:
            self.status_label.setText("● Completed")
            self.status_label.setStyleSheet("color: #22c55e;")
        else:
            self.status_label.setText(f"● Failed (exit {exit_code})")
            self.status_label.setStyleSheet("color: #ef4444;")
        self.action_button.setText("Close")

    def _on_action(self):
        if self.process.state() == QProcess.Running:
            self.process.kill()
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, manifest):
        super().__init__()
        self.setWindowTitle("Hands-on Labs Module Switcher")
        self.resize(760, 620)
        self.python_bin = (manifest.get("config") or {}).get("python_bin")

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.header = Header()
        outer.addWidget(self.header)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        self.sku_page = SkuListPage(manifest["skus"])
        self.module_page = ModuleListPage()
        self.stack.addWidget(self.sku_page)
        self.stack.addWidget(self.module_page)

        self.sku_page.sku_selected.connect(self._show_modules)
        self.module_page.module_run_requested.connect(self._run_module)
        self.header.back_button.clicked.connect(self._show_skus)

    def _show_modules(self, sku):
        self.module_page.load_sku(sku)
        self.stack.setCurrentWidget(self.module_page)
        self.header.set_subtitle(sku["id"])
        self.header.back_button.show()

    def _show_skus(self):
        self.stack.setCurrentWidget(self.sku_page)
        self.header.set_subtitle("Select a lab to get started")
        self.header.back_button.hide()

    def _run_module(self, sku, module):
        dialog = RunDialog(sku, module, self.python_bin, parent=self)
        dialog.exec()


def main():
    manifest = yaml.safe_load(MANIFEST_PATH.read_text())
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    if LOGO_PATH.exists():
        app.setWindowIcon(QIcon(str(LOGO_PATH)))
    window = MainWindow(manifest)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
