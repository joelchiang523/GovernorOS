# GovernorOS

**A self-learning AI programming assistant with a 5-layer memory architecture.**

GovernorOS wraps [Aider](https://aider.chat) with a persistent memory system that learns from
every coding session. It uses a dual-model design (Governor for judgement, Researcher for
generation) and a Harness-driven feedback loop to improve its strategies over time.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   main_loop.py                       │
│   Aider → Harness → State Machine → Episode Record  │
└───────────────────┬─────────────────────────────────┘
                    │
        ┌───────────▼───────────┐
        │     dream_cycle.py    │  Memory Lifecycle Engine
        │  L1→L2→L3→L4→L5      │
        └───────────────────────┘
```

### 5-Layer Memory

| Layer | File | Description |
|-------|------|-------------|
| L1 | `memory/L1_buffer.txt` | Raw event buffer (cleared each session) |
| L2 | `memory/L2_working.md` | MemPalace — single source of truth for current task |
| L3 | `memory/L3_episodes.jsonl` | Episode pool — scored records of each Aider run |
| L4 | `memory/L4_knowledge.json` | Extracted patterns ("if X, then Y") |
| L5 | `memory/L5_strategies.json` | Execution strategies (condition/action/avoid) |

### Dual-Model Design

| Role | Default Model | Responsibility |
|------|--------------|----------------|
| Governor | `qwen3.5:27b` | MemPalace updates, state decisions, L4/L5 review |
| Researcher | `prutser/gemma-4-26B-A4B-...` | Autoresearch briefs, L3→L4 bridge, L5 drafts |

### State Machine

```
continue → [Aider runs] → PASS → continue (or stop if TASK_COMPLETE)
                        → FAIL ×1 → continue
                        → FAIL ×2 → switch_tool (Autoresearch)
                        → FAIL ×3 → rollback → continue
```

---

## Quick Start

### Prerequisites

- [Ollama](https://ollama.ai) with your chosen models pulled
- [Aider](https://aider.chat) installed (`pip install aider-install && aider-install`)
- Python 3.10+

```bash
# Pull models (adjust to your hardware)
ollama pull qwen3.5:27b

# Install dependencies
pip install requests aider-chat
```

### Run a Task

```bash
# Set up environment
export OLLAMA_API_BASE=http://localhost:11434
export OLLAMA_MODEL_GOVERNOR=qwen3.5:27b
export OLLAMA_MODEL_RESEARCHER=qwen3.5:27b   # use same model if you only have one

# Launch the loop
python main_loop.py \
  --task "Build a Python CLI tool that converts CSV to JSON" \
  --harness "python -m pytest tests/ -q" \
  --aider-model "ollama/qwen3.5:27b" \
  --aider-files "main.py" \
  --work-dir ./my_project \
  --max-iter 10
```

### Morning / Evening Cycle

```bash
# Evening — consolidate today's episodes into L4/L5
./startup.sh evening

# Morning — load strategies into PROMPT.md
./startup.sh morning

# Full status
./startup.sh status
```

---

## Components

### `main_loop.py` — Main Orchestration Loop

- Calls Aider with the current task + MemPalace context
- Runs the Harness after each change
- Drives the state machine (continue / switch_tool / rollback / stop)
- Records every episode to L3

Key flags:
```
--task          Task description (injected into Aider prompt)
--harness       Shell command to validate the code (e.g. pytest)
--aider-model   Ollama/OpenAI model string
--aider-files   Space-separated list of files for Aider to edit/create
--work-dir      Project directory
--max-iter      Maximum iterations (default 20)
```

### `dream_cycle.py` — Memory Lifecycle Engine

```bash
# Update MemPalace after a session
python dream_cycle.py --update-mempalace --context "..."

# Consolidate episodes → L4 patterns (Governor reviews)
python dream_cycle.py --sleep

# Extract L5 strategies from L4 (two-phase: Researcher drafts, Governor approves)
python dream_cycle.py --deep

# Run Bridge: L3 events → validated L4/L5 candidates
python dream_cycle.py --bridge

# Apply memory decay (confidence -= 0.05/week after 30 days)
python dream_cycle.py --decay
```

### `git_diff_intel.py` — Diff Pattern Tracker

Accumulates pass/fail statistics per file combination and extracts high-risk / high-success
patterns as L5 candidates.

```bash
python git_diff_intel.py --record --patch-type=bugfix --files=main.py --harness=pass --delta=1.0
python git_diff_intel.py --status
python git_diff_intel.py --analyze --json
```

---

## Memory Initialisation

```bash
# Create fresh memory files from examples
cp memory/L2_working.md.example memory/L2_working.md
cp memory/L3_episodes.jsonl.example memory/L3_episodes.jsonl
cp memory/L4_knowledge.json.example memory/L4_knowledge.json
cp memory/L5_strategies.json.example memory/L5_strategies.json
touch memory/L1_buffer.txt
```

---

## Example Project

`Code/examples/duplicate_finder/` — a tkinter GUI duplicate file finder, built by
GovernorOS in a single iteration using `qwen3.5:27b`. Includes the harness used to
validate it.

---

## Configuration

Override defaults via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_API_BASE` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL_GOVERNOR` | `qwen3.5:27b` | Governor model |
| `OLLAMA_MODEL_RESEARCHER` | `qwen3.5:27b` | Researcher model |
| `AIDER_TIMEOUT` | `900` | Aider subprocess timeout (seconds) |

---

## Privacy Note

The `memory/` directory holds task-specific learned data and is excluded from this
repository via `.gitignore`. Only the schema/example files are committed. Do not
commit `L3_episodes.jsonl`, `L4_knowledge.json`, or `L5_strategies.json` — they may
contain details about your private projects.

---

## License

MIT
