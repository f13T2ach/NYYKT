"""入口：无参数 → TUI；带参数 → 命令行模式。"""

import sys

from .cli import main as cli_main


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        from .app import main as tui_main

        return tui_main()
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
