from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import typer

from recall.config import resolve_root
from recall.connectors.telegram.capture import (
    append_telegram_update,
    capture_telegram_updates,
    load_update_payload,
)
from recall.connectors.telegram.client import FileTelegramClient
from recall.connectors.telegram.config import load_telegram_config, resolve_tdlib_state_dir
from recall.connectors.telegram.drain import PendingTelegramClient
from recall.connectors.telegram.entities import (
    sync_telegram_entities as sync_telegram_entities_for_date,
)
from recall.connectors.telegram.importer import import_telegram_export
from recall.connectors.telegram.normalize import normalize_telegram_day
from recall.connectors.telegram.tdlib import (
    TdlibJsonTransport,
    TdlibTelegramClient,
    build_tdlib_auth_settings,
)
from recall.storage.paths import RecallPaths
from recall.storage.state import get_connector_cursor, list_connector_cursors
from recall.voice_corpus import after_publication

DEFAULT_DAEMON_POLL_SECONDS = 5.0
DEFAULT_DAEMON_IDLE_SLEEP_SECONDS = 2.0


def _tdlib_log_verbosity_option() -> int | None:
    return typer.Option(
        None,
        "--tdlib-log-verbosity-level",
        min=0,
        help="Override the configured TDLib log verbosity for this run.",
    )


def _paths_for(root: Path | None) -> RecallPaths:
    return RecallPaths.from_root(resolve_root(root))


def _capture_updates_from_file(
    *,
    updates_file: Path,
    account: str,
    root: Path | None,
    after_update_id: int | None,
    limit: int | None,
    mode: str,
) -> None:
    paths = _paths_for(root)
    paths.ensure_directories()
    client = FileTelegramClient.from_path(updates_file)
    result = capture_telegram_updates(
        paths,
        client=client,
        account=account,
        after_update_id=after_update_id,
        limit=limit,
        capture_mode=mode,
    )
    typer.echo(f"mode={mode}")
    typer.echo(f"account={account}")
    typer.echo(f"updates_file={updates_file}")
    typer.echo(f"after_update_id={after_update_id if after_update_id is not None else '-'}")
    typer.echo(f"captured_updates={result.captured_updates}")
    typer.echo("dates_written=" + (",".join(result.dates_written) if result.dates_written else "-"))
    typer.echo(
        f"last_update_id={result.last_update_id if result.last_update_id is not None else '-'}"
    )
    if result.cursor is not None:
        typer.echo(f"cursor_key={result.cursor.cursor_key}")
        typer.echo(f"cursor_value={result.cursor.cursor_value}")
    typer.echo("next_step=run 'recall normalize telegram --date YYYY-MM-DD'")


def _capture_updates_from_tdlib(
    *,
    account: str,
    root: Path | None,
    after_update_id: int | None,
    limit: int | None,
    mode: str,
    tdlib_log_verbosity_level: int | None,
):
    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)
    after_update_id = _resolve_after_update_id(
        paths, account=account, after_update_id=after_update_id
    )
    settings = build_tdlib_auth_settings(paths, config, account=account)
    if tdlib_log_verbosity_level is not None:
        settings = replace(settings, log_verbosity_level=tdlib_log_verbosity_level)
    transport = TdlibJsonTransport(
        library_path=settings.library_path,
        log_verbosity_level=settings.log_verbosity_level,
    )
    client = TdlibTelegramClient(transport=transport, settings=settings)
    try:
        result = capture_telegram_updates(
            paths,
            client=client,
            account=account,
            after_update_id=after_update_id,
            limit=limit,
            capture_mode=mode,
        )
    finally:
        client.close()

    typer.echo(f"mode={mode}")
    typer.echo(f"account={account}")
    typer.echo("transport=tdlib")
    typer.echo(f"after_update_id={after_update_id if after_update_id is not None else '-'}")
    typer.echo(f"captured_updates={result.captured_updates}")
    typer.echo("dates_written=" + (",".join(result.dates_written) if result.dates_written else "-"))
    typer.echo(
        f"last_update_id={result.last_update_id if result.last_update_id is not None else '-'}"
    )
    if result.cursor is not None:
        typer.echo(f"cursor_key={result.cursor.cursor_key}")
        typer.echo(f"cursor_value={result.cursor.cursor_value}")
    typer.echo("next_step=run 'recall normalize telegram --date YYYY-MM-DD'")
    return result


