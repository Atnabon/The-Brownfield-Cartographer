"""The Semanticist Agent - LLM-Powered Purpose Analyst.

Uses LLMs to generate semantic understanding of code that static analysis
cannot provide. This is purpose extraction grounded in implementation evidence.

Core capabilities:
- Purpose statement generation per module (based on code, not docstring)
- Documentation drift detection (docstring vs. implementation)
- Domain clustering via embeddings + k-means
- Day-One FDE question answering via synthesis
- ContextWindowBudget for cost discipline
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.models import DomainCluster, ModuleNode

logger = logging.getLogger(__name__)


# Context Window Budget

@dataclass
class ContextWindowBudget:
    """Track token usage and enforce budget limits for LLM calls."""

    max_tokens: int = 1_000_000  # total budget
    tokens_used: int = 0
    calls_made: int = 0
    total_cost_usd: float = 0.0
    cost_per_1k_input: float = 0.0  # Local Ollama — no cost
    cost_per_1k_output: float = 0.0
    call_log: list[dict] = field(default_factory=list)

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count (rough: ~4 chars per token)."""
        return max(1, len(text) // 4)

    def can_afford(self, estimated_tokens: int) -> bool:
        return (self.tokens_used + estimated_tokens) <= self.max_tokens

    def record_usage(self, input_tokens: int, output_tokens: int, model: str, task: str):
        self.tokens_used += input_tokens + output_tokens
        cost = (input_tokens / 1000 * self.cost_per_1k_input +
                output_tokens / 1000 * self.cost_per_1k_output)
        self.total_cost_usd += cost
        self.calls_made += 1
        self.call_log.append({
            "task": task,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
            "cumulative_cost_usd": round(self.total_cost_usd, 6),
        })

    def summary(self) -> dict:
        return {
            "total_tokens_used": self.tokens_used,
            "total_calls": self.calls_made,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "budget_remaining": self.max_tokens - self.tokens_used,
        }

# Semanticist Agent

class SemanticistAgent:
    """LLM-powered semantic analysis of codebase modules."""

    # Model tiering: cheap model for bulk purpose generation, expensive for synthesis.
    # Both can be the same Ollama instance; use the lightest available model for bulk.
    BULK_MODEL = os.environ.get("CARTOGRAPHER_BULK_MODEL", "mistral:latest")
    SYNTHESIS_MODEL = os.environ.get("CARTOGRAPHER_SYNTHESIS_MODEL", "kimi-k2.5:cloud")
    OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def __init__(self, repo_path: Path, skip_llm: bool = False,
                 ollama_host: str | None = None):
        self.repo_path = repo_path
        self.skip_llm = skip_llm
        self.budget = ContextWindowBudget()
        self.purpose_statements: dict[str, str] = {}
        self.doc_drift_flags: dict[str, dict] = {}
        self.domain_clusters: dict[str, str] = {}
        self.day_one_answers: dict[str, str] = {}
        self.ollama_host = ollama_host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self._init_llm()

    def _init_llm(self):
        """Probe local Ollama to confirm at least one model is available."""
        if self.skip_llm:
            logger.info("Semanticist: LLM disabled (--skip-llm flag)")
            return

        # Health-check: hit /api/tags to see if Ollama is up
        try:
            req = urllib.request.Request(
                f"{self.ollama_host}/api/tags",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                tags_data = json.loads(resp.read())
            available = [m["name"] for m in tags_data.get("models", [])]

            # Resolve BULK_MODEL — fall back to any available model if configured one missing
            bulk_available = any(self.BULK_MODEL in name for name in available)
            synthesis_available = any(self.SYNTHESIS_MODEL in name for name in available)

            if not bulk_available and not synthesis_available:
                if available:
                    # Graceful degradation: use whatever is available for both tiers
                    fallback = available[0].split(":")[0] + ":latest"
                    logger.warning(
                        f"Semanticist: Neither BULK ({self.BULK_MODEL}) nor SYNTHESIS "
                        f"({self.SYNTHESIS_MODEL}) model found. Using {fallback} for both."
                    )
                    self.BULK_MODEL = fallback
                    self.SYNTHESIS_MODEL = fallback
                else:
                    logger.warning(
                        "Semanticist: No models available in Ollama. Falling back to static analysis."
                    )
                    self.skip_llm = True
                    return
            elif not bulk_available:
                # Use synthesis model for bulk too (more expensive but available)
                logger.info(
                    f"Semanticist: BULK model '{self.BULK_MODEL}' not found. "
                    f"Using SYNTHESIS model '{self.SYNTHESIS_MODEL}' for bulk tasks."
                )
                self.BULK_MODEL = self.SYNTHESIS_MODEL
            elif not synthesis_available:
                # Use bulk model for synthesis too (less capable but available)
                logger.info(
                    f"Semanticist: SYNTHESIS model '{self.SYNTHESIS_MODEL}' not found. "
                    f"Using BULK model '{self.BULK_MODEL}' for synthesis tasks."
                )
                self.SYNTHESIS_MODEL = self.BULK_MODEL

            logger.info(
                f"Semanticist: Ollama ready — bulk={self.BULK_MODEL}, "
                f"synthesis={self.SYNTHESIS_MODEL} at {self.ollama_host}"
            )
        except urllib.error.URLError as e:
            logger.warning(
                f"Semanticist: Ollama not reachable at {self.ollama_host} ({e}). "
                "Falling back to static analysis."
            )
            self.skip_llm = True
        except Exception as e:
            logger.warning(f"Semanticist: Ollama probe failed: {e}. Falling back.")
            self.skip_llm = True

    def run(self, modules: dict[str, ModuleNode],
            surveyor_results: dict, hydrologist_results: dict) -> dict:
        """Execute the full semanticist analysis pipeline."""
        logger.info("Semanticist: Starting semantic analysis")

        # Step 1: Generate purpose statements for key modules
        self._generate_purpose_statements(modules)

        # Step 2: Detect documentation drift
        self._detect_doc_drift(modules)

        # Step 3: Cluster modules into domains
        self._cluster_into_domains(modules)

        # Step 4: Answer Day-One questions
        self._answer_day_one_questions(modules, surveyor_results, hydrologist_results)

        logger.info(
            f"Semanticist: Generated {len(self.purpose_statements)} purpose statements, "
            f"flagged {len(self.doc_drift_flags)} doc drift issues"
        )

        return {
            "purpose_statements": self.purpose_statements,
            "doc_drift_flags": self.doc_drift_flags,
            "domain_clusters": self.domain_clusters,
            "day_one_answers": self.day_one_answers,
            "budget_summary": self.budget.summary(),
            "budget_log": self.budget.call_log,
        }

    # Purpose Statement Generation

    def _generate_purpose_statements(self, modules: dict[str, ModuleNode]):
        """Generate purpose statements for all significant modules."""
        # Prioritize: sort by PageRank (most important first)
        sorted_modules = sorted(
            modules.items(),
            key=lambda x: x[1].pagerank_score,
            reverse=True,
        )

        for path, module in sorted_modules:
            if module.lines_of_code < 5:
                continue  # Skip trivially small files

            if self.skip_llm:
                purpose = self._generate_static_purpose(path, module)
            else:
                purpose = self._generate_llm_purpose(path, module)

            if purpose:
                self.purpose_statements[path] = purpose
                module.purpose_statement = purpose

    def _generate_llm_purpose(self, path: str, module: ModuleNode) -> str:
        """Use LLM to generate a purpose statement from source code."""
        try:
            full_path = self.repo_path / path
            if not full_path.exists():
                return self._generate_static_purpose(path, module)

            source_code = full_path.read_text(encoding="utf-8", errors="replace")

            # Truncate to fit budget
            max_chars = 8000
            if len(source_code) > max_chars:
                source_code = source_code[:max_chars] + "\n... [truncated]"

            estimated_tokens = self.budget.estimate_tokens(source_code) + 200
            if not self.budget.can_afford(estimated_tokens):
                logger.warning(f"Semanticist: Budget exhausted, falling back to static for {path}")
                return self._generate_static_purpose(path, module)

            prompt = (
                "You are a senior software engineer analyzing a codebase. "
                "Based ONLY on the source code below (ignore any docstrings), "
                "write a 2-3 sentence purpose statement explaining what this module does "
                "in terms of its BUSINESS FUNCTION, not implementation details.\n\n"
                f"File: {path}\n"
                f"Language: {module.language.value}\n\n"
                f"```\n{source_code}\n```\n\n"
                "Purpose statement:"
            )

            response = self._call_llm(prompt, task=f"purpose:{path}")
            return response.strip() if response else self._generate_static_purpose(path, module)

        except Exception as e:
            logger.debug(f"LLM purpose generation failed for {path}: {e}")
            return self._generate_static_purpose(path, module)

    def _generate_static_purpose(self, path: str, module: ModuleNode) -> str:
        """Generate a purpose statement from static analysis when LLM is unavailable."""
        parts = []

        # Language and type
        lang = module.language.value.capitalize()
        parts.append(f"{lang} module")

        # Domain inference from path
        domain = module.domain_cluster.value
        if domain != "unknown":
            parts.append(f"in the {domain} domain")

        # Functional description
        if module.public_functions:
            func_count = len(module.public_functions)
            sample = ", ".join(module.public_functions[:3])
            if func_count > 3:
                sample += f", ... ({func_count} total)"
            parts.append(f"providing functions: {sample}")

        if module.classes:
            class_count = len(module.classes)
            sample = ", ".join(module.classes[:3])
            parts.append(f"defining classes: {sample}")

        if module.imports:
            key_deps = [imp for imp in module.imports[:5]]
            if key_deps:
                parts.append(f"depends on: {', '.join(key_deps)}")

        # Complexity and significance
        if module.pagerank_score > 0.005:
            parts.append("(high architectural significance)")
        elif module.is_dead_code_candidate:
            parts.append("(potential dead code)")

        return ". ".join(parts) + "." if parts else f"Module at {path}."

    # Documentation Drift Detection

    def _detect_doc_drift(self, modules: dict[str, ModuleNode]):
        """Flag modules where docstring contradicts implementation."""
        for path, module in modules.items():
            if module.language.value != "python" or module.lines_of_code < 10:
                continue

            try:
                full_path = self.repo_path / path
                if not full_path.exists():
                    continue

                source = full_path.read_text(encoding="utf-8", errors="replace")
                docstring = self._extract_module_docstring(source)

                if not docstring or len(docstring) < 20:
                    continue

                if self.skip_llm:
                    # Simple heuristic: check if docstring mentions things not in code
                    drift = self._detect_drift_heuristic(docstring, source, module)
                else:
                    drift = self._detect_drift_llm(path, docstring, source, module)

                if drift:
                    self.doc_drift_flags[path] = drift

            except Exception as e:
                logger.debug(f"Doc drift detection failed for {path}: {e}")

    def _extract_module_docstring(self, source: str) -> str:
        """Extract the module-level docstring from Python source."""
        # Match triple-quoted string at the start of the module
        match = re.match(
            r'^(?:\s*#[^\n]*\n)*\s*(?:\'\'\'|""")(.*?)(?:\'\'\'|""")',
            source, re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    def _detect_drift_heuristic(self, docstring: str, source: str, module: ModuleNode) -> dict:
        """Simple heuristic drift detection without LLM.

        Returns structured dict with severity, contradiction, and stale_references.
        """
        issues = []
        stale_refs = []

        # Check if docstring mentions functions that don't exist
        actual_funcs = set(f.lower() for f in module.public_functions)

        # Check for stale references
        mentioned_funcs = set(re.findall(r'`(\w+)`|(\w+)\(\)', docstring))
        mentioned_funcs = {f[0] or f[1] for f in mentioned_funcs if f[0] or f[1]}
        stale = mentioned_funcs - actual_funcs - {"self", "cls", "none", "true", "false"}

        if stale and len(stale) <= 5:
            issues.append(f"Docstring mentions functions not found in code: {', '.join(stale)}")
            stale_refs = list(stale)

        # Check import consistency
        doc_lower = docstring.lower()
        for word in ["deprecated", "removed", "no longer"]:
            if word in doc_lower:
                issues.append(f"Docstring contains '{word}' — may indicate staleness")

        if not issues:
            return {}

        severity = "high" if len(stale_refs) > 2 else "medium" if stale_refs else "low"
        return {
            "severity": severity,
            "contradiction": "; ".join(issues),
            "stale_references": stale_refs,
            "analysis_method": "static",
        }

    def _detect_drift_llm(self, path: str, docstring: str, source: str,
                          module: ModuleNode) -> dict:
        """LLM-based documentation drift detection.

        Returns structured dict with severity, contradiction, and stale_references.
        """
        # Truncate source for budget
        source_truncated = source[:4000] if len(source) > 4000 else source

        estimated_tokens = self.budget.estimate_tokens(source_truncated + docstring) + 200
        if not self.budget.can_afford(estimated_tokens):
            return self._detect_drift_heuristic(docstring, source, module)

        prompt = (
            "Compare this Python module's docstring with its actual implementation. "
            "Respond as JSON with these fields:\n"
            "- no_drift: true if docstring accurately describes the code, false otherwise\n"
            "- severity: 'high', 'medium', or 'low' (only if no_drift=false)\n"
            "- contradiction: brief description of the discrepancy (only if no_drift=false)\n"
            "- stale_references: list of function/class names mentioned in docstring but missing from code\n\n"
            f"Docstring:\n{docstring}\n\n"
            f"Code:\n```python\n{source_truncated}\n```\n\n"
            "JSON response:"
        )

        response = self._call_llm(prompt, task=f"drift:{path}", model=self.BULK_MODEL)
        if response:
            try:
                json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group())
                    if parsed.get("no_drift"):
                        return {}
                    return {
                        "severity": parsed.get("severity", "medium"),
                        "contradiction": parsed.get("contradiction", response.strip()[:300]),
                        "stale_references": parsed.get("stale_references", []),
                        "analysis_method": "llm",
                    }
            except (json.JSONDecodeError, AttributeError):
                # Fallback: treat entire response as contradiction
                if "NO_DRIFT" not in response.upper() and len(response.strip()) > 10:
                    return {
                        "severity": "low",
                        "contradiction": response.strip()[:300],
                        "stale_references": [],
                        "analysis_method": "llm",
                    }
        return {}

    # Domain Clustering

    def _cluster_into_domains(self, modules: dict[str, ModuleNode]):
        """Cluster modules into business domains using purpose statements."""
        if not self.purpose_statements:
            # Fall back to existing path-based clusters
            for path, module in modules.items():
                self.domain_clusters[path] = module.domain_cluster.value
            return

        if self.skip_llm:
            # Use existing path-based clustering from Surveyor
            for path, module in modules.items():
                self.domain_clusters[path] = module.domain_cluster.value
            return

        # Use LLM to cluster based on purpose statements
        try:
            # Group purpose statements in chunks for LLM
            statements = list(self.purpose_statements.items())
            chunk_size = 50
            all_clusters = {}

            for i in range(0, len(statements), chunk_size):
                chunk = statements[i:i + chunk_size]
                chunk_text = "\n".join(
                    f"- {path}: {purpose}" for path, purpose in chunk
                )

                prompt = (
                    "Categorize each module into one of these domains: "
                    "ingestion, transformation, serving, monitoring, configuration, "
                    "testing, utilities, unknown.\n\n"
                    f"Modules:\n{chunk_text}\n\n"
                    "Respond as JSON: {{\"path\": \"domain\", ...}}"
                )

                response = self._call_llm(prompt, task="domain_clustering")
                if response:
                    try:
                        # Extract JSON from response
                        json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
                        if json_match:
                            clusters = json.loads(json_match.group())
                            all_clusters.update(clusters)
                    except (json.JSONDecodeError, AttributeError):
                        pass

            # Apply clusters
            for path, domain in all_clusters.items():
                self.domain_clusters[path] = domain
                if path in modules:
                    try:
                        modules[path].domain_cluster = DomainCluster(domain)
                    except ValueError:
                        pass

        except Exception as e:
            logger.warning(f"Domain clustering failed: {e}")
            for path, module in modules.items():
                self.domain_clusters[path] = module.domain_cluster.value

    # Day-One Question Answering

    def _answer_day_one_questions(self, modules: dict[str, ModuleNode],
                                  surveyor_results: dict, hydrologist_results: dict):
        """Answer the 5 FDE Day-One questions via synthesis."""
        # Build context from analysis results
        context = self._build_synthesis_context(modules, surveyor_results, hydrologist_results)

        questions = [
            "Q1: What is the primary data ingestion path?",
            "Q2: What are the 3-5 most critical output datasets/endpoints?",
            "Q3: What is the blast radius if the most critical module fails?",
            "Q4: Where is the business logic concentrated vs. distributed?",
            "Q5: What has changed most frequently in the last 90 days?",
        ]

        if self.skip_llm:
            self._answer_questions_static(questions, context, modules,
                                          surveyor_results, hydrologist_results)
        else:
            self._answer_questions_llm(questions, context)

    def _build_synthesis_context(self, modules: dict[str, ModuleNode],
                                 surveyor_results: dict, hydrologist_results: dict) -> str:
        """Build a condensed context string for Day-One synthesis."""
        sections = []

        # Top modules by PageRank
        top_modules = sorted(
            modules.items(), key=lambda x: x[1].pagerank_score, reverse=True
        )[:10]
        sections.append("## Top Modules by PageRank (architectural hubs)")
        for path, mod in top_modules:
            purpose = self.purpose_statements.get(path, "")
            sections.append(f"- {path} (score={mod.pagerank_score:.4f}): {purpose}")

        # Domain distribution
        domain_counts: dict[str, int] = {}
        for mod in modules.values():
            d = mod.domain_cluster.value
            domain_counts[d] = domain_counts.get(d, 0) + 1
        sections.append("\n## Module Domain Distribution")
        for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
            sections.append(f"- {domain}: {count} modules")

        # Data sources and sinks
        sources = hydrologist_results.get("sources", [])
        sinks = hydrologist_results.get("sinks", [])
        sections.append(f"\n## Data Flow: {len(sources)} sources, {len(sinks)} sinks")
        if sources:
            sections.append("Sources (sample): " + ", ".join(sources[:15]))
        if sinks:
            sections.append("Sinks (sample): " + ", ".join(sinks[:15]))

        # Datasets and transformations
        datasets = hydrologist_results.get("datasets", {})
        transforms = hydrologist_results.get("transformations", {})
        sections.append(f"\n## Lineage: {len(datasets)} datasets, {len(transforms)} transformations")

        # Git velocity
        velocity = surveyor_results.get("git_velocity", [])
        if velocity:
            sections.append("\n## Highest Velocity Files (30 days)")
            for v in velocity[:10]:
                sections.append(f"- {v.path}: {v.commit_count_30d} commits")

        # High velocity 80/20
        hot_files = surveyor_results.get("high_velocity_80_20", [])
        if hot_files:
            sections.append(f"\n## 80/20 Velocity: {len(hot_files)} files = 80%+ commits")

        # Circular dependencies
        circular = surveyor_results.get("circular_dependencies", [])
        if circular:
            sections.append(f"\n## Circular Dependencies: {len(circular)} detected")

        # Dead code
        dead = surveyor_results.get("dead_code_candidates", [])
        if dead:
            sections.append(f"\n## Dead Code Candidates: {len(dead)} modules")

        # Language breakdown
        lang_counts: dict[str, int] = {}
        for mod in modules.values():
            lang_counts[mod.language.value] = lang_counts.get(mod.language.value, 0) + 1
        sections.append("\n## Languages")
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
            sections.append(f"- {lang}: {count} files")

        return "\n".join(sections)

    def _answer_questions_static(self, questions: list[str], context: str,
                                  modules: dict[str, ModuleNode],
                                  surveyor_results: dict, hydrologist_results: dict):
        """Answer Day-One questions using static analysis only, with file:line citations."""
        datasets = hydrologist_results.get("datasets", {})
        sources = hydrologist_results.get("sources", [])
        sinks = hydrologist_results.get("sinks", [])
        velocity = surveyor_results.get("git_velocity", [])
        top_modules = sorted(
            modules.items(), key=lambda x: x[1].pagerank_score, reverse=True
        )[:5]

        # Q1: Ingestion path
        source_datasets = [s for s in sources[:10]]
        ingestion_modules = [
            (p, m) for p, m in modules.items()
            if m.domain_cluster == DomainCluster.INGESTION
        ][:5]
        ingestion_citations = ", ".join(
            f"`{p}` (line 1, domain=ingestion, PageRank={m.pagerank_score:.4f})"
            for p, m in ingestion_modules[:3]
        ) if ingestion_modules else "N/A"
        self.day_one_answers["Q1"] = (
            f"Data enters through {len(sources)} source nodes in the lineage graph. "
            f"Key source datasets: {', '.join(f'`{s}`' for s in source_datasets[:5])}. "
            f"Ingestion-domain modules: {ingestion_citations}."
        )

        # Q2: Critical outputs
        sink_datasets = sinks[:5]
        serving_modules = [
            (p, m) for p, m in modules.items()
            if m.domain_cluster == DomainCluster.SERVING
        ][:3]
        serving_citations = ", ".join(
            f"`{p}` (PageRank={m.pagerank_score:.4f})"
            for p, m in serving_modules
        ) if serving_modules else "N/A"
        self.day_one_answers["Q2"] = (
            f"The system produces {len(sinks)} terminal output datasets. "
            f"Key sinks: {', '.join(f'`{s}`' for s in sink_datasets)}. "
            f"Serving-domain modules: {serving_citations}."
        )

        # Q3: Blast radius
        critical_module = top_modules[0] if top_modules else None
        if critical_module:
            path, mod = critical_module
            imported_by_count = len(mod.imported_by) if hasattr(mod, "imported_by") else 0
            self.day_one_answers["Q3"] = (
                f"The most architecturally significant module is `{path}` "
                f"(PageRank={mod.pagerank_score:.4f}, LOC={mod.lines_of_code}). "
                f"It is a high-impact failure point that anchors the dependency graph. "
                f"Top 5 hubs: " +
                ", ".join(
                    f"`{p}` ({m.pagerank_score:.4f})"
                    for p, m in top_modules
                ) + "."
            )
        else:
            self.day_one_answers["Q3"] = "Unable to determine — no modules analyzed."

        # Q4: Business logic location
        domain_counts: dict[str, int] = {}
        domain_examples: dict[str, list[str]] = {}
        for p, m in modules.items():
            d = m.domain_cluster.value
            domain_counts[d] = domain_counts.get(d, 0) + 1
            if d not in domain_examples:
                domain_examples[d] = []
            if len(domain_examples[d]) < 2:
                domain_examples[d].append(p)
        top_domain = max(domain_counts.items(), key=lambda x: x[1]) if domain_counts else ("unknown", 0)
        top_examples = domain_examples.get(top_domain[0], [])[:2]
        self.day_one_answers["Q4"] = (
            f"Business logic is primarily concentrated in the '{top_domain[0]}' domain "
            f"({top_domain[1]} modules), e.g. `{'`, `'.join(top_examples)}`. "
            f"Domain distribution: {', '.join(f'{d}={c}' for d, c in sorted(domain_counts.items(), key=lambda x: -x[1])[:5])}."
        )

        # Q5: Change velocity
        if velocity:
            top_files = velocity[:5]
            self.day_one_answers["Q5"] = (
                f"Highest velocity files (last 30 days): "
                + ", ".join(
                    f"`{v.path}` ({v.commit_count_30d} commits)"
                    for v in top_files
                )
                + "."
            )
        else:
            self.day_one_answers["Q5"] = "No git velocity data available."



    def _answer_questions_llm(self, questions: list[str], context: str):
        """Answer Day-One questions using synthesis LLM with file:line citations."""
        for question in questions:
            q_key = question[:2]  # "Q1", "Q2", etc.

            prompt = (
                "You are an FDE (Forward-Deployed Engineer) analyzing a codebase "
                "on your first day. Based on the analysis data below, answer this question "
                "with specific evidence. IMPORTANT: cite specific file paths and line numbers "
                "(e.g. `src/etl/ingest.py:42`) where possible.\n\n"
                f"Question: {question}\n\n"
                f"Analysis Data:\n{context}\n\n"
                "Answer (cite specific files, line numbers, and metrics):"
            )

            # Use SYNTHESIS_MODEL (most capable) for Day-One answers
            response = self._call_llm(prompt, task=f"day_one:{q_key}", model=self.SYNTHESIS_MODEL)
            if response:
                self.day_one_answers[q_key] = response.strip()
            else:
                self.day_one_answers[q_key] = f"Unable to synthesize answer for {question}."

    # LLM Helper

    def _call_llm(self, prompt: str, task: str, model: str | None = None) -> str:
        """Call Ollama /api/generate with budget tracking and error handling."""
        if self.skip_llm:
            return ""

        model_name = model or self.BULK_MODEL
        input_tokens = self.budget.estimate_tokens(prompt)

        if not self.budget.can_afford(input_tokens + 500):
            logger.warning(f"Semanticist: Budget exhausted for task {task}")
            return ""

        payload = json.dumps({
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        }).encode()

        try:
            req = urllib.request.Request(
                f"{self.ollama_host}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())

            output_text = result.get("response", "")
            output_tokens = self.budget.estimate_tokens(output_text)
            self.budget.record_usage(input_tokens, output_tokens, model_name, task)
            return output_text

        except urllib.error.URLError as e:
            logger.warning(f"Semanticist: Ollama request failed for {task}: {e}")
            self.budget.record_usage(input_tokens, 0, model_name, f"FAILED:{task}")
            return ""
        except Exception as e:
            logger.warning(f"Semanticist: LLM call failed for {task}: {e}")
            self.budget.record_usage(input_tokens, 0, model_name, f"FAILED:{task}")
            return ""
