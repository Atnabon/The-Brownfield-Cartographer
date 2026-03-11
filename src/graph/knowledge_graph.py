"""Knowledge Graph - NetworkX wrapper with Pydantic serialization.

Central data store combining:
- Module import graph (from Surveyor)
- Data lineage graph (from Hydrologist)
- Serialization to/from JSON for persistence
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import networkx as nx
from networkx.readwrite import json_graph

from src.models import (
    AnalysisResult,
    DatasetNode,
    EdgeType,
    FunctionNode,
    GraphEdge,
    KnowledgeGraphData,
    ModuleNode,
    TransformationNode,
)

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """Central knowledge graph combining structure and lineage data."""

    def __init__(self):
        self.module_graph = nx.DiGraph()
        self.lineage_graph = nx.DiGraph()
        self.data = KnowledgeGraphData()
        self._analysis_result: AnalysisResult | None = None

    def add_module(self, module: ModuleNode):
        """Add a module node to the graph."""
        self.data.modules[module.path] = module
        self.module_graph.add_node(module.path, **{
            "language": module.language.value,
            "loc": module.lines_of_code,
            "complexity": module.complexity_score,
            "pagerank": module.pagerank_score,
        })

    def add_dataset(self, dataset: DatasetNode):
        """Add a dataset node to the graph."""
        self.data.datasets[dataset.name] = dataset
        self.lineage_graph.add_node(dataset.name, node_type="dataset")

    def add_function(self, function: FunctionNode):
        """Add a function node to the graph."""
        self.data.functions[function.qualified_name] = function

    def add_transformation(self, transformation: TransformationNode):
        """Add a transformation node to the graph."""
        self.data.transformations[transformation.name] = transformation

    def add_edge(self, edge: GraphEdge):
        """Add an edge to the appropriate graph."""
        self.data.edges.append(edge)

        if edge.edge_type == EdgeType.IMPORTS:
            self.module_graph.add_edge(
                edge.source, edge.target,
                edge_type=edge.edge_type.value,
                weight=edge.weight,
            )
        elif edge.edge_type in (EdgeType.PRODUCES, EdgeType.CONSUMES):
            self.lineage_graph.add_edge(
                edge.source, edge.target,
                edge_type=edge.edge_type.value,
                weight=edge.weight,
            )

    def merge_surveyor_results(self, results: dict):
        """Merge Surveyor agent results into the knowledge graph."""
        # Copy the module graph with its edges and PageRank first
        if "module_graph" in results:
            self.module_graph = results["module_graph"]

        # Add module metadata without overwriting graph edges
        for path, module in results.get("modules", {}).items():
            self.data.modules[module.path] = module

        for name, func in results.get("functions", {}).items():
            self.data.functions[func.qualified_name] = func

        for edge in results.get("edges", []):
            self.data.edges.append(edge)

    def merge_hydrologist_results(self, results: dict):
        """Merge Hydrologist agent results into the knowledge graph."""
        for name, dataset in results.get("datasets", {}).items():
            self.add_dataset(dataset)

        for name, transform in results.get("transformations", {}).items():
            self.add_transformation(transform)

        for edge in results.get("edges", []):
            self.add_edge(edge)

        # Copy the lineage graph directly
        if "lineage_graph" in results:
            self.lineage_graph = results["lineage_graph"]

    def serialize_module_graph(self) -> dict:
        """Serialize the module import graph to a JSON-compatible dict."""
        graph_data = json_graph.node_link_data(self.module_graph)

        # Add module metadata - use PageRank from ModuleNode objects
        modules_meta = {}
        for path, module in self.data.modules.items():
            mod_dict = module.model_dump(mode="json")
            modules_meta[path] = mod_dict

        # Compute PageRank on the stored graph for completeness
        top_modules = []
        if self.module_graph.number_of_nodes() > 0:
            try:
                pr = nx.pagerank(self.module_graph)
                for path, score in sorted(pr.items(), key=lambda x: -x[1])[:20]:
                    if path in modules_meta:
                        modules_meta[path]["pagerank_score"] = score
                    top_modules.append({"path": path, "score": score})
            except Exception:
                pass

        return {
            "graph": graph_data,
            "modules": modules_meta,
            "top_modules_by_pagerank": top_modules,
            "statistics": {
                "total_modules": len(self.data.modules),
                "total_edges": self.module_graph.number_of_edges(),
                "languages": self._count_languages(),
                "total_functions": len(self.data.functions),
                "total_classes": sum(len(m.classes) for m in self.data.modules.values()),
            },
        }

    def serialize_lineage_graph(self) -> dict:
        """Serialize the data lineage graph to a JSON-compatible dict."""
        graph_data = json_graph.node_link_data(self.lineage_graph)

        datasets_meta = {}
        for name, dataset in self.data.datasets.items():
            datasets_meta[name] = dataset.model_dump(mode="json")

        transforms_meta = {}
        for name, transform in self.data.transformations.items():
            transforms_meta[name] = transform.model_dump(mode="json")

        return {
            "graph": graph_data,
            "datasets": datasets_meta,
            "transformations": transforms_meta,
            "statistics": {
                "total_datasets": len(self.data.datasets),
                "total_transformations": len(self.data.transformations),
                "total_edges": self.lineage_graph.number_of_edges(),
            },
        }

    def save_to_directory(self, output_dir: Path):
        """Save all graph data to a .cartography/ directory."""
        output_dir.mkdir(parents=True, exist_ok=True)

        # Module graph
        module_graph_path = output_dir / "module_graph.json"
        with open(module_graph_path, "w") as f:
            json.dump(self.serialize_module_graph(), f, indent=2, default=str)
        logger.info(f"Saved module graph to {module_graph_path}")

        # Lineage graph
        lineage_graph_path = output_dir / "lineage_graph.json"
        with open(lineage_graph_path, "w") as f:
            json.dump(self.serialize_lineage_graph(), f, indent=2, default=str)
        logger.info(f"Saved lineage graph to {lineage_graph_path}")

    @classmethod
    def load_from_directory(cls, input_dir: Path) -> "KnowledgeGraph":
        """Load a previously saved knowledge graph from a .cartography/ directory."""
        kg = cls()

        module_graph_path = input_dir / "module_graph.json"
        if module_graph_path.exists():
            with open(module_graph_path) as f:
                data = json.load(f)
            kg.module_graph = json_graph.node_link_graph(data["graph"])
            for path, mod_data in data.get("modules", {}).items():
                kg.data.modules[path] = ModuleNode(**mod_data)
            logger.info(f"Loaded module graph from {module_graph_path}")

        lineage_graph_path = input_dir / "lineage_graph.json"
        if lineage_graph_path.exists():
            with open(lineage_graph_path) as f:
                data = json.load(f)
            kg.lineage_graph = json_graph.node_link_graph(data["graph"])
            for name, ds_data in data.get("datasets", {}).items():
                kg.data.datasets[name] = DatasetNode(**ds_data)
            for name, tr_data in data.get("transformations", {}).items():
                kg.data.transformations[name] = TransformationNode(**tr_data)
            logger.info(f"Loaded lineage graph from {lineage_graph_path}")

        return kg

    def _count_languages(self) -> dict[str, int]:
        """Count files per language."""
        counts: dict[str, int] = {}
        for module in self.data.modules.values():
            lang = module.language.value
            counts[lang] = counts.get(lang, 0) + 1
        return counts

    def get_summary(self) -> dict:
        """Return a summary of the knowledge graph contents."""
        return {
            "modules": len(self.data.modules),
            "functions": len(self.data.functions),
            "datasets": len(self.data.datasets),
            "transformations": len(self.data.transformations),
            "import_edges": self.module_graph.number_of_edges(),
            "lineage_edges": self.lineage_graph.number_of_edges(),
            "languages": self._count_languages(),
        }
