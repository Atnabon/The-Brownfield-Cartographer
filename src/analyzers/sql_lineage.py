"""SQL lineage extraction using sqlglot.

Parses .sql files, dbt model files, and inline SQL to build
a table dependency graph showing which tables are read from and written to.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SQLTableReference:
    """A reference to a table in SQL."""

    name: str
    schema_name: str | None = None
    database: str | None = None
    alias: str | None = None

    @property
    def full_name(self) -> str:
        parts = []
        if self.database:
            parts.append(self.database)
        if self.schema_name:
            parts.append(self.schema_name)
        parts.append(self.name)
        return ".".join(parts)


@dataclass
class SQLLineageResult:
    """Lineage result for a single SQL file/statement."""

    source_file: str
    source_tables: list[SQLTableReference] = field(default_factory=list)
    target_tables: list[SQLTableReference] = field(default_factory=list)
    ctes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_sql: str = ""


class SQLLineageAnalyzer:
    """Extracts table-level lineage from SQL files using sqlglot."""

    SUPPORTED_DIALECTS = [
        "postgres", "bigquery", "snowflake", "duckdb",
        "mysql", "sqlite", "trino", "spark",
    ]

    def __init__(self, default_dialect: str = "postgres"):
        self.default_dialect = default_dialect
        self._sqlglot_available = False
        try:
            import sqlglot
            self._sqlglot_available = True
            logger.info("sqlglot available for SQL parsing")
        except ImportError:
            logger.warning("sqlglot not available, using regex fallback for SQL parsing")

    def analyze_file(self, file_path: Path, dialect: str | None = None) -> list[SQLLineageResult]:
        """Analyze a SQL file and extract lineage for each statement."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return [SQLLineageResult(
                source_file=str(file_path),
                errors=[f"Failed to read file: {e}"],
            )]

        return self.analyze_sql(content, str(file_path), dialect)

    def analyze_sql(
        self, sql: str, source_file: str = "<inline>", dialect: str | None = None,
    ) -> list[SQLLineageResult]:
        """Analyze SQL string and return lineage results."""
        dialect = dialect or self.default_dialect

        if self._sqlglot_available:
            return self._analyze_with_sqlglot(sql, source_file, dialect)
        return self._analyze_with_regex(sql, source_file)

    def _analyze_with_sqlglot(
        self, sql: str, source_file: str, dialect: str,
    ) -> list[SQLLineageResult]:
        """Use sqlglot to parse SQL and extract table dependencies."""
        import sqlglot
        from sqlglot import exp

        results = []

        # Handle dbt ref() and source() macros before parsing
        processed_sql = self._preprocess_dbt_sql(sql)

        try:
            statements = sqlglot.parse(processed_sql, dialect=dialect, error_level=sqlglot.ErrorLevel.WARN)
        except Exception as e:
            # Try without dialect
            try:
                statements = sqlglot.parse(processed_sql, error_level=sqlglot.ErrorLevel.WARN)
            except Exception as e2:
                return [SQLLineageResult(
                    source_file=source_file,
                    errors=[f"Failed to parse SQL: {e2}"],
                    raw_sql=sql[:500],
                )]

        for stmt in statements:
            if stmt is None:
                continue

            result = SQLLineageResult(source_file=source_file, raw_sql=sql[:500])

            try:
                # Extract CTEs
                for cte in stmt.find_all(exp.CTE):
                    alias_node = cte.args.get("alias")
                    if alias_node:
                        result.ctes.append(alias_node.name)

                # Extract source tables (FROM, JOIN)
                source_tables = set()
                for table in stmt.find_all(exp.Table):
                    table_name = table.name
                    if not table_name:
                        continue
                    # Skip CTE references
                    if table_name in result.ctes:
                        continue

                    schema = None
                    db = None
                    if table.args.get("db"):
                        schema = table.args["db"].name
                    if table.args.get("catalog"):
                        db = table.args["catalog"].name

                    ref = SQLTableReference(
                        name=table_name,
                        schema_name=schema,
                        database=db,
                    )
                    source_tables.add(ref.full_name)
                    result.source_tables.append(ref)

                # Extract target tables (CREATE, INSERT, MERGE)
                if isinstance(stmt, (exp.Create,)):
                    schema_expr = stmt.find(exp.Schema)
                    if schema_expr:
                        table = schema_expr.find(exp.Table)
                        if table:
                            ref = SQLTableReference(name=table.name)
                            result.target_tables.append(ref)
                    else:
                        table = stmt.find(exp.Table)
                        if table and table.name:
                            # The first table in CREATE is the target
                            ref = SQLTableReference(name=table.name)
                            result.target_tables.append(ref)
                            # Remove from source tables if present
                            result.source_tables = [
                                t for t in result.source_tables
                                if t.full_name != ref.full_name
                            ]

                if isinstance(stmt, (exp.Insert,)):
                    table = stmt.find(exp.Table)
                    if table and table.name:
                        ref = SQLTableReference(name=table.name)
                        result.target_tables.append(ref)
                        result.source_tables = [
                            t for t in result.source_tables
                            if t.full_name != ref.full_name
                        ]

            except Exception as e:
                result.errors.append(f"Error extracting lineage: {e}")

            results.append(result)

        return results if results else [SQLLineageResult(source_file=source_file)]

    def _preprocess_dbt_sql(self, sql: str) -> str:
        """Replace dbt Jinja macros with parseable SQL references."""
        # Replace {{ ref('model_name') }} with __dbt_ref__model_name
        sql = re.sub(
            r"\{\{\s*ref\s*\(\s*['\"](\w+)['\"]\s*\)\s*\}\}",
            r"__dbt_ref__\1",
            sql,
        )
        # Replace {{ source('schema', 'table') }} with __dbt_source__schema__table
        sql = re.sub(
            r"\{\{\s*source\s*\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)\s*\}\}",
            r"__dbt_source__\1__\2",
            sql,
        )
        # Replace {{ config(...) }} with empty string
        sql = re.sub(r"\{\{\s*config\s*\([^)]*\)\s*\}\}", "", sql)
        # Replace other Jinja blocks with empty strings
        sql = re.sub(r"\{%[^%]*%\}", "", sql)
        sql = re.sub(r"\{\{[^}]*\}\}", "1", sql)  # Replace remaining {{ }} with placeholder
        return sql

    def _analyze_with_regex(self, sql: str, source_file: str) -> list[SQLLineageResult]:
        """Regex fallback for SQL lineage extraction."""
        result = SQLLineageResult(source_file=source_file, raw_sql=sql[:500])

        # Source tables: FROM / JOIN
        for match in re.finditer(
            r'(?:FROM|JOIN)\s+([`"\']?[\w.]+[`"\']?)', sql, re.IGNORECASE,
        ):
            table_name = match.group(1).strip('`"\'')
            if table_name.upper() not in ("SELECT", "WHERE", "SET", "VALUES", "TABLE"):
                result.source_tables.append(SQLTableReference(name=table_name))

        # Target tables: CREATE TABLE/VIEW, INSERT INTO
        for match in re.finditer(
            r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([`"\']?[\w.]+[`"\']?)',
            sql,
            re.IGNORECASE,
        ):
            table_name = match.group(1).strip('`"\'')
            result.target_tables.append(SQLTableReference(name=table_name))

        for match in re.finditer(
            r'INSERT\s+(?:INTO|OVERWRITE)\s+([`"\']?[\w.]+[`"\']?)',
            sql,
            re.IGNORECASE,
        ):
            table_name = match.group(1).strip('`"\'')
            result.target_tables.append(SQLTableReference(name=table_name))

        # dbt ref()
        for match in re.finditer(r"ref\(\s*['\"](\w+)['\"]\s*\)", sql):
            result.source_tables.append(SQLTableReference(name=f"__dbt_ref__{match.group(1)}"))

        # dbt source()
        for match in re.finditer(
            r"source\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"]\s*\)", sql,
        ):
            result.source_tables.append(
                SQLTableReference(name=f"__dbt_source__{match.group(1)}__{match.group(2)}")
            )

        return [result]

    def analyze_directory(self, root: Path, dialect: str | None = None) -> list[SQLLineageResult]:
        """Analyze all SQL files in a directory."""
        results = []
        for path in sorted(root.rglob("*.sql")):
            if any(skip in str(path) for skip in ("__pycache__", ".git", "node_modules")):
                continue
            try:
                file_results = self.analyze_file(path, dialect)
                results.extend(file_results)
            except Exception as e:
                logger.error(f"Failed to analyze {path}: {e}")
                results.append(SQLLineageResult(
                    source_file=str(path),
                    errors=[str(e)],
                ))
        return results
