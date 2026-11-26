"""兼容入口：`python -m triposr_lite` 委托到产品 CLI。

产品 CLI 位于 `app/integrated_app/cli.py`，提供 Result 信封、语义退出码与
完整子命令族；本模块只保留旧调用方式，不再维护第二套解析逻辑。
"""
from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    from app.integrated_app.cli import main as product_main

    return int(product_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
