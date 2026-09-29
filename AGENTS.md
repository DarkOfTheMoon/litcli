# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 运行与命令

**零第三方依赖**：仅用 Python 3.11+ 标准库（`tomllib`、`urllib`、`xml.etree`、`subprocess`）；本机是 3.12。无 `pyproject.toml` / `setup.py`——不打包、不安装，直接用入口跑。PDF 文本抽取需系统装了 `pdftotext`（poppler-utils）。

```bash
# 运行 CLI：本仓库无安装步骤，二选一
./bin/litcli <子命令>          # wrapper：把项目根注入 sys.path
python3 -m litcli.cli <子命令>  # 等价

# 跑全部测试（unittest，非 pytest）
python3 -m unittest discover -s tests

# 跑单个模块
python3 -m unittest tests.test_backends

# 跑单个用例
python3 -m unittest tests.test_models.TestModels.test_to_csl
```

CLI 帮助用 `./bin/litcli --help`；本机 Zotero 库用 `litcli search "..."`（`--all` 跨后端聚合，`litcli backends` 看可用后端）。

## 架构分层（后端无关的领域层 + 可插拔后端层）

litcli 的核心是「一条文献在多个后端（Zotero / arXiv）间的读到写」统一归一化。五层自上而下：

- **领域层**（与任何参考管理器无关，`litcli/models.py`、`citation.py`、`pdf.py`、`metadata.py`）：
  - `models.AcademicItem` 是唯一归一化模型（`source/key/title/creators/year/abstract/doi/url/item_type/attachments/extra`），与 CSL JSON 双向转换（`to_csl` / `from_csl`）、可导 BibTeX。
  - `citation.py` 提供零依赖的 author-date in-text / 参考文献简版渲染；正统路径是 `export --format csljson` 喂 `pandoc --citeproc`，**不依赖 Better BibTeX**。
  - `pdf.py`：下载 + `pdftotext` 抽文本，后端无关的「拿到 bytes/路径 → 抽文本」。
  - `metadata.py`：纯启发式从 PDF 首页文本猜 title/authors/year/abstract，**不调 LLM、完全离线**；`from_text_lines` 是纯函数（可脱离 pdftotext 复用）。`gen-meta` 结果需人工校验后再 `add --csl` 导入。
  - 注意 `citation.py:46` 用了 `it.author`——`AcademicItem` 并无该字段，`render_bibliography` 在无 creators 且无 year 时会崩；这是已知缺陷。

- **聚合层**（`litcli/aggregate.py`，纯函数、不碰后端不联网）：供 `search --all` 用的去重/合并/排序。身份键**优先生效 DOI**、无 DOI 退化为归一标题（`norm_title` / `norm_doi`）；`_merge_two` 只把缺失字段补给最完整记录（副本操作，不污染入参，见 `test_does_not_mutate_input`）；`extra["sources"]` 记录全部来源；排序按年份降序、同年标题升序。

- **后端层**（`litcli/backends/`）：`base.py` 定义抽象 `Backend` + `capabilities`（`read`/`write`/`attachment`）。`add`/`patch` 内部先 `_require("write")`，只读后端抛 `NotSupportedError`（CLI 捕获后输出 `[跳过]` 并以退出码 2 返回）。
  - `zotero_local.py`：Zotero 10 本地 API（`localhost:23119/api`，`users/0` 本机别名，**无需 API key**）。读+写+附件，写入用 `If-Unmodified-Since-Version` 并发控制，写请求需 `Zotero-Server-ID`（惰性拉自 `/connector/ping`）。
  - `arxiv.py`：arXiv 开放 API（Atom/XML），只读 + 可拉 PDF 直链，按 arXiv id（去 `v` 版本）归一化。

- **配置层**（`litcli/config.py`）：读 `~/.config/litcli/config.toml`（不支持则在 `$LITCLI_CONFIG`），`tomllib` 解析。加载优先级：显式 path → `$LITCLI_CONFIG` → `~/.config/litcli/config.toml` → **内置默认**（只有 Zotero localhost）。后端注册表用 try-import：arXiv 文件未就绪时不影响 Zotero 单后端工作。配置示例见 `config.example.toml`。

- **CLI 编排层**（`litcli/cli.py`）：argparse 子命令 `search/get/add/set/export/pdf/gen-meta/backends`，分发到后端。`add` 的 `--creator` 用 `"家族, 名字"`，可多次。

## 惯例与约定

- **代码注释与 docstring 一律用英文**；用户可见文案与用户使用文档（AGENTS.md、`.claude/skills/`、argparse 帮助、CLI 输出）用**中文**。标识符统一英文；按载体区分语言，不混用。
- 加后端：继承 `Backend`，实现 `search`/`get`，覆盖 `capabilities`（只读就 `{"read"}`），在 `config._registry()` 的 try-import 里注册，并在 `config.example.toml` 加字段。
- 新功能若要在领域层「输入某数据 → 输出统一模型」，优先做成纯函数（不 I/O、不联网），便于离线单测（现有测试都走构造数据、不连真库）。
- 写 `AcademicItem` 新增字段时，同步检查 `to_csl`/`from_csl`/`merge_items` 是否该感知该字段。
- `.claude/skills/zotero/SKILL.md` 是面向 Zotero/arXiv 库互操作的技能，其命令表与 CLI 保持一致（其中「去重/排序暂不做」一句已过时——聚合层现已支持）。
