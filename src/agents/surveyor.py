"""The Surveyor Agent - Static Structure Analyst.

Performs deep static analysis of the codebase:
- Module import graph (cross-language)
- Public API surface extraction
- Complexity signals (cyclomatic complexity, LOC, comment ratio)
- Git change velocity (files changing most frequently)
- Dead code candidate identification
- PageRank for identifying architectural hubs
"""

from __future__ import annotations

import logging
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import networkx as nx

from src.analyzers.tree_sitter_analyzer import (
    LanguageRouter,
    ModuleAnalysis,
    TreeSitterAnalyzer,
)
from src.models import (
    CircularDependency,
    DomainCluster,
    EdgeType,
    GitVelocityEntry,
    GraphEdge,
    Language,
    ModuleNode,
    FunctionNode,
)

logger = logging.getLogger(__name__)


class SurveyorAgent:
    """Builds the structural skeleton of a codebase."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.analyzer = TreeSitterAnalyzer()
        self.module_graph = nx.DiGraph()
        self.modules: dict[str, ModuleNode] = {}
        self.functions: dict[str, FunctionNode] = {}
        self.edges: list[GraphEdge] = []
        self.file_analyses: list[ModuleAnalysis] = []

    def run(self) -> dict:
        """Execute full surveyor analysis pipeline."""
        logger.info(f"Surveyor: Analyzing codebase at {self.repo_path}")

        # Step 1: Analyze all files
        self.file_analyses = self.analyzer.analyze_directory(self.repo_path)
        logger.info(f"Surveyor: Analyzed {len(self.file_analyses)} files")

        # Step 2: Build module nodes
        self._build_module_nodes()

        # Step 3: Build import graph
        self._build_import_graph()

        # Step 4: Calculate PageRank
        self._calculate_pagerank()

        # Step 5: Extract git velocity
        git_velocity = self._extract_git_velocity()

        # Step 6: Detect circular dependencies
        circular_deps = self._detect_circular_dependencies()

        # Step 7: Identify dead code candidates
        dead_code = self._identify_dead_code()

        # Step 8: Identify 80/20 high-velocity files
        high_velocity_80_20 = self._identify_80_20_velocity(git_velocity)

        # Step 9: Assign domain clusters
        self._assign_domain_clusters()

        return {
            "modules": self.modules,
            "functions": self.functions,
            "edges": self.edges,
            "git_velocity": git_velocity,
            "circular_dependencies": circular_deps,
            "dead_code_candidates": dead_code,
            "module_graph": self.module_graph,
            "high_velocity_80_20": high_velocity_80_20,
        }

    def _build_module_nodes(self):
        """Convert file analyses into ModuleNode objects."""
        for analysis in self.file_analyses:
            rel_path = self._relative_path(analysis.path)

            comment_ratio = 0.0
            if analysis.lines_of_code > 0:
                comment_ratio = analysis.comment_lines / analysis.lines_of_code

            module = ModuleNode(
                path=rel_path,
                language=analysis.language,
                lines_of_code=analysis.lines_of_code,
                comment_ratio=comment_ratio,
                complexity_score=analysis.complexity_score,
                imports=[imp.module for imp in analysis.imports],
                public_functions=[f.name for f in analysis.functions if f.is_public],
                classes=[c.name for c in analysis.classes],
            )
            self.modules[rel_path] = module

            # Extract function nodes
            for func in analysis.functions:
                qualified_name = f"{rel_path}::{func.name}"
                self.functions[qualified_name] = FunctionNode(
                    qualified_name=qualified_name,
                    parent_module=rel_path,
                    signature=func.signature,
                    is_public_api=func.is_public,
                )

    def _build_import_graph(self):
        """Build the module import graph as a NetworkX DiGraph."""
        # Map module names to file paths for resolution
        module_path_map = self._build_module_path_map()

        for analysis in self.file_analyses:
            source_path = self._relative_path(analysis.path)
            self.module_graph.add_node(source_path)

            for imp in analysis.imports:
                # Try to resolve the import to a file in the repo
                resolved = self._resolve_import(imp.module, source_path, module_path_map)
                if resolved and resolved != source_path:
                    self.module_graph.add_edge(source_path, resolved)
                    self.edges.append(GraphEdge(
                        source=source_path,
                        target=resolved,
                        edge_type=EdgeType.IMPORTS,
                        weight=1.0,
                    ))

    def _build_module_path_map(self) -> dict[str, str]:
        """Build a map from Python module names to file paths.

        Handles various package layouts including src/ directories and
        nested package structures.
        """
        mapping = {}
        for analysis in self.file_analyses:
            rel_path = self._relative_path(analysis.path)
            if analysis.language == Language.PYTHON:
                # Convert file path to module name
                module_name = rel_path.replace("/", ".").replace("\\", ".")
                if module_name.endswith(".py"):
                    module_name = module_name[:-3]
                if module_name.endswith(".__init__"):
                    package_name = module_name[:-9]
                    mapping[package_name] = rel_path
                mapping[module_name] = rel_path

                # Also map from every possible sub-path suffix
                # e.g., packages/lib/src/foo/bar.py -> foo.bar, bar
                parts = module_name.split(".")
                for i in range(1, len(parts)):
                    suffix = ".".join(parts[i:])
                    # Only add if not already mapped (first match wins)
                    if suffix not in mapping:
                        mapping[suffix] = rel_path
                    # Handle __init__ package suffix
                    if suffix.endswith(".__init__"):
                        pkg = suffix[:-9]
                        if pkg not in mapping:
                            mapping[pkg] = rel_path
        return mapping

    def _resolve_import(
        self, import_name: str, source_path: str, module_map: dict[str, str],
    ) -> str | None:
        """Try to resolve an import to a file path in the repo."""
        # Direct match
        if import_name in module_map:
            return module_map[import_name]

        # Try progressively shorter prefixes (e.g., 'src.models.types' -> 'src.models')
        parts = import_name.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in module_map:
                return module_map[prefix]

        return None

    def _calculate_pagerank(self):
        """Calculate PageRank scores for all modules."""
        if len(self.module_graph) == 0:
            return

        try:
            pagerank = nx.pagerank(self.module_graph)
            for path, score in pagerank.items():
                if path in self.modules:
                    self.modules[path].pagerank_score = score
        except Exception as e:
            logger.warning(f"PageRank calculation failed: {e}")

    def _extract_git_velocity(self, days: int = 30) -> list[GitVelocityEntry]:
        """Parse git log to compute change frequency per file."""
        velocity = []

        try:
            since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            result = subprocess.run(
                ["git", "log", f"--since={since_date}", "--name-only", "--pretty=format:%H|%aI|%an"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"Git log failed: {result.stderr}")
                return velocity

            # Parse git log output
            file_stats: dict[str, dict] = defaultdict(
                lambda: {"count": 0, "last_date": None, "authors": set()}
            )

            current_info = None
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line and line.count("|") >= 2:
                    parts = line.split("|", 2)
                    current_info = {
                        "date": parts[1] if len(parts) > 1 else None,
                        "author": parts[2] if len(parts) > 2 else None,
                    }
                elif current_info and line:
                    stats = file_stats[line]
                    stats["count"] += 1
                    if current_info.get("author"):
                        stats["authors"].add(current_info["author"])
                    if current_info.get("date") and (
                        stats["last_date"] is None or current_info["date"] > stats["last_date"]
                    ):
                        stats["last_date"] = current_info["date"]

            for file_path, stats in file_stats.items():
                entry = GitVelocityEntry(
                    path=file_path,
                    commit_count_30d=stats["count"],
                    authors=list(stats["authors"]),
                )
                if stats["last_date"]:
                    try:
                        entry.last_commit_date = datetime.fromisoformat(
                            stats["last_date"].replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        pass
                velocity.append(entry)

                # Update module velocity
                if file_path in self.modules:
                    self.modules[file_path].change_velocity_30d = stats["count"]

            # Sort by commit count descending
            velocity.sort(key=lambda x: x.commit_count_30d, reverse=True)

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"Git velocity extraction failed: {e}")

        return velocity

    def _detect_circular_dependencies(self) -> list[CircularDependency]:
        """Find all circular dependency cycles in the import graph."""
        cycles = []
        try:
            for cycle in nx.simple_cycles(self.module_graph):
                if len(cycle) > 1:
                    cycles.append(CircularDependency(
                        cycle=cycle,
                        severity="warning" if len(cycle) <= 3 else "error",
                    ))
        except Exception as e:
            logger.warning(f"Circular dependency detection failed: {e}")
        return cycles

    def _identify_dead_code(self) -> list[str]:
        """Identify modules that are never imported by any other module."""
        dead_code = []
        imported_modules = set()

        for _, target in self.module_graph.edges():
            imported_modules.add(target)

        for path, module in self.modules.items():
            # Skip __init__.py, test files, and config files
            if (
                path.endswith("__init__.py")
                or "/test" in path
                or "test_" in Path(path).name
                or module.language != Language.PYTHON
            ):
                continue

            if path not in imported_modules and self.module_graph.in_degree(path) == 0:
                # Has no importers - could be an entry point or dead code
                if not any(
                    kw in path for kw in ("cli", "main", "__main__", "setup", "conftest", "manage")
                ):
                    dead_code.append(path)
                    module.is_dead_code_candidate = True

        return dead_code

    def _identify_80_20_velocity(self, git_velocity: list[GitVelocityEntry]) -> list[dict]:
        """Identify the 20% of files responsible for 80% of change activity.

        Returns the smallest set of files whose cumulative commits
        account for at least 80% of all commits in the analysis window.
        """
        if not git_velocity:
            return []

        total_commits = sum(v.commit_count_30d for v in git_velocity)
        if total_commits == 0:
            return []

        sorted_files = sorted(git_velocity, key=lambda x: x.commit_count_30d, reverse=True)
        cumulative = 0
        hot_files = []

        for entry in sorted_files:
            cumulative += entry.commit_count_30d
            hot_files.append({
                "path": entry.path,
                "commits": entry.commit_count_30d,
                "cumulative_pct": round(cumulative / total_commits * 100, 1),
                "authors": entry.authors,
            })
            if cumulative / total_commits >= 0.8:
                break

        logger.info(
            f"80/20 velocity: {len(hot_files)} files ({len(hot_files)}/{len(sorted_files)} = "
            f"{len(hot_files)/len(sorted_files)*100:.0f}%) account for 80%+ of commits"
        )
        return hot_files

    def _assign_domain_clusters(self):
        """Assign domain clusters to modules based on path patterns."""
        for path, module in self.modules.items():
            path_lower = path.lower()
            if any(kw in path_lower for kw in ("test", "spec", "fixture")):
                module.domain_cluster = DomainCluster.TESTING
            elif any(kw in path_lower for kw in (
                "ingest", "extract", "load", "source", "staging", "raw",
                "airbyte", "connector",
            )):
                module.domain_cluster = DomainCluster.INGESTION
            elif any(kw in path_lower for kw in (
                "transform", "intermediate", "model", "mart", "dbt",
                "aggregate", "enrich",
            )):
                module.domain_cluster = DomainCluster.TRANSFORMATION
            elif any(kw in path_lower for kw in (
                "serve", "api", "endpoint", "report", "dashboard",
                "superset", "chart", "export", "output",
            )):
                module.domain_cluster = DomainCluster.SERVING
            elif any(kw in path_lower for kw in (
                "monitor", "alert", "metric", "log", "health", "sensor",
            )):
                module.domain_cluster = DomainCluster.MONITORING
            elif any(kw in path_lower for kw in (
                "config", "setting", "env", "constant", "schema.yml",
                "profile", "docker", "deploy",
            )):
                module.domain_cluster = DomainCluster.CONFIGURATION
            elif any(kw in path_lower for kw in (
                "util", "helper", "common", "shared", "lib", "tool",
            )):
                module.domain_cluster = DomainCluster.UTILITIES

    def _relative_path(self, path: str) -> str:
        """Convert absolute path to relative path from repo root."""
        try:
            return str(Path(path).relative_to(self.repo_path))
        except ValueError:
            return path

    def get_top_modules_by_pagerank(self, n: int = 10) -> list[tuple[str, float]]:
        """Return the top N modules by PageRank score."""
        ranked = sorted(
            self.modules.items(),
            key=lambda x: x[1].pagerank_score,
            reverse=True,
        )
        return [(path, mod.pagerank_score) for path, mod in ranked[:n]]

    def get_high_velocity_files(self, n: int = 20) -> list[tuple[str, int]]:
        """Return files with highest git change frequency."""
        ranked = sorted(
            self.modules.items(),
            key=lambda x: x[1].change_velocity_30d,
            reverse=True,
        )
        return [(path, mod.change_velocity_30d) for path, mod in ranked[:n] if mod.change_velocity_30d > 0]
