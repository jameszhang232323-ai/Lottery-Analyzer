"""在受限沙箱里让 Python 正常使用临时目录。

背景：DSH 的 Windows 沙箱只允许写入工作区，内核对显式以 0700 创建的目录做访问检查时
会拒绝当前进程（缺少沙箱身份的权限项），而 ``tempfile.mkdtemp`` 恰好用 0700 建目录。
结果 pip、pytest 缓存、任何依赖系统临时目录的库都会报 ``PermissionError(13)``。

本文件把 ``tempfile.mkdtemp`` 换成“默认权限创建”的实现：目录继承父目录的访问控制项，
沙箱进程因此可以写入。只在 Windows 生效，且仅影响临时目录的权限位，不改变临时目录语义。

安装方式（一次性，只影响本机虚拟环境）：由 ``tooling/setup-windows-sandbox.ps1`` 复制到
``backend/.venv/Lib/site-packages/sitecustomize.py``。它不进入版本库的 Python 包。
"""

from __future__ import annotations

import os
import secrets
import tempfile


def _mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    base = dir or tempfile.gettempdir()
    head = prefix or "tmp"
    tail = suffix or ""
    for _ in range(100):
        candidate = os.path.join(base, head + secrets.token_hex(4) + tail)
        try:
            os.mkdir(candidate)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not create a unique temporary directory")


if os.name == "nt":
    tempfile.mkdtemp = _mkdtemp
