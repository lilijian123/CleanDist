"""可取消、代次隔离的DeepSeek请求，不依赖Qt网络对象。"""
import json
import re
import threading
import urllib.error
import urllib.request

from ..core.ai import API_URL, build_request, parse_response, validate_key


def plain_answer(text):
    text = re.sub(r'^\s*#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*([^*]+)\*\*|`([^`]+)`', lambda match: match[1] or match[2], text)
    return text.strip()


class AiService:
    def __init__(self):
        self._lock = threading.RLock()
        self._generation, self._closed, self._response = 0, False, None
        self._state = {'busy': False, 'text': '', 'error': '', 'target': '', 'mode': '', 'request': 0}

    def state(self):
        with self._lock: return dict(self._state)

    def ask(self, summary, mode, key, model, target):
        if not key: raise ValueError('请先在设置中保存 DeepSeek API Key。')
        key = validate_key(key)
        payload = build_request(summary, mode, model)
        self.cancel()
        with self._lock:
            if self._closed: raise ValueError('应用已关闭')
            self._generation += 1
            generation = self._generation
            self._state = {'busy': True, 'text': '', 'error': '', 'target': str(target), 'mode': mode, 'request': generation}
        threading.Thread(target=self._run, args=(generation, payload, key), daemon=True).start()
        return self.state()

    def _finish(self, generation, text='', error=''):
        with self._lock:
            if not self._closed and generation == self._generation:
                self._state.update(busy=False, text=plain_answer(text), error=error)

    def _run(self, generation, payload, key):
        response = None
        try:
            request = urllib.request.Request(API_URL, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
            # 不跟随重定向，防止认证头发送给非官方端点。
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs): return None
            opener = urllib.request.build_opener(NoRedirect())
            response = opener.open(request, timeout=60)
            with self._lock:
                if self._closed or generation != self._generation: return
                self._response = response
            data = bytearray()
            while True:
                with self._lock:
                    if self._closed or generation != self._generation: return
                chunk = response.read(8192)
                if not chunk: break
                data.extend(chunk)
                if len(data) > 1024*1024: raise ValueError('DeepSeek 回答过大，请重试。')
            self._finish(generation, parse_response(data, response.status))
        except urllib.error.HTTPError as error:
            try:
                try: parse_response(b'', error.code)
                except ValueError as failure: self._finish(generation, error=str(failure))
            finally:
                error.close()
        except ValueError as error:
            self._finish(generation, error=str(error))
        except (OSError, RuntimeError):
            self._finish(generation, error='DeepSeek 请求未完成，请检查网络后重试。')
        finally:
            if response is not None: response.close()
            with self._lock:
                if generation == self._generation: self._response = None

    def cancel(self):
        with self._lock:
            self._generation += 1
            response, self._response = self._response, None
            if self._state['busy']:
                self._state.update(busy=False, error='已取消请求；已发出的请求可能仍产生API用量。')
        if response is not None:
            threading.Thread(target=response.close, daemon=True).start()
        return self.state()

    def shutdown(self):
        self.cancel()
        with self._lock: self._closed = True

    def reset_process(self):
        with self._lock:
            if self._state['mode'] not in ('analyze', 'predict'): return
            self.cancel()
            self._state = {'busy': False, 'text': '', 'error': '', 'target': '', 'mode': '', 'request': self._generation}
