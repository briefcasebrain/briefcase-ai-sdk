"""Interpreter-start bootstrap used by ``briefcase-ai exec``.

This directory is put at the front of ``PYTHONPATH`` for the child process,
so Python imports the ``sitecustomize.py`` next to this file at startup.
Nothing here is imported by ``import briefcase``.
"""

import os

#: Directory to prepend to ``PYTHONPATH``.
BOOTSTRAP_DIR = os.path.dirname(os.path.abspath(__file__))

__all__ = ["BOOTSTRAP_DIR"]
