"""``python -m excdump ...`` -- same commands as the CLI."""

import sys

from .cli import main

raise SystemExit(main(sys.argv))
