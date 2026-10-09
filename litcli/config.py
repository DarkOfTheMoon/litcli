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
    "openalex": {"type": "openalex"},
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
    try:
        from .backends import openalex

        reg["openalex"] = openalex.OpenAlexBackend
    except ImportError:
        pass
    try:
        from .backends import openlibrary

        reg["openlibrary"] = openlibrary.OpenLibraryBackend
    except ImportError:
        pass
    try:
        from .backends import uspto

        reg["uspto"] = uspto.UsptoBackend
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


# --- 写回 config.toml(供 `backends authorize` 落 key) ---

def _toml_string(text: str) -> str:
    """把值写成 TOML 基本字符串(转义引号/反斜杠/控制符)。"""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _upsert_key(text: str, section: str, key: str, value: str) -> str:
    """在 [section] 内新增/替换 key = value 行;无该 section 则追加。保留其余内容。"""
    lines = text.splitlines()
    marker = f"[{section}]"
    try:
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == marker)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(marker)
        lines.append(f"{key} = {value}")
        return "\n".join(lines) + "\n"
    # 找到该 section 结束(下一个 [节] 或文件末尾)
    end = idx + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    replace_at = None
    for i in range(idx + 1, end):
        if lines[i].split("=", 1)[0].strip() == key:
            replace_at = i
            break
    new_line = f"{key} = {value}"
    if replace_at is not None:
        lines[replace_at] = new_line
    else:
        lines.insert(end, new_line)
    return "\n".join(lines) + "\n"


def write_backend_key(name: str, key: str, path: str | Path | None = None) -> Path:
    """把某个后端的 api_key 写回 config.toml 的 [backends.<name>] 段。

    仅改写目标字段,保留文件中其它段与注释;若文件不存在则先落内置默认骨架,
    避免丢失默认后端。返回实际写到的文件路径。
    """
    path = Path(path) if path else (Path(os.environ[CONFIG_ENV]) if CONFIG_ENV in os.environ else DEFAULT_CONFIG)
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = _default_toml_text()
    text = _upsert_key(text, f"backends.{name}", "api_key", _toml_string(key))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _default_toml_text() -> str:
    """把内置默认后端渲染为 config.toml 文本(缺失文件时作为骨架)。"""
    lines: list[str] = []
    for name, cfg in _INBUILT_DEFAULT["backends"].items():
        lines.append(f"[backends.{name}]")
        for k, v in cfg.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            else:
                lines.append(f"{k} = {_toml_string(str(v))}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
