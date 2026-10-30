"""DeepSeek 设置、可取消的网络请求和进程分析回答。"""
import json
import re
import time

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Qt
from PySide6.QtGui import QKeySequence, QTextDocument
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout,
    QLineEdit, QMenu, QSizePolicy, QTextEdit, QToolButton, QVBoxLayout, QWidget)

from ..core.ai import (API_URL, MODELS, build_request, parse_response,
    read_secret, store_secret, summarize_report, validate_key)
from ..platforms import system
from .widgets import button, label


class DeepSeekClient(QObject):
    TIMEOUT_MS = 60000
    MAX_RESPONSE = 1024 * 1024

    def __init__(self, parent=None):
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self.reply = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.fail('DeepSeek 请求超时，请稍后重试。'))

    def start(self, api_key, payload, success, failure):
        self.cancel()
        request = QNetworkRequest(QUrl(API_URL))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, 'application/json')
        request.setRawHeader(b'Authorization', ('Bearer ' + validate_key(api_key)).encode('ascii'))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.data = bytearray()
        self.reason = None
        reply = self.manager.post(request, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        reply.setReadBufferSize(self.MAX_RESPONSE + 1)
        self.reply = reply

        def consume():
            if self.reply is not reply or not reply.isOpen():
                return
            self.data.extend(bytes(reply.read(self.MAX_RESPONSE + 1 - len(self.data))))
            if len(self.data) > self.MAX_RESPONSE:
                self.fail('DeepSeek 回答过大，请重试。')

        def finish():
            if self.reply is not reply:
                reply.deleteLater()
                return
            consume()
            # consume() 可能同步触发 abort()/finished()，以身份核验避免重复回调。
            if self.reply is not reply:
                return
            self.reply = None
            self.timer.stop()
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            try:
                if self.reason:
                    raise ValueError(self.reason)
                if status is None:
                    raise ValueError('无法连接 DeepSeek，请检查网络或代理设置。')
                if reply.error() != QNetworkReply.NetworkError.NoError and status == 200:
                    raise ValueError('DeepSeek 连接中断，回答未完整接收，请重试。')
                answer = parse_response(bytes(self.data), status)
            except ValueError as error:
                failure(str(error))
            else:
                success(answer)
            finally:
                reply.deleteLater()
                self.data.clear()

        reply.readyRead.connect(consume)
        reply.finished.connect(finish)
        self.timer.start(self.TIMEOUT_MS)

    def fail(self, message):
        if self.reply is not None:
            self.reason = message
            self.reply.abort()

    def cancel(self):
        self.timer.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()


class SecretEdit(QLineEdit):
    def __init__(self):
        super().__init__()
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setDragEnabled(False)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy) or event.matches(QKeySequence.StandardKey.Cut):
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        paste = menu.addAction('粘贴', self.paste)
        paste.setEnabled(not self.isReadOnly())
        menu.addAction('全选', self.selectAll)
        menu.exec(event.globalPos())


