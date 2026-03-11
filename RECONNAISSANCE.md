# RECONNAISSANCE: ol-data-platform (mitodl/ol-data-platform)

**Repository:** https://github.com/mitodl/ol-data-platform
**Date:** 2026-03-11
**Description:** Data pipelines and analytics platform for MIT Open Learning

---

## Primary Tech Stack

| Component | Technology | Details |
|---|---|---|
| **Orchestration** | Dagster (dg framework) | 10 Dagster projects under `dg_projects/` |
| **Transformation** | dbt (587 SQL models) | Project: `open_learning` under `src/ol_dbt/` |
| **SQL Dialect** | Trino (Starburst) | Production: `mitol-ol-data-lake-production.trino.galaxy.starburst.io` |
| **Local Dev DB** | DuckDB | Via `dbt-duckdb`, Iceberg table format |
| **Data Ingestion** | Airbyte (self-hosted OSS) | `api-airbyte.odl.mit.edu` (production) |
| **Data Lake** | S3 (Parquet/Iceberg) | All Airbyte connections end in "→ S3 Data Lake" |
| **BI/Analytics** | Apache Superset | `src/ol_superset/` with chart/dashboard configs as YAML |
| **Language** | Python 3.13+ | Managed with `uv` workspaces |
| **Secrets** | HashiCorp Vault | QA + production Vault instances |
| **CI/CD** | GitHub Actions | 2 workflows: `project_automation.yaml`, `publish_dbt_docs.yaml` |
| **Infra** | Docker, Kubernetes | Dagster K8s deployment via `dg_deployments/` |
| **Linting** | SQLFluff, ruff, sqlfmt | SQL and Python quality checks |

---

## Top-Level Folder Structure

```
ol-data-platform/
├── dg_projects/           # Dagster projects (10 domain-specific code locations)
│   ├── lakehouse/         # CENTRAL: dbt + Airbyte + Superset orchestration
│   ├── openedx/           # OpenEdX tracking log processing & normalization
│   ├── edxorg/            # edX.org data archives, API, and GCS operations
│   ├── legacy_openedx/    # Legacy OpenEdX MySQL extraction
│   ├── canvas/            # Canvas LMS data ingestion via API
│   ├── learning_resources/# MIT learning resource metadata, video shorts
│   ├── data_platform/     # Platform metadata (database definitions)
│   ├── data_loading/      # Bulk S3 data loading (edxorg S3 ingest)
│   ├── student_risk_probability/  # ML: student risk probability model
│   └── b2b_organization/  # B2B org data exports
├── packages/
│   └── ol-orchestrate-lib/ # SHARED LIBRARY: resources, IO managers, helpers
├── src/
│   ├── ol_dbt/            # dbt project (587 models, macros, seeds, snapshots)
│   ├── ol_orchestrate/    # Legacy (moved to packages/ol-orchestrate-lib)
│   └── ol_superset/       # Superset dashboard/chart/dataset configs + CLI
├── dg_deployments/        # Dagster K8s deployment configs, Dockerfiles
├── bin/                   # CLI utilities (dbt staging model generator, uv ops)
├── docs/                  # Local development guides, Postgres pooling docs
├── dockerfiles/           # Additional Dockerfiles (orchestrate)
└── scripts/               # Setup scripts (get-pants.sh)
```

---

## Five FDE Day-One Questions

### 1. What Is the Primary Data Ingestion Path?

**Answer: Airbyte OSS → S3 Data Lake → Trino (Starburst) → dbt staging models**

The ingestion pipeline is:

```
Source Databases/APIs
        ↓
  Airbyte (self-hosted OSS)
  [~30+ connections, each ending "→ S3 Data Lake"]
        ↓
  S3 (Parquet format, raw schema: ol_warehouse_*_raw)
        ↓
  Trino/Starburst (query engine over S3)
        ↓
  dbt staging models (233 SQL files)
        ↓
  intermediate → dimensional → marts → reporting
```

**Key Evidence:**
- All 13 dbt source YAML files declare `loader: airbyte` and source name `ol_warehouse_raw_data`
- The `lakehouse` Dagster project (in `dg_projects/lakehouse/lakehouse/definitions.py`) is the central orchestrator:
  - Builds Airbyte assets via `build_airbyte_assets_definitions()` with connections filtered by `"s3 data lake"` suffix
  - Creates per-connection jobs + schedules (`sync_and_stage_{group_name}`) at 6h/12h/24h intervals
  - Maps Airbyte outputs to dbt sources via `key_prefix="ol_warehouse_raw_data"`
- **30+ Airbyte connections** feed data to S3, including:
  - **6-hour cadence** (most critical): `xpro_production_app_db`, `mitx_online_production_app_db`, `ocw_studio_app_db`, `odl_video_service`, `learn_ai_production`
  - **12-hour cadence**: `mitx_online_open_edx_db`, `mitx_residential_open_edx_db`, tracking logs, forum databases
  - **24-hour cadence**: `bootcamps`, `edxorg`, `micromasters`, `salesforce`, `mailgun`, `irx_bigquery`

