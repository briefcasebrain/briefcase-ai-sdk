"""Raises on import, as a plugin with a missing dependency would."""
raise ImportError("bc_beta needs the 'betasdk' package, which is not installed")
