# AI System v3.0 — v4.0 優化策略規劃

> **版本**：v4.0-plan  
> **撰寫日期**：2026-04-12  
> **基準評分**：7.0/10（見下方模組評分表）  
> **目標評分**：8.5/10  
> **核心原則**：保留五層記憶架構與雙模型分工的優點，補強現有短板，不大幅重寫。

---

## 一、現況評分與問題清單

### 模組評分表（基準）

| 模組 | 現況分 | 主要問題 |
|---|---|---|
| main_loop.py | 7.5 | DDI 補全 bug（已修）；Total Recall 在 benchmark 下永遠跳過 |
| dream_cycle.py | 7.0 | L5 空白；L4 有 ID 重複；age decay 新加未驗證 |
| benchmark_runner.py | 8.5 | 最穩定；無重大問題 |
| git_diff_intel.py | 6.5 | 資料收集完整但未被其他模組消費 |
| mempalace.py | 6.0 | benchmark 全跳過，影響力低 |
| task_intake.py | 5.5 | 在 benchmark 流程中被繞過 |
| **L5 策略庫** | 3.0 | **0 筆 active，功能形同虛設** |
| **L4 知識庫** | 6.0 | confidence 普遍 0.55–0.70，有 ID 重複 |
| Total Recall | — | 已整合但 FAST_MODE 永遠跳過，從未驗證 |

### 根本問題歸類

```
問題 A：L5 永遠空白
  └─ 根因：L4 confidence 未超過 0.75（升 L5 門檻），dream cycle 跑太少
  
問題 B：L4 ID 重複與 confidence 偏低
  └─ 根因：--sleep 每次重新生成 id，未去重；scoring 參數偏保守
  
問題 C：Total Recall 無法在 benchmark 被驗證
  └─ 根因：FAST_MODE=1 統一跳過所有 LLM 呼叫，recall 無法獨立開關
  
問題 D：DiffIntel 資料孤島
  └─ 根因：--analyze 結果只印出報表，未寫入 L4/L5，無法被 DDI 利用
  
問題 E：task_intake.py 存在感低
  └─ 根因：benchmark 直接呼叫 main_loop.py，不經過任務分派層
  
問題 F：字串任務（string_ops）timeout
  └─ 根因：DDI-Decompose 只回傳部分子任務（已修正：補全驗證）
```

---

## 二、優化策略總覽（保留優點）

### 保留不動的優點

| 優點 | 原因 |
|---|---|
| 五層記憶架構（L1-L5） | 分層門檻設計正確，防止錯誤知識固化 |
| 雙模型分工（Governor/Researcher） | 高風險節點雙層把關的思路正確 |
| DDI 4-stage pipeline | 架構完整，補全驗證修正後更健壯 |
| GsTask 自適應 timeout | 70%→90% 效果已驗證 |
| Harness 唯一真相原則 | 不可改動 |
| git_snapshot / last_good_snapshot | rollback 精度高 |
| bridge_schema_validate（純 Python） | 格式把關輕量可靠 |

### 六大優化方向（按優先級）

```
P0（本週）：修資料品質
  ├─ O1：L4 ID 去重與重新編號
  └─ O2：dream cycle 頻率提升（每 5 episodde 觸發一次 --sleep）

P1（下週）：啟動 L5
  ├─ O3：降低 L4→L5 升級門檻（0.75→0.65），先讓 L5 有資料
  └─ O4：L4 confidence 校準（初始值 0.65→0.70，evidence_count 加速加分）

P2（兩週後）：打通 DiffIntel
  └─ O5：DiffIntel → L4 直接寫入通道

P3（三週後）：Total Recall 獨立開關
  └─ O6：FAST_MODE 拆成三個子開關（mempalace / bridge / recall）
```

---

## 三、詳細改善規劃

---

### O1：L4 ID 去重與重新編號

**問題**：L4 目前有 k_009 和 k_010 各出現兩次，造成 confidence 計算混亂。

**修正方式**：在 `dream_cycle.py` 的 `cmd_sleep()` Phase 2 後，加入 ID 正規化步驟。

**檔案**：`dream_cycle.py`  
**位置**：`cmd_sleep()` → Phase 2 複核完成後

