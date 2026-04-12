"""cog test suite. Run with `python3 -m unittest discover tests`."""

import os
import sys

# Ensure `import cog` resolves to the repo-root single-file module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
