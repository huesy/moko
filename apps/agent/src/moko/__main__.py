"""Allow `python -m moko` as well as the `moko` console script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
