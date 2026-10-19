#!/usr/bin/env bash
# 安装 litcli(生产):`pip install --break-system-packages --user .` 把当前源码**复制**到用户
# 站点(非 editable),产出冻结快照;console_script 落到 ~/.local/bin/litcli(已在 PATH)。
# 生产装的是快照,与仓库源码**解耦**:生产期间改源码不波及已装版本(要发布新版本重跑本脚本)。
# 开发请走仓库内 `./bin/litcli`(独立用源码,不影响生产安装)。
# 本机 Python 3.12 是 PEP 668 externally-managed(系统/用户 pip 默认被拦),故必须 --break-system-packages;
# litcli 零第三方依赖(纯标准库),不会与 apt 管理的包冲突;--user 限定用户作用域,无需 sudo、不写 /usr/lib。
# 可选 --with-cnki:额外安装 Playwright + Chromium(cnki 中文题录检索后端用,不进核心零依赖包)。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SKILL_DIR="${HOME}/.claude/skills/zotero"
WITH_CNKI=0
for arg in "$@"; do
  case "$arg" in
    --with-cnki) WITH_CNKI=1 ;;
    *) echo "[litcli] 未知参数: $arg(支持 --with-cnki)" >&2 ;;
  esac
done
cd "${HERE}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[litcli] 错误: 未找到 python3,无法安装" >&2
  exit 1
fi

# 普通安装(非 editable):把当前源码**复制**进用户站点,产出冻结生产快照。
# 注意不要用 `-e`(editable 会引用仓库源码,生产期间改源码将即时波及运行中的命令)。
# 版本号由 pip 输出(Successfully installed litcli-0.1.0)呈现,此处不再多起一个 pip 子进程查。
python3 -m pip install --break-system-packages --user .
echo "[litcli] 已安装(快照,复制)"


if [ "${WITH_CNKI}" = "1" ]; then
  echo "[litcli] 安装 CNKI 后端依赖(Playwright + Chromium)..."
  python3 -m pip install --break-system-packages --user playwright
  python3 -m playwright install chromium
  echo "[litcli] CNKI 依赖已装。首次搜索会在可见浏览器里请你手动过滑块 + 登录知网(校外账号),之后会话复用。"
fi


if [ -f "${HERE}/.claude/skills/zotero/SKILL.md" ]; then
  mkdir -p "${SKILL_DIR}"
  cp -f "${HERE}/.claude/skills/zotero/SKILL.md" "${SKILL_DIR}/SKILL.md"
  echo "[litcli] 已复制 skill: ${SKILL_DIR}/SKILL.md"
else
  echo "[litcli] 警告: 未找到 ${HERE}/.claude/skills/zotero/SKILL.md,已跳过 skill 安装"
fi

if ! printf '%s' "${PATH}" | grep -q "${BIN_DIR}"; then
  echo "[litcli] 提示: PATH 尚无 ${BIN_DIR}。建议在 shell 启动文件加:"
  echo "        export PATH=\"${BIN_DIR}:\$PATH\""
fi

echo "[litcli] 完成。自测: litcli backends"
