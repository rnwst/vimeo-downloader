#!/usr/bin/env python3
"""Download and assemble a Vimeo JSON/DASH playlist."""

# pyright: strict

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Literal, NamedTuple, cast
import urllib.error
import urllib.parse
import urllib.request

TrackKind = Literal["video", "audio"]


class DownloadError(Exception):
    """An error that can be shown directly to the user."""


class Rendition(NamedTuple):
    """A validated audio or video rendition from the playlist."""

    rendition_id: str
    base_url: str
    codecs: str
    bitrate: int
    width: int
    height: int
    init_segment: str | None
    init_segment_url: str | None
    segment_urls: tuple[str, ...]


class Playlist(NamedTuple):
    """The validated playlist fields used by the downloader."""

    clip_id: str
    base_url: str
    video: tuple[Rendition, ...]
    audio: tuple[Rendition, ...]


class TrackDownload(NamedTuple):
    """Parameters needed to download one media track."""

    playlist_url: str
    playlist_base_url: str
    rendition: Rendition
    destination: Path
    kind: TrackKind
    workers: int


class Arguments(NamedTuple):
    """Fully typed command-line arguments."""

    playlist_url: str
    output: str | None
    video: str
    audio: str
    list_renditions: bool
    workers: int
    retries: int
    timeout: float
    referer: str | None
    header: list[str]
    ffmpeg: str
    force: bool


class DownloadPlan(NamedTuple):
    """Resolved choices and paths for a complete download."""

    playlist_url: str
    playlist: Playlist
    video: Rendition
    audio: Rendition | None
    output: Path
    ffmpeg: str | None
    workers: int


class HttpClient:
    """Small retrying HTTP client shared by concurrent segment requests."""

    def __init__(
        self,
        headers: dict[str, str],
        timeout: float,
        retries: int,
    ) -> None:
        self.headers = headers
        self.timeout = timeout
        self.retries = retries

    def get(self, url: str) -> tuple[bytes, str]:
        """Fetch a URL and return its content and post-redirect URL."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise DownloadError(f"unsupported URL scheme in {_safe_url(url)}")

        for attempt in range(self.retries + 1):
            retry_after: float | None = None
            try:
                request = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read(), response.geturl()
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                if retryable:
                    value = exc.headers.get("Retry-After")
                    if value and value.isdigit():
                        retry_after = min(float(value), 60.0)
                if not retryable or attempt == self.retries:
                    hint = ""
                    if exc.code in {401, 403}:
                        hint = (
                            "; the signed URL may have expired, or the request may "
                            "need --referer/--header"
                        )
                    raise DownloadError(
                        f"HTTP {exc.code} while fetching {_safe_url(url)}{hint}"
                    ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as exc:
                if attempt == self.retries:
                    raise DownloadError(
                        f"request failed for {_safe_url(url)}: {exc}"
                    ) from exc
            except ValueError as exc:
                raise DownloadError(f"invalid URL {_safe_url(url)}: {exc}") from exc

            delay = (
                retry_after if retry_after is not None else min(0.5 * 2**attempt, 8.0)
            )
            time.sleep(delay)

        raise AssertionError("unreachable")

    def get_bytes(self, url: str) -> bytes:
        """Fetch a URL and return only its content."""
        return self.get(url)[0]


def _safe_url(url: str) -> str:
    """Omit signed query values from errors while retaining a useful location."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return number


def _parse_headers(values: Sequence[str], referer: str | None) -> dict[str, str]:
    headers = {"User-Agent": "vimeo-playlist-downloader/1.0"}
    for value in values:
        name, separator, content = value.partition(":")
        name = name.strip()
        content = content.strip()
        valid_name = re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
        if not separator or not name or valid_name is None:
            raise DownloadError(f"invalid header {value!r}; expected 'Name: value'")
        if "\r" in content or "\n" in content:
            raise DownloadError(f"invalid newline in header {name!r}")
        headers[name] = content
    if referer:
        headers["Referer"] = referer
    return headers


def _json_object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DownloadError(f"{location} must be an object")
    untyped = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in untyped):
        raise DownloadError(f"{location} contains a non-string key")
    return cast(dict[str, object], untyped)


def _json_array(value: object, location: str) -> list[object]:
    if not isinstance(value, list):
        raise DownloadError(f"{location} must be an array")
    return cast(list[object], value)


