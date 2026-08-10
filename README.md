# Vimeo Playlist Downloader

`vimeo-playlist-downloader` is an unofficial command-line application that
downloads the fragmented video and audio tracks described by a Vimeo
`playlist.json`, then stream-copies them into one output file.

This project is not affiliated with or endorsed by Vimeo. Use it only for
video you are authorized to download.

## Requirements

- Python 3.10 or newer (verified with Python 3.12)
- `ffmpeg` on `PATH` for the normal video-plus-audio output

The segment downloader itself uses only the Python standard library. `ffmpeg`
is not needed with `--audio none`.

## Installation

Install the application in an isolated environment with
[`pipx`](https://pipx.pypa.io/):

```sh
pipx install vimeo-playlist-downloader
```

Alternatively, install it into the current Python environment:

```sh
python3 -m pip install vimeo-playlist-downloader
```

Upgrade or remove a pipx installation with:

```sh
pipx upgrade vimeo-playlist-downloader
pipx uninstall vimeo-playlist-downloader
```

## Usage

Download the highest-resolution video and highest-bitrate audio:

```sh
vimeo-playlist-downloader 'https://vod-adaptive-ak.vimeo.com/.../playlist.json?...' -o video.mp4
```

List the available rendition IDs and resolutions:

```sh
vimeo-playlist-downloader 'PLAYLIST_URL' --list
```

Select a resolution, or select an exact rendition ID shown by `--list`:

```sh
vimeo-playlist-downloader 'PLAYLIST_URL' -o video.mp4 --video 720p
```

If the player request depends on browser context, pass the same referer and any
required headers. Headers are sent to both the playlist and segment URLs:

```sh
vimeo-playlist-downloader 'PLAYLIST_URL' -o video.mp4 \
  --referer 'https://example.com/player-page' \
  --header 'Cookie: session=...'
```

Run `vimeo-playlist-downloader --help` for all options. Vimeo's signed URLs can
expire; if a previously valid URL returns HTTP 403, capture a fresh playlist
URL before retrying.

## Development

Create a virtual environment and install the application in editable mode:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --editable .
python -m pip install --requirement requirements-dev.txt
```

Run the same quality checks as GitHub Actions:

```sh
black --check --diff download_vimeo.py
pylint download_vimeo.py
pyright
```

Build and validate the source and wheel distributions:

```sh
python -m build
python -m twine check dist/*
```

Install the locally built wheel with:

```sh
pipx install --force dist/*.whl
```

## License

This project is distributed under the MIT License. See `LICENSE`.
