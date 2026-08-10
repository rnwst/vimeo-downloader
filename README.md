# Vimeo playlist downloader

`download_vimeo.py` downloads the fragmented video and audio tracks described by
a Vimeo `playlist.json`, then stream-copies them into one output file.

Use it only for video you are authorized to download.

## Requirements

- Python 3.10 or newer (verified with Python 3.12)
- `ffmpeg` on `PATH` for the normal video-plus-audio output

The segment downloader itself uses only the Python standard library. `ffmpeg`
is not needed with `--audio none`.

## Usage

Download the highest-resolution video and highest-bitrate audio:

```sh
python3 download_vimeo.py 'https://vod-adaptive-ak.vimeo.com/.../playlist.json?...' -o video.mp4
```

List the available rendition IDs and resolutions:

```sh
python3 download_vimeo.py 'PLAYLIST_URL' --list
```

Select a resolution, or select an exact rendition ID shown by `--list`:

```sh
python3 download_vimeo.py 'PLAYLIST_URL' -o video.mp4 --video 720p
```

If the player request depends on browser context, pass the same referer and any
required headers. Headers are sent to both the playlist and segment URLs:

```sh
python3 download_vimeo.py 'PLAYLIST_URL' -o video.mp4 \
  --referer 'https://example.com/player-page' \
  --header 'Cookie: session=...'
```

Run `python3 download_vimeo.py --help` for all options. Vimeo's signed URLs
can expire; if a previously valid URL returns HTTP 403, capture a fresh
playlist URL before retrying.
