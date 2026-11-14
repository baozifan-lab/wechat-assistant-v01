"""Local Gateway readiness and an OS-owned, per-profile startup lock."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path


@contextmanager
def startup_lock(profile):
    path = Path(profile) / 'zhishi-gateway-start.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    acquired = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'1')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        # Closing the handle releases the lock, including after a supervisor crash.
        stream.close()


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of death.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_start_identity(pid):
    """Identify a process instance, so a recycled PID cannot impersonate it."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                           ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                           ctypes.POINTER(wintypes.FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # The command field can contain spaces and parentheses.
        stat = Path(f'/proc/{pid}/stat').read_text('ascii')
        return stat[stat.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        return None


def save_gateway_identity(directory, pid):
    directory = Path(directory)
    identity = process_start_identity(pid)
    (directory / 'wechat-gateway.pid').write_text(str(pid), encoding='ascii')
    (directory / 'wechat-gateway.identity.json').write_text(
        json.dumps({'pid': pid, 'started': identity}), encoding='utf-8')


def saved_gateway_alive(directory):
    try:
        pid = int((Path(directory) / 'wechat-gateway.pid').read_text('ascii').strip())
        identity = json.loads((Path(directory) / 'wechat-gateway.identity.json').read_text('utf-8'))
        started = identity.get('started')
    except (OSError, ValueError, AttributeError):
        return False
    return bool(started and identity.get('pid') == pid and process_alive(pid)
                and process_start_identity(pid) == started)


def gateway_readiness(port):
    """Use the installed Gateway's channel-aware readiness endpoint, never a CLI runtime."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=0.5)
    try:
        connection.request('GET', '/readyz')
        response = connection.getresponse()
        value = json.loads(response.read(16384))
        if not isinstance(value, dict):
            return {}
        return {'ready': response.status == 200 and value.get('ready') is True
                and value.get('coreReady') is True
                and 'openclaw-weixin' not in value.get('suppressed', []),
                'core_ready': value.get('coreReady') is True}
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    finally:
        connection.close()
