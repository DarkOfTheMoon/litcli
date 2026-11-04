# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-04

### Added

- 领域模型 `AcademicItem`（CSL JSON ⇄ 对象、BibTeX/RIS 导出）与 author-date 引文渲染；`export` 产出的
  `csljson` 可直接喂 `pandoc --citeproc`。
- 可插拔后端层：本机 Zotero 10 本地 API（读/写/附件，运行时授权，免 zotero.org Web API key）、arXiv、
  Crossref、OpenAlex（含 refs/citing/related 引文图谱），以及按需启用的 OpenLibrary、USPTO、CNKI、
  豆瓣、Sci-Hub、Z-Library、DOAJ、Europe PMC、DOAB、Zenodo。
- 跨后端聚合：`search --all` 按 DOI → ISBN → 归一标题去重合并，记录全部来源；`--type/--title/--author/
  --year/--container` 字段级过滤，`--sort cited` 按被引降序。
- 全文定位链：本地库 → Unpaywall/OpenAlex → Europe PMC JATS XML → Zenodo 文件 → Unpaywall 落地页，
  `--scihub` 再兜底镜像并标注非 OA；`fetch book` 走 Z-Library / DOAB 出候选书单。
- `data add --pdf` 从 PDF 首页启发式抽候选，交互确认后写库；写前过查重/半残门。
- shell 补全（bash/zsh/fish）：补子命令、选项、选项取值与本地文件路径，脚本自包含、补全时不起子进程。
- 反爬墙的本地解算（不绕过）：Sci-Hub 的 ALTCHA、DOAB 的 Anubis、Z-Library 的 cookie 工作证明都用
  `hashlib` 现场做题；知网/豆瓣用 Playwright 复用本机登录会话，滑块交人工完成。
- 知网连写中文关键词先分词再以 `*`（知网的 AND）连接，否则整串被当成一个词而 0 命中。
- `scripts/release.sh`：门禁（工作区干净、测试、lint、CHANGELOG、tag 唯一）+ 版本 bump + wheel/sdist +
  可选 Gitee Release。

### Changed

- 网络层统一 `httpx`（连接池 / HTTP/2 / 代理 / 超时）、`tenacity` 退避重试、`pyrate-limiter` 站点节律、
  `lxml` 解析，取代早期手写的 urllib 传输。
- CLI 入口按意图拆为 `litcli/commands/` 各族命令，`cli.py` 只留解析、分发与异常映射。
- 所有文本走英文（代码/注释/CLI 输出），用户文档保持中文。

### Fixed

- Zotero 收藏集合成员罗列（Zotero 忽略 `collection` 参数，改由本地 `collections` 字段归属）。
- Crossref 裸 DOI 与 OpenAlex 完整 URL、两种 ISBN 写法统一归一，避免同一本书去重失败。

## [Unreleased]

### Added

- `scripts/release.sh --release-only`：给已存在的 tag 补建 Release（切版时没有 token 时用），不 bump、
  不提交、不推送；`dist/` 缺失会重建。
- 发布 token 也可放 `~/.config/litcli/gitee.token`（`chmod 600`，不入库），脚本优先读环境变量。

