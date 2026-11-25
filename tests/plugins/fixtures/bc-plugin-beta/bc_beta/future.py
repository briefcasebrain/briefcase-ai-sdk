"""Declares a plugin API this briefcase-ai does not have yet."""
from briefcase.integrations.vcs import VcsClientBase

BRIEFCASE_PLUGIN_API = 99


class FutureClient(VcsClientBase):
    pass
