"""Strategy registry. Add an algorithm = write a module + ``@register_strategy`` it.

Registered strategies are auto-discoverable by the API/UI and selectable per position.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(strategy_id: str) -> Callable[[type[Strategy]], type[Strategy]]:
    def decorator(cls: type[Strategy]) -> type[Strategy]:
        if strategy_id in _REGISTRY:
            raise ValueError(f"strategy '{strategy_id}' already registered")
        _REGISTRY[strategy_id] = cls
        return cls

    return decorator


def get_strategy_cls(strategy_id: str) -> type[Strategy]:
    try:
        return _REGISTRY[strategy_id]
    except KeyError:
        raise KeyError(f"unknown strategy '{strategy_id}'") from None


def all_strategies() -> dict[str, type[Strategy]]:
    return dict(_REGISTRY)


def create_strategy(strategy_id: str, params: dict[str, Any] | None = None) -> Strategy:
    """Instantiate a strategy, validating ``params`` against its ParamsModel."""
    cls = get_strategy_cls(strategy_id)
    parsed = cls.ParamsModel(**(params or {}))
    return cls(parsed)
