from __future__ import annotations

from pathlib import Path

import typer

from recall import __version__
from recall.cli.events import show_events
from recall.config import resolve_root
from recall.connectors.slack.cli import capture_slack, normalize_slack
from recall.storage.db import initialize_database
from recall.storage.paths import RecallPaths

app = typer.Typer(help="Recall command-line interface.", no_args_is_help=True)
capture_app = typer.Typer(help="Capture raw evidence from source systems.")
normalize_app = typer.Typer(help="Normalize captured evidence into daily events.")
events_app = typer.Typer(help="Inspect normalized events.")

app.add_typer(capture_app, name="capture")
app.add_typer(normalize_app, name="normalize")
app.add_typer(events_app, name="events")


def _load_paths(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


@app.command()
def init(
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to initialize.",
    ),
) -> None:
    """Create the initial Recall directory layout and SQLite database."""

    paths = _load_paths(root)
    paths.ensure_directories()
    initialize_database(paths)

    typer.echo(f"Initialized Recall workspace at {paths.root}")
    typer.echo(f"SQLite state database: {paths.database}")
    typer.echo(f"Normalized event store: {paths.normalized}")


@app.command()
def paths(
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to inspect.",
    ),
) -> None:
    """Print the current workspace paths."""

    current = _load_paths(root)
    typer.echo(f"root={current.root}")
    typer.echo(f"config={current.config}")
    typer.echo(f"sources_config={current.sources_config}")
    typer.echo(f"raw={current.raw}")
    typer.echo(f"normalized={current.normalized}")
    typer.echo(f"entities={current.entities}")
    typer.echo(f"derived={current.derived}")
    typer.echo(f"state={current.state}")
    typer.echo(f"database={current.database}")


@app.command()
def version() -> None:
    """Print the current Recall version."""

    typer.echo(__version__)


capture_app.command("slack")(capture_slack)
normalize_app.command("slack")(normalize_slack)
events_app.command("show")(show_events)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
