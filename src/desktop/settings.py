"""桌面偏好与只写凭据接口；网页永远不取得保存的API Key。"""
import json
import os
from pathlib import Path
import tempfile
import threading

from ..core import ai


class DesktopSettings:
    def __init__(self, root=None):
        self._root = Path(root) if root is not None else ai.SECRET_ROOT
        self._path = self._root / '.desktop-settings.json'
        self._lock = threading.RLock()
        self._theme, self._model, self._key, self._error = 'dark', ai.MODELS[0], '', ''
        try:
            if self._path.exists():
                values = json.loads(self._path.read_text(encoding='utf-8'))
                if not isinstance(values, dict): raise ValueError('无效的本地配置')
                self._theme = values.get('theme', 'dark')
                self._model = values.get('model', ai.MODELS[0])
            else:
                # 延续旧版主题/模型偏好；这一步不创建Qt界面。
                from PySide6.QtCore import QSettings
                old = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'ResourceObserver', 'settings')
                self._theme = old.value('theme', 'dark')
                self._model = old.value('ai/model', ai.MODELS[0])
        except (OSError, ValueError):
            self._error = '本地配置未能读取，请检查配置文件或重新保存。'
        try:
            self._key = ai.read_secret()
        except (OSError, ValueError, RuntimeError):
            self._error = '本地加密密钥未能读取，请恢复凭据文件，或在设置中移除后重新保存。'
        if self._theme not in ('dark', 'light'): self._theme = 'dark'
        if self._model not in ai.MODELS: self._model = ai.MODELS[0]

    def public(self):
        with self._lock:
            return {'theme': self._theme, 'model': self._model, 'models': list(ai.MODELS),
                    'has_key': bool(self._key), 'settings_error': self._error}

    def credentials(self):
        with self._lock:
            return self._key, self._model

    def save(self, theme=None, model=None, key=None):
        with self._lock:
            theme = self._theme if theme is None else theme
            model = self._model if model is None else model
            if theme not in ('dark', 'light') or model not in ai.MODELS:
                raise ValueError('无效的主题或模型')
            if key is not None and key.strip():
                validated = ai.validate_key(key)
                ai.store_secret(validated)
                self._key = validated
            self._root.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self._root,
                        prefix='.desktop-settings.', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({'theme': theme, 'model': model}, stream)
                os.replace(temporary, self._path)
            finally:
                if temporary is not None and temporary.exists(): temporary.unlink()
            self._theme, self._model, self._error = theme, model, ''
            return self.public()

    def clear_key(self):
        with self._lock:
            ai.store_secret(None)
            self._key = ''
            return self.public()

    def shutdown(self):
        with self._lock: self._key = ''