**實作邏輯**：
```python
def _normalize_l4_ids(items: list[dict]) -> list[dict]:
    """重新編號所有 L4 items，解決 id 重複問題。"""
    seen_patterns = {}
    normalized = []
    counter = 1
    for item in items:
        pattern = item.get("pattern", "")
        # 相同 pattern 的合併（取 confidence 較高者，累加 evidence_count）
        key = pattern[:40]  # 前 40 字做 key
        if key in seen_patterns:
            existing = seen_patterns[key]
            existing["confidence"] = max(existing["confidence"], item["confidence"])
            existing["evidence_count"] += item.get("evidence_count", 0)
            existing["source_episodes"] = list(set(
                existing.get("source_episodes", []) + item.get("source_episodes", [])
            ))
            continue
        item["id"] = f"k_{counter:03d}"
        seen_patterns[key] = item
        normalized.append(item)
        counter += 1
    return normalized
```

**驗收標準**：`--status` 顯示 L4 無重複 id；所有 id 按 k_001 連續編號。

---

### O2：Dream Cycle 觸發頻率提升

**問題**：目前 `--bridge` 每 10 episode 才觸發一次 `--sleep`，L4 更新緩慢，導致 L5 餓死。

**現況**：
```
validated episodde 達 356 筆，但 L4 只有 10 筆 active
原因：--sleep 觸發次數少，且閾值偏高（l3_to_l4_min_count=3）
```

**修正方式**：

1. **`dream_cycle.py --bridge`**：觸發門檻從 10 episode → 5 episode
2. **`run_training.sh`**：每輪 benchmark 結束後，強制執行一次 `--sleep` + `--deep`（不管 episode 數量）

**檔案**：`dream_cycle.py`

```python
# 現況：
BRIDGE_AUTO_SLEEP_INTERVAL = 10  

# 改為：
BRIDGE_AUTO_SLEEP_INTERVAL = 5
```

**`run_training.sh` 新增 Step**：
```bash
# Step 4.5（介於 benchmark 結束與 evening 之間）
echo "[Step 4.5] 強制記憶固化（--sleep + --deep）..."
python3 dream_cycle.py --sleep
python3 dream_cycle.py --deep
python3 dream_cycle.py --wake
```

**驗收標準**：每次跑完 10 題 benchmark，L4 至少新增 2 筆；30 題後 L5 有 1+ 筆 active。

---

### O3：L4→L5 升級門檻調整（0.75→0.65）

**問題**：L5 策略庫完全空白，根因是 L4 的 confidence 最高只有 0.70，低於升 L5 門檻 0.75。

**策略**：降低門檻，先讓 L5 有資料，再觀察效果後調整。  
這不影響 L4 準確性，只是讓「已有中等信心」的知識被提升為可執行策略。

**檔案**：`dream_cycle.py`

```python
# 現況：
L4_TO_L5_MIN_CONF = 0.75

# 改為：
L4_TO_L5_MIN_CONF = 0.65
```

**同步修改**：Phase 2 Governor 複核的 confidence 上限也需調整

```python
# 現況：Governor 複核時 confidence 上限 0.75
# 改為：L4 首次生成上限 0.72（給升 L5 留空間）
L4_INITIAL_CONF_CAP = 0.72  # 由 0.75 降至 0.72

# L5 首次生成上限維持 0.80
L5_INITIAL_CONF_CAP = 0.80
```

**安全機制**（避免品質下降）：
- L5 每次被用於推理後，`success_count` / `fail_count` 追蹤
- fail_rate > 35%（從 40% 降）時提前降回 L4
- 新增 `needs_validation` 欄位，首次從低 confidence 升上來的 L5 標記此旗標

**驗收標準**：2 週內 L5 有 3+ 筆 active；fail_rate < 35%。

---

### O4：L4 Confidence 校準

**問題**：目前 L4 confidence 初始值偏低（0.55–0.65），衰減快，導致知識剛生成就開始走向退休。

**根因分析**：
```
evidence_count=23 的 k_001（bubble sort bug），confidence 只有 0.60
→ 23 個 episode 支撐，應該更有信心
→ 說明 evidence_count 對 confidence 的加分效果不足
```

**修正方式**：在 `dream_cycle.py` 的 `cmd_sleep()` Phase 1 prompt 中，增加 evidence_count 對 confidence 的指導：

```
evidence_count 對 confidence 的參考：
  1-2 筆  → confidence 0.55–0.60
  3-5 筆  → confidence 0.62–0.68
  6-10 筆 → confidence 0.70–0.73
  11+ 筆  → confidence 0.73–0.75（上限 0.75）
```

