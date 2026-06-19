"""Built-in algorithm plugins.

Importing this package AUTO-DISCOVERS and imports every module here, so each
``@register_strategy`` class self-registers. Adding a new algorithm = drop a module
in this folder; no need to edit this file or any core code.
"""

from __future__ import annotations

import importlib
import pkgutil

for _info in pkgutil.iter_modules(__path__):
    if not _info.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_info.name}")
