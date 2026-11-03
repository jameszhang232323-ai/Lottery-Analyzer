"""``python -m dajidali`` 入口：与 ``dajidali`` console script 共用 :func:`main`。"""

from __future__ import annotations

from dajidali.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
