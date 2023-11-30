import sys
from time import sleep, time
import click
import os

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from toot import api
from toot.cli.base import cli, json_option, pass_context, Context
from toot.cli.base import DURATION_EXAMPLES, VISIBILITY_CHOICES, get_default_visibility
from toot.cli.validators import validate_duration, validate_language
from toot.utils import EOF_KEY, delete_tmp_status_file, editor_input, multiline_input
from toot.utils.datetime import parse_datetime


@cli.command()
@click.argument("text", required=False)
@click.option(
    "--media", "-m",
    help="""Path to media file to attach, can be used multiple times to attach
            multiple files.""",
    type=click.File(mode="rb"),
    multiple=True
)
@click.option(
    "--description", "-d",
    help="""Plain-text description of the media for accessibility purposes, one
            per attached media""",
    multiple=True,
)
@click.option(
    "--thumbnail",
    help="Path to an image file to serve as media thumbnail, one per attached media",
    type=click.File(mode="rb"),
    multiple=True
)
@click.option(
    "--visibility", "-v",
    help="Post visibility",
    type=click.Choice(VISIBILITY_CHOICES),
    default=get_default_visibility(),
)
@click.option(
    "--sensitive", "-s",
    help="Mark status and attached media as sensitive",
    default=False,
    is_flag=True,
)
@click.option(
    "--spoiler-text", "-p",
    help="Text to be shown as a warning or subject before the actual content.",
)
@click.option(
    "--reply-to", "-r",
    help="ID of the status being replied to, if status is a reply.",
)
@click.option(
    "--language", "-l",
    help="ISO 639-1 language code of the toot, to skip automatic detection.",
    callback=validate_language,
)
@click.option(
    "--editor", "-e",
    is_flag=False,
    flag_value=os.getenv("EDITOR"),
    help="""Specify an editor to compose your toot. When used without a value
            it will use the editor defined in the $EDITOR environment variable.""",
)
@click.option(
    "--scheduled-at",
    help="""ISO 8601 Datetime at which to schedule a status. Must be at least 5
            minutes in the future.""",
)
@click.option(
    "--scheduled-in",
    help=f"""Schedule the toot to be posted after a given amount of time,
             {DURATION_EXAMPLES}. Must be at least 5 minutes.""",
    callback=validate_duration,
)
@click.option(
    "--content-type", "-t",
    help="MIME type for the status text (not supported on all instances)",
)
@click.option(
    "--poll-option",
    help="Possible answer to the poll, can be given multiple times.",
    multiple=True,
)
@click.option(
    "--poll-expires-in",
    help=f"Duration that the poll should be open, {DURATION_EXAMPLES}",
    callback=validate_duration,
    default="24h",
)
@click.option(
    "--poll-multiple",
    help="Allow multiple answers to be selected.",
    is_flag=True,
    default=False,
)
@click.option(
    "--poll-hide-totals",
    help="Hide vote counts until the poll ends.",
    is_flag=True,
    default=False,
)
@json_option
@pass_context
def post(
    ctx: Context,
    text: Optional[str],
    media: Tuple[str],
    description: Tuple[str],
    thumbnail: Tuple[str],
    visibility: str,
    sensitive: bool,
    spoiler_text: Optional[str],
    reply_to: Optional[str],
    language: Optional[str],
    editor: Optional[str],
    scheduled_at: Optional[str],
    scheduled_in: Optional[int],
    content_type: Optional[str],
    poll_option: Tuple[str],
    poll_expires_in: int,
    poll_multiple: bool,
    poll_hide_totals: bool,
    json: bool
):
    """Post a new status"""
    if editor and not sys.stdin.isatty():
        raise click.ClickException("Cannot run editor if not in tty.")

    if len(media) > 4:
        raise click.ClickException("Cannot attach more than 4 files.")

    media_ids = _upload_media(ctx.app, ctx.user, media, description, thumbnail)
    status_text = _get_status_text(text, editor, media)
    scheduled_at = _get_scheduled_at(scheduled_at, scheduled_in)

    if not status_text and not media_ids:
        raise click.ClickException("You must specify either text or media to post.")

    response = api.post_status(
        ctx.app,
        ctx.user,
        status_text,
        visibility=visibility,
        media_ids=media_ids,
        sensitive=sensitive,
        spoiler_text=spoiler_text,
        in_reply_to_id=reply_to,
        language=language,
        scheduled_at=scheduled_at,
        content_type=content_type,
        poll_options=poll_option,
        poll_expires_in=poll_expires_in,
        poll_multiple=poll_multiple,
        poll_hide_totals=poll_hide_totals,
    )

    if json:
        click.echo(response.text)
    else:
        status = response.json()
        if "scheduled_at" in status:
            scheduled_at = parse_datetime(status["scheduled_at"])
            scheduled_at = datetime.strftime(scheduled_at, "%Y-%m-%d %H:%M:%S%z")
            click.echo(f"Toot scheduled for: {scheduled_at}")
        else:
            click.echo(f"Toot posted: {status['url']}")

    delete_tmp_status_file()


def _get_status_text(text, editor, media):
    isatty = sys.stdin.isatty()

    if not text and not isatty:
        text = sys.stdin.read().rstrip()

    if isatty:
        if editor:
            text = editor_input(editor, text)
        elif not text and not media:
            click.echo(f"Write or paste your toot. Press {EOF_KEY} to post it.")
            text = multiline_input()

    return text


def _get_scheduled_at(scheduled_at, scheduled_in):
    if scheduled_at:
        return scheduled_at

    if scheduled_in:
        scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=scheduled_in)
        return scheduled_at.replace(microsecond=0).isoformat()

    return None


def _upload_media(app, user, media, description, thumbnail):
    # Match media to corresponding description and thumbnail
    media = media or []
    descriptions = description or []
    thumbnails = thumbnail or []
    uploaded_media = []

    for idx, file in enumerate(media):
        description = descriptions[idx].strip() if idx < len(descriptions) else None
        thumbnail = thumbnails[idx] if idx < len(thumbnails) else None
        result = _do_upload(app, user, file, description, thumbnail)
        uploaded_media.append(result)

    _wait_until_all_processed(app, user, uploaded_media)

    return [m["id"] for m in uploaded_media]


def _do_upload(app, user, file, description, thumbnail):
    click.echo(f"Uploading media: {file.name}")
    return api.upload_media(app, user, file, description=description, thumbnail=thumbnail)


def _wait_until_all_processed(app, user, uploaded_media):
    """
    Media is uploaded asynchronously, and cannot be attached until the server
    has finished processing it. This function waits for that to happen.

    Once media is processed, it will have the URL populated.
    """
    if all(m["url"] for m in uploaded_media):
        return

    # Timeout after waiting 1 minute
    start_time = time()
    timeout = 60

    click.echo("Waiting for media to finish processing...")
    for media in uploaded_media:
        _wait_until_processed(app, user, media, start_time, timeout)


def _wait_until_processed(app, user, media, start_time, timeout):
    if media["url"]:
        return

    media = api.get_media(app, user, media["id"])
    while not media["url"]:
        sleep(1)
        if time() > start_time + timeout:
            raise click.ClickException(f"Media not processed by server after {timeout} seconds. Aborting.")
        media = api.get_media(app, user, media["id"])
