"""CLI entry point for The Brownfield Cartographer.

Usage:
    cartographer analyze <repo_path_or_url> [--output <dir>]
"""

from __future__ import annotations

import logging
import sys

import click
from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(verbose: bool = False):
    """Configure logging with Rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=False, show_path=False)],
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool):
    """The Brownfield Cartographer - Codebase Intelligence System.

    Build a living, queryable knowledge graph of any production codebase.
    """
    setup_logging(verbose)


@cli.command()
@click.argument("target", type=str)
@click.option(
    "--output", "-o",
    type=click.Path(),
    default=None,
    help="Output directory for cartography artifacts (default: <target>/.cartography/)",
)
def analyze(target: str, output: str | None):
    """Analyze a codebase and generate the knowledge graph.

    TARGET can be a local path or a GitHub URL.

    Examples:
        cartographer analyze ./my-project
        cartographer analyze https://github.com/user/repo
        cartographer analyze /path/to/codebase --output ./results
    """
    from src.orchestrator import Orchestrator

    try:
        orchestrator = Orchestrator(target_path=target, output_dir=output)
        results = orchestrator.run()
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/] {e}")
        sys.exit(1)
    except RuntimeError as e:
        console.print(f"[red]Error:[/] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Analysis interrupted.[/]")
        sys.exit(130)


@cli.command()
@click.argument("target", type=str)
@click.argument("node", type=str)
def blast_radius(target: str, node: str):
    """Show the blast radius of a module or dataset.

    Shows all downstream dependencies that would be affected if the
    specified node changes.

    TARGET is the path to a previously analyzed codebase.
    NODE is the module path or dataset name to analyze.
    """
    import json
    from pathlib import Path

    cartography_dir = Path(target) / ".cartography"
    lineage_path = cartography_dir / "lineage_graph.json"

    if not lineage_path.exists():
        console.print(f"[red]Error:[/] No lineage graph found. Run 'analyze' first.")
        sys.exit(1)

    from src.agents.hydrologist import HydrologistAgent

    # Load the lineage graph and run blast_radius
    with open(lineage_path) as f:
        data = json.load(f)

    from networkx.readwrite import json_graph
    import networkx as nx

    graph = json_graph.node_link_graph(data["graph"])
    hydrologist = HydrologistAgent(Path(target))
    hydrologist.lineage_graph = graph

    result = hydrologist.blast_radius(node)

    if "error" in result:
        console.print(f"[red]{result['error']}[/]")
        sys.exit(1)

    console.print(f"\n[bold]Blast Radius for:[/] {result.get('root', node)}")
    console.print(f"[bold]Depth:[/] {result['depth']}")
    console.print(f"[bold]Affected nodes ({len(result['affected_nodes'])}):[/]")
    for n in result["affected_nodes"]:
        console.print(f"  → {n}")


@cli.command()
@click.argument("target", type=str)
def summary(target: str):
    """Show a summary of a previously analyzed codebase."""
    import json
    from pathlib import Path

    cartography_dir = Path(target) / ".cartography"

    for fname in ("module_graph.json", "lineage_graph.json"):
        fpath = cartography_dir / fname
        if fpath.exists():
            with open(fpath) as f:
                data = json.load(f)
            stats = data.get("statistics", {})
            console.print(f"\n[bold]{fname}:[/]")
            for k, v in stats.items():
                console.print(f"  {k}: {v}")
        else:
            console.print(f"[yellow]{fname} not found[/]")


if __name__ == "__main__":
    cli()
