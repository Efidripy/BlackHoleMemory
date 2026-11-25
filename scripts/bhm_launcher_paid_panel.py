"""Launcher LLM card parts: the Local/Remote/Paid selector and the paid-model form (URL, model, API key, test)."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from bhm_launcher_host import launcher as _launcher
from bhm_launcher_paid_tier import PAID_TIER_DEFAULT_BASE_URL
from bhm_launcher_paid_tier import PAID_TIER_DEFAULT_MODEL
from bhm_launcher_paid_tier import paid_api_key_present

CHOICES = ("local", "remote", "paid")


class ModeSelector(_launcher.QWidget):
    changed = _launcher.pyqtSignal(str)

    def __init__(self, choice: str) -> None:
        super().__init__()
        row = _launcher.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.buttons: dict[str, _launcher.QPushButton] = {}
        for name in CHOICES:
            button = _launcher.QPushButton(name.capitalize())
            button.setObjectName("ModeToggle")
            button.setCheckable(True)
            button.setFixedSize(58, 24)
            button.setCursor(_launcher.Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, n=name: self.select(n))
            self.buttons[name] = button
            row.addWidget(button)
        self.choice = choice
        self.show_choice(choice)

    def show_choice(self, choice: str) -> None:
        self.choice = choice
        for name, button in self.buttons.items():
            button.setChecked(name == choice)

    def select(self, choice: str) -> None:
        self.show_choice(choice)
        self.changed.emit(choice)


class PaidPanel(_launcher.QFrame):
    """Paid model settings. The key is saved by the dashboard in the user environment, never kept in settings."""

    settings_changed = _launcher.pyqtSignal(str, str)  # base_url, model
    key_entered = _launcher.pyqtSignal(str)
    test_done = _launcher.pyqtSignal(str)

    def __init__(self, base_url: str, model: str) -> None:
        super().__init__()
        layout = _launcher.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        def field(label: str, value: str = "", hide: bool = False) -> _launcher.QLineEdit:
            caption = _launcher.QLabel(label)
            caption.setObjectName("FieldLabel")
            edit = _launcher.QLineEdit(value)
            edit.setObjectName("LlmInput")
            edit.setFixedHeight(26)
            if hide:
                edit.setEchoMode(_launcher.QLineEdit.EchoMode.Password)
            layout.addWidget(caption)
            layout.addWidget(edit)
            return edit

        self.url_input = field("Paid model URL (local proxy)", base_url or PAID_TIER_DEFAULT_BASE_URL)
        self.model_input = field("Model", model or PAID_TIER_DEFAULT_MODEL)
        self.key_input = field("API key", hide=True)
        self.show_key_state()
        for edit in (self.url_input, self.model_input):
            edit.editingFinished.connect(self.emit_settings)
        self.key_input.editingFinished.connect(self.emit_key)

        self.test_button = _launcher.QPushButton("Test paid model")
        self.test_button.setObjectName("GhostButton")
        self.test_button.setFixedHeight(26)
        self.test_button.clicked.connect(self.run_test)
        self.result = _launcher.QLabel("Secrets and personal data are filtered before sending.")
        self.result.setObjectName("Muted")
        self.result.setWordWrap(True)
        layout.addWidget(self.test_button)
        layout.addWidget(self.result)
        self.test_done.connect(self.result.setText)

    def show_key_state(self) -> None:
        saved = paid_api_key_present()
        self.key_input.setPlaceholderText("saved in your user environment" if saved else "paste the API key")

    def emit_settings(self) -> None:
        self.settings_changed.emit(self.url_input.text().strip(), self.model_input.text().strip())

    def emit_key(self) -> None:
        key = self.key_input.text().strip()
        if key:
            self.key_input.clear()
            self.key_entered.emit(key)
            self.show_key_state()

    def run_test(self) -> None:
        url, model = self.url_input.text().strip(), self.model_input.text().strip()
        key = self.key_input.text().strip() or os.environ.get("BHM_PAID_LLM_API_KEY", "")
        self.test_done.emit("Testing...")
        threading.Thread(target=self.probe, args=(url, model, key), daemon=True).start()

    def probe(self, url: str, model: str, key: str) -> None:
        started = time.perf_counter()
        try:
            body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}).encode()
            request = urllib.request.Request(
                url.rstrip("/") + "/chat/completions", data=body, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=45) as response:
                response.read(4096)
            self.test_done.emit(f"OK: {model} answered in {time.perf_counter() - started:.1f}s")
        except Exception as exc:  # shown to the operator; the key never appears in these messages
            self.test_done.emit(f"Failed: {type(exc).__name__} {str(exc)[:80]}")
