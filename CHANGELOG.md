# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- 来源性质标注：每个后端声明自己给的是什么副本（`oa` 开放获取授权 / `public` 公开副本、非 OA 授权 /
  `local` 你的库），表格行内标 `[oa]`/`[public]`/`[local]`，JSON 给同名的 `access` 字段，`fetch` 的
  命中与候选列表也一并说明。标签跟着记录走，后端改名或换实现不影响。
- `--debug`（或环境变量 `LITCLI_DEBUG=1`）：把每个请求、状态码、耗时、限速等待与重试逐行打到
  stderr，query 里的 key / email 打码；意外异常额外打印调用栈。该开关在子命令前后都接受，
  stdout 不受影响，不开启时只有一次布尔判断的开销。

### Changed

- 后端失败改为带分类的异常（`litcli/errors.py`）：`bad-input` / `not-found` / `paywalled` / `auth` /
  `bot-wall` / `unavailable` / `unsupported` / `external`。调用方（`--all` 聚合、全文链、CLI 出口）
  按分类决定策略与退出标签，不再靠字符串匹配或笼统的 `RuntimeError`；HTTP 状态经 `http_kind()`
  统一归入同一套分类，各后端只在站点含义不同处就地上报。
- 出错时可附一行 `[hint]`（如缺浏览器时的 `litcli backends install-browser`），与错误正文分开呈现。

## [0.3.1] - 2026-11-06

### Added

- 二进制安装器（Linux `.run` 自解压 / Windows 安装 `.exe`）：`packaging/` 下的 PyInstaller 冻结配置 + 两个安装器脚本，`scripts/build_binary.{sh,ps1}` 构建并自验（装、跑、卸）。产物只有字节码与依赖，目标机不需要 Python，也不需要源码。CI 增加 Linux + Windows 构建作业。
- `litcli backends install-browser [chromium|firefox|webkit]`：经随包的 Playwright 驱动下载浏览器，落在 Playwright 自己的缓存里，升级复用、卸载保留。Chromium 不塞进安装包。
- `release.sh --binary`：切版时构建本平台安装器并连同 wheel/sdist 一起上传；`verify_release.sh` 在产物含 Linux 安装器时按消费者视角装一遍、跑一遍、卸一遍。
- `backends install-browser --check`：无头启动一次浏览器并报版本（`--dry-run` 只列出下载目标）；CI 在冻结包上真下载 Chromium 后必跑，Linux 与 Windows 都覆盖。
- 冻结包显式把浏览器钉在用户缓存（`~/.cache/ms-playwright` / `%LOCALAPPDATA%\ms-playwright`）：Playwright 在 PyInstaller 环境下默认 `PLAYWRIGHT_BROWSERS_PATH=0`，浏览器会落进升级即被覆盖的包内目录，此前冻结包的下载位置与启动查找位置不一致。
- `--binary` 发版要求 Linux 与 Windows 安装器齐全：另一个平台由 CI 产物或 `--installer <path>` 提供，缺任一平台即拒绝发布，`--release-only` 也会补取；`verify_release.sh` 同步要求 Windows 安装器在产物与 `SHA256SUMS` 里。

## [0.3.0] - 2026-11-06

### Added

- 支持 Windows：配置与浏览器 profile 落到 `%APPDATA%\litcli`（`XDG_CONFIG_HOME` 在 POSIX 上同样被尊重）；
  CI 增加 `windows-latest` 作业跑全量测试与入口冒烟。

### Changed

- `--out` 现在接受目录（已存在或以分隔符结尾），文件名仍按默认规则生成；此前只能给完整路径。
- 仓库内 Windows 调试入口 `bin\litcli.cmd`（等价于 POSIX 的 `bin/litcli`）。

### Fixed

- `fetch` 的默认文件名在 Windows 上非法：老式 DOI 含 `:` `<` `>`（如
  `10.1002/…16:15<1705::AID-SIM599>…`），此前直接拼进文件名。现统一替换非法字符、去尾部点/空格、
  截断过长主干并避开 `CON`/`NUL` 等保留设备名。
