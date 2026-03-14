# The Brownfield Cartographer

**Multi-agent Codebase Intelligence System for Rapid FDE Onboarding**

A tool that ingests any GitHub repository (or local path) and produces a living, queryable knowledge graph of the system's architecture, data flows, and semantic structure.

## Features

### Four Specialized Agents

- **The Surveyor** — Static structure analysis: multi-language AST parsing (tree-sitter), module import graph, PageRank, git velocity (30-day + 80/20 rule), dead code detection, circular dependency detection, domain clustering
- **The Hydrologist** — Data flow & lineage analysis: SQL lineage via sqlglot (8 dialects), Python data operations (pandas/PySpark/SQLAlchemy), dbt YAML + Airflow DAG parsing, notebook analysis, blast_radius with path reconstruction, find_sources/find_sinks
- **The Semanticist** — LLM-powered semantic analysis (Google Gemini): purpose statement generation (code-based, not docstring), documentation drift detection, domain clustering via embeddings, Day-One FDE question answering, ContextWindowBudget for cost discipline
- **The Archivist** — Living context artifact generation: CODEBASE.md (injectable into AI agents), onboarding_brief.md (5 FDE Day-One answers with evidence), cartography_trace.jsonl (full audit log)

### Navigator Query Interface

Interactive query mode with 4 tools:
- `find <concept>` — Semantic search for code implementing a concept
- `trace <dataset> [up|down]` — Trace data lineage with file:line citations
- `blast <module>` — Show blast radius (import + lineage impact)
- `explain <path>` — Explain a module with LLM enhancement

### Knowledge Graph

- NetworkX-based directed graphs for structure and lineage
- Pydantic v2 schemas with field validators for all node/edge types
- JSON serialization with round-trip load/save
- Multi-language support: Python, SQL, YAML, JavaScript/TypeScript

## Installation

```bash
# Using uv (recommended)
uv venv
uv pip install -e .

# Or using pip
pip install -e .
```

## Quick Start

### Analyze a local codebase

```bash
cartographer analyze ./path/to/codebase
```

### Analyze a GitHub repository

```bash
cartographer analyze https://github.com/user/repo
```

### Analyze without LLM (no API key required)

```bash
cartographer analyze ./my-project --skip-llm
```

### Incremental analysis (only changed files)

```bash
cartographer analyze ./my-project --incremental
```

### Custom output directory

```bash
cartographer analyze ./my-project --output ./results
```

### Start interactive query mode

```bash
cartographer query ./path/to/analyzed-codebase
```

### View analysis summary

```bash
cartographer summary ./path/to/codebase
```

### Check blast radius

```bash
cartographer blast-radius ./path/to/codebase "module_name"
```

## Output Artifacts

All outputs are saved to `<target>/.cartography/`:

| File | Description |
|------|-------------|
| `CODEBASE.md` | Living context file for AI agent injection (architecture overview, critical path, data sources/sinks, known debt, module purpose index) |
| `onboarding_brief.md` | FDE Day-One Brief answering the 5 critical questions with evidence citations |
| `module_graph.json` | Module import graph with PageRank scores, complexity metrics, domain clusters |
| `lineage_graph.json` | Data lineage DAG showing data flow across SQL, Python, YAML |
| `cartography_trace.jsonl` | Audit log of every analysis action with timestamps |

## Architecture

```
┌─────────────────────────────────────────────┐
│                    CLI                       │
│  analyze | query | summary | blast-radius   │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│              Orchestrator                    │
│  Surveyor → Hydrologist → Semanticist →     │
│  Archivist   (incremental update support)   │
└──┬──────┬──────────┬──────────┬─────────────┘
   │      │          │          │
   ▼      ▼          ▼          ▼
┌──────┐┌──────┐ ┌────────┐ ┌────────┐
│Survey││Hydro.│ │Semantic││Archivist│
│  or  ││      │ │  ist   ││        │
└──┬───┘└──┬───┘ └───┬────┘└───┬────┘
   │       │         │         │
   └───────┴─────┬───┴─────────┘
                 │
          ┌──────▼───────┐
          │  Knowledge   │
          │    Graph     │
          │ (NetworkX +  │
          │  Pydantic)   │
          └──────┬───────┘
                 │
          ┌──────▼───────┐
          │  Navigator   │
          │ (Interactive │
          │  Query)      │
          └──────────────┘
```

### Analyzers

- **TreeSitterAnalyzer** — Multi-language AST parsing with LanguageRouter
- **SQLLineageAnalyzer** — sqlglot-based SQL dependency extraction (PostgreSQL, BigQuery, Snowflake, DuckDB, MySQL, SQLite, Trino, Spark)
- **DAGConfigParser** — Airflow DAG and dbt YAML config parsing

### Models (Pydantic Schemas)

**Node Types:** `ModuleNode`, `DatasetNode`, `FunctionNode`, `TransformationNode`

**Edge Types:** `IMPORTS`, `PRODUCES`, `CONSUMES`, `CALLS`, `CONFIGURES`

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_HOST` | Ollama server URL | `http://localhost:11434` |

**LLM setup (local, no API key needed):**
```bash
# Install Ollama: https://ollama.com
ollama pull kimi-k2.5:cloud

# Then run the Cartographer — it auto-detects Ollama
cartographer analyze ./my-project
```

The Cartographer uses Ollama with `kimi-k2.5:cloud` for:
- Module purpose statements (Semanticist)
- Documentation drift detection (Semanticist)
- Day-One FDE question answering (Semanticist)
- `explain` query in interactive mode (Navigator)

If Ollama is not running or the model is not available, the system **gracefully falls back** to static analysis for all LLM tasks — no errors, just less semantic depth. You can also explicitly disable LLM with `--skip-llm`.


## Target Codebases

Tested against:

1. **mitodl/ol-data-platform** — MIT Open Learning data platform (Python + SQL/dbt + YAML, 1,108 files)
2. **Self-referential** — The Cartographer's own codebase (Python, 16 files)

## Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run on a target
python -m src.cli analyze <target>

# Run interactively
python -m src.cli query <target>
```
