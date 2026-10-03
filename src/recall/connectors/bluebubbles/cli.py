from __future__ import annotations

import os
from pathlib import Path

import typer
import uvicorn

from recall.config import resolve_root
from recall.connectors.bluebubbles.config import (
    BlueBubblesSourceConfig,
    bluebubbles_config_path,
    load_bluebubbles_config,
)
from recall.connectors.bluebubbles.diagnostics import safe_error
from recall.connectors.bluebubbles.entities import (
    sync_bluebubbles_entities as sync_bluebubbles_entities_for_date,
)
from recall.connectors.bluebubbles.exporter import export_bluebubbles_history
from recall.connectors.bluebubbles.importer import import_bluebubbles_export
from recall.connectors.bluebubbles.normalize import normalize_bluebubbles_day
from recall.connectors.bluebubbles.recovery import (
    DEFAULT_RECOVERY_HOURS,
    DEFAULT_RECOVERY_PAGE_SIZE,
    recover_bluebubbles_messages,
)
from recall.connectors.bluebubbles.webhook import create_bluebubbles_webhook_app
from recall.storage.paths import RecallPaths
from recall.storage.state import list_connector_cursors


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def serve_bluebubbles(
    skip_recovery: bool = typer.Option(
        False,
        "--skip-recovery",
        help="Skip startup recovery of recently missed BlueBubbles messages.",
    ),
    recover_hours: int = typer.Option(
        DEFAULT_RECOVERY_HOURS,
        "--recover-hours",
        min=1,
        help="Maximum lookback window for startup BlueBubbles recovery.",
    ),
    recovery_page_size: int = typer.Option(
        DEFAULT_RECOVERY_PAGE_SIZE,
        "--recovery-page-size",
        min=1,
        help="How many BlueBubbles messages to fetch per recovery page.",
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
    """Run the BlueBubbles webhook receiver."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_bluebubbles_config(paths)
    if not config.webhook_token:
        raise typer.BadParameter("Configure webhook_token before serving BlueBubbles webhooks")
    if skip_recovery:
        typer.echo("recovery_status=skipped")
        typer.echo("recovery_reason=disabled_by_flag")
    else:
        _run_startup_recovery(
            paths,
            recover_hours=recover_hours,
            recovery_page_size=recovery_page_size,
            config=config,
        )
    app = create_bluebubbles_webhook_app(paths, config)
    token_hint = "?token=<configured-token>"
    typer.echo("mode=webhook")
    typer.echo(f"bind={config.webhook_bind_host}:{config.webhook_port}")
    typer.echo(f"endpoint=/bluebubbles/webhook{token_hint}")
    typer.echo(
        "webhook_url_hint=use http://<recall-host-lan-ip>:"
        f"{config.webhook_port}/bluebubbles/webhook{token_hint} from the BlueBubbles server"
    )
    typer.echo(f"config={bluebubbles_config_path(paths)}")
    # Query-string authentication must not reach Uvicorn access logs.
    uvicorn.run(app, host=config.webhook_bind_host, port=config.webhook_port, access_log=False)


def _run_startup_recovery(
    paths: RecallPaths,
    *,
    recover_hours: int,
    recovery_page_size: int,
    config: BlueBubblesSourceConfig,
) -> None:
    if not config.server_url:
        typer.echo("recovery_status=skipped")
        typer.echo("recovery_reason=missing_server_url")
        return

    password = os.getenv(config.password_env_var)
    if not password:
        typer.echo("recovery_status=skipped")
        typer.echo(f"recovery_reason=missing_env:{config.password_env_var}")
        return

    try:
        result = recover_bluebubbles_messages(
            paths,
            account=config.account,
            server_url=config.server_url,
            password=password,
            recover_hours=recover_hours,
            page_size=recovery_page_size,
        )
    except Exception as exc:
        typer.echo("recovery_status=failed")
        typer.echo(f"recovery_error={safe_error(exc)}")
        return

    typer.echo("recovery_status=ok")
    typer.echo(f"recovery_window_start={result.window_start}")
    typer.echo(f"recovery_window_end={result.window_end}")
    typer.echo(f"recovered_messages={result.recovered_messages}")
    typer.echo(f"recovery_skipped_existing={result.skipped_existing}")
    typer.echo(
        "recovery_dates_written="
        + (",".join(result.dates_written) if result.dates_written else "-")
    )
    typer.echo(f"recovery_cursor_before={result.cursor_before or '-'}")
    typer.echo(f"recovery_cursor_after={result.cursor_after or '-'}")


def recover_bluebubbles(
    account: str | None = typer.Option(None, "--account", help="Account label to record."),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Explicit ISO-8601 lower bound for recovery instead of using the stored cursor.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Optional ISO-8601 upper bound for recovery instead of now.",
    ),
    recover_hours: int = typer.Option(
        DEFAULT_RECOVERY_HOURS,
        "--recover-hours",
        min=1,
        help="Maximum lookback window when using the stored cursor or no explicit --since.",
    ),
    page_size: int = typer.Option(
        DEFAULT_RECOVERY_PAGE_SIZE,
        "--page-size",
        min=1,
        help="How many BlueBubbles messages to fetch per API page.",
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
    """Recover recently missed BlueBubbles messages via the REST API."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_bluebubbles_config(paths)
    resolved_account = account or config.account
    if not config.server_url:
        raise typer.BadParameter(
            f"Missing BlueBubbles server_url in {bluebubbles_config_path(paths)}."
        )

    password = os.getenv(config.password_env_var)
    if not password:
        raise typer.BadParameter(
            f"Missing BlueBubbles password in environment variable {config.password_env_var}. "
            f"See {bluebubbles_config_path(paths)} or config/sources/bluebubbles.toml.example."
        )

    result = recover_bluebubbles_messages(
        paths,
        account=resolved_account,
        server_url=config.server_url,
        password=password,
        since=since,
        until=until,
        recover_hours=recover_hours,
        page_size=page_size,
    )
    typer.echo("mode=recover")
    typer.echo(f"account={resolved_account}")
    typer.echo(f"window_start={result.window_start}")
    typer.echo(f"window_end={result.window_end}")
    typer.echo(f"cursor_before={result.cursor_before or '-'}")
    typer.echo(f"cursor_after={result.cursor_after or '-'}")
    typer.echo(f"recovered_messages={result.recovered_messages}")
    typer.echo(f"skipped_existing={result.skipped_existing}")
    typer.echo(f"pages_fetched={result.pages_fetched}")
    typer.echo("dates_written=" + (",".join(result.dates_written) if result.dates_written else "-"))
    typer.echo(
        "next_step=run 'recall normalize bluebubbles --date YYYY-MM-DD' for each affected date"
    )


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


def show_bluebubbles_state(
    account: str | None = typer.Option(
        None, "--account", help="BlueBubbles account label to inspect."
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
    """Show stored BlueBubbles cursor state for one account."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_bluebubbles_config(paths)
    resolved_account = account or config.account
    cursors = list_connector_cursors(paths, source="bluebubbles", account=resolved_account)

    typer.echo("source=bluebubbles")
    typer.echo(f"account={resolved_account}")
    if not cursors:
        typer.echo("cursor_state=empty")
        return

    for cursor in cursors:
        typer.echo(f"cursor_key={cursor.cursor_key}")
        typer.echo(f"cursor_value={cursor.cursor_value}")
        typer.echo(f"updated_at={cursor.updated_at}")


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
    include_attachment_bytes: bool = typer.Option(
        False,
        "--include-attachment-bytes",
        help="Copy locally available attachment files into the export bundle.",
    ),
) -> None:
    """Export historical BlueBubbles-compatible message history from the local Messages DB."""

    result = export_bluebubbles_history(
        messages_db=messages_db,
        output_dir=output_dir,
        from_date=from_date,
        to_date=to_date,
        export_id=export_id,
        include_attachment_bytes=include_attachment_bytes,
    )
    typer.echo("mode=export")
    typer.echo(f"messages_db={messages_db.expanduser().resolve()}")
    typer.echo(f"output_dir={result.output_dir}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo(f"messages={result.messages_path}")
    typer.echo(f"message_count={result.message_count}")
    typer.echo(f"include_attachment_bytes={str(include_attachment_bytes).lower()}")
    typer.echo(
        "next_step=copy this export bundle to Recall and run "
        "'recall import bluebubbles-export <path>'"
    )
