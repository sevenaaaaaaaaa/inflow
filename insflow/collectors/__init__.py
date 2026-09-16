"""Insight Flow 采集器 - Source 插件宿主"""

from .base import SourcePlugin, CollectContext, CollectResult
from .registry import Registry, get_registry

__all__ = [
    "SourcePlugin",
    "CollectContext", 
    "CollectResult",
    "Registry",
    "get_registry",
]
