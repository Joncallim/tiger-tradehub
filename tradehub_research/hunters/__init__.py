"""Six pure Phase-1 Hunter implementations, one module per family.

Importing the package is the public registration boundary.  Keeping that side
effect here means library callers do not need to know about the private
``registry`` module before asking :mod:`tradehub_research.screens` for the
manifest.
"""

from tradehub_research.hunters import registry as _registry

__all__ = ["_registry"]
