#!/usr/bin/env bash
# 安装 litcli:软链 bin/litcli → ~/.local/bin,并复制 skill 到全局 ~/.claude/skills/zotero。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SKILL_DIR="${HOME}/.claude/skills/zotero"
mkdir -p "${BIN_DIR}" "$(dirname "${SKILL_DIR}")"

ln -sf "${HERE}/bin/litcli" "${BIN_DIR}/litcli"
echo "[litcli] 已软链: ${BIN_DIR}/litcli -> ${HERE}/bin/litcli"

if ! printf '%s' "${PATH}" | grep -q "${BIN_DIR}"; then
  echo "[litcli] 提示: PATH 尚无 ${BIN_DIR}。建议在 shell 启动文件加:"
  echo "        export PATH=\"${BIN_DIR}:\$PATH\""
fi

if [ -f "${HERE}/.claude/skills/zotero/SKILL.md" ]; then
  mkdir -p "${SKILL_DIR}"
  cp -f "${HERE}/.claude/skills/zotero/SKILL.md" "${SKILL_DIR}/SKILL.md"
  echo "[litcli] 已复制 skill: ${SKILL_DIR}/SKILL.md"
else
  echo "[litcli] 警告: 未找到 ${HERE}/.claude/skills/zotero/SKILL.md,已跳过 skill 安装"
fi

echo "[litcli] 完成。自测: litcli backends"