def _optional_string(data: dict[str, object], key: str, location: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DownloadError(f"{location}.{key} must be a string")
    return value


def _integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (OverflowError, ValueError):
        return 0


def _parse_segment_urls(value: object, location: str) -> tuple[str, ...]:
    raw_segments = _json_array(value, f"{location}.segments")
    urls: list[str] = []
    for index, raw_segment in enumerate(raw_segments, start=1):
        segment = _json_object(raw_segment, f"{location}.segments[{index}]")
        url = segment.get("url")
        if not isinstance(url, str) or not url:
            raise DownloadError(f"{location} segment {index} has no URL")
        urls.append(url)
    return tuple(urls)


def _parse_rendition(value: object, location: str) -> Rendition:
    data = _json_object(value, location)
    return Rendition(
        rendition_id=_optional_string(data, "id", location) or "?",
        base_url=_optional_string(data, "base_url", location) or "",
        codecs=_optional_string(data, "codecs", location) or "unknown codec",
        bitrate=_integer(data, "avg_bitrate") or _integer(data, "bitrate"),
        width=_integer(data, "width"),
        height=_integer(data, "height"),
        init_segment=_optional_string(data, "init_segment", location),
        init_segment_url=_optional_string(data, "init_segment_url", location),
        segment_urls=_parse_segment_urls(data.get("segments"), location),
    )


def _parse_playlist(value: object) -> Playlist:
    data = _json_object(value, "playlist")
    raw_video = _json_array(data.get("video"), "playlist.video")
    if not raw_video:
        raise DownloadError("playlist contains no video renditions")
    video = tuple(
        _parse_rendition(item, f"playlist.video[{index}]")
        for index, item in enumerate(raw_video, start=1)
    )

    raw_audio_value: object = data.get("audio", [])
    raw_audio = _json_array(raw_audio_value, "playlist.audio")
    audio = tuple(
        _parse_rendition(item, f"playlist.audio[{index}]")
        for index, item in enumerate(raw_audio, start=1)
    )
    return Playlist(
        clip_id=_optional_string(data, "clip_id", "playlist") or "video",
        base_url=_optional_string(data, "base_url", "playlist") or "",
        video=video,
        audio=audio,
    )


def load_playlist(client: HttpClient, url: str) -> tuple[Playlist, str]:
    """Fetch, decode, and validate a playlist."""
    raw, final_url = client.get(url)
    try:
        decoded: object = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadError(f"playlist is not valid UTF-8 JSON: {exc}") from exc
    return _parse_playlist(decoded), final_url


def _video_key(stream: Rendition) -> tuple[int, int, int, int]:
    return (
        stream.width * stream.height,
        stream.height,
        stream.width,
        stream.bitrate,
    )


def select_video(streams: Sequence[Rendition], choice: str) -> Rendition:
    """Select a video rendition by quality keyword, height, or ID."""
    normalized = choice.lower()
    if normalized == "best":
        return max(streams, key=_video_key)
    if normalized == "worst":
        return min(streams, key=_video_key)

    for stream in streams:
        if stream.rendition_id == choice:
            return stream

    match = re.fullmatch(r"(\d+)(?:p)?", normalized)
    if match:
        height = int(match.group(1))
        matches = [stream for stream in streams if stream.height == height]
        if matches:
            return max(matches, key=_video_key)

    available = ", ".join(
        f"{stream.height}p" for stream in sorted(streams, key=_video_key)
    )
    raise DownloadError(
        f"video rendition {choice!r} was not found (available: {available})"
    )


def select_audio(streams: Sequence[Rendition], choice: str) -> Rendition | None:
    """Select an audio rendition by quality keyword or ID."""
    normalized = choice.lower()
    if normalized == "none":
        return None
    if not streams:
        if normalized == "best":
            return None
        raise DownloadError("playlist contains no audio renditions")
    if normalized == "best":
        return max(streams, key=lambda stream: stream.bitrate)
    if normalized == "worst":
        return min(streams, key=lambda stream: stream.bitrate)
    for stream in streams:
        if stream.rendition_id == choice:
            return stream
    raise DownloadError(f"audio rendition {choice!r} was not found")


def _stream_base_url(download: TrackDownload) -> str:
    playlist_base = urllib.parse.urljoin(
        download.playlist_url, download.playlist_base_url
    )
    return urllib.parse.urljoin(playlist_base, download.rendition.base_url)


def _stream_description(kind: TrackKind, stream: Rendition) -> str:
    bitrate = stream.bitrate // 1000
    if kind == "video":
        return f"video {stream.width}x{stream.height}, {stream.codecs}, {bitrate} kbps"
    return f"audio {stream.codecs}, {bitrate} kbps"


def _initialization_bytes(
    client: HttpClient, download: TrackDownload, base_url: str
) -> bytes:
    inline_init = download.rendition.init_segment
    if inline_init:
        try:
            return base64.b64decode(inline_init, validate=True)
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise DownloadError(
                f"selected {download.kind} rendition has invalid base64 "
                "initialization data"
            ) from exc

    init_url = download.rendition.init_segment_url
    if init_url:
        return client.get_bytes(urllib.parse.urljoin(base_url, init_url))
    raise DownloadError(
        f"selected {download.kind} rendition has no initialization segment"
    )


def _ordered_downloads(
    client: HttpClient, urls: Sequence[str], workers: int
) -> Iterator[bytes]:
    pending: dict[int, Future[bytes]] = {}
    next_to_submit = 0
    window = max(workers * 2, 1)

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="media-segment"
    ) as executor:
        try:
            for next_to_yield in range(len(urls)):
                while (
                    next_to_submit < len(urls)
                    and next_to_submit < next_to_yield + window
                ):
                    pending[next_to_submit] = executor.submit(
                        client.get_bytes, urls[next_to_submit]
                    )
                    next_to_submit += 1
                yield pending.pop(next_to_yield).result()
        finally:
            for future in pending.values():
                future.cancel()


