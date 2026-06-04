"""The ``briefcase`` evaluation-run CLI — a thin, Briefcase-native lifecycle over the oci-jj engine.

Register a dataset, store secrets, submit a verdict run, then monitor and fetch results — a job
lifecycle mapped onto oci-jj + verdictml.
"""
from briefcase.cli.engine import OciJJEngine
from briefcase.cli.state import Store

__all__ = ["Store", "OciJJEngine", "main"]


def main(argv=None, store=None, engine=None, stack=None) -> int:
    from briefcase.cli.__main__ import main as _main

    return _main(argv, store=store, engine=engine, stack=stack)
