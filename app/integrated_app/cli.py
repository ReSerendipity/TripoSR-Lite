#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 ReSerendipity
# SPDX-License-Identifier: Apache-2.0
"""TripoSR-Lite 全局 CLI 入口（S1 骨架，S2 扩展为完整子命令族）。

脚本直跑无包上下文：先把仓根注入 sys.path，再绝对导入现有入口。
"""
from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # --json 是产品 CLI 的全局开关；S1 骨架里只做兼容透传剔除。
    raw = [arg for arg in raw if arg != "--json"]
    from triposr_lite.__main__ import main as legacy_main

    return int(legacy_main(raw) or 0)


if __name__ == "__main__":
    sys.exit(main())