def _render_progress(completed: int, total: int, downloaded: int) -> None:
    if not sys.stderr.isatty():
        return
    sys.stderr.write(
        f"\r  {completed}/{total} segments, " f"{downloaded / (1024 * 1024):.1f} MiB"
    )
    sys.stderr.flush()


def _write_track(
    client: HttpClient,
    download: TrackDownload,
    initialization: bytes,
    urls: Sequence[str],
) -> int:
    downloaded = len(initialization)
    try:
        with download.destination.open("xb") as output:
            output.write(initialization)
            for completed, data in enumerate(
                _ordered_downloads(client, urls, download.workers), start=1
            ):
                output.write(data)
                downloaded += len(data)
                _render_progress(completed, len(urls), downloaded)
    except OSError as exc:
        raise DownloadError(f"could not write {download.destination}: {exc}") from exc
    return downloaded


def download_track(client: HttpClient, download: TrackDownload) -> None:
    """Download and concatenate one initialized media track."""
    base_url = _stream_base_url(download)
    urls = tuple(
        urllib.parse.urljoin(base_url, url) for url in download.rendition.segment_urls
    )
    if not urls:
        raise DownloadError(f"selected {download.kind} rendition contains no segments")
    initialization = _initialization_bytes(client, download, base_url)
    description = _stream_description(download.kind, download.rendition)
    print(f"Downloading {description} ({len(urls)} segments)", file=sys.stderr)
    try:
        downloaded = _write_track(client, download, initialization, urls)
    finally:
        if sys.stderr.isatty():
            sys.stderr.write("\n")
    print(
        f"Downloaded {description}: {downloaded / (1024 * 1024):.1f} MiB",
        file=sys.stderr,
    )


def _find_ffmpeg(command: str) -> str:
    executable = shutil.which(command)
    if not executable:
        raise DownloadError(
            f"ffmpeg executable {command!r} was not found; install ffmpeg, pass "
            "--ffmpeg PATH, or use --audio none for a video-only file"
        )
    return executable


