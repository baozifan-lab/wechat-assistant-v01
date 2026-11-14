"""Start the optional local PostgreSQL installation used by pilot demos."""
import os
from pathlib import Path
import subprocess


def ensure_local_postgres(base=None):
    if os.name != 'nt':
        return 'skipped', '本机数据库自动启动仅支持 Windows。'
    base = Path(base) if base is not None else Path.home() / 'AppData' / 'Local' / 'LocalPostgreSQL'
    control = base / 'pgsql' / 'bin' / 'pg_ctl.exe'
    data = base / 'data'
    if not control.is_file() or not (data / 'PG_VERSION').is_file():
        return 'unavailable', '未找到本机 PostgreSQL 程序或数据目录。'
    # PostgreSQL can inherit pg_ctl's output handles on Windows. Pipes would
    # remain open for the lifetime of the server and stall startup.
    options = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL, 'timeout': 70,
               'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    try:
        status = subprocess.run([str(control), 'status', '-D', str(data)], **options)
        if status.returncode == 0:
            return 'running', '本机数据库已在运行。'
        result = subprocess.run([str(control), 'start', '-D', str(data), '-l', str(base / 'server.log'),
                                 '-o', '-c listen_addresses=127.0.0.1', '-w', '-t', '60'], **options)
        if result.returncode == 0:
            return 'started', '本机数据库已启动。'
    except (OSError, subprocess.SubprocessError):
        pass
    return 'failed', '本机数据库启动失败，请检查数据库运行日志；工作台仍可使用。'