class AiSettingsPage(QWidget):
    def __init__(self, settings, run_job):
        super().__init__()
        self.settings, self.run_job = settings, run_job
        self._key = ''
        self._model = settings.value('ai/model', MODELS[0])
        if self._model not in MODELS:
            self._model = MODELS[0]
        self.closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(label('AI 设置', 'title'))
        layout.addWidget(label('DeepSeek', 'section'))
        layout.addWidget(label('API Key'))
        self.key_edit = SecretEdit()
        self.key_edit.setMaxLength(512)
        self.key_edit.setPlaceholderText('输入 DeepSeek API Key')
        self.key_edit.setAccessibleName('DeepSeek API Key')
        layout.addWidget(self.key_edit)
        layout.addWidget(label('模型'))
        self.model = QComboBox()
        self.model.addItem('DeepSeek Flash', MODELS[0])
        self.model.addItem('DeepSeek V4 Pro', MODELS[1])
        self.model.setCurrentIndex(MODELS.index(self._model))
        layout.addWidget(self.model)
        row = QHBoxLayout()
        self.save_button = button('保存', self.save, True)
        self.clear_button = button('清除密钥', self.clear)
        row.addWidget(self.save_button)
        row.addWidget(self.clear_button)
        row.addStretch()
        layout.addLayout(row)
        self.status = label('未配置 API Key', 'muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        note = label('仅连接 DeepSeek 官方接口。保存后，下次启动自动读取密钥。API Key 加密保存在项目的 .env，自动生成的主密钥另存于 .env.key；请妥善保管这两个文件。点击 AI 分析、AI 预测或文件条目的 AI 图标后才发送相应摘要并产生 API 用量。', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.set_busy(True)
        self.status.setText('正在读取本地加密配置…')
        def loaded(key):
            if not self.closed:
                self._key = key
                self.key_edit.setText(key)
                self.status.setText('已读取本地保存的密钥' if key else '未配置 API Key，请输入并保存')
        self.run_job(read_secret, loaded, lambda _: self.show_status('无法读取本地配置，请检查 .env 和 .env.key。'),
                     lambda: self.set_busy(False))

    def credentials(self):
        return self._key, self._model

    def show_status(self, text):
        if not self.closed:
            self.status.setText(text)

    def set_busy(self, busy):
        if not self.closed:
            for widget in (self.key_edit, self.model, self.save_button, self.clear_button):
                widget.setEnabled(not busy)

    def save(self):
        try:
            key = validate_key(self.key_edit.text())
        except ValueError as error:
            self.status.setText(str(error))
            return
        self._key, self._model = key, self.model.currentData()
        self.settings.setValue('ai/model', self._model)
        self.persist(self._key)

    def clear(self):
        self._key = ''
        self.key_edit.clear()
        self.persist(None)

    def persist(self, key):
        def accepted(_=None):
            self.settings.sync()
            self.show_status('密钥已加密保存，下次启动自动读取' if key is not None else '本地保存的 API Key 已清除')
        def failed(_):
            self.show_status('保存失败，当前密钥仅本次运行有效，请检查本地文件权限。' if key is not None else
                             '本次运行已清空，但本地保存的密钥未能清除，请检查文件权限后重试。')
        self.set_busy(True)
        self.status.setText('正在更新本地加密配置…')
        # 文件写入完全在任务中完成，不依赖关闭窗口后可能被丢弃的UI回调。
        self.run_job(lambda: store_secret(key), accepted, failed, lambda: self.set_busy(False))

    def shutdown(self):
        self.closed = True
        self._key = ''


class AnswerDocument(QTextDocument):
    def loadResource(self, resource_type, name):
        # 回答只展示文本排版，不从模型给出的地址加载本地或远程资源。
        return QByteArray()


def show_answer(view, text):
    # 兼容模型偶尔忽略纯文本约束的回答，不保留标题/加粗样式或加载图片。
    if re.search(r'(?m)^\s*(?:#{1,6}\s|[-*+>]\s|\d+[.)]\s|\|)|[`*]|!?\[[^\]\n]*\]\(|(?<!\w)_[^_\n]+_(?!\w)', text):
        document = AnswerDocument()
        document.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub |
                             QTextDocument.MarkdownFeature.MarkdownNoHTML)
        text = document.toPlainText()
    view.setPlainText(text)


