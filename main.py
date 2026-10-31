"""Zelos Logs extension entry point.

Nothing runs at import: packaging and standalone action runs import this module. Importing `cli`
imports `app` and `actions`, which registers the actions.
"""

import sys

from zelos_extension_logs import cli

if __name__ == "__main__":
    sys.exit(cli.main())
