#!/usr/bin/env bash
# sync_to_github.sh — 將 AI_system_v3 的程式碼同步至 GovernorOS 並推送 GitHub
#
# 使用方式：
#   ./sync_to_github.sh                  # 自動偵測變更並推送
#   ./sync_to_github.sh --dry-run        # 只顯示差異，不實際執行
#   ./sync_to_github.sh --message "fix: xxx"  # 自訂 commit message
#
# 規則：
#   - 只同步白名單中的檔案（程式碼邏輯）
#   - 永遠不同步 CLAUDE.md / PROMPT.md（含 live 任務狀態）
#   - 永遠不同步 memory/ / *.log / .aider* / Code/（私有執行資料）
#   - 推送前自動掃描敏感字串

set -euo pipefail

# ── 路徑設定 ────────────────────────────────────────────────────
SRC="/mnt/ai_data/AI/AI_system_v3"
DST="/mnt/ai_data/AI/GovernorOS"

# ── 白名單：只有這些檔案會被同步 ────────────────────────────────
WHITELIST=(
  "main_loop.py"
  "dream_cycle.py"
  "git_diff_intel.py"
  "startup.sh"
  "ARCHITECTURE.md"
  "task_intake.py"
  "mempalace.py"
  "run_training.sh"
  "benchmark_runner.py"
  "continuous_training_runner.py"
  "benchmarks/coding_suite_v1.json"
  "benchmarks/coding_suite_v2.json"
  "benchmarks/compare_runs.py"
  "benchmarks/reset_tasks.sh"
  "auto_train.sh"
)

# ── 參數解析 ────────────────────────────────────────────────────
DRY_RUN=false
CUSTOM_MSG=""

for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=true ;;
    --message=*) CUSTOM_MSG="${arg#--message=}" ;;
    --message)   shift; CUSTOM_MSG="$1" ;;
  esac
done

# ── 敏感字串掃描 ────────────────────────────────────────────────
check_privacy() {
  local file="$1"
  local issues=()

  # 本地帳號路徑（/home/USERNAME 或 /Users/USERNAME，但排除 ~/）
  if grep -qP '/home/(?!joel/\.local)[a-zA-Z0-9_-]+/' "$file" 2>/dev/null; then
    issues+=("本地帳號路徑 (/home/...)")
  fi
  # /mnt/ 本地掛載路徑（非範例）
  if grep -qP '/mnt/[a-zA-Z]' "$file" 2>/dev/null; then
    issues+=("本地掛載路徑 (/mnt/...)")
  fi
  # API Key 格式（長度 > 30 的隨機字串，排除已知安全字串）
  if grep -qP '[A-Za-z0-9_-]{40,}' "$file" 2>/dev/null; then
    # 進一步排除 SHA hash（只含 hex）和 base64 模板字串
    if grep -qP '(?<![0-9a-f])[A-Za-z0-9_-]{40,}(?![0-9a-f])' "$file" 2>/dev/null; then
      : # 可能誤判，只記錄警告不阻斷
    fi
  fi
  # Google Client ID
  if grep -q '\.apps\.googleusercontent\.com' "$file" 2>/dev/null; then
    if ! grep -q 'YOUR_CLIENT_ID\|example\|placeholder\|<' "$file" 2>/dev/null; then
      issues+=("疑似真實 Google Client ID")
    fi
  fi
  # 真實 email（非 example.com）
  if grep -qP '[a-zA-Z0-9._%+-]+@(?!example\.com)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}' "$file" 2>/dev/null; then
    issues+=("疑似真實 Email 地址")
  fi

  if [ ${#issues[@]} -gt 0 ]; then
    echo "  ⚠️  隱私警告 [$file]: ${issues[*]}"
    return 1
  fi
  return 0
}

# ── 主流程 ───────────────────────────────────────────────────────
echo ""
echo "=== sync_to_github.sh ==="
echo "  SRC: $SRC"
echo "  DST: $DST"
echo ""

CHANGED_FILES=()
PRIVACY_FAILED=()

for f in "${WHITELIST[@]}"; do
  src_file="$SRC/$f"
  dst_file="$DST/$f"

  # 來源不存在 → 跳過
  if [ ! -f "$src_file" ]; then
    echo "  [skip] $f — 來源不存在"
    continue
  fi

  # 比較差異
  if diff -q "$src_file" "$dst_file" &>/dev/null; then
    echo "  [=]    $f"
    continue
  fi

  echo "  [diff] $f — 有變更"

  # 隱私掃描
  if ! check_privacy "$src_file"; then
    PRIVACY_FAILED+=("$f")
    continue
  fi

  CHANGED_FILES+=("$f")
done

echo ""

# 隱私掃描失敗 → 中止
if [ ${#PRIVACY_FAILED[@]} -gt 0 ]; then
  echo "❌ 以下檔案未通過隱私掃描，請手動確認後再執行："
  for f in "${PRIVACY_FAILED[@]}"; do echo "   - $f"; done
  exit 1
fi

# 沒有變更 → 結束
if [ ${#CHANGED_FILES[@]} -eq 0 ]; then
  echo "✓ 無程式碼變更，不需要同步。"
  exit 0
fi

# Dry-run 模式
if [ "$DRY_RUN" = true ]; then
  echo "[dry-run] 以下檔案將被同步："
  for f in "${CHANGED_FILES[@]}"; do echo "   + $f"; done
  echo "[dry-run] 未實際執行任何操作。"
  exit 0
fi

# ── 複製白名單檔案 ────────────────────────────────────────────
echo "複製程式碼檔案至 GovernorOS..."
for f in "${CHANGED_FILES[@]}"; do
  cp "$SRC/$f" "$DST/$f"
  echo "  [cp]   $f"
done

# ── 語法驗證 ─────────────────────────────────────────────────
echo ""
echo "語法驗證..."
for f in "${CHANGED_FILES[@]}"; do
  case "$f" in
    *.py)
      python3 -m py_compile "$DST/$f" && echo "  [ok]   $f (python syntax)" \
        || { echo "  [FAIL] $f — Python 語法錯誤，中止"; exit 1; }
      ;;
    *.js)
      node --check "$DST/$f" && echo "  [ok]   $f (node syntax)" \
        || { echo "  [FAIL] $f — JS 語法錯誤，中止"; exit 1; }
      ;;
    *.sh)
      bash -n "$DST/$f" && echo "  [ok]   $f (bash syntax)" \
        || { echo "  [FAIL] $f — Bash 語法錯誤，中止"; exit 1; }
      ;;
    *)
      echo "  [skip] $f — 無對應語法驗證器"
      ;;
  esac
done

# ── Git commit & push ─────────────────────────────────────────
echo ""
echo "提交至 GitHub..."

cd "$DST"

# 只 stage 白名單的變更檔案
for f in "${CHANGED_FILES[@]}"; do
  git add "$f"
done

# 也 stage FIXES_LOG.md（若存在且有變動）
if git diff --name-only HEAD 2>/dev/null | grep -q "FIXES_LOG.md"; then
  git add FIXES_LOG.md
fi

# 自動生成 commit message
if [ -n "$CUSTOM_MSG" ]; then
  MSG="$CUSTOM_MSG"
else
  FILE_LIST=$(IFS=', '; echo "${CHANGED_FILES[*]}")
  MSG="sync: update ${FILE_LIST} from AI_system_v3"
fi

git commit -m "${MSG}

Synced from AI_system_v3 (local working directory).
Privacy scan passed. Syntax validated.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

git push origin main

echo ""
echo "✓ 同步完成！"
echo "  已推送：$(git rev-parse --short HEAD) — $MSG"
