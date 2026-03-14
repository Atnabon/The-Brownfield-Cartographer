"""The Navigator Agent - Interactive Query Interface.

A query interface with four tools that allows exploratory investigation
and precise structured querying of the codebase knowledge graph:

1. find_implementation(concept) - Semantic search for code
2. trace_lineage(dataset, direction) - Graph traversal with citations
3. blast_radius(module_path) - Downstream dependency graph
4. explain_module(path) - LLM-generated module explanation
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import networkx as nx

from src.graph.knowledge_graph import KnowledgeGraph
from src.models import ModuleNode

logger = logging.getLogger(__name__)

# Try to import sklearn for TF-IDF vector search
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False
    logger.debug("sklearn not available — find_implementation will use keyword scoring")


class NavigatorAgent:
    """Interactive query interface for the codebase knowledge graph."""

    EXPLAIN_MODEL = "kimi-k2.5:cloud"

    def __init__(self, repo_path: Path, cartography_dir: Path,
                 skip_llm: bool = False, ollama_host: str | None = None):
        self.repo_path = repo_path
        self.cartography_dir = cartography_dir
        self.skip_llm = skip_llm
        self.ollama_host = ollama_host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.kg: Optional[KnowledgeGraph] = None
        self.purpose_statements: dict[str, str] = {}
        # TF-IDF index for vector similarity search
        self._tfidf_vectorizer: Optional[object] = None
        self._tfidf_matrix = None
        self._tfidf_paths: list[str] = []
        self._load_knowledge_graph()
        self._load_semantic_data()
        self._build_tfidf_index()
        self._init_llm()

    def _load_knowledge_graph(self):
        """Load the knowledge graph from serialized artifacts."""
        try:
            self.kg = KnowledgeGraph.load_from_directory(self.cartography_dir)
            logger.info(
                f"Navigator: Loaded knowledge graph "
                f"({len(self.kg.data.modules)} modules, "
                f"{self.kg.lineage_graph.number_of_nodes()} lineage nodes)"
            )
        except Exception as e:
            logger.error(f"Navigator: Failed to load knowledge graph: {e}")
            self.kg = KnowledgeGraph()

    def _load_semantic_data(self):
        """Load purpose statements from CODEBASE.md or module data."""
        if self.kg:
            for path, mod in self.kg.data.modules.items():
                if mod.purpose_statement:
                    self.purpose_statements[path] = mod.purpose_statement

    def _build_tfidf_index(self):
        """Build a TF-IDF vector index over purpose statements + function names.

        Enables cosine-similarity-based search in find_implementation().
        Falls back gracefully if sklearn is not installed.
        """
        if not _SKLEARN_AVAILABLE or not self.kg:
            return

        try:
            docs = []
            paths = []
            for path, module in self.kg.data.modules.items():
                purpose = self.purpose_statements.get(path, module.purpose_statement or "")
                funcs = " ".join(module.public_functions)
                classes = " ".join(module.classes)
                # Corpus document: path tokens + purpose + function names + class names
                path_tokens = path.replace("/", " ").replace("_", " ").replace(".py", "")
                doc = f"{path_tokens} {purpose} {funcs} {classes}"
                docs.append(doc)
                paths.append(path)

            if docs:
                vectorizer = TfidfVectorizer(
                    ngram_range=(1, 2),
                    max_features=10000,
                    strip_accents="unicode",
                    min_df=1,
                )
                matrix = vectorizer.fit_transform(docs)
                self._tfidf_vectorizer = vectorizer
                self._tfidf_matrix = matrix
                self._tfidf_paths = paths
                logger.info(
                    f"Navigator: TF-IDF index built ({len(paths)} modules, "
                    f"{matrix.shape[1]} features)"
                )
        except Exception as e:
            logger.warning(f"Navigator: TF-IDF index build failed: {e}")
            self._tfidf_vectorizer = None

    def _vector_search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """Cosine-similarity search over the TF-IDF index.

        Returns list of (path, score) sorted by score descending.
        """
        if not _SKLEARN_AVAILABLE or self._tfidf_vectorizer is None or self._tfidf_matrix is None:
            return []

        try:
            query_vec = self._tfidf_vectorizer.transform([query])
            scores = cosine_similarity(query_vec, self._tfidf_matrix).flatten()
            top_indices = scores.argsort()[::-1][:top_k]
            return [
                (self._tfidf_paths[i], float(scores[i]))
                for i in top_indices
                if scores[i] > 0
            ]
        except Exception as e:
            logger.debug(f"TF-IDF search failed: {e}")
            return []

    def _init_llm(self):
        """Probe local Ollama for explain_module support."""
        if self.skip_llm:
            return
        try:
            req = urllib.request.Request(
                f"{self.ollama_host}/api/tags",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                tags_data = json.loads(resp.read())
            available = [m["name"] for m in tags_data.get("models", [])]
            if not any(self.EXPLAIN_MODEL in n for n in available):
                logger.warning(
                    f"Navigator: Model '{self.EXPLAIN_MODEL}' not in Ollama. "
                    "explain_module will use static data only."
                )
                self.skip_llm = True
            else:
                logger.info(f"Navigator: Ollama ready — {self.EXPLAIN_MODEL}")
        except Exception:
            # Ollama not running — silently degrade
            self.skip_llm = True

    # ------------------------------------------------------------------
    # Tool 1: find_implementation
    # ------------------------------------------------------------------

    def find_implementation(self, concept: str) -> list[dict]:
        """Find modules related to a concept using TF-IDF cosine similarity.

        Hybrid search: TF-IDF vector scoring (primary) + keyword scoring (fallback).
        Returns list of {path, score, purpose, evidence_type, evidence}.
        """
        if not self.kg:
            return [{"error": "Knowledge graph not loaded"}]

        # Try TF-IDF vector search first
        vector_scores: dict[str, float] = {}
        if _SKLEARN_AVAILABLE and self._tfidf_vectorizer is not None:
            vector_results = self._vector_search(concept, top_k=30)
            for path, score in vector_results:
                vector_scores[path] = score

        results = []
        concept_lower = concept.lower()
        concept_words = set(concept_lower.split())

        for path, module in self.kg.data.modules.items():
            score = 0.0
            evidence = []

            # Vector similarity score (primary signal when TF-IDF is available)
            if path in vector_scores:
                score += vector_scores[path] * 10.0  # scale to be comparable with keyword scores
                evidence.append(f"vector similarity={vector_scores[path]:.3f}")

            # Path match (secondary signal)
            path_lower = path.lower()
            if concept_lower in path_lower:
                score += 3.0
                evidence.append(f"path contains '{concept}'")
            elif any(w in path_lower for w in concept_words):
                score += 1.5
                matching = [w for w in concept_words if w in path_lower]
                evidence.append(f"path contains: {', '.join(matching)}")

            # Purpose statement match
            purpose = self.purpose_statements.get(path, module.purpose_statement or "")
            if purpose and not vector_scores:  # only if no TF-IDF to avoid double-counting
                purpose_lower = purpose.lower()
                if concept_lower in purpose_lower:
                    score += 5.0
                    evidence.append("exact match in purpose statement")
                elif any(w in purpose_lower for w in concept_words if len(w) > 2):
                    word_matches = [w for w in concept_words if w in purpose_lower and len(w) > 2]
                    score += len(word_matches) * 1.5
                    evidence.append(f"purpose mentions: {', '.join(word_matches)}")

            # Function name match
            for func in module.public_functions:
                func_lower = func.lower()
                if concept_lower in func_lower or any(w in func_lower for w in concept_words):
                    score += 2.0
                    evidence.append(f"function: {func}()")
                    break

            # Class name match
            for cls in module.classes:
                cls_lower = cls.lower()
                if concept_lower in cls_lower or any(w in cls_lower for w in concept_words):
                    score += 2.0
                    evidence.append(f"class: {cls}")
                    break

            if score > 0:
                results.append({
                    "path": path,
                    "score": round(score, 3),
                    "purpose": purpose[:150] if purpose else "",
                    "evidence": evidence,
                    "evidence_type": "vector_similarity" if path in vector_scores else "static_analysis",
                    "language": module.language.value,
                    "domain": module.domain_cluster.value,
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:20]

    # ------------------------------------------------------------------
    # Tool 2: trace_lineage
    # ------------------------------------------------------------------

    def trace_lineage(self, dataset: str, direction: str = "upstream") -> dict:
        """Trace data lineage upstream or downstream with file:line citations.

        Args:
            dataset: Name of the dataset node to trace from
            direction: 'upstream' or 'downstream'

        Returns dict with traced nodes and edge details.
        """
        if not self.kg:
            return {"error": "Knowledge graph not loaded"}

        graph = self.kg.lineage_graph

        # Find matching node
        node = self._resolve_node(dataset, graph)
        if not node:
            return {
                "error": f"Dataset '{dataset}' not found in lineage graph",
                "suggestions": self._suggest_nodes(dataset, graph),
            }

        # Traverse
        if direction == "upstream":
            traced = self._bfs_reverse(node, graph)
        else:
            traced = self._bfs_forward(node, graph)

        # Enrich with edge metadata
        enriched = []
        for traced_node in traced:
            node_info = {
                "name": traced_node["name"],
                "depth": traced_node["depth"],
                "node_type": graph.nodes.get(traced_node["name"], {}).get("node_type", "unknown"),
            }

            # Get edge metadata
            if direction == "upstream":
                for pred in graph.predecessors(traced_node["name"]):
                    edge_data = graph.edges.get((pred, traced_node["name"]), {})
                    if edge_data:
                        node_info["source_file"] = edge_data.get("source_file", "")
                        node_info["line_range"] = edge_data.get("line_range", "")
                        node_info["transformation_type"] = edge_data.get("transformation_type", "")
                        break
            else:
                for succ in graph.successors(traced_node["name"]):
                    edge_data = graph.edges.get((traced_node["name"], succ), {})
                    if edge_data:
                        node_info["source_file"] = edge_data.get("source_file", "")
                        node_info["line_range"] = edge_data.get("line_range", "")
                        node_info["transformation_type"] = edge_data.get("transformation_type", "")
                        break

            enriched.append(node_info)

        return {
            "root": node,
            "direction": direction,
            "total_nodes": len(enriched),
            "nodes": enriched,
        }

    def _bfs_forward(self, start: str, graph: nx.DiGraph) -> list[dict]:
        """BFS forward (downstream) from a node."""
        visited = set()
        queue = [(start, 0)]
        results = []

        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current != start:
                results.append({"name": current, "depth": depth})

            for succ in graph.successors(current):
                if succ not in visited:
                    queue.append((succ, depth + 1))

        return results

    def _bfs_reverse(self, start: str, graph: nx.DiGraph) -> list[dict]:
        """BFS reverse (upstream) from a node."""
        visited = set()
        queue = [(start, 0)]
        results = []

        while queue:
            current, depth = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current != start:
                results.append({"name": current, "depth": depth})

            for pred in graph.predecessors(current):
                if pred not in visited:
                    queue.append((pred, depth + 1))

        return results

    # ------------------------------------------------------------------
    # Tool 3: blast_radius
    # ------------------------------------------------------------------

    def blast_radius(self, module_path: str) -> dict:
        """Show the blast radius of a module or dataset.

        Checks both the module import graph and the lineage graph.
        Returns all downstream dependencies that would be affected.
        """
        if not self.kg:
            return {"error": "Knowledge graph not loaded"}

        results = {"module_path": module_path, "import_impact": {}, "lineage_impact": {}}

        # Check module import graph
        node = self._resolve_node(module_path, self.kg.module_graph)
        if node:
            import_affected = []
            visited = set()
            queue = [(node, 0)]
            max_depth = 0

            while queue:
                current, depth = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                if current != node:
                    import_affected.append({"module": current, "depth": depth})
                    max_depth = max(max_depth, depth)

                for succ in self.kg.module_graph.successors(current):
                    if succ not in visited:
                        queue.append((succ, depth + 1))

            # Also check reverse — who imports this?
            importers = list(self.kg.module_graph.predecessors(node)) if node in self.kg.module_graph else []

            results["import_impact"] = {
                "root": node,
                "downstream_modules": len(import_affected),
                "max_depth": max_depth,
                "affected": import_affected[:30],
                "imported_by": importers[:20],
            }

        # Check lineage graph
        lineage_node = self._resolve_node(module_path, self.kg.lineage_graph)
        if lineage_node:
            lineage_result = self.trace_lineage(lineage_node, direction="downstream")
            results["lineage_impact"] = lineage_result

        if not node and not lineage_node:
            results["error"] = f"'{module_path}' not found in module or lineage graph"
            results["suggestions"] = (
                self._suggest_nodes(module_path, self.kg.module_graph) +
                self._suggest_nodes(module_path, self.kg.lineage_graph)
            )[:10]

        return results

    # ------------------------------------------------------------------
    # Tool 4: explain_module
    # ------------------------------------------------------------------

    def explain_module(self, path: str) -> dict:
        """Generate an explanation of a specific module.

        Uses stored purpose statement + static analysis data, optionally
        enhanced with LLM if available.
        """
        if not self.kg:
            return {"error": "Knowledge graph not loaded"}

        # Find module
        module = self.kg.data.modules.get(path)
        if not module:
            # Try partial match
            matches = [p for p in self.kg.data.modules if path in p]
            if len(matches) == 1:
                path = matches[0]
                module = self.kg.data.modules[path]
            elif matches:
                return {
                    "error": f"Ambiguous path '{path}'. Matches: {matches[:10]}",
                }
            else:
                return {"error": f"Module '{path}' not found"}

        explanation = {
            "path": path,
            "language": module.language.value,
            "domain": module.domain_cluster.value,
            "lines_of_code": module.lines_of_code,
            "complexity_score": module.complexity_score,
            "pagerank_score": module.pagerank_score,
            "is_dead_code_candidate": module.is_dead_code_candidate,
            "change_velocity_30d": module.change_velocity_30d,
            "public_functions": module.public_functions,
            "classes": module.classes,
            "imports": module.imports[:20],
            "purpose_statement": module.purpose_statement or "",
            "evidence_type": "static_analysis",
        }

        # Try LLM enhancement via local Ollama
        if not self.skip_llm:
            try:
                full_path = self.repo_path / path
                if full_path.exists():
                    source = full_path.read_text(encoding="utf-8", errors="replace")[:6000]
                    prompt = (
                        f"Explain this {module.language.value} module in 3-4 sentences. "
                        f"Focus on its business purpose and role in the system.\n\n"
                        f"File: {path}\n\n```\n{source}\n```\n\nExplanation:"
                    )
                    payload = json.dumps({
                        "model": self.EXPLAIN_MODEL,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.2},
                    }).encode()
                    req = urllib.request.Request(
                        f"{self.ollama_host}/api/generate",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        result = json.loads(resp.read())
                    llm_text = result.get("response", "").strip()
                    if llm_text:
                        explanation["llm_explanation"] = llm_text
                        explanation["evidence_type"] = "llm_analysis"
            except Exception as e:
                logger.debug(f"LLM explanation failed for {path}: {e}")

        return explanation

    # ------------------------------------------------------------------
    # Interactive Query Loop
    # ------------------------------------------------------------------

    def interactive_loop(self):
        """Run the interactive Navigator query loop."""
        print("\n" + "=" * 60)
        print("  The Brownfield Cartographer — Navigator")
        print("  Interactive Query Interface")
        print("=" * 60)
        print()
        print("Available commands:")
        print("  find <concept>             — Search for code implementing a concept")
        print("  trace <dataset> [up|down]  — Trace data lineage")
        print("  blast <module_or_dataset>  — Show blast radius")
        print("  explain <module_path>      — Explain a module")
        print("  chain find <concept> -> explain — Find then explain top result")
        print("  chain blast <module> -> trace  — Blast then trace downstream")
        print("  stats                      — Show knowledge graph statistics")
        print("  quit                       — Exit")
        print()

        while True:
            try:
                user_input = input("navigator> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_input:
                continue

            parts = user_input.split(maxsplit=1)
            command = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            if command in ("quit", "exit", "q"):
                print("Goodbye!")
                break
            elif command == "find":
                self._handle_find(args)
            elif command == "trace":
                self._handle_trace(args)
            elif command == "blast":
                self._handle_blast(args)
            elif command == "explain":
                self._handle_explain(args)
            elif command == "chain":
                self._handle_chain(args)
            elif command == "stats":
                self._handle_stats()
            else:
                print(f"  Unknown command: {command}")
                print("  Use: find, trace, blast, explain, chain, stats, quit")
            print()

    def _handle_find(self, concept: str):
        if not concept:
            print("  Usage: find <concept>")
            return
        results = self.find_implementation(concept)
        if not results:
            print(f"  No results found for '{concept}'")
            return
        print(f"\n  Found {len(results)} matches for '{concept}':\n")
        for r in results[:10]:
            print(f"  [{r['score']:.1f}] {r['path']}")
            if r.get("purpose"):
                print(f"        Purpose: {r['purpose'][:100]}")
            if r.get("evidence"):
                print(f"        Evidence: {', '.join(r['evidence'][:3])}")

    def _handle_trace(self, args: str):
        parts = args.split()
        if not parts:
            print("  Usage: trace <dataset> [upstream|downstream]")
            return
        dataset = parts[0]
        direction = parts[1] if len(parts) > 1 else "upstream"
        if direction.startswith("up"):
            direction = "upstream"
        elif direction.startswith("down"):
            direction = "downstream"

        result = self.trace_lineage(dataset, direction)
        if "error" in result:
            print(f"  Error: {result['error']}")
            if result.get("suggestions"):
                print(f"  Did you mean: {', '.join(result['suggestions'][:5])}")
            return

        print(f"\n  Lineage trace ({direction}) from '{result['root']}':")
        print(f"  Total nodes: {result['total_nodes']}\n")
        for node in result.get("nodes", [])[:20]:
            indent = "    " * node["depth"]
            src = node.get("source_file", "")
            line = node.get("line_range", "")
            cite = f" [{src}:{line}]" if src else ""
            print(f"  {indent}→ {node['name']} ({node['node_type']}){cite}")

    def _handle_blast(self, module_path: str):
        if not module_path:
            print("  Usage: blast <module_or_dataset>")
            return
        result = self.blast_radius(module_path)
        if "error" in result:
            print(f"  Error: {result['error']}")
            if result.get("suggestions"):
                print(f"  Did you mean: {', '.join(result['suggestions'][:5])}")
            return

        imp = result.get("import_impact", {})
        if imp and imp.get("downstream_modules", 0) > 0:
            print(f"\n  Import blast radius for '{imp.get('root', module_path)}':")
            print(f"  Downstream modules: {imp['downstream_modules']}")
            print(f"  Max depth: {imp['max_depth']}")
            if imp.get("imported_by"):
                print(f"  Imported by: {', '.join(imp['imported_by'][:5])}")
            for a in imp.get("affected", [])[:15]:
                print(f"    → {a['module']} (depth {a['depth']})")

        lin = result.get("lineage_impact", {})
        if lin and lin.get("total_nodes", 0) > 0:
            print(f"\n  Lineage blast radius from '{lin.get('root', module_path)}':")
            print(f"  Affected nodes: {lin['total_nodes']}")
            for node in lin.get("nodes", [])[:15]:
                print(f"    → {node['name']} (depth {node['depth']})")

    def _handle_explain(self, path: str):
        if not path:
            print("  Usage: explain <module_path>")
            return
        result = self.explain_module(path)
        if "error" in result:
            print(f"  Error: {result['error']}")
            return
        print(f"\n  Module: {result['path']}")
        print(f"  Language: {result['language']}")
        print(f"  Domain: {result['domain']}")
        print(f"  LOC: {result['lines_of_code']}, Complexity: {result['complexity_score']:.1f}")
        print(f"  PageRank: {result['pagerank_score']:.4f}")
        print(f"  Velocity (30d): {result['change_velocity_30d']} commits")
        if result.get("llm_explanation"):
            print(f"\n  LLM Explanation:\n  {result['llm_explanation']}")
        elif result.get("purpose_statement"):
            print(f"\n  Purpose: {result['purpose_statement']}")
        if result.get("public_functions"):
            print(f"\n  Public Functions: {', '.join(result['public_functions'][:10])}")
        if result.get("classes"):
            print(f"  Classes: {', '.join(result['classes'][:10])}")

    def _handle_stats(self):
        if not self.kg:
            print("  Knowledge graph not loaded.")
            return
        summary = self.kg.get_summary()
        index_type = "TF-IDF vector" if self._tfidf_vectorizer is not None else "keyword"
        print("\n  Knowledge Graph Statistics:")
        print(f"  Modules: {summary['modules']}")
        print(f"  Functions: {summary['functions']}")
        print(f"  Datasets: {summary['datasets']}")
        print(f"  Transformations: {summary['transformations']}")
        print(f"  Import edges: {summary['import_edges']}")
        print(f"  Lineage edges: {summary['lineage_edges']}")
        print(f"  Languages: {summary['languages']}")
        print(f"  Search index: {index_type}")

    def _handle_chain(self, args: str):
        """Handle multi-step tool chaining.

        Syntax:
          chain find <concept> -> explain    (find top result, then explain it)
          chain blast <module> -> trace      (blast radius, then trace lineage of first hit)
        """
        if "->" not in args:
            print("  Usage: chain <step1> -> <step2>")
            print("  Examples:")
            print("    chain find sql lineage -> explain")
            print("    chain blast src/agents/hydrologist.py -> trace")
            return

        parts = args.split("->", 1)
        step1 = parts[0].strip()
        step2 = parts[1].strip().lower()

        step1_parts = step1.split(maxsplit=1)
        if not step1_parts:
            print("  Invalid chain syntax.")
            return

        cmd = step1_parts[0].lower()
        cmd_args = step1_parts[1] if len(step1_parts) > 1 else ""

        if cmd == "find" and step2 == "explain":
            # Step 1: find top matching module
            results = self.find_implementation(cmd_args)
            if not results:
                print(f"  No results found for '{cmd_args}'")
                return
            top = results[0]
            print(f"\n  Chain: find '{cmd_args}' → top result: {top['path']}")
            print(f"         Score: {top['score']}, Evidence: {', '.join(top['evidence'][:3])}")
            print("\n  → Explaining top result:")
            self._handle_explain(top["path"])

        elif cmd == "blast" and step2 == "trace":
            # Step 1: blast radius
            print(f"\n  Chain: blast '{cmd_args}' → then trace downstream")
            result = self.blast_radius(cmd_args)
            if "error" in result:
                print(f"  Error: {result['error']}")
                return
            # collect first downstream lineage hit
            lineage_impact = result.get("lineage_impact", {})
            nodes = lineage_impact.get("nodes", [])
            if nodes:
                first_hit = nodes[0]["name"]
                print(f"  → First downstream lineage node: {first_hit}")
                print("\n  → Tracing downstream from that node:")
                self._handle_trace(f"{first_hit} downstream")
            else:
                import_impact = result.get("import_impact", {})
                affected = import_impact.get("affected", [])
                if affected:
                    first_mod = affected[0]["module"]
                    print(f"  → First downstream import: {first_mod}")
                    print("\n  → Explaining that module:")
                    self._handle_explain(first_mod)
                else:
                    print("  No downstream nodes found.")
        else:
            print(f"  Unknown chain: '{cmd} -> {step2}'")
            print("  Supported chains: 'find -> explain', 'blast -> trace'")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_node(self, name: str, graph: nx.DiGraph) -> str | None:
        """Resolve a node name, trying exact match then partial."""
        if name in graph:
            return name

        # Partial match
        matches = [n for n in graph.nodes() if name.lower() in n.lower()]
        if len(matches) == 1:
            return matches[0]

        return None

    def _suggest_nodes(self, name: str, graph: nx.DiGraph) -> list[str]:
        """Suggest similar node names."""
        name_lower = name.lower()
        suggestions = [n for n in graph.nodes() if name_lower in n.lower()]
        return sorted(suggestions)[:10]