**Secondary ingestion paths:**
- `edxorg` project: edX.org API + GCS course archive extraction
- `openedx` project: OpenEdX tracking log normalization from S3
- `canvas` project: Canvas LMS API client
- `learning_resources` project: Sloan API, Google Sheets, video processing
- `legacy_openedx` project: MySQL extraction (legacy path)
- `data_loading` project: Bulk S3 edxorg ingest

---

### 2. What Are the 3-5 Most Critical Output Datasets/Endpoints?

**1. Combined Marts (`marts/combined/`) — 11 models**
The cross-platform unified view. These combine data from MITx Online, MITx Pro, MicroMasters, Bootcamps, edX.org, and OCW:
- `marts__combined__users` — Unified learner profiles across all platforms
- `marts__combined_course_enrollment_detail` — All enrollments across platforms
- `marts__combined__orders` — Cross-platform commerce/transactions
- `marts__combined_program_enrollment_detail` — Program-level enrollment data
- `marts__combined_course_engagements` — Unified engagement metrics

**2. Reporting Models (`reporting/`) — 21 models**
Direct Superset data sources, including:
- `enrollment_detail_report` — Enrollment analytics
- `learner_demographics_and_cert_info` — Learner demographics
- `cheating_detection_report` — Academic integrity monitoring
- `student_risk_probability_report` — ML-driven risk scores
- `learner_engagement_report` — Engagement analytics

**3. Dimensional Models (`dimensional/`) — 16 models**
Star schema analytics layer:
- `dim_user`, `dim_course_content`, `dim_video`, `dim_problem`, `dim_platform`
- Transaction facts: `tfact_video_events`, `tfact_problem_events`, `tfact_chatbot_events`
- Aggregate facts: `afact_video_engagement`, `afact_problem_engagement`

**4. Superset Dashboards — 18 dashboards**
The BI consumption layer (defined in `src/ol_superset/assets/dashboards/`):
- Course Engagement, Enrollment Activity, Learner Engagement
- Learner Demographics, Organization Administration
- Program Enrollment and Credential, Suspicious Behavior Report
- Course AI Chatbot, Combined Learners Search

**5. External/IRx Models (`external/irx/`) — 110 models**
Data shared with MIT Institutional Research (IRx) across MITx (44), MITx Online (33), and xPro (33).

---

### 3. What Is the Blast Radius If the Most Critical Module Fails?

**Most Critical Module: `dg_projects/lakehouse/`**

The `lakehouse` Dagster project is the single nexus that ties everything together:

| Dependency | Impact if Lakehouse fails |
|---|---|
| **Airbyte ingestion** | All ~30 Airbyte sync jobs are defined here. No raw data refresh. |
| **dbt full project** | `full_dbt_project` asset runs all 587 dbt models. Nothing transforms. |
| **Superset sync** | `superset_assets` sync mart/reporting/dimensional models to Superset. Dashboards go stale. |
| **Automation sensor** | The `dbt_automation_sensor` triggers downstream dbt models. All non-staging downstream goes dark. |
| **All schedules** | Airbyte sync schedules + instructor onboarding schedule are defined here. |

**Blast radius: TOTAL.** If `lakehouse/definitions.py` fails to load, zero pipelines run, zero dbt models execute, and all 18 Superset dashboards go stale.

**Second most critical: `packages/ol-orchestrate-lib/`**

The shared library (`ol_orchestrate`) is imported by nearly every Dagster project. It provides:
- Vault authentication (`resources/secrets/vault.py`)
- API client factories (Canvas, OpenEdX, Learn, GitHub)
- Database clients (Postgres, Athena, BigQuery)
- IO managers, constants, Dagster helpers, hooks
- Automation policies (used by lakehouse dbt translator)

If `ol-orchestrate-lib` breaks, all Dagster projects fail to import.

---

### 4. Where Is the Business Logic Concentrated vs. Distributed?

**CONCENTRATED in dbt SQL models (587 files, ~70% of business logic):**

```
src/ol_dbt/models/
├── staging/         233 models — Schema standardization, deduplication, type casting
│   ├── mitxpro/      62 models (largest domain)
│   ├── mitxonline/   59 models
│   ├── micromasters/  26 models
│   ├── edxorg/       23 models
│   └── ... (12 domains total)
├── intermediate/    178 models — Domain-specific transforms, joins, business rules
│   ├── mitxpro/      47 models (largest)
│   ├── mitxonline/   44 models
│   ├── micromasters/  20 models
│   ├── edxorg/       17 models
│   ├── combined/      7 models (CRITICAL cross-platform unification)
│   └── ... (14 domains total)
├── marts/            27 models — Final business entities (users, orders, enrollments)
├── reporting/        21 models — Dashboard-ready aggregations
├── dimensional/      16 models — Star schema (dims + facts)
├── external/        110 models — IRx data sharing
└── migration/         3 models — edxorg→mitxonline migration
```

