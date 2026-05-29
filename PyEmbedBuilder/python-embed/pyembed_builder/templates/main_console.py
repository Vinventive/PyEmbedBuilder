"""PyEmbedBuilder starter entry point."""

import sys


def main() -> int:
    print("This is the default entry point.")
    print("Replace main.py or set a custom entry point for your app.")
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
