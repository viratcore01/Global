"""Allow `python -m radar ...` without installing the console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
