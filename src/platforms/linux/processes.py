"""通过 pidfd 对已验证身份的进程发送用户选择的 SIGTERM 或 SIGKILL。"""
import errno
import os
import select
import signal

import psutil

from ...core.processes import matching_process, StopResult, stop_descendants


def stop_process(key, *, force=False, descendants=False):
    pid, _ = key
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError('无效的PID')
    if pid in (1, os.getpid()):
        raise ValueError('不允许通过此操作结束系统初始化进程或本应用自身')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise OSError('当前系统不支持安全的 pidfd 进程结束；未发送信号')
    try:
        descriptor = os.pidfd_open(pid, 0)
    except PermissionError as error:
        raise PermissionError('没有权限打开目标进程；未提权或发送信号') from error
    except ProcessLookupError as error:
        raise ProcessLookupError('进程已经退出，请刷新列表') from error
    except OSError as error:
        raise OSError('无法取得安全的 pidfd 进程句柄；未发送信号') from error
    try:
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        # 先固定内核进程对象，再按列表记录验证创建时间；从不回退到 kill(pid)。
        if poller.poll(0):
            raise ProcessLookupError('进程已经退出，请刷新列表')
        try:
            matching_process(key)
        except psutil.AccessDenied as error:
            raise PermissionError('没有权限核对进程身份；未发送信号') from error
        except psutil.NoSuchProcess as error:
            raise ProcessLookupError('进程已经退出，请刷新列表') from error
        if poller.poll(0):
            raise ProcessLookupError('进程已经退出，请刷新列表')
        if descendants:
            return stop_descendants(key, lambda target: stop_process(target, force=force))
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL if force else signal.SIGTERM)
        except PermissionError as error:
            raise PermissionError('没有权限发送结束信号') from error
        except ProcessLookupError as error:
            raise ProcessLookupError('进程在结束请求前已经退出，请刷新列表') from error
        except OSError as error:
            if error.errno in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError('当前内核不支持安全的 pidfd 信号；未使用 PID 结束') from error
            raise
        exited = bool(poller.poll(1500))
        mode = '强制' if force else '普通'
        message = '进程已结束' if exited else f'已发送{mode}结束请求，进程尚未退出'
        return StopResult(key, exited, message)
    finally:
        os.close(descriptor)