同時在 `_normalize_l4_ids()` 合併時，重新計算 confidence：

```python
def _recalc_confidence(item: dict) -> float:
    ev = item.get("evidence_count", 1)
    base = item.get("confidence", 0.60)
    # evidence_count 加成：每 5 筆 +0.02，上限 0.75
    bonus = min(0.15, (ev // 5) * 0.02)
    return min(0.75, base + bonus)
```

**驗收標準**：evidence_count >= 10 的 L4 items，confidence >= 0.70。

---

### O5：DiffIntel → L4 直接寫入通道

**問題**：`git_diff_intel.py --analyze` 已能萃取「高風險改動 pattern」，但結果只印報表，未寫入 L4。

**現況資料**：
```
356 筆 L3 episodes → 每筆都有 git diff 記錄
但 git_diff_records.jsonl 的分析結果從未回饋到 L4
```

**修正方式**：在 `git_diff_intel.py` 增加 `--export-l4` 子指令，將高信心 pattern 直接寫入 `L4_knowledge.json`：

**檔案**：`git_diff_intel.py`

```python
def cmd_export_l4(records: list[dict], l4_path: Path) -> int:
    """
    從 DiffIntel 分析結果萃取 L4 candidates，寫入 L4_knowledge.json。
    規則：
      - 同一檔案組合 fail_rate >= 50%，累積 >= 6 筆 → 生成 avoid-pattern L4
      - 單一檔案 pass_rate >= 80%，累積 >= 6 筆 → 生成 action-pattern L4
    """
    patterns = _analyze_patterns(records)
    candidates = []
    for p in patterns:
        if p["fail_rate"] >= 0.50 and p["count"] >= 6:
            candidates.append({
                "pattern": f"同時修改 {'+'.join(p['files'])} 時 fail_rate={p['fail_rate']:.0%}，需謹慎",
                "scope": "Git Diff Intelligence",
                "confidence": min(0.70, 0.50 + p["count"] * 0.02),
                "evidence_count": p["count"],
                "source": "diff_intel",
                "status": "active",
            })
    # 寫入 L4（不覆蓋已有 source != diff_intel 的項目）
    ...
    return len(candidates)
```

**整合到 `startup.sh evening`**：
```bash
# Step 4.6：DiffIntel → L4
python3 git_diff_intel.py --export-l4
```

**驗收標準**：`startup.sh analyze` 執行後，L4 新增 diff-intel 來源的 pattern；下一輪任務的 DDI 可在 PROMPT.md 看到相關知識注入。

---

### O6：FAST_MODE 拆成三個子開關

**問題**：`GOVERNOR_FAST_MODE=1` 是大開關，跳過 MemPalace、Bridge、**Total Recall** 全部，導致 Total Recall 從未在任何情況下執行，無法驗證其效果。

**修正方式**：拆成三個獨立環境變數：

| 舊開關 | 新開關 | 預設 | 說明 |
|---|---|---|---|
| `GOVERNOR_FAST_MODE=1` | `SKIP_MEMPALACE=1` | 0 | 跳過 `--update-mempalace` |
| （同上） | `SKIP_BRIDGE=1` | 0 | 跳過 `--bridge` 任務後處理 |
| （同上） | `SKIP_RECALL=0` | 0 | **預設不跳過**，benchmark 也啟用 |

**`benchmark_runner.py` 修改**：
```python
# 現況：
bench_env = {**os.environ, "GOVERNOR_FAST_MODE": "1"}

# 改為：
bench_env = {
    **os.environ,
    "SKIP_MEMPALACE": "1",   # benchmark 跳過 mempalace（快）
    "SKIP_BRIDGE": "1",      # benchmark 跳過 bridge（快）
    "SKIP_RECALL": "0",      # benchmark 保留 Total Recall（驗證效果）
}
```

**`main_loop.py` 修改**：
```python
# 現況：
FAST_MODE = os.environ.get("GOVERNOR_FAST_MODE", "0") == "1"

# 改為：
SKIP_MEMPALACE = os.environ.get("SKIP_MEMPALACE", "0") == "1"
SKIP_BRIDGE    = os.environ.get("SKIP_BRIDGE", "0") == "1"
SKIP_RECALL    = os.environ.get("SKIP_RECALL", "0") == "1"

# recall_past_failures() 中：
if SKIP_RECALL:
    return ""
```

**驗收標準**：benchmark 執行時 log 出現 `[Recall] 找到相似失敗案例，已注入 context`（或 `[Recall] 無相關過去失敗`），不再全部跳過。

