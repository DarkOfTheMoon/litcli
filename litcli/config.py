"""配置与后端发现:读 ~/.config/litcli/config.toml,按 type 实例化后端。

使用 :mod:`tomllib`(Python 3.11+ 内置),零依赖。
后端注册表用 try-import,允许多后端文件未就绪时仅 Zotero 也能独立工作。
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .backends.base import Backend

# 关键:未配置时内置的默认后端(本地 Zotero 可读可写 + arXiv 只读,开箱即用)
_INBUILT_DEFAULT = {"backends": {
    "zotero": {"type": "zotero_local", "base": "http://localhost:23119/api"},
    "arxiv": {"type": "arxiv"},
    "crossref": {"type": "crossref"},
}}

CONFIG_ENV = "LITCLI_CONFIG"
DEFAULT_CONFIG = Path.home() / ".config" / "litcli" / "config.toml"


def load_config(path: str | Path | None = None) -> dict:
    """加载配置;优先级:显式 path > 环境变量 LITCLI_CONFIG > 默认 ~/.config/litcli/config.toml > 内置默认。"""
    for candidate in (path, Path(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else None, DEFAULT_CONFIG):
        if candidate and Path(candidate).exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f)
    return dict(_INBUILT_DEFAULT)


def _registry() -> dict[str, type[Backend]]:
    """后端类型注册表(type 字符串 -> 类)。"""
    from .backends import zotero_local

    reg: dict[str, type[Backend]] = {"zotero_local": zotero_local.ZoteroLocalBackend}
    try:
        from .backends import arxiv

        reg["arxiv"] = arxiv.ArxivBackend
    except ImportError:
        pass  # arXiv 尚未就绪时,不影响 Zotero 定义
    try:
        from .backends import crossref

        reg["crossref"] = crossref.CrossrefBackend
    except ImportError:
        pass
    return reg


def _make_backend(name: str, cfg: dict) -> Backend:
    reg = _registry()
    backend_type = cfg.get("type", "zotero_local")
    if backend_type not in reg:
        raise KeyError(f"未知后端类型 '{backend_type}'(可选: {sorted(reg)})")
    kwargs = {k: v for k, v in cfg.items() if k != "type"}
    return reg[backend_type](name=name, **kwargs)


def discover_backends(config: dict | None = None) -> dict[str, Backend]:
    """从配置实例化全部启用的后端。"""
    cfg = config if config is not None else load_config()
    out: dict[str, Backend] = {}
    for name, bcfg in (cfg.get("backends") or {}).items():
        out[name] = _make_backend(name, bcfg)
    return out


def get_backend(name: str = "zotero", config: dict | None = None) -> Backend:
    backends = discover_backends(config)
    if name not in backends:
        raise KeyError(f"未配置后端 '{name}';已配置: {sorted(backends)}")
    return backends[name]


def get_all_backends(config: dict | None = None) -> dict[str, Backend]:
    return discover_backends(config)