class FileAiDialog(QDialog):
    def __init__(self, settings_page, open_settings, parent=None):
        super().__init__(parent)
        self.settings_page, self.open_settings = settings_page, open_settings
        self.client = DeepSeekClient(self)
        self.summary = None
        self.generation = 0
        self.busy = self.closed = False
        self.setWindowTitle('AI 清理解读')
        self.resize(560, 340)
        layout = QVBoxLayout(self)
        self.target = label('', 'section')
        self.target.setTextFormat(Qt.TextFormat.PlainText)
        self.target.setWordWrap(True)
        layout.addWidget(self.target)
        note = label('仅发送路径、大小和风险依据，不读取文件内容。AI 建议不会改变本地清理规则。', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = label('', 'muted')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.answer = QTextEdit()
        self.answer.setDocument(AnswerDocument(self.answer))
        self.answer.setReadOnly(True)
        self.answer.setPlaceholderText('用途、清理建议和影响将显示在这里。')
        layout.addWidget(self.answer, 1)
        row = QHBoxLayout()
        self.retry_button = button('重新解读', self.ask)
        self.cancel_button = button('取消请求', self.cancel)
        row.addWidget(self.retry_button)
        row.addWidget(self.cancel_button)
        row.addStretch()
        row.addWidget(button('AI 设置', self.settings))
        row.addWidget(button('关闭', self.close))
        layout.addLayout(row)
        self.finished.connect(self.cancel)
        self.refresh()

    def show_summary(self, summary):
        if self.closed:
            return
        self.cancel()
        self.summary = summary
        self.target.setText(summary['name'])
        self.answer.clear()
        self.show()
        self.raise_()
        self.ask()

    def refresh(self):
        self.retry_button.setEnabled(not self.closed and not self.busy)
        self.cancel_button.setVisible(self.busy)

    def ask(self):
        if self.closed or self.busy or self.summary is None:
            return
        try:
            key, model = self.settings_page.credentials()
            if not key:
                raise ValueError('请先打开 AI 设置，填写并保存 DeepSeek API Key。')
            payload = build_request(self.summary, 'cleanup', model)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.generation += 1
        generation = self.generation
        self.busy = True
        self.answer.clear()
        self.status.setText('正在等待 DeepSeek 解读…')
        self.refresh()

        def show(text, success):
            if self.closed or generation != self.generation:
                return
            self.busy = False
            self.status.setText('已收到解读' if success else text)
            if success:
                show_answer(self.answer, text)
            self.refresh()

        try:
            self.client.start(key, payload, lambda text: show(text, True), lambda text: show(text, False))
        except (ValueError, RuntimeError):
            show('请求未能发出，请检查 API Key 或重新尝试。', False)

    def cancel(self):
        self.generation += 1
        self.client.cancel()
        if self.busy:
            self.status.setText('已取消请求。已发出的请求可能仍产生 API 用量。')
        self.busy = False
        self.refresh()

    def settings(self):
        self.close()
        self.open_settings()

    def shutdown(self):
        self.closed = True
        self.cancel()
        self.close()


class ProcessAiWidget(QWidget):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.settings_page = self.open_settings = None
        self.client = DeepSeekClient(self)
        self.generation = 0
        self.busy = self.closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        self.analyze_button = button('AI 分析', lambda: self.ask('analyze'), True)
        self.predict_button = button('AI 预测', lambda: self.ask('predict'))
        self.cancel_button = button('取消请求', self.cancel)
        for widget in (self.analyze_button, self.predict_button, self.cancel_button):
            row.addWidget(widget)
        layout.addLayout(row)
        self.more_toggle = QToolButton()
        self.more_toggle.setText('发送内容与设置')
        self.more_toggle.setCheckable(True)
        self.more_toggle.setAutoRaise(True)
        self.more_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.more_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(self.more_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.options = QWidget()
        options_layout = QVBoxLayout(self.options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.preview_button = button('查看发送摘要', self.preview)
        row.addWidget(self.preview_button)
        row.addWidget(button('AI 设置', lambda: self.open_settings() if self.open_settings else None))
        row.addStretch()
        options_layout.addLayout(row)
        self.status = label('等待有效采样；先在 AI 设置中保存 API Key。', 'muted')
        self.status.setWordWrap(True)
        note = label('发送所选进程的数值指标与有限趋势，不发送完整路径、文件内容或命令行。预测仅基于观察窗口，供排查参考。', 'muted')
        note.setWordWrap(True)
        options_layout.addWidget(note)
        self.options.hide()
        layout.addWidget(self.options)
        layout.addWidget(self.status)
        self.more_toggle.toggled.connect(self.toggle_options)
        self.answer = QTextEdit()
        self.answer.setDocument(AnswerDocument(self.answer))
        self.answer.setReadOnly(True)
        self.answer.setMinimumHeight(80)
        self.answer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.answer.setPlaceholderText('DeepSeek 的回答将显示在这里，可选择并复制。')
        layout.addWidget(self.answer, 1)
        self.refresh()

    def toggle_options(self, expanded):
        self.options.setVisible(expanded)
        self.more_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def refresh(self):
        report, _, captured = self.context()
        ready = bool(not self.closed and report and captured is not None and time.monotonic() - captured <= 15)
        self.analyze_button.setEnabled(ready and not self.busy)
        self.predict_button.setEnabled(ready and not self.busy)
        self.preview_button.setEnabled(ready)
        self.cancel_button.setEnabled(self.busy)
        self.cancel_button.setVisible(self.busy)
        if ready and not self.busy and self.status.text().startswith('等待有效采样'):
            self.status.setText('基础指标已就绪，可进行 AI 分析或 AI 预测；详细指标将陆续补充。')

    def summary(self):
        report, name, captured = self.context()
        if report is None or captured is None or time.monotonic() - captured > 15:
            raise ValueError('暂无有效的近期采样，请保持分析页打开并等待采样。')
        summary = summarize_report(report, system.NAME, name)
        summary['snapshot_age_seconds'] = round(max(0, time.monotonic() - captured), 1)
        return summary

    def ask(self, mode):
        if self.closed or self.busy:
            return
        try:
            summary = self.summary()
            key, model = self.settings_page.credentials() if self.settings_page else ('', MODELS[0])
            if not key:
                raise ValueError('请先打开 AI 设置，填写并保存 DeepSeek API Key。')
            payload = build_request(summary, mode, model)
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.generation += 1
        generation = self.generation
        self.busy = True
        self.answer.clear()
        caption = 'AI 预测' if mode == 'predict' else 'AI 分析'
        heading = f'{caption} · {summary["process"]["name"]} · PID {summary["process"]["pid"]} · {model}'
        self.status.setText(heading + '\n正在等待 DeepSeek 回答…')
        self.refresh()
        def show(text, success):
            if self.closed or generation != self.generation:
                return
            self.busy = False
            self.status.setText(heading + ('\n已收到回答' if success else '\n' + text))
            if success:
                show_answer(self.answer, text)
            self.refresh()
        try:
            self.client.start(key, payload, lambda text: show(text, True), lambda text: show(text, False))
        except (ValueError, RuntimeError):
            show('请求未能发出，请检查 API Key 或重新尝试。', False)

    def preview(self):
        try:
            summary = self.summary()
        except ValueError as error:
            self.status.setText(str(error))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('发送给 DeepSeek 的进程摘要')
        dialog.resize(620, 560)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))
        layout.addWidget(text)
        layout.addWidget(button('关闭', dialog.close))
        dialog.exec()

    def cancel(self):
        self.generation += 1
        self.client.cancel()
        self.busy = False
        if not self.closed:
            self.status.setText('已取消请求。已发出的请求可能仍产生 DeepSeek API 用量。')
            self.refresh()

    def reset(self):
        self.cancel()
        self.answer.clear()
        self.status.setText('等待有效采样；点击 AI 分析或 AI 预测后才会发送。')

    def shutdown(self):
        self.closed = True
        self.cancel()