**Key patterns:**
- **Staging layer**: Standardization via macros (`cast_timestamp_to_iso8601`, `apply_deduplication_query`, `extract_course_id`)
- **Intermediate layer**: Platform-specific business rules, then cross-platform combination in `combined/`
- **Marts layer**: `combined/` models are the most business-logic-dense, unifying users/enrollments/orders across 8 platforms

**DISTRIBUTED in Python (orchestration logic only, not business logic):**
- `/dg_projects/*/` — Dagster asset definitions, schedules, sensors (thin orchestration wrappers)
- `/packages/ol-orchestrate-lib/` — Shared infra code (API clients, DB connectors, helpers)
- Business logic in Python is minimal and limited to:
  - `student_risk_probability/` project (ML model execution)
  - `learning_resources/` project (video processing)
  - `b2b_organization/` project (data exports)

**Bottom line:** Business logic lives overwhelmingly in dbt SQL. Python is infrastructure/orchestration only.

---

### 5. What Has Changed Most in the Last 90 Days? (Git Velocity Map)

**185 commits** from 2025-12-11 to present, 6 human contributors + 2 bots.

#### Top Contributors
| Contributor | Commits | Focus |
|---|---|---|
| Tobias Macey | 57 | Core platform, lakehouse, deployment |
| renovate[bot] | 50 | Dependency updates (uv.lock files) |
| Rachel Lougee | 39 | Superset dashboards, reporting models |
| Kate Delaney | 14 | dbt models, reporting |
| Matt Bertrand | 8 | Misc |

#### Hottest Areas by Directory (file touches)
| Directory | Touches | What's happening |
|---|---|---|
| `src/ol_superset/assets/charts/` | 634 | **Massive** Superset chart config work |
| `src/ol_superset/assets/datasets/` | 389 | Dataset definitions for Superset |
| `src/ol_superset/assets/dashboards/` | 90 | Dashboard configuration changes |
| `dg_projects/lakehouse/` | 67 | Core orchestration evolution |
| `src/ol_dbt/models/reporting/` | 47 | Reporting model refinement |
| `dg_projects/legacy_openedx/` | 36 | Legacy system maintenance |
| `dg_projects/openedx/` | 30 | OpenEdX pipeline updates |
| `dg_projects/learning_resources/` | 30 | Learning resource pipeline |
| `src/ol_dbt/models/migration/` | 17 | edxorg→mitxonline migration |
| `src/ol_dbt/models/dimensional/` | 13 | Dimensional model work |
| `packages/ol-orchestrate-lib/` | 15 | Shared library updates (Postgres) |

#### Hottest Individual Files (excluding lock files)
| File | Touches | Context |
|---|---|---|
| `src/ol_dbt/models/reporting/_reporting__models.yml` | 21 | Schema changes for reporting models |
| `pyproject.toml` | 19 | Dependency + config changes |
| `packages/ol-orchestrate-lib/pyproject.toml` | 15 | Shared lib dependencies |
| `dg_projects/lakehouse/pyproject.toml` | 15 | Lakehouse dependencies |
| `dg_projects/lakehouse/Dockerfile` | 14 | Lakehouse container changes |
| `edxorg_to_mitxonline_enrollments.sql` | 10 | Migration model active development |
| `edxorg/assets/edxorg_archive.py` | 6 | edX archive extraction changes |
| `ol_orchestrate/lib/postgres/*.py` | 12 | Postgres connection pooling fixes |

**Takeaway:** The last 90 days show heavy investment in **Superset BI layer** (1,113 file touches in `ol_superset/`), **reporting model refinement**, and **Postgres connection pool hardening** in the shared library. The edxorg→mitxonline migration is also actively in progress.

---

## Key Config Files

| File | Purpose |
|---|---|
| `src/ol_dbt/dbt_project.yml` | dbt project config: model materialization, grants, schema assignments |
| `src/ol_dbt/profiles.yml` | dbt connection profiles: Trino (QA/prod), DuckDB (local dev) |
| `src/ol_dbt/packages.yml` | dbt packages: dbt_utils, dbt_expectations, trino_utils, codegen |
| `pyproject.toml` (root) | Workspace root: dependencies, Dagster project registry, linting config |
| `packages/ol-orchestrate-lib/pyproject.toml` | Shared library dependencies |
| `dg_projects/*/pyproject.toml` | Per-project dependencies |
| `docker-compose.yaml` | Local dev: Dagster webserver + daemon + Postgres |
| `dg_deployments/Dockerfile.dagster-k8s` | Production Dagster Docker image |
| `.env.example` | Required environment variables (Vault, AWS, dbt credentials) |
| `.pre-commit-config.yaml` | Pre-commit hooks (ruff, sqlfluff, sqlfmt) |
| `src/ol_superset/sync_config.yml` | Superset sync configuration |

