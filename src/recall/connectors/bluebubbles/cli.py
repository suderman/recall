from __future__ import annotations

from pathlib import Path

import typer
import uvicorn

from recall.config import resolve_root
from recall.connectors.bluebubbles.config import bluebubbles_config_path, load_bluebubbles_config
from recall.connectors.bluebubbles.entities import (
    sync_bluebubbles_entities as sync_bluebubbles_entities_for_date,
)
from recall.connectors.bluebubbles.exporter import export_bluebubbles_history
from recall.connectors.bluebubbles.importer import import_bluebubbles_export
from recall.connectors.bluebubbles.normalize import normalize_bluebubbles_day
from recall.connectors.bluebubbles.webhook import create_bluebubbles_webhook_app
from recall.storage.paths import RecallPaths


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def serve_bluebubbles(
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Run the BlueBubbles webhook receiver."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_bluebubbles_config(paths)
    app = create_bluebubbles_webhook_app(paths, config)
    token_hint = f"?token={config.webhook_token}" if config.webhook_token else ""
    typer.echo("mode=webhook")
    typer.echo(f"bind={config.webhook_bind_host}:{config.webhook_port}")
    typer.echo(f"endpoint=/bluebubbles/webhook{token_hint}")
    typer.echo(
        "webhook_url_hint=use http://<recall-host-lan-ip>:"
        f"{config.webhook_port}/bluebubbles/webhook{token_hint} from the BlueBubbles server"
    )
    typer.echo(f"config={bluebubbles_config_path(paths)}")
    uvicorn.run(app, host=config.webhook_bind_host, port=config.webhook_port)


def normalize_bluebubbles(
    date: str = typer.Option(..., "--date", help="Date to normalize in YYYY-MM-DD format."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Normalize one day of BlueBubbles raw webhook data."""

    paths = _paths_for(root)
    paths.ensure_directories()
    event_path, artifact_path = normalize_bluebubbles_day(paths, date=date)
    typer.echo(f"Normalized BlueBubbles events for {date}")
    typer.echo(f"events={event_path}")
    typer.echo(f"artifacts={artifact_path}")
    typer.echo(
        f"next_step=run 'recall entities sync bluebubbles --date {date}' or "
        f"'recall artifacts show --date {date} --source bluebubbles'"
    )


def sync_bluebubbles_entities(
    date: str = typer.Option(
        ..., "--date", help="Date to sync identities from in YYYY-MM-DD format."
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Sync BlueBubbles identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_bluebubbles_entities_for_date(paths, date=date)
    typer.echo(f"Synced BlueBubbles entities for {date}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")


def import_bluebubbles_export_bundle(
    export_path: Path = typer.Argument(..., exists=True, resolve_path=True),
    account: str | None = typer.Option(None, "--account", help="Account label to record."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Import a BlueBubbles historical export bundle into raw storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_bluebubbles_config(paths)
    result = import_bluebubbles_export(
        paths,
        export_path=export_path,
        account=account or config.account,
    )
    typer.echo("mode=import")
    typer.echo(f"import_id={result.import_id}")
    typer.echo(f"import_dir={result.import_dir}")
    typer.echo(f"messages_imported={result.messages_imported}")
    typer.echo("dates_written=" + (",".join(result.dates_written) if result.dates_written else "-"))
    typer.echo(
        "next_step=run 'recall normalize bluebubbles --date YYYY-MM-DD' for each imported date"
    )


def export_bluebubbles_history_bundle(
    output_dir: Path = typer.Argument(..., resolve_path=True),
    from_date: str = typer.Option(..., "--from", help="Start date in YYYY-MM-DD format."),
    to_date: str = typer.Option(..., "--to", help="End date in YYYY-MM-DD format."),
    messages_db: Path = typer.Option(
        Path.home() / "Library/Messages/chat.db",
        "--messages-db",
        resolve_path=True,
        help="Path to the local macOS Messages database on the export machine.",
    ),
    export_id: str | None = typer.Option(None, "--export-id", help="Stable export identifier."),
) -> None:
    """Export historical BlueBubbles-compatible message history from the local Messages DB."""

    result = export_bluebubbles_history(
        messages_db=messages_db,
        output_dir=output_dir,
        from_date=from_date,
        to_date=to_date,
        export_id=export_id,
    )
    typer.echo("mode=export")
    typer.echo(f"messages_db={messages_db.expanduser().resolve()}")
    typer.echo(f"output_dir={result.output_dir}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo(f"messages={result.messages_path}")
    typer.echo(f"message_count={result.message_count}")
    typer.echo(
        "next_step=copy this export bundle to Recall and run "
        "'recall import bluebubbles-export <path>'"
    )