def mux_tracks(ffmpeg: str, video: Path, audio: Path, destination: Path) -> None:
    """Stream-copy separate video and audio tracks into one container."""
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
    ]
    if destination.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        command.extend(["-movflags", "+faststart"])
    command.append(str(destination))

    print("Muxing video and audio", file=sys.stderr)
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DownloadError(f"could not run ffmpeg: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise DownloadError(f"ffmpeg failed: {detail}")


def list_renditions(playlist: Playlist) -> None:
    """Print the available video and audio renditions."""
    print(f"Clip: {playlist.clip_id}")
    print("Video renditions:")
    for stream in sorted(playlist.video, key=_video_key, reverse=True):
        description = _stream_description("video", stream)
        print(f"  {stream.rendition_id}  {description}")
    print("Audio renditions:")
    if not playlist.audio:
        print("  none")
    for stream in sorted(
        playlist.audio, key=lambda rendition: rendition.bitrate, reverse=True
    ):
        description = _stream_description("audio", stream)
        print(f"  {stream.rendition_id}  {description}")


def _default_output_name(playlist: Playlist) -> str:
    safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", playlist.clip_id).strip("._")
    return f"{safe_name or 'video'}.mp4"


def _create_plan(
    args: Arguments, playlist: Playlist, playlist_url: str
) -> DownloadPlan:
    output = Path(args.output or _default_output_name(playlist)).expanduser()
    if output.exists() and not args.force:
        raise DownloadError(
            f"output already exists: {output} (use --force to replace it)"
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError(
            f"could not create output directory {output.parent}: {exc}"
        ) from exc

    video = select_video(playlist.video, args.video)
    audio = select_audio(playlist.audio, args.audio)
    return DownloadPlan(
        playlist_url=playlist_url,
        playlist=playlist,
        video=video,
        audio=audio,
        output=output,
        ffmpeg=_find_ffmpeg(args.ffmpeg) if audio is not None else None,
        workers=args.workers,
    )


def _track_download(
    plan: DownloadPlan,
    rendition: Rendition,
    destination: Path,
    kind: TrackKind,
) -> TrackDownload:
    return TrackDownload(
        playlist_url=plan.playlist_url,
        playlist_base_url=plan.playlist.base_url,
        rendition=rendition,
        destination=destination,
        kind=kind,
        workers=plan.workers,
    )


def _replace_output(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise DownloadError(f"could not create output {destination}: {exc}") from exc


def _execute_plan(client: HttpClient, plan: DownloadPlan) -> None:
    suffix = plan.output.suffix or ".mp4"
    with tempfile.TemporaryDirectory(
        prefix=f".{plan.output.name}.", dir=plan.output.parent
    ) as temp:
        temp_dir = Path(temp)
        video_path = temp_dir / "video.mp4"
        download_track(client, _track_download(plan, plan.video, video_path, "video"))
        completed_path = video_path

        if plan.audio is not None:
            audio_path = temp_dir / "audio.mp4"
            download_track(
                client, _track_download(plan, plan.audio, audio_path, "audio")
            )
            completed_path = temp_dir / f"complete{suffix}"
            if plan.ffmpeg is None:
                raise DownloadError("ffmpeg was not resolved for the audio track")
            mux_tracks(plan.ffmpeg, video_path, audio_path, completed_path)

        _replace_output(completed_path, plan.output)
    print(f"Saved {plan.output}", file=sys.stderr)


def run(args: Arguments) -> None:
    """Load the playlist and execute the requested CLI operation."""
    headers = _parse_headers(args.header, args.referer)
    client = HttpClient(headers, args.timeout, args.retries)
    playlist, playlist_url = load_playlist(client, args.playlist_url)
    if args.list_renditions:
        list_renditions(playlist)
        return
    _execute_plan(client, _create_plan(args, playlist, playlist_url))


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Download and assemble a Vimeo JSON/DASH playlist.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("playlist_url", help="URL of playlist.json")
    parser.add_argument("-o", "--output", help="destination file (normally .mp4)")
    parser.add_argument(
        "--video",
        default="best",
        metavar="CHOICE",
        help="video rendition: best, worst, HEIGHTp, or rendition ID",
    )
    parser.add_argument(
        "--audio",
        default="best",
        metavar="CHOICE",
        help="audio rendition: best, worst, none, or rendition ID",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_renditions",
        help="list available renditions without downloading",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=8,
        help="maximum parallel segment requests per track",
    )
    parser.add_argument(
        "--retries",
        type=_nonnegative_int,
        default=4,
        help="retries for transient HTTP and network failures",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        help="timeout in seconds for each request",
    )
    parser.add_argument(
        "--referer",
        help="Referer header to send with playlist and segment requests",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="additional HTTP header; may be repeated (for example, Cookie)",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        metavar="PATH",
        help="ffmpeg executable used to mux separate video and audio tracks",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    return parser


def _parse_arguments(argv: Sequence[str] | None) -> Arguments:
    namespace = build_parser().parse_args(argv)
    return Arguments(
        playlist_url=cast(str, namespace.playlist_url),
        output=cast(str | None, namespace.output),
        video=cast(str, namespace.video),
        audio=cast(str, namespace.audio),
        list_renditions=cast(bool, namespace.list_renditions),
        workers=cast(int, namespace.workers),
        retries=cast(int, namespace.retries),
        timeout=cast(float, namespace.timeout),
        referer=cast(str | None, namespace.referer),
        header=cast(list[str], namespace.header),
        ffmpeg=cast(str, namespace.ffmpeg),
        force=cast(bool, namespace.force),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line utility and return its process exit status."""
    args = _parse_arguments(argv)
    try:
        run(args)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nDownload interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