---

## 四、實作排程

| 週次 | 優化項目 | 預計影響 | 驗收方式 |
|---|---|---|---|
| **Week 1** | O1（L4 去重）+ O2（觸發頻率）| L4 結構乾淨；新 benchmark 後 L4 增加 | `--status` 無重複 id |
| **Week 1** | O3（L5 門檻 0.75→0.65）+ O4（confidence 校準）| L5 首次有資料 | L5 active >= 3 |
| **Week 2** | O5（DiffIntel → L4）| 來自 diff 的 pattern 自動入庫 | L4 新增 source=diff_intel |
| **Week 2** | O6（FAST_MODE 拆分）| Total Recall 在 benchmark 啟用 | log 出現 `[Recall]` |
| **Week 3** | 跑完整 benchmark 驗證所有優化 | 預計通過率 90%→95%（string_ops 補全修正 + recall 輔助）| benchmark pass_rate |

---

## 五、不建議做的方向

以下是「看起來合理但實際會破壞系統優點」的方向，明確排除：

| 方向 | 排除原因 |
|---|---|
| 把 Governor 換成更小的模型 | 27b 在 integrate 步驟的品質優勢不可替代 |
| 移除 bridge_schema_validate | 純 Python 把關是最穩定的防線，移除後 L3 資料品質會下降 |
| 合併 L4 和 L5 為單層 | 兩層分離的設計使「規律」與「策略」職責清晰，合併會造成 Conflict Resolver 失效 |
| 把 total_recall 改為每次迭代注入 | 只有 iter=1 注入是刻意設計（避免 context 污染），不應改為每輪 |
| 直接把 L3 全部餵給 Governor 做 --sleep | MAX_SLEEP_GROUPS=10 的限制是 Cost Governor 的核心，移除會造成 token 超支 |
| 大幅增加 max_iter | 根因是 DDI decompose 不完整（已修），timeout 靠 GsTask，不靠增加迭代 |

---

## 六、預期優化後系統評分

| 模組 | 現況 | 目標 | 優化項 |
|---|---|---|---|
| main_loop.py | 7.5 | 8.5 | O6（Recall 啟用） |
| dream_cycle.py | 7.0 | 8.5 | O1+O2+O3+O4 |
| benchmark_runner.py | 8.5 | 8.5 | O6 子開關 |
| git_diff_intel.py | 6.5 | 8.0 | O5（→L4 通道）|
| L5 策略庫 | 3.0 | 7.0 | O2+O3+O4 |
| L4 知識庫 | 6.0 | 8.0 | O1+O4 |
| Total Recall | — | 7.0 | O6 |

**整體目標評分：8.5/10**

---

## 七、成功指標

```
短期（2 週）
  ✓ L4 無重複 id
  ✓ L4 evidence_count >= 10 的項目 confidence >= 0.70
  ✓ L5 active >= 3 筆
  ✓ benchmark log 中出現 [Recall] 字樣

中期（1 個月）
  ✓ benchmark pass_rate 穩定 >= 95%
  ✓ string_ops 通過（DDI 補全驗證修正後）
  ✓ L5 被 PROMPT.md 注入至少一條策略
  ✓ DiffIntel 貢獻至少 2 筆 L4 pattern

長期（3 個月）
  ✓ L5 fail_rate < 35%（策略有效）
  ✓ L4 knowledge 持續累積至 20+ 筆
  ✓ 不同任務類型的 Total Recall 命中率 > 40%
```

---

## 附錄：本次已完成的修正

| 修正 | 狀態 | commit |
|---|---|---|
| DDI-Decompose 補全驗證（string_ops 根因） | ✅ 已完成 | 3e7088d |
| GsTask 自適應 timeout（600s→900/1200/1800s） | ✅ 已完成 | 3e7088d |
| keep_alive="0"（VRAM 競爭修正） | ✅ 已完成 | 7a5573e |
| GOVERNOR_FAST_MODE=1（benchmark 提速）| ✅ 已完成 | 7a5573e |
| Total Recall TF-IDF 整合 | ✅ 已整合（O6 後可驗證）| 3e7088d |
| Compiled Truth + Timeline（L4 timestamps）| ✅ 已完成 | 3e7088d |
| Dream Cycle 自動化（每 10 ep 觸發 --sleep）| ✅ 已完成（O2 加密頻率）| 3e7088d |
