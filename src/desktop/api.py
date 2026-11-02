"""本地网页唯一业务入口；只公开白名单动作，不公开内部服务或凭据。"""
from pathlib import Path
import threading

from ..platforms import system
from ..demo.workload import create_demo_directory, launch_demo
from .ai import AiService
from .settings import DesktopSettings
from .files import FileService
from .resources import AppService, ProcessService
from .process_control import ProcessControl
from .directory_governance import DirectoryService
from .dependencies import DependencyService


class DesktopApi:
    def __init__(self, settings=None, files=None, processes=None, apps=None,
                 control=None, directories=None, dependencies=None):
        self._settings = settings or DesktopSettings()
        self._files = files or FileService(purpose='cleanup')
        self._file_workspaces = {'cleanup': self._files,
                                 **{tool: FileService(purpose=tool) for tool in ('large', 'duplicates', 'matrix')}}
        self._processes = processes or ProcessService()
        self._apps = apps or AppService()
        self._control = control or ProcessControl()
        self._directories = directories or DirectoryService()
        self._dependencies = dependencies or DependencyService(self._directories.preview)
        self._processes.set_trace_listener(self._directories.notify)
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
                    'can_trace': system.NAME in ('Linux', 'Windows'), 'home': str(Path.home()),
                    'disks': [str(path) for path in system.list_disks()] if system.HAS_DISK_SCAN else [],
                    'app_sources': list(system.APP_SOURCES)}
        if action == 'state':
            workspaces = {tool: service.state() for tool, service in self._file_workspaces.items()}
            files = {**workspaces['cleanup'], 'workspaces': workspaces}
            return {'files': files, 'memory': self._processes.state(), 'apps': self._apps.state(),
                    'ai': self._ai.state(), 'operation': next((state['operation'] for state in workspaces.values()
                                                              if state.get('operation', {}).get('busy')), {}),
                    'control': self._control.state(), 'directories': self._directories.state(),
                    'dependencies': self._dependencies.state(),
                    'demo': {'running': bool(self._demo_process and self._demo_process.poll() is None),
                             'path': str(self._demo_root) if self._demo_root else ''}}
        if action == 'process_select':
            with self._lock:
                previous = self._processes.state().get('selected')
                selected = self._processes.select(**data)
                if not previous or list(previous['key']) != list(selected['key']):
                    self._ai.reset_process()
                return selected
        if action in ('process_limit_apply', 'process_limit_clear'):
            with self._lock:
                selected = self._processes.state().get('selected')
                if not selected or list(selected['key']) != data.get('key'):
                    raise ValueError('只能管控当前选中的进程身份')
                operation = self._control.apply if action == 'process_limit_apply' else self._control.clear
                return operation(**data)
        file_routes = {'scan': 'scan', 'scan_cancel': 'cancel', 'file_groups': 'groups', 'file_rows': 'rows',
                       'duplicates': 'duplicates', 'duplicates_cancel': 'cancel_duplicates', 'matrix': 'matrix',
                       'cleanup_preview': 'preview', 'cleanup_execute': 'execute', 'reports': 'reports'}
        if action in file_routes:
            arguments = dict(data)
            workspace = arguments.pop('workspace', 'cleanup')
            if workspace not in self._file_workspaces:
                raise ValueError('未知的文件工具')
            return getattr(self._file_workspaces[workspace], file_routes[action])(**arguments)
        routes = {
            'process_rows': self._processes.rows, 'process_icons': self._processes.icons,
            'process_stop': self._processes.stop,
            'process_trace': self._processes.trace, 'apps_scan': self._apps.scan,
            'app_rows': self._apps.rows, 'settings_save': self._settings.save,
            'settings_clear_key': self._settings.clear_key, 'ai_cancel': self._ai.cancel,
            'ai_request': self._ai.request_data,
            'directory_add': self._directories.add, 'directory_remove': self._directories.remove,
            'directory_rows': self._directories.rows, 'vault_preview': self._directories.preview,
            'vault_recycle': self._directories.recycle, 'vault_restore': self._directories.restore,
            'vault_purge': self._directories.purge,
            'deps_scan': self._dependencies.scan, 'deps_rows': self._dependencies.rows,
            'deps_details': self._dependencies.details, 'deps_preview': self._dependencies.preview,
            'deps_uninstall': self._dependencies.uninstall,
            'portable_register': self._dependencies.portable_register,
            'portable_keep': self._dependencies.portable_keep,
            'portable_preview': self._dependencies.portable_preview,
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
                    file = dict(data.get('file', {}))
                    workspace = file.pop('workspace', 'cleanup')
                    if workspace not in self._file_workspaces:
                        raise ValueError('未知的文件工具')
                    summary = self._file_workspaces[workspace].summary(**file)
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
        self._processes.shutdown()
        self._dependencies.shutdown()
        self._directories.shutdown()
        for service in self._file_workspaces.values():
            service.shutdown()
        self._apps.shutdown()
        self._settings.shutdown()
        if self._demo_process and self._demo_process.poll() is None:
            self._demo_process.terminate()
        self._control.shutdown()
