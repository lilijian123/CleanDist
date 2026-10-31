"""本地网页唯一业务入口；只公开白名单动作，不公开内部服务或凭据。"""
from pathlib import Path
import threading

from ..platforms import system
from ..demo.workload import create_demo_directory, launch_demo
from .ai import AiService
from .settings import DesktopSettings
from .files import FileService
from .resources import AppService, ProcessService


class DesktopApi:
    def __init__(self, settings=None, files=None, processes=None, apps=None):
        self._settings = settings or DesktopSettings()
        self._files = files or FileService()
        self._processes = processes or ProcessService()
        self._apps = apps or AppService()
        self._ai = AiService()
        self._window = None
        self._closed = False
        self._demo_process, self._demo_root = None, None
        self._lock = threading.RLock()

    def call(self, action, payload=None):
        try:
            if self._closed: raise ValueError('应用已关闭')
            if not isinstance(action, str) or (payload is not None and not isinstance(payload, dict)):
                raise ValueError('无效的请求')
            payload = payload or {}
            return {'ok': True, 'data': self._dispatch(action, payload)}
        except (ValueError, OSError, TypeError, KeyError, RuntimeError) as error:
            message = str(error) or '操作未完成'
            key, _ = self._settings.credentials()
            if key: message = message.replace(key, '[已隐藏]')
            return {'ok': False, 'error': message[:1500]}

    def _dispatch(self, action, data):
        if action == 'bootstrap':
            return {**self._settings.public(), 'platform': system.NAME,
                    'can_disk_scan': system.HAS_DISK_SCAN, 'can_force_stop': getattr(system, 'CAN_FORCE_STOP', False),
                    'can_trace': system.NAME == 'Linux', 'home': str(Path.home()),
                    'disks': [str(path) for path in system.list_disks()] if system.HAS_DISK_SCAN else [],
                    'app_sources': list(system.APP_SOURCES)}
        if action == 'state':
            files = self._files.state()
            return {'files': files, 'memory': self._processes.state(), 'apps': self._apps.state(),
                    'ai': self._ai.state(), 'operation': files.get('operation', {}),
                    'demo': {'running': bool(self._demo_process and self._demo_process.poll() is None),
                             'path': str(self._demo_root) if self._demo_root else ''}}
        if action == 'process_select':
            with self._lock:
                previous = self._processes.state().get('selected')
                selected = self._processes.select(**data)
                if not previous or list(previous['key']) != list(selected['key']):
                    self._ai.reset_process()
                return selected
        routes = {
            'scan': self._files.scan, 'scan_cancel': self._files.cancel,
            'file_groups': self._files.groups, 'file_rows': self._files.rows,
            'duplicates': self._files.duplicates, 'duplicates_cancel': self._files.cancel_duplicates, 'matrix': self._files.matrix,
            'cleanup_preview': self._files.preview, 'cleanup_execute': self._files.execute,
            'reports': self._files.reports, 'process_rows': self._processes.rows, 'process_icons': self._processes.icons,
            'process_stop': self._processes.stop,
            'process_trace': self._processes.trace, 'apps_scan': self._apps.scan,
            'app_rows': self._apps.rows, 'settings_save': self._settings.save,
            'settings_clear_key': self._settings.clear_key, 'ai_cancel': self._ai.cancel,
        }
        if action in routes: return routes[action](**data)
        if action == 'pick_folder':
            if self._window is None: raise ValueError('桌面窗口尚未就绪')
            import webview
            result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            return str(result[0]) if result else None
        if action == 'ai':
            with self._lock:
                mode = data.get('mode', 'analyze')
                if mode == 'cleanup':
                    summary = self._files.summary(**data.get('file', {}))
                    target = summary.get('name', '清理项')
                else:
                    summary = self._processes.summary()
                    target = f'{summary["process"]["name"]} · PID {summary["process"]["pid"]}'
                key, model = self._settings.credentials()
                return self._ai.ask(summary, mode, key, model, target)
        if action == 'demo': return self._demo(bool(data.get('enabled', True)))
        raise ValueError('不支持的操作')

    def _demo(self, enabled):
        with self._lock:
            if self._demo_process and self._demo_process.poll() is None:
                self._demo_process.terminate()
            self._demo_process = None
            if enabled:
                self._demo_root = create_demo_directory()
                self._demo_process = launch_demo(self._demo_root)
                self._files.scan([str(self._demo_root)], 'folder')
            return {'running': enabled, 'path': str(self._demo_root) if self._demo_root else ''}

    def _shutdown(self):
        if self._closed: return
        self._closed = True
        self._ai.shutdown()
        self._files.shutdown()
        self._processes.shutdown()
        self._apps.shutdown()
        self._settings.shutdown()
        if self._demo_process and self._demo_process.poll() is None:
            self._demo_process.terminate()
