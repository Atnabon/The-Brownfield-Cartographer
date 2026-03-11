"""The Hydrologist Agent - Data Flow & Lineage Analyst.

Constructs the DataLineageGraph by analyzing data sources, transformations,
and sinks across all languages in the repository:
- Python: pandas, SQLAlchemy, PySpark read/write operations
- SQL/dbt: sqlglot-parsed table dependencies
- YAML/Config: Airflow DAG definitions, dbt schema.yml
- Notebooks: Jupyter .ipynb data source references

Provides blast_radius, find_sources, and find_sinks queries.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

import networkx as nx

from src.analyzers.dag_config_parser import DAGConfigParser
from src.analyzers.sql_lineage import SQLLineageAnalyzer, SQLLineageResult
from src.analyzers.tree_sitter_analyzer import ModuleAnalysis
from src.models import (
    DatasetNode,
    EdgeType,
    GraphEdge,
    StorageType,
    TransformationNode,
    TransformationType,
)

logger = logging.getLogger(__name__)


class HydrologistAgent:
    """Builds the DataLineageGraph showing data flow through the system."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.lineage_graph = nx.DiGraph()
        self.datasets: dict[str, DatasetNode] = {}
        self.transformations: dict[str, TransformationNode] = {}
        self.edges: list[GraphEdge] = []
        self.sql_analyzer = SQLLineageAnalyzer()
        self.dag_parser = DAGConfigParser()

    def run(self, file_analyses: list[ModuleAnalysis] | None = None) -> dict:
        """Execute full hydrologist analysis pipeline."""
        logger.info(f"Hydrologist: Analyzing data flows at {self.repo_path}")

        # Step 1: Analyze SQL files for table lineage
        sql_results = self.sql_analyzer.analyze_directory(self.repo_path)
        self._process_sql_lineage(sql_results)

        # Step 2: Analyze Python files for data operations
        if file_analyses:
            self._analyze_python_data_ops(file_analyses)

        # Step 3: Analyze DAG/config files
        dag_result = self.dag_parser.analyze_directory(self.repo_path)
        self._process_dag_config(dag_result)

        # Step 4: Analyze Jupyter notebooks
        self._analyze_notebooks()

        # Step 5: Identify sources and sinks
        sources = self.find_sources()
        sinks = self.find_sinks()

        logger.info(
            f"Hydrologist: Found {len(self.datasets)} datasets, "
            f"{len(self.transformations)} transformations, "
            f"{len(sources)} sources, {len(sinks)} sinks"
        )

        return {
            "datasets": self.datasets,
            "transformations": self.transformations,
            "edges": self.edges,
            "lineage_graph": self.lineage_graph,
            "sources": sources,
            "sinks": sinks,
        }

    def _process_sql_lineage(self, results: list[SQLLineageResult]):
        """Process SQL lineage results into the graph."""
        for result in results:
            if result.errors and not result.source_tables and not result.target_tables:
                continue

            rel_path = self._relative_path(result.source_file)

            # Add source tables as dataset nodes
            for table_ref in result.source_tables:
                name = table_ref.full_name
                if name not in self.datasets:
                    self.datasets[name] = DatasetNode(
                        name=name,
                        storage_type=StorageType.TABLE,
                    )
                self.lineage_graph.add_node(name, node_type="dataset")

            # Add target tables
            for table_ref in result.target_tables:
                name = table_ref.full_name
                if name not in self.datasets:
                    self.datasets[name] = DatasetNode(
                        name=name,
                        storage_type=StorageType.TABLE,
                    )
                self.lineage_graph.add_node(name, node_type="dataset")

            # Create transformation node linking sources to targets
            if result.source_tables or result.target_tables:
                transform_name = f"sql:{rel_path}"
                source_names = [t.full_name for t in result.source_tables]
                target_names = [t.full_name for t in result.target_tables]

                # If no explicit targets, the SQL file itself is probably a dbt model
                if not target_names and rel_path.endswith(".sql"):
                    # Use the filename (without extension) as the target
                    model_name = Path(rel_path).stem
                    # Check if it looks like a dbt ref name
                    if "__dbt_ref__" not in model_name:
                        target_names = [model_name]
                        if model_name not in self.datasets:
                            self.datasets[model_name] = DatasetNode(
                                name=model_name,
                                storage_type=StorageType.TABLE,
                            )
                            self.lineage_graph.add_node(model_name, node_type="dataset")

                self.transformations[transform_name] = TransformationNode(
                    name=transform_name,
                    source_datasets=source_names,
                    target_datasets=target_names,
                    transformation_type=TransformationType.SQL_QUERY,
                    source_file=rel_path,
                    line_range=result.line_range,
                    sql_query_if_applicable=result.raw_sql[:500] if result.raw_sql else None,
                )

                self.lineage_graph.add_node(transform_name, node_type="transformation")

                # Add edges with rich metadata
                for source in source_names:
                    self.lineage_graph.add_edge(
                        source, transform_name,
                        transformation_type="sql_query",
                        source_file=rel_path,
                        line_range=result.line_range,
                    )
                    self.edges.append(GraphEdge(
                        source=source,
                        target=transform_name,
                        edge_type=EdgeType.CONSUMES,
                        metadata={
                            "transformation_type": "sql_query",
                            "source_file": rel_path,
                            "line_range": list(result.line_range),
                            "operation_type": result.operation_type,
                        },
                    ))

                for target in target_names:
                    self.lineage_graph.add_edge(
                        transform_name, target,
                        transformation_type="sql_query",
                        source_file=rel_path,
                        line_range=result.line_range,
                    )
                    self.edges.append(GraphEdge(
                        source=transform_name,
                        target=target,
                        edge_type=EdgeType.PRODUCES,
                        metadata={
                            "transformation_type": "sql_query",
                            "source_file": rel_path,
                            "line_range": list(result.line_range),
                            "operation_type": result.operation_type,
                        },
                    ))

    def _analyze_python_data_ops(self, file_analyses: list[ModuleAnalysis]):
        """Find data read/write operations in Python files."""
        # Patterns for data operations
        read_patterns = [
            (r'pd\.read_csv\s*\(\s*["\']([^"\']+)["\']', "pandas_read_csv", StorageType.FILE),
            (r'pd\.read_sql\s*\(\s*["\']([^"\']+)["\']', "pandas_read_sql", StorageType.TABLE),
            (r'pd\.read_parquet\s*\(\s*["\']([^"\']+)["\']', "pandas_read_parquet", StorageType.FILE),
            (r'pd\.read_excel\s*\(\s*["\']([^"\']+)["\']', "pandas_read_excel", StorageType.FILE),
            (r'spark\.read\.\w+\(\s*["\']([^"\']+)["\']', "spark_read", StorageType.FILE),
            (r'\.read_table\s*\(\s*["\']([^"\']+)["\']', "read_table", StorageType.TABLE),
            (r'open\s*\(\s*["\']([^"\']+)["\']', "file_open", StorageType.FILE),
        ]

        write_patterns = [
            (r'\.to_csv\s*\(\s*["\']([^"\']+)["\']', "pandas_to_csv", StorageType.FILE),
            (r'\.to_parquet\s*\(\s*["\']([^"\']+)["\']', "pandas_to_parquet", StorageType.FILE),
            (r'\.to_sql\s*\(\s*["\']([^"\']+)["\']', "pandas_to_sql", StorageType.TABLE),
            (r'\.write\.\w+\(\s*["\']([^"\']+)["\']', "spark_write", StorageType.FILE),
            (r'\.to_excel\s*\(\s*["\']([^"\']+)["\']', "pandas_to_excel", StorageType.FILE),
        ]

        for analysis in file_analyses:
            if analysis.language.value != "python":
                continue

            try:
                content = Path(analysis.path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            rel_path = self._relative_path(analysis.path)
            sources_found = []
            targets_found = []

            for pattern, op_type, storage_type in read_patterns:
                for match in re.finditer(pattern, content):
                    dataset_name = match.group(1)
                    if dataset_name not in self.datasets:
                        self.datasets[dataset_name] = DatasetNode(
                            name=dataset_name,
                            storage_type=storage_type,
                        )
                        self.lineage_graph.add_node(dataset_name, node_type="dataset")
                    sources_found.append(dataset_name)

            for pattern, op_type, storage_type in write_patterns:
                for match in re.finditer(pattern, content):
                    dataset_name = match.group(1)
                    if dataset_name not in self.datasets:
                        self.datasets[dataset_name] = DatasetNode(
                            name=dataset_name,
                            storage_type=storage_type,
                        )
                        self.lineage_graph.add_node(dataset_name, node_type="dataset")
                    targets_found.append(dataset_name)

            if sources_found or targets_found:
                transform_name = f"python:{rel_path}"
                self.transformations[transform_name] = TransformationNode(
                    name=transform_name,
                    source_datasets=sources_found,
                    target_datasets=targets_found,
                    transformation_type=TransformationType.PYTHON_TRANSFORM,
                    source_file=rel_path,
                )
                self.lineage_graph.add_node(transform_name, node_type="transformation")

                for src in sources_found:
                    self.lineage_graph.add_edge(
                        src, transform_name,
                        transformation_type="python_transform",
                        source_file=rel_path,
                    )
                    self.edges.append(GraphEdge(
                        source=src, target=transform_name, edge_type=EdgeType.CONSUMES,
                        metadata={
                            "transformation_type": "python_transform",
                            "source_file": rel_path,
                        },
                    ))
                for tgt in targets_found:
                    self.lineage_graph.add_edge(
                        transform_name, tgt,
                        transformation_type="python_transform",
                        source_file=rel_path,
                    )
                    self.edges.append(GraphEdge(
                        source=transform_name, target=tgt, edge_type=EdgeType.PRODUCES,
                        metadata={
                            "transformation_type": "python_transform",
                            "source_file": rel_path,
                        },
                    ))

    def _process_dag_config(self, dag_result):
        """Process Airflow DAG and dbt config results."""
        # Process Airflow DAGs
        for dag in dag_result.dags:
            for task in dag.tasks:
                name = f"airflow:{dag.name}:{task.name}"
                self.transformations[name] = TransformationNode(
                    name=name,
                    transformation_type=TransformationType.AIRFLOW_TASK,
                    source_file=dag.source_file,
                )
                self.lineage_graph.add_node(name, node_type="transformation")

                # Add task dependency edges
                for dep in task.dependencies:
                    dep_name = f"airflow:{dag.name}:{dep}"
                    self.lineage_graph.add_node(dep_name, node_type="transformation")
                    self.lineage_graph.add_edge(dep_name, name)

        # Process dbt models
        for model in dag_result.dbt_models:
            model_dataset = model.name
            if model_dataset not in self.datasets:
                self.datasets[model_dataset] = DatasetNode(
                    name=model_dataset,
                    storage_type=StorageType.TABLE,
                )
                self.lineage_graph.add_node(model_dataset, node_type="dataset")

        # Process dbt sources
        for source in dag_result.dbt_sources:
            for table in source.tables:
                full_name = f"{source.name}.{table}" if source.name else table
                if full_name not in self.datasets:
                    self.datasets[full_name] = DatasetNode(
                        name=full_name,
                        storage_type=StorageType.TABLE,
                        is_source_of_truth=True,
                    )
                    self.lineage_graph.add_node(full_name, node_type="dataset")

    def _analyze_notebooks(self):
        """Analyze Jupyter notebooks for data references."""
        for nb_path in sorted(self.repo_path.rglob("*.ipynb")):
            if any(skip in str(nb_path) for skip in (".git", "node_modules", ".ipynb_checkpoints")):
                continue

            try:
                content = nb_path.read_text(encoding="utf-8", errors="replace")
                nb_data = json.loads(content)
            except Exception:
                continue

            cells = nb_data.get("cells", [])
            rel_path = self._relative_path(str(nb_path))

            for cell in cells:
                if cell.get("cell_type") != "code":
                    continue
                source = "".join(cell.get("source", []))

                # Look for data read patterns
                for match in re.finditer(r'read_csv\s*\(\s*["\']([^"\']+)["\']', source):
                    ds_name = match.group(1)
                    if ds_name not in self.datasets:
                        self.datasets[ds_name] = DatasetNode(
                            name=ds_name, storage_type=StorageType.FILE,
                        )
                        self.lineage_graph.add_node(ds_name, node_type="dataset")

    def find_sources(self) -> list[str]:
        """Find data sources (nodes with in-degree=0 in lineage graph)."""
        sources = []
        for node in self.lineage_graph.nodes():
            if (
                self.lineage_graph.in_degree(node) == 0
                and self.lineage_graph.out_degree(node) > 0
            ):
                sources.append(node)
        return sorted(sources)

    def find_sinks(self) -> list[str]:
        """Find data sinks (nodes with out-degree=0 in lineage graph)."""
        sinks = []
        for node in self.lineage_graph.nodes():
            if (
                self.lineage_graph.out_degree(node) == 0
                and self.lineage_graph.in_degree(node) > 0
            ):
                sinks.append(node)
        return sorted(sinks)

    def blast_radius(self, node: str) -> dict:
        """Find all downstream dependents of a node using BFS.

        Returns dict with:
        - affected_nodes: list of all downstream nodes
        - depth: maximum depth of impact
        - paths: dict mapping each affected node to its shortest path from root
        - subgraph_edges: the affected subgraph edges
        """
        if node not in self.lineage_graph:
            # Try partial match
            matches = [n for n in self.lineage_graph.nodes() if node in n]
            if len(matches) == 1:
                node = matches[0]
            elif len(matches) > 1:
                return {
                    "error": f"Ambiguous node '{node}'. Matches: {matches[:10]}",
                    "affected_nodes": [],
                    "depth": 0,
                    "paths": {},
                }
            else:
                return {
                    "error": f"Node '{node}' not found in lineage graph",
                    "affected_nodes": [],
                    "depth": 0,
                    "paths": {},
                }

        # BFS to find all downstream nodes with paths
        affected = []
        visited = set()
        parent_map: dict[str, str | None] = {node: None}
        queue = [(node, 0)]
        max_depth = 0
        subgraph_edges = []

        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current != node:
                affected.append(current)
                max_depth = max(max_depth, depth)

            for successor in self.lineage_graph.successors(current):
                if successor not in visited:
                    queue.append((successor, depth + 1))
                    subgraph_edges.append((current, successor))
                    if successor not in parent_map:
                        parent_map[successor] = current

        # Reconstruct paths from root to each affected node
        paths: dict[str, list[str]] = {}
        for affected_node in affected:
            path = []
            cur = affected_node
            while cur is not None:
                path.append(cur)
                cur = parent_map.get(cur)
            paths[affected_node] = list(reversed(path))

        return {
            "affected_nodes": affected,
            "depth": max_depth,
            "paths": paths,
            "subgraph_edges": subgraph_edges,
            "root": node,
        }

    def trace_upstream(self, node: str) -> list[str]:
        """Trace all upstream dependencies of a dataset node."""
        if node not in self.lineage_graph:
            matches = [n for n in self.lineage_graph.nodes() if node in n]
            if len(matches) == 1:
                node = matches[0]
            else:
                return []

        upstream = []
        visited = set()
        queue = [node]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current != node:
                upstream.append(current)

            for pred in self.lineage_graph.predecessors(current):
                if pred not in visited:
                    queue.append(pred)

        return upstream

    def _relative_path(self, path: str) -> str:
        """Convert absolute path to relative path from repo root."""
        try:
            return str(Path(path).relative_to(self.repo_path))
        except ValueError:
            return path
