"""Orchestrator - Wires agents in sequence and manages the analysis pipeline.

Pipeline: Surveyor -> Hydrologist -> Semanticist -> Archivist
          Serializes all outputs to .cartography/
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.agents.hydrologist import HydrologistAgent
from src.agents.surveyor import SurveyorAgent
from src.graph.knowledge_graph import KnowledgeGraph

logger = logging.getLogger(__name__)
console = Console()


class Orchestrator:
    """Manages the codebase analysis pipeline."""

    def __init__(self, target_path: str, output_dir: str | None = None,
                 skip_llm: bool = False, incremental: bool = False):
        self.target_path = self._resolve_target(target_path)
        self.output_dir = Path(output_dir) if output_dir else self.target_path / ".cartography"
        self.knowledge_graph = KnowledgeGraph()
        self.trace_log: list[dict] = []
        self._start_time: float = 0
        self.skip_llm = skip_llm
        self.incremental = incremental

    def _resolve_target(self, target: str) -> Path:
        """Resolve target to a local path, cloning if GitHub URL."""
        if target.startswith("http://") or target.startswith("https://"):
            return self._clone_repo(target)
        path = Path(target).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Target path does not exist: {target}")
        return path

    def _clone_repo(self, url: str) -> Path:
        """Clone a GitHub repository to a temporary directory."""
        repo_name = url.rstrip("/").split("/")[-1].replace(".git", "")
        clone_dir = Path(tempfile.mkdtemp()) / repo_name

        console.print(f"[bold blue]Cloning repository:[/] {url}")
        self._log_action("clone_repo", {"url": url, "target": str(clone_dir)})

        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", url, str(clone_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Git clone failed: {result.stderr}")
            console.print(f"[green]Cloned to:[/] {clone_dir}")
            return clone_dir
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Git clone timed out for {url}")

    def _get_changed_files(self) -> set[str] | None:
        """Get files changed since last analysis run (for incremental mode)."""
        if not self.incremental:
            return None

        trace_path = self.output_dir / "cartography_trace.jsonl"
        if not trace_path.exists():
            logger.info("No previous trace found. Running full analysis.")
            return None

        # Find the last analysis timestamp
        last_timestamp = None
        try:
            with open(trace_path) as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get("action") == "pipeline_complete":
                        last_timestamp = entry.get("timestamp")
        except Exception:
            return None

        if not last_timestamp:
            return None

        try:
            result = subprocess.run(
                ["git", "log", f"--since={last_timestamp}", "--name-only", "--pretty=format:"],
                cwd=str(self.target_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                changed = {f.strip() for f in result.stdout.splitlines() if f.strip()}
                if changed:
                    console.print(
                        f"[bold cyan]Incremental mode:[/] {len(changed)} files changed since last run"
                    )
                    return changed
                else:
                    console.print("[green]Incremental mode: No changes since last run.[/]")
                    return set()
        except Exception:
            pass

        return None

    def run(self) -> dict:
        """Execute the full analysis pipeline."""
        self._start_time = time.time()

        console.print(f"\n[bold cyan]{'='*60}[/]")
        console.print(f"[bold cyan]  The Brownfield Cartographer - Codebase Analysis[/]")
        console.print(f"[bold cyan]{'='*60}[/]")
        console.print(f"[bold]Target:[/] {self.target_path}")
        console.print(f"[bold]Output:[/] {self.output_dir}")
        if self.skip_llm:
            console.print("[yellow]LLM analysis disabled (--skip-llm)[/]")
        if self.incremental:
            console.print("[cyan]Incremental mode enabled[/]")
        console.print()

        self._log_action("pipeline_start", {
            "target": str(self.target_path),
            "output": str(self.output_dir),
            "timestamp": datetime.now().isoformat(),
            "skip_llm": self.skip_llm,
            "incremental": self.incremental,
        })

        # Check for incremental mode
        changed_files = self._get_changed_files()
        if changed_files is not None and len(changed_files) == 0:
            console.print("[green]No changes detected. Skipping re-analysis.[/]")
            return {"summary": self.knowledge_graph.get_summary(), "skipped": True}

        # Phase 1: Surveyor
        console.print("[bold yellow]Phase 1: The Surveyor (Static Structure Analysis)[/]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Analyzing module structure...", total=None)
            surveyor = SurveyorAgent(self.target_path)
            surveyor_results = surveyor.run()
            progress.update(task, description="[green]Module structure analysis complete!")

        self._log_action("surveyor_complete", {
            "modules_found": len(surveyor_results["modules"]),
            "edges_found": len(surveyor_results["edges"]),
            "circular_deps": len(surveyor_results["circular_dependencies"]),
            "dead_code": len(surveyor_results["dead_code_candidates"]),
        })

        self._print_surveyor_summary(surveyor_results, surveyor)

        # Phase 2: Hydrologist
        console.print("\n[bold yellow]Phase 2: The Hydrologist (Data Flow & Lineage Analysis)[/]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Analyzing data lineage...", total=None)
            hydrologist = HydrologistAgent(self.target_path)
            hydrologist_results = hydrologist.run(surveyor.file_analyses)
            progress.update(task, description="[green]Data lineage analysis complete!")

        self._log_action("hydrologist_complete", {
            "datasets_found": len(hydrologist_results["datasets"]),
            "transformations_found": len(hydrologist_results["transformations"]),
            "sources": hydrologist_results["sources"],
            "sinks": hydrologist_results["sinks"],
        })

        self._print_hydrologist_summary(hydrologist_results)

        # Phase 3: Semanticist
        console.print("\n[bold yellow]Phase 3: The Semanticist (Semantic Analysis)[/]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Generating purpose statements...", total=None)
            from src.agents.semanticist import SemanticistAgent
            semanticist = SemanticistAgent(self.target_path, skip_llm=self.skip_llm)
            semanticist_results = semanticist.run(
                surveyor_results["modules"],
                surveyor_results,
                hydrologist_results,
            )
            progress.update(task, description="[green]Semantic analysis complete!")

        self._log_action("semanticist_complete", {
            "purpose_statements": len(semanticist_results["purpose_statements"]),
            "doc_drift_flags": len(semanticist_results["doc_drift_flags"]),
            "budget": semanticist_results.get("budget_summary", {}),
        })

        self._print_semanticist_summary(semanticist_results)

        # Merge into knowledge graph
        console.print("\n[bold yellow]Merging results into Knowledge Graph...[/]")
        self.knowledge_graph.merge_surveyor_results(surveyor_results)
        self.knowledge_graph.merge_hydrologist_results(hydrologist_results)

        # Serialize outputs
        console.print("[bold yellow]Serializing outputs...[/]")
        self._serialize_outputs()

        # Phase 4: Archivist
        console.print("\n[bold yellow]Phase 4: The Archivist (Generating Artifacts)[/]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Generating CODEBASE.md and onboarding brief...", total=None)
            from src.agents.archivist import ArchivistAgent
            archivist = ArchivistAgent(self.target_path, self.output_dir)
            archivist_results = archivist.run(
                surveyor_results["modules"],
                surveyor_results,
                hydrologist_results,
                semanticist_results,
            )
            progress.update(task, description="[green]Artifacts generated!")

        self._log_action("archivist_complete", {
            "codebase_md_length": archivist_results.get("codebase_md_length", 0),
            "onboarding_brief_length": archivist_results.get("onboarding_brief_length", 0),
        })

        # Save trace log (includes all phases)
        self._save_trace_log()

        elapsed = time.time() - self._start_time
        self._log_action("pipeline_complete", {
            "elapsed_seconds": elapsed,
            "timestamp": datetime.now().isoformat(),
        })
        self._save_trace_log()  # Save again with pipeline_complete

        summary = self.knowledge_graph.get_summary()
        console.print(f"\n[bold green]{'='*60}[/]")
        console.print(f"[bold green]  Analysis Complete ({elapsed:.1f}s)[/]")
        console.print(f"[bold green]{'='*60}[/]")
        console.print(f"  Modules: {summary['modules']}")
        console.print(f"  Functions: {summary['functions']}")
        console.print(f"  Datasets: {summary['datasets']}")
        console.print(f"  Transformations: {summary['transformations']}")
        console.print(f"  Import edges: {summary['import_edges']}")
        console.print(f"  Lineage edges: {summary['lineage_edges']}")
        console.print(f"  Languages: {summary['languages']}")
        console.print(f"  Purpose statements: {len(semanticist_results['purpose_statements'])}")
        console.print(f"  Doc drift flags: {len(semanticist_results['doc_drift_flags'])}")
        console.print(f"\n  Outputs saved to: [bold]{self.output_dir}[/]")
        console.print(f"  - CODEBASE.md")
        console.print(f"  - onboarding_brief.md")
        console.print(f"  - module_graph.json")
        console.print(f"  - lineage_graph.json")
        console.print(f"  - cartography_trace.jsonl\n")

        return {
            "summary": summary,
            "surveyor": surveyor_results,
            "hydrologist": hydrologist_results,
            "semanticist": semanticist_results,
            "archivist": archivist_results,
            "elapsed_seconds": elapsed,
        }

    def _print_surveyor_summary(self, results: dict, surveyor: SurveyorAgent):
        """Print a summary of Surveyor findings."""
        modules = results["modules"]
        console.print(f"  [green]✓[/] Analyzed [bold]{len(modules)}[/] modules")

        lang_counts: dict[str, int] = {}
        for m in modules.values():
            lang = m.language.value
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
            console.print(f"    {lang}: {count} files")

        top_modules = surveyor.get_top_modules_by_pagerank(5)
        if top_modules:
            console.print("  [bold]Top modules by PageRank:[/]")
            for path, score in top_modules:
                console.print(f"    {score:.4f}  {path}")

        circular = results["circular_dependencies"]
        if circular:
            console.print(f"  [yellow]⚠ {len(circular)} circular dependencies detected[/]")

        dead = results["dead_code_candidates"]
        if dead:
            console.print(f"  [yellow]⚠ {len(dead)} dead code candidates[/]")

        high_vel = surveyor.get_high_velocity_files(5)
        if high_vel:
            console.print("  [bold]Highest velocity files (30d):[/]")
            for path, count in high_vel:
                console.print(f"    {count} commits  {path}")

        hot_files = results.get("high_velocity_80_20", [])
        if hot_files:
            console.print(
                f"  [bold]80/20 Velocity:[/] {len(hot_files)} files account for 80%+ of commits"
            )

    def _print_hydrologist_summary(self, results: dict):
        """Print a summary of Hydrologist findings."""
        console.print(f"  [green]✓[/] Found [bold]{len(results['datasets'])}[/] datasets")
        console.print(f"  [green]✓[/] Found [bold]{len(results['transformations'])}[/] transformations")

        sources = results["sources"]
        sinks = results["sinks"]
        if sources:
            console.print(f"  [bold]Data sources ({len(sources)}):[/]")
            for s in sources[:10]:
                console.print(f"    → {s}")

        if sinks:
            console.print(f"  [bold]Data sinks ({len(sinks)}):[/]")
            for s in sinks[:10]:
                console.print(f"    ← {s}")

    def _print_semanticist_summary(self, results: dict):
        """Print a summary of Semanticist findings."""
        console.print(
            f"  [green]✓[/] Generated [bold]{len(results['purpose_statements'])}[/] purpose statements"
        )
        if results["doc_drift_flags"]:
            console.print(
                f"  [yellow]⚠ {len(results['doc_drift_flags'])} documentation drift flags[/]"
            )
        budget = results.get("budget_summary", {})
        if budget.get("total_calls", 0) > 0:
            console.print(
                f"  [dim]LLM: {budget['total_calls']} calls, "
                f"{budget['total_tokens_used']:,} tokens, "
                f"${budget['total_cost_usd']:.4f}[/]"
            )

    def _serialize_outputs(self):
        """Save analysis outputs to the .cartography/ directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_graph.save_to_directory(self.output_dir)

    def _save_trace_log(self):
        """Save trace log to JSONL file."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        trace_path = self.output_dir / "cartography_trace.jsonl"
        with open(trace_path, "w") as f:
            for entry in self.trace_log:
                f.write(json.dumps(entry, default=str) + "\n")
        logger.info(f"Saved trace log to {trace_path}")

    def _log_action(self, action: str, details: dict):
        """Log an action to the trace log."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "details": details,
        }
        self.trace_log.append(entry)
        logger.debug(f"TRACE: {action} - {details}")
