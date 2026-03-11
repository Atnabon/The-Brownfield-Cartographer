"""Airflow DAG and dbt config parser.

Extracts pipeline topology from:
- Airflow DAG Python files (operator definitions, task dependencies)
- dbt schema.yml / dbt_project.yml files
- Generic YAML pipeline definitions (Prefect, Dagster)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class DAGTask:
    """A task/node in a DAG definition."""

    name: str
    operator: str = ""
    dependencies: list[str] = field(default_factory=list)
    source_file: str = ""
    line_number: int = 0
    parameters: dict = field(default_factory=dict)


@dataclass
class DAGDefinition:
    """A complete DAG/pipeline definition."""

    name: str
    source_file: str
    tasks: list[DAGTask] = field(default_factory=list)
    schedule: str | None = None
    description: str = ""
    dag_type: str = "unknown"  # airflow, dbt, prefect, etc.


@dataclass
class DbtModel:
    """A dbt model definition from schema.yml."""

    name: str
    description: str = ""
    columns: list[dict] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    source_file: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class DbtSource:
    """A dbt source definition."""

    name: str
    schema_name: str = ""
    database: str = ""
    tables: list[str] = field(default_factory=list)
    source_file: str = ""


@dataclass
class DAGConfigResult:
    """Complete result from DAG/config parsing."""

    dags: list[DAGDefinition] = field(default_factory=list)
    dbt_models: list[DbtModel] = field(default_factory=list)
    dbt_sources: list[DbtSource] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DAGConfigParser:
    """Parser for Airflow DAGs, dbt configs, and other pipeline definitions."""

    def analyze_directory(self, root: Path) -> DAGConfigResult:
        """Analyze all DAG/config files in a directory tree."""
        result = DAGConfigResult()

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(skip in str(path) for skip in ("__pycache__", ".git", "node_modules", ".venv")):
                continue

            try:
                if path.suffix == ".py":
                    self._try_parse_airflow_dag(path, result)
                elif path.suffix in (".yml", ".yaml"):
                    self._try_parse_yaml_config(path, result)
            except Exception as e:
                logger.debug(f"Error parsing {path}: {e}")
                result.errors.append(f"{path}: {e}")

        return result

    def _try_parse_airflow_dag(self, path: Path, result: DAGConfigResult):
        """Try to parse a Python file as an Airflow DAG."""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        # Check if this looks like an Airflow DAG file
        if "airflow" not in content.lower() and "DAG(" not in content:
            return

        dag_def = DAGDefinition(
            name=path.stem,
            source_file=str(path),
            dag_type="airflow",
        )

        # Extract DAG name
        dag_match = re.search(r'DAG\s*\(\s*["\']([^"\']+)["\']', content)
        if dag_match:
            dag_def.name = dag_match.group(1)

        # Extract schedule
        sched_match = re.search(r'schedule(?:_interval)?\s*=\s*["\']([^"\']+)["\']', content)
        if sched_match:
            dag_def.schedule = sched_match.group(1)

        # Extract task definitions (operators)
        task_pattern = re.compile(
            r'(\w+)\s*=\s*(\w+(?:Operator|Sensor|Task))\s*\(', re.MULTILINE,
        )
        tasks = {}
        for match in task_pattern.finditer(content):
            var_name = match.group(1)
            operator = match.group(2)

            # Try to get task_id
            task_id_match = re.search(
                rf'{var_name}\s*=\s*\w+\([^)]*task_id\s*=\s*["\']([^"\']+)["\']',
                content,
                re.DOTALL,
            )
            task_name = task_id_match.group(1) if task_id_match else var_name

            tasks[var_name] = DAGTask(
                name=task_name,
                operator=operator,
                source_file=str(path),
                line_number=content[:match.start()].count("\n") + 1,
            )

        # Extract dependencies (>> and << operators)
        dep_pattern = re.compile(r'(\w+)\s*>>\s*(\w+)')
        for match in dep_pattern.finditer(content):
            upstream = match.group(1)
            downstream = match.group(2)
            if downstream in tasks:
                tasks[downstream].dependencies.append(
                    tasks[upstream].name if upstream in tasks else upstream
                )

        # Also handle list dependencies: [task1, task2] >> task3
        list_dep_pattern = re.compile(r'\[([^\]]+)\]\s*>>\s*(\w+)')
        for match in list_dep_pattern.finditer(content):
            upstream_list = match.group(1)
            downstream = match.group(2)
            upstream_vars = [v.strip() for v in upstream_list.split(",")]
            if downstream in tasks:
                for uv in upstream_vars:
                    tasks[downstream].dependencies.append(
                        tasks[uv].name if uv in tasks else uv
                    )

        dag_def.tasks = list(tasks.values())

        if dag_def.tasks:
            result.dags.append(dag_def)

    def _try_parse_yaml_config(self, path: Path, result: DAGConfigResult):
        """Try to parse a YAML file as dbt/pipeline config."""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            data = yaml.safe_load(content)
        except Exception:
            return

        if not isinstance(data, dict):
            return

        # dbt schema.yml (has 'models' or 'sources' key)
        if "models" in data and isinstance(data["models"], list):
            self._parse_dbt_schema_models(data["models"], str(path), result)

        if "sources" in data and isinstance(data["sources"], list):
            self._parse_dbt_sources(data["sources"], str(path), result)

        # dbt_project.yml
        if "name" in data and "profile" in data:
            # This is likely a dbt_project.yml
            logger.info(f"Found dbt project config: {path}")

    def _parse_dbt_schema_models(
        self, models: list, source_file: str, result: DAGConfigResult,
    ):
        """Parse dbt schema.yml models section."""
        for model_data in models:
            if not isinstance(model_data, dict):
                continue
            name = model_data.get("name", "")
            if not name:
                continue

            columns = []
            if "columns" in model_data and isinstance(model_data["columns"], list):
                for col in model_data["columns"]:
                    if isinstance(col, dict):
                        columns.append({
                            "name": col.get("name", ""),
                            "description": col.get("description", ""),
                            "tests": col.get("tests", []),
                        })

            tests = []
            if "tests" in model_data and isinstance(model_data["tests"], list):
                tests = [str(t) for t in model_data["tests"]]

            model = DbtModel(
                name=name,
                description=model_data.get("description", ""),
                columns=columns,
                tests=tests,
                source_file=source_file,
            )
            result.dbt_models.append(model)

    def _parse_dbt_sources(
        self, sources: list, source_file: str, result: DAGConfigResult,
    ):
        """Parse dbt schema.yml sources section."""
        for source_data in sources:
            if not isinstance(source_data, dict):
                continue
            name = source_data.get("name", "")
            if not name:
                continue

            tables = []
            if "tables" in source_data and isinstance(source_data["tables"], list):
                for table in source_data["tables"]:
                    if isinstance(table, dict):
                        tables.append(table.get("name", ""))
                    elif isinstance(table, str):
                        tables.append(table)

            source = DbtSource(
                name=name,
                schema_name=source_data.get("schema", ""),
                database=source_data.get("database", ""),
                tables=tables,
                source_file=source_file,
            )
            result.dbt_sources.append(source)