---

## dbt Model Summary

| Layer | Count | Schema | Materialization | Purpose |
|---|---|---|---|---|
| **staging** | 233 | `_staging` | table | Raw data standardization (12 domains) |
| **intermediate** | 178 | `_intermediate` | table | Domain transforms + cross-platform combine |
| **external** | 110 | `_external` | table | IRx data sharing (MITx/MITxOnline/xPro) |
| **marts** | 27 | `_mart` | table | Final business entities |
| **reporting** | 21 | `_reporting` | table | Dashboard-ready aggregations |
| **dimensional** | 16 | `_dimensional` | table | Star schema (dims + facts) |
| **migration** | 3 | `_migration` | table | Platform migration support |
| **TOTAL** | **587** | | | |

**Schema YAML files:** 51 total (model docs + source definitions)
**Seeds:** 4 CSVs (platforms, certificate mappings, course roles, seed docs)
**Macros:** 22 custom macros (deduplication, ISO8601, course ID extraction, grants)
**dbt packages:** dbt_utils, trino_utils, dbt_expectations, dbt_meta_testing, dbtplyr, codegen

---

## Data Source Domains (12 staging domains)

| Domain | Staging Models | Source | Description |
|---|---|---|---|
| **mitxpro** | 62 | Airbyte | MIT xPro (professional education) app DB |
| **mitxonline** | 59 | Airbyte | MITx Online app DB + OpenEdX |
| **micromasters** | 26 | Airbyte | MicroMasters app DB |
| **edxorg** | 23 | Airbyte + API | edX.org BigQuery (IRx), course archives |
| **bootcamps** | 19 | Airbyte | Bootcamps app DB |
| **mitxresidential** | 13 | Airbyte | Residential MITx OpenEdX DB |
| **mitlearn** | 9 | Airbyte | MIT Learn (learning resources platform) |
| **zendesk** | 7 | Airbyte | Zendesk support tickets |
| **learn-ai** | 5 | Airbyte | Learn AI chatbot data |
| **ovs** | 5 | Airbyte | ODL Video Service |
| **ocw** | 3 | Airbyte | OCW Studio app DB |
| **salesforce** | 2 | Airbyte | Salesforce CRM |

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    SOURCE SYSTEMS                                │
│  MITx Online │ xPro │ edX.org │ MicroMasters │ Bootcamps │ ... │
│  Canvas │ Salesforce │ Zendesk │ OCW │ Learn AI │ OVS           │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                    ┌───────▼────────┐
                    │  Airbyte OSS   │  (~30 connections)
                    │  (self-hosted) │  6h / 12h / 24h cadence
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │   S3 (Parquet) │  Raw data in ol_warehouse_*_raw
                    └───────┬────────┘
                            │
                    ┌───────▼────────┐
                    │ Trino/Starburst│  Query engine (Starburst Galaxy)
                    └───────┬────────┘
                            │
      ┌─────────────────────▼──────────────────────────┐
      │              dbt (587 models)                    │
      │  staging(233) → intermediate(178) → marts(27)   │
      │                                  → reporting(21) │
      │                                  → dimensional(16)│
      │                                  → external(110) │
      └────────────┬──────────────────┬────────────────┘
                   │                  │
           ┌───────▼──────┐  ┌───────▼──────┐
           │   Superset   │  │   IRx/MIT    │
           │ (18 dashboards)│  │ (external   │
           │ Charts/Reports│  │  data share) │
           └──────────────┘  └──────────────┘

      Orchestrated by: Dagster (lakehouse project)
      ├── Airbyte sync schedules
      ├── dbt build automation
      ├── Superset dataset sync
      └── Automation condition sensors
```

---

## Risk Areas & Watch Points

1. **Single point of failure**: `dg_projects/lakehouse/definitions.py` (~300 lines) defines ALL core pipelines. A syntax error here = total outage.
2. **Shared library coupling**: `packages/ol-orchestrate-lib/` is imported by all 10 Dagster projects. Breaking changes propagate everywhere.
3. **Postgres connection pooling**: Active remediation work in last 90 days (`ol_orchestrate/lib/postgres/*.py`) — suggests recent production issues.
4. **Migration in progress**: `edxorg_to_mitxonline_enrollments.sql` (10 touches) — active platform migration from edX.org to MITx Online.
5. **Superset config explosion**: 634 chart file touches — very active BI layer with drift risk between Superset configs and dbt models.
6. **Airbyte credential management**: Vault-authenticated Airbyte password for Dagster — failure path is complex (see `definitions.py` fallbacks).
