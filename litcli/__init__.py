"""litcli — 可插拔多后端的通用学术 CLI。

领域层(models/citation/pdf)与任何参考管理器无关;
后端层(backends/)以 Backend 抽象 + capabilities 区分「读」「读+写」「可附附件」;
配置(config)从 ~/.config/litcli/config.toml 实例化后端。
"""

from ._version import __version__
