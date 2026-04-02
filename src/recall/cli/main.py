from __future__ import annotations

from pathlib import Path

import typer

from recall import __version__
from recall.cli.artifacts import (
    download_bluebubbles_artifact_bytes,
    download_slack_artifact_bytes,
    show_artifacts,
)
from recall.cli.events import show_events
from recall.config import resolve_root
from recall.connectors.bluebubbles.cli import (
    export_bluebubbles_history_bundle,
    import_bluebubbles_export_bundle,
    normalize_bluebubbles,
    serve_bluebubbles,
    sync_bluebubbles_entities,
)
from recall.connectors.slack.cli import (
    capture_slack,
    normalize_slack,
    show_slack_state,
    sync_slack_entities,
)
from recall.connectors.telegram.cli import (
    append_telegram,
    capture_telegram_once,
    normalize_telegram,
    run_telegram_capture,
    show_telegram_state,
    sync_telegram_entities,
)
from recall.storage.db import initialize_database
from recall.storage.paths import RecallPaths

app = typer.Typer(help="Recall command-line interface.", no_args_is_help=True)
capture_app = typer.Typer(help="Capture raw evidence from source systems.")
normalize_app = typer.Typer(help="Normalize captured evidence into daily events.")
events_app = typer.Typer(help="Inspect normalized events.")
artifacts_app = typer.Typer(help="Inspect normalized artifact metadata.")
artifacts_download_app = typer.Typer(help="Download source-native artifact bytes.")
entities_app = typer.Typer(help="Manage identity and entity storage.")
entities_sync_app = typer.Typer(help="Sync source identities into SQLite.")
state_app = typer.Typer(help="Inspect operational connector state.")
state_show_app = typer.Typer(help="Show stored connector state.")
bluebubbles_capture_app = typer.Typer(help="BlueBubbles capture commands.")
telegram_capture_app = typer.Typer(help="Telegram capture commands.")
import_app = typer.Typer(help="Import historical export bundles.")
export_app = typer.Typer(help="Export source-native history bundles.")

app.add_typer(capture_app, name="capture")
app.add_typer(normalize_app, name="normalize")
app.add_typer(events_app, name="events")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(entities_app, name="entities")
app.add_typer(state_app, name="state")
app.add_typer(import_app, name="import")
app.add_typer(export_app, name="export")
entities_app.add_typer(entities_sync_app, name="sync")
state_app.add_typer(state_show_app, name="show")
artifacts_app.add_typer(artifacts_download_app, name="download")
capture_app.add_typer(bluebubbles_capture_app, name="bluebubbles")
capture_app.add_typer(telegram_capture_app, name="telegram")


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
    typer.echo(f"artifacts={current.artifacts}")
    typer.echo(f"entities={current.entities}")
    typer.echo(f"derived={current.derived}")
    typer.echo(f"state={current.state}")
    typer.echo(f"database={current.database}")


@app.command()
def version() -> None:
    """Print the current Recall version."""

    typer.echo(__version__)


capture_app.command("slack")(capture_slack)
bluebubbles_capture_app.command("serve")(serve_bluebubbles)
telegram_capture_app.command("append")(append_telegram)
telegram_capture_app.command("once")(capture_telegram_once)
telegram_capture_app.command("run")(run_telegram_capture)
normalize_app.command("slack")(normalize_slack)
normalize_app.command("bluebubbles")(normalize_bluebubbles)
normalize_app.command("telegram")(normalize_telegram)
events_app.command("show")(show_events)
artifacts_app.command("show")(show_artifacts)
artifacts_download_app.command("slack")(download_slack_artifact_bytes)
artifacts_download_app.command("bluebubbles")(download_bluebubbles_artifact_bytes)
entities_sync_app.command("slack")(sync_slack_entities)
entities_sync_app.command("bluebubbles")(sync_bluebubbles_entities)
entities_sync_app.command("telegram")(sync_telegram_entities)
state_show_app.command("slack")(show_slack_state)
state_show_app.command("telegram")(show_telegram_state)
import_app.command("bluebubbles-export")(import_bluebubbles_export_bundle)
export_app.command("bluebubbles-history")(export_bluebubbles_history_bundle)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
