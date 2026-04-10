#!/usr/bin/env bash
# reset_tasks.sh — 將所有 benchmark task 重置回初始 broken 狀態
# 每次跑 benchmark 前執行，確保從乾淨狀態開始

set -euo pipefail
TASKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/tasks" && pwd)"

for task_dir in "$TASKS_DIR"/*/; do
  task=$(basename "$task_dir")
  if [ -d "$task_dir/.git" ]; then
    git -C "$task_dir" checkout -- . -q 2>/dev/null || true
    git -C "$task_dir" clean -fd -q 2>/dev/null || true
    # 重置到第一個 commit（init 狀態）
    first_commit=$(git -C "$task_dir" rev-list --max-parents=0 HEAD)
    git -C "$task_dir" reset --hard "$first_commit" -q
    echo "  [reset] $task → $(git -C "$task_dir" log --oneline -1)"
  fi
done
echo "✓ 所有 tasks 已重置"
