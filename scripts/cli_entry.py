"""Entry point for the standalone binary (scripts/build_cli_binary.sh). Not part of the package."""

import sys

from semula.cli import main

if __name__ == "__main__":
    sys.exit(main())