def _resolve_after_update_id(
    paths: RecallPaths,
    *,
    account: str,
    after_update_id: int | None,
) -> int | None:
    if after_update_id is not None:
        return after_update_id

    cursor = get_connector_cursor(
        paths,
        source="telegram",
        account=account,
        cursor_key="last_update_id",
    )
    if cursor is None:
        return None
    return int(cursor.cursor_value)


def append_telegram(
    payload_path: Path = typer.Argument(..., exists=True, resolve_path=True),
    update_type: str = typer.Option(..., "--update-type", help="Raw Telegram update type."),
    update_id: int | None = typer.Option(None, "--update-id", help="TDLib update id to store."),
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    received_at: str | None = typer.Option(
        None,
        "--received-at",
        help="Override the receive timestamp with an ISO-8601 value.",
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
    """Append one Telegram raw update envelope from a JSON file."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    payload = load_update_payload(payload_path)
    result = append_telegram_update(
        paths,
        account=resolved_account,
        payload=payload,
        update_type=update_type,
        update_id=update_id,
        received_at=received_at,
        capture_mode="manual",
    )
    typer.echo("mode=append")
    typer.echo(f"account={resolved_account}")
    typer.echo(f"payload={payload_path}")
    typer.echo(f"raw_dir={result.raw_dir}")
    typer.echo(f"updates={result.updates_path}")
    if result.cursor is not None:
        typer.echo(f"cursor_key={result.cursor.cursor_key}")
        typer.echo(f"cursor_value={result.cursor.cursor_value}")
    typer.echo(f"next_step=run 'recall normalize telegram --date {result.date}'")


def capture_telegram_once(
    updates_file: Path = typer.Argument(..., exists=True, resolve_path=True),
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    after_update_id: int | None = typer.Option(
        None,
        "--after-update-id",
        help="Only capture updates newer than this id.",
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
    """Capture one Telegram update from a file-backed update stream."""

    paths = _paths_for(root)
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    _capture_updates_from_file(
        updates_file=updates_file,
        account=resolved_account,
        root=root,
        after_update_id=after_update_id,
        limit=1,
        mode="once",
    )


def run_telegram_capture(
    updates_file: Path = typer.Argument(..., exists=True, resolve_path=True),
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    after_update_id: int | None = typer.Option(
        None,
        "--after-update-id",
        help="Only capture updates newer than this id.",
    ),
    max_updates: int | None = typer.Option(
        None,
        "--max-updates",
        min=1,
        help="Stop after capturing this many updates.",
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
    """Capture a batch of Telegram updates from a file-backed update stream."""

    paths = _paths_for(root)
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    _capture_updates_from_file(
        updates_file=updates_file,
        account=resolved_account,
        root=root,
        after_update_id=after_update_id,
        limit=max_updates,
        mode="run",
    )


def capture_telegram_tdlib_once(
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    after_update_id: int | None = typer.Option(
        None,
        "--after-update-id",
        help="Only capture updates newer than this id.",
    ),
    tdlib_log_verbosity_level: int | None = _tdlib_log_verbosity_option(),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Capture one Telegram update directly from TDLib."""

    paths = _paths_for(root)
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    _capture_updates_from_tdlib(
        account=resolved_account,
        root=root,
        after_update_id=after_update_id,
        limit=1,
        mode="tdlib-once",
        tdlib_log_verbosity_level=tdlib_log_verbosity_level,
    )


def run_telegram_tdlib_capture(
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    after_update_id: int | None = typer.Option(
        None,
        "--after-update-id",
        help="Only capture updates newer than this id.",
    ),
    max_updates: int | None = typer.Option(
        None,
        "--max-updates",
        min=1,
        help="Stop after capturing this many updates.",
    ),
    tdlib_log_verbosity_level: int | None = _tdlib_log_verbosity_option(),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Capture a batch of Telegram updates directly from TDLib."""

    paths = _paths_for(root)
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    _capture_updates_from_tdlib(
        account=resolved_account,
        root=root,
        after_update_id=after_update_id,
        limit=max_updates,
        mode="tdlib-run",
        tdlib_log_verbosity_level=tdlib_log_verbosity_level,
    )


def run_telegram_tdlib_daemon(
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    after_update_id: int | None = typer.Option(
        None,
        "--after-update-id",
        help="Override the stored cursor for the first polling cycle.",
    ),
    poll_seconds: float = typer.Option(
        DEFAULT_DAEMON_POLL_SECONDS,
        "--poll-seconds",
        min=0.1,
        help="How long TDLib waits for updates in each polling cycle.",
    ),
    idle_sleep_seconds: float = typer.Option(
        DEFAULT_DAEMON_IDLE_SLEEP_SECONDS,
        "--idle-sleep-seconds",
        min=0.0,
        help="Sleep between idle polling cycles to reduce churn.",
    ),
    max_updates_per_cycle: int | None = typer.Option(
        None,
        "--max-updates-per-cycle",
        min=1,
        help="Cap each polling cycle for easier recovery and testing.",
    ),
    max_cycles: int | None = typer.Option(
        None,
        "--max-cycles",
        min=1,
        help="Optional safety bound for tests or supervised runs.",
    ),
    tdlib_log_verbosity_level: int | None = _tdlib_log_verbosity_option(),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Run a restart-friendly Telegram TDLib capture loop."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    resolved_after_update_id = _resolve_after_update_id(
        paths,
        account=resolved_account,
        after_update_id=after_update_id,
    )
    settings = build_tdlib_auth_settings(paths, config, account=resolved_account)
    if tdlib_log_verbosity_level is not None:
        settings = replace(settings, log_verbosity_level=tdlib_log_verbosity_level)
    transport = TdlibJsonTransport(
        library_path=settings.library_path,
        log_verbosity_level=settings.log_verbosity_level,
    )
    client = TdlibTelegramClient(
        transport=transport,
        settings=settings,
        receive_timeout_seconds=poll_seconds,
    )

    typer.echo("mode=tdlib-daemon")
    typer.echo(f"account={resolved_account}")
    typer.echo("transport=tdlib")
    typer.echo(
        "start_after_update_id="
        f"{resolved_after_update_id if resolved_after_update_id is not None else '-'}"
    )
    typer.echo(f"poll_seconds={poll_seconds}")
    typer.echo(f"idle_sleep_seconds={idle_sleep_seconds}")
    typer.echo(f"tdlib_log_verbosity_level={settings.log_verbosity_level}")
    typer.echo(
        "next_step=run 'recall normalize telegram --date YYYY-MM-DD' after capture accumulates"
    )

    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            result = capture_telegram_updates(
                paths,
                client=client,
                account=resolved_account,
                after_update_id=resolved_after_update_id,
                limit=max_updates_per_cycle,
                capture_mode="tdlib-daemon",
            )
            cycles += 1
            typer.echo(
                f"cycle={cycles} captured_updates={result.captured_updates} "
                "last_update_id="
                f"{result.last_update_id if result.last_update_id is not None else '-'}"
            )

            if result.last_update_id is not None:
                resolved_after_update_id = result.last_update_id

            if result.captured_updates == 0 and idle_sleep_seconds > 0:
                time.sleep(idle_sleep_seconds)
    except KeyboardInterrupt:
        typer.echo("status=stopped")
        raise typer.Exit(code=0) from None
    finally:
        client.close()


def drain_telegram_pending(
    root: Path = typer.Option(..., "--root", file_okay=False, resolve_path=True),
    max_updates: int = typer.Option(..., "--max-updates", min=1),
    account: str | None = typer.Option(None, "--account"),
) -> None:
    """Drain a bounded batch of saved receipts without authentication or network calls."""
    paths = _paths_for(root)
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    directory = resolve_tdlib_state_dir(paths, config.tdlib_state_dir) / resolved_account
    client = PendingTelegramClient(paths, account=resolved_account, directory=directory)
    try:
        result = capture_telegram_updates(
            paths,
            client=client,
            account=resolved_account,
            after_update_id=_resolve_after_update_id(
                paths, account=resolved_account, after_update_id=None
            ),
            limit=max_updates,
            capture_mode="pending-offline",
        )
        typer.echo("mode=pending-offline")
        typer.echo(f"account={resolved_account}")
        typer.echo(f"captured_updates={result.captured_updates}")
        typer.echo("dates_written=" + (",".join(result.dates_written) or "-"))
        typer.echo(
            f"last_update_id={result.last_update_id if result.last_update_id is not None else '-'}"
        )
        typer.echo(f"pending_remaining={client.remaining}")
    finally:
        client.close()


def normalize_telegram(
    date: str = typer.Option(..., "--date", help="Date to normalize in YYYY-MM-DD format."),
    voice_policy: Path | None = typer.Option(
        None, "--voice-policy", help="Opt in to local voice collection after publication."
    ),
    voice_account: str | None = typer.Option(
        None, "--voice-account", help="Explicit account for voice collection only."
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
    """Normalize one day of captured Telegram raw updates."""

    if (voice_policy is None) != (voice_account is None) or (
        voice_account is not None
        and (not voice_account.strip() or voice_account != voice_account.strip())
    ):
        raise typer.BadParameter("Use --voice-policy with an explicit nonblank --voice-account")
    paths = _paths_for(root)
    paths.ensure_directories()
    event_path, artifact_path = normalize_telegram_day(paths, date=date)
    typer.echo(f"Normalized Telegram events for {date}")
    typer.echo(f"events={event_path}")
    typer.echo(f"artifacts={artifact_path}")
    typer.echo(
        f"next_step=run 'recall entities sync telegram --date {date}' or "
        f"'recall artifacts show --date {date} --source telegram'"
    )
    if voice_policy is not None:
        assert voice_account is not None
        report = after_publication(
            paths, day=date, source="telegram", account=voice_account, policy_path=voice_policy
        )
        typer.echo("voice=" + json.dumps(report, ensure_ascii=True, sort_keys=True))
        if not report["verified_current"]:
            raise typer.Exit(1)


def sync_telegram_entities(
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
    """Sync Telegram identities and aliases into SQLite entity storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    result = sync_telegram_entities_for_date(paths, date=date)
    typer.echo(f"Synced Telegram entities for {date}")
    typer.echo(f"persons={result.persons_synced}")
    typer.echo(f"identities={result.identities_synced}")
    typer.echo(f"person_aliases={result.person_aliases_synced}")
    typer.echo(f"identity_aliases={result.aliases_synced}")
    typer.echo(f"resolutions={result.resolutions_synced}")


def show_telegram_state(
    account: str | None = typer.Option(
        None, "--account", help="Telegram account label to inspect."
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
    """Show stored Telegram cursor state for one account."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)
    resolved_account = account or config.account
    cursors = list_connector_cursors(paths, source="telegram", account=resolved_account)

    typer.echo("source=telegram")
    typer.echo(f"account={resolved_account}")
    if not cursors:
        typer.echo("cursor_state=empty")
        return

    for cursor in cursors:
        typer.echo(f"cursor_key={cursor.cursor_key}")
        typer.echo(f"cursor_value={cursor.cursor_value}")
        typer.echo(f"updated_at={cursor.updated_at}")


def import_telegram_export_bundle(
    export_path: Path = typer.Argument(..., exists=True, resolve_path=True),
    account: str | None = typer.Option(None, "--account", help="Telegram account label to record."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Workspace root to use.",
    ),
) -> None:
    """Import a Telegram Desktop JSON export bundle into raw storage."""

    paths = _paths_for(root)
    paths.ensure_directories()
    config = load_telegram_config(paths)
    result = import_telegram_export(
        paths,
        export_path=export_path,
        account=account or config.account,
    )
    typer.echo("mode=import")
    typer.echo(f"import_id={result.import_id}")
    typer.echo(f"import_dir={result.import_dir}")
    typer.echo(f"messages_imported={result.messages_imported}")
    typer.echo(f"messages_skipped={result.messages_skipped}")
    if result.messages_skipped:
        typer.echo("coverage=partial; inspect retained result.json for skipped records")
    typer.echo("dates_written=" + (",".join(result.dates_written) if result.dates_written else "-"))
    typer.echo("next_step=run 'recall normalize telegram --date YYYY-MM-DD' for each imported date")
