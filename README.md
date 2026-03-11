# The Brownfield Cartographer

**Multi-agent Codebase Intelligence System for Rapid FDE Onboarding**

A tool that ingests any GitHub repository (or local path) and produces a living, queryable knowledge graph of the system's architecture, data flows, and semantic structure.

## Features

- **The Surveyor** — Static structure analysis: module import graph, PageRank, git velocity, dead code detection
- **The Hydrologist** — Data flow & lineage analysis: SQL lineage via sqlglot, Python data operations, DAG config parsing
- **Knowledge Graph** — NetworkX-based graph with Pydantic schemas, serialized to JSON
- **Multi-language Support** — Python, SQL, YAML, JavaScript/TypeScript via tree-sitter AST parsing

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

### Specify custom output directory

```bash
cartographer analyze ./my-project --output ./results
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

All outputs are saved to `<target>/.cartography/` (or the specified output directory):

| File | Description |
|------|-------------|
| `module_graph.json` | Module import graph with PageRank scores, complexity metrics |
| `lineage_graph.json` | Data lineage DAG showing data flow across SQL, Python, YAML |
| `cartography_trace.jsonl` | Audit log of every analysis action |

## Architecture

```
┌─────────────────────────────────────────────┐
│                    CLI                       │
│            (src/cli.py)                      │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│              Orchestrator                    │
│          (src/orchestrator.py)               │
│  Wires agents in sequence, serializes       │
│  outputs to .cartography/                   │
└──────┬───────────────────────┬──────────────┘
       │                       │
┌──────▼─────────┐   ┌────────▼───────────┐
│   Surveyor     │   │   Hydrologist      │
│  (Static       │   │  (Data Flow &      │
│   Structure)   │   │   Lineage)         │
└──────┬─────────┘   └────────┬───────────┘
       │                       │
       │  ┌────────────────┐   │
       └──►  Knowledge     ◄───┘
          │  Graph         │
          │  (NetworkX +   │
          │   Pydantic)    │
          └────────────────┘
```

### Analyzers

- **TreeSitterAnalyzer** — Multi-language AST parsing with LanguageRouter
- **SQLLineageAnalyzer** — sqlglot-based SQL dependency extraction (supports PostgreSQL, BigQuery, Snowflake, DuckDB, etc.)
- **DAGConfigParser** — Airflow DAG and dbt YAML config parsing

### Models (Pydantic Schemas)

**Node Types:**
- `ModuleNode` — Code file with imports, functions, complexity metrics
- `DatasetNode` — Data source/sink (table, file, stream, API)
- `FunctionNode` — Function with signature and purpose
- `TransformationNode` — Data transformation operation

**Edge Types:**
- `IMPORTS` — Module import relationships
- `PRODUCES` — Transformation → dataset
- `CONSUMES` — Dataset → transformation
- `CALLS` — Function call graph
- `CONFIGURES` — Config file → module/pipeline

## Development

```bash
# Install with dev dependencies
uv pip install -e ".[dev]"

# Run on a target
python -m src.cli analyze <target>
```

## Target Codebases

This system has been tested against:

1. **mitodl/ol-data-platform** — MIT Open Learning data platform (Python + SQL/dbt + YAML)

## Project Status (Interim)

### Working
- [x] Multi-language AST parsing (Python via tree-sitter, SQL, YAML via regex)
- [x] Module import graph construction with PageRank
- [x] Git velocity extraction (30-day commit frequency)
- [x] Circular dependency detection
- [x] Dead code candidate identification
- [x] SQL lineage extraction via sqlglot (with dbt Jinja preprocessing)
- [x] Python data operation detection (pandas, PySpark, SQLAlchemy)
- [x] Airflow DAG parsing
- [x] dbt schema.yml / source parsing
- [x] Knowledge graph serialization to JSON
- [x] CLI with analyze, summary, and blast-radius commands

### In Progress
- [ ] Semanticist agent (LLM-powered purpose statements)
- [ ] Archivist agent (CODEBASE.md generation)
- [ ] Navigator agent (interactive query interface)
- [ ] Vector-indexed semantic search