- `pdftotext` 的输出按 UTF-8 解码（此前跟系统代码页，Windows 下中文 PDF 会乱码）。
- 重定向输出在 Windows 上强制 UTF-8，避免中文题录在 cp936 管道里抛 `UnicodeEncodeError`。

## [0.2.0] - 2026-11-05

### Added

- `README.en.md`：英文入口页（是什么 / 装什么 / 怎么跑 / 后端与授权边界），与中文 README 互加语言切换；
  `tests/test_docs.py` 比对两份文档的命令与后端清单，防止一边加、一边忘。
- `.github/ISSUE_TEMPLATE/bug.yml`（中英双语）与仓库 topics。
- `tests/test_cli_surface.py`：遍历 argparse 全树逐个跑 `--help`，并断言每个子命令都有对应的分发处理，
  防住「改了参数表没人发现」。
- `tests/test_skill_commands.py`：`SKILL.md` 的命令表与后端名和 parser、后端注册表比对，防文档漂移。
- `scripts/verify_release.sh <tag>`：按消费者视角验证已发布产物——下载附件、核对 `SHA256SUMS`、装进临时
  venv、跑 `--version` 与一条真实检索；`release.sh --push` 发完自动调用，装不起来就算发布失败。
- GitHub 镜像仓承载 CI（`master` 与 `v*` tag 触发 lint / 测试 / 入口冒烟 / 打包），`release.sh --push`
  同步推所有 remote。
- `scripts/release.sh --release-only`：给已存在的 tag 补建 Release（切版时没有 token 时用），不 bump、
  不提交、不推送；`dist/` 缺失会重建。
- 发布 token 也可放 `~/.config/litcli/gitee.token`（`chmod 600`，不入库），脚本优先读环境变量。

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

- **`search` 默认搜全部来源**（已配置后端 + 免 key 的 OA 语料），`--backend` 收窄到单个，二者互斥；
  单个后端失败只跳过并记一行 stderr，不再让整条检索失败。`--limit` 改为总量上限，不再是每后端上限。
- **`fetch` 收成一条命令**：`fetch <doi|arXiv id|ISBN>`，去掉 `fulltext`/`book` 两层子命令（书名用 `--book`，
  ISBN 自动识别）。默认直接落盘，stdout 只回保存路径、诊断走 stderr；`--no-download` 只回链接。
  arXiv id 现在会被归一（此前 `fetch fulltext arxiv/…` 一律判无全文）。
- **退出码区分结果**：`fetch` 0 拿到 / 1 没有全文 / 2 错误，调用方不必解析英文输出。
- `--download` 在 `fetch` 上成为默认，旧写法需改为 `--no-download`。

- README 重排为用户视角：先讲能做什么、给一条不需要 Zotero 的 60 秒上手，再是安装 / 命令 / 后端授权边界 /
  限制；架构细节收进 AGENTS.md 并改为链接。修正安装只提 PEP 668、`--bump --push` 未提镜像同步、
  测试段漏记新增用例等过时描述。
- `SKILL.md` 补「一条检索怎么走」工作流（先查用户库 → 外扩 → 收敛 → 顺藤摸瓜 → 落盘 → 落不到就明说），
  命令面去掉写死的本机绝对路径，修掉只读后端清单漏项。
- `config.example.toml` 头部补齐可选后端清单，修正 OpenAlex 说明的错字。

- 网络层统一 `httpx`（连接池 / HTTP/2 / 代理 / 超时）、`tenacity` 退避重试、`pyrate-limiter` 站点节律、
  `lxml` 解析，取代早期手写的 urllib 传输。
- CLI 入口按意图拆为 `litcli/commands/` 各族命令，`cli.py` 只留解析、分发与异常映射。
- 所有文本走英文（代码/注释/CLI 输出），用户文档保持中文。

### Fixed

- Zotero 收藏集合成员罗列（Zotero 忽略 `collection` 参数，改由本地 `collections` 字段归属）。
- Crossref 裸 DOI 与 OpenAlex 完整 URL、两种 ISBN 写法统一归一，避免同一本书去重失败。

