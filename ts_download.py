#!/usr/bin/env python3
"""Download sibling TS segments from one segment URL and join them with FFmpeg.

Examples:
    python3 ts_download.py "https://host/path/seg-2-v1-a1.ts" -o video.mp4
    python3 ts_download.py "https://host/path/index.m3u8" -o video.mp4

The script finds the segment number in the file name (seg-2-...), then
downloads seg-1, seg-2, seg-3, ... until the server answers 404, and
concatenates the pieces with FFmpeg's concat demuxer (-c copy, no re-encode).
Given an .m3u8 playlist instead, it downloads the segments the playlist lists
(for a master playlist, from the variant with the highest bandwidth).
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# Words that usually precede the segment number in a file name.
SEGMENT_PREFIX = re.compile(r"(?:seg(?:ment)?|frag(?:ment)?|chunk|part|media)[-_]?(\d+)", re.I)
NUMBER = re.compile(r"\d+")
END_STATUSES = {404, 410}
USER_AGENT = "Mozilla/5.0 (compatible; ts-downloader/1.0)"


def build_template(url, group=None):
    """Return (make_url, start_number) for the segment number in the URL's file name.

    make_url(n) gives the URL of segment n; zero padding (seg-007) is kept.
    group picks the Nth number in the file name (1-based) instead of guessing.
    """
    parts = urllib.parse.urlsplit(url)
    head, _, name = parts.path.rpartition("/")

    if group is not None:
        numbers = list(NUMBER.finditer(name))
        if not 1 <= group <= len(numbers):
            raise ValueError(f"file name {name!r} has {len(numbers)} number(s), --group {group} is out of range")
        start, end = numbers[group - 1].span()
    else:
        # Prefer a number after "seg"/"chunk"/...; otherwise take the last number.
        match = SEGMENT_PREFIX.search(name) or next(reversed(list(NUMBER.finditer(name))), None)
        if not match:
            raise ValueError(f"no segment number found in file name {name!r}")
        start, end = match.span(match.lastindex or 0)

    digits = name[start:end]
    width = len(digits) if digits.startswith("0") and len(digits) > 1 else 0

    def make_url(n):
        new_name = f"{name[:start]}{n:0{width}d}{name[end:]}"
        return urllib.parse.urlunsplit(parts._replace(path=f"{head}/{new_name}"))

    return make_url, int(digits)


def fetch(url, dest, headers, retries):
    """Download url into dest. Return False if the server says the segment does not exist."""
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response, open(dest, "wb") as out:
                shutil.copyfileobj(response, out)
            return True
        except urllib.error.HTTPError as err:
            if err.code in END_STATUSES:
                return False
            if attempt == retries or err.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(2 ** attempt)
    return False


def read_text(url, headers, retries):
    """Fetch a small text resource (a playlist) and return (final_url, text)."""
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.geturl(), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if attempt == retries or err.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(2 ** attempt)


def is_playlist(url):
    return urllib.parse.urlsplit(url).path.lower().endswith((".m3u8", ".m3u"))


def parse_attributes(line):
    """Parse 'KEY=value,KEY2="quoted,value"' from an #EXT tag."""
    return {k.upper(): v.strip('"') for k, v in re.findall(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)', line)}


def playlist_segments(url, headers, retries, variant=None):
    """Return the segment URLs listed in an HLS playlist.

    A master playlist is resolved to one of its variants: the highest bandwidth,
    or the 1-based index given in variant.
    """
    url, text = read_text(url, headers, retries)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError(f"{url} is not an m3u8 playlist")

    variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = parse_attributes(line.partition(":")[2])
            uri = next((l for l in lines[i + 1:] if not l.startswith("#")), None)
            if uri:
                variants.append((int(attrs.get("BANDWIDTH", "0") or 0), attrs.get("RESOLUTION", "?"),
                                 urllib.parse.urljoin(url, uri)))
    if variants:
        for n, (bandwidth, resolution, _) in enumerate(variants, 1):
            print(f"variant {n}: {resolution}, {bandwidth} bit/s", file=sys.stderr)
        if variant is None:
            chosen = max(variants, key=lambda v: v[0])
        elif 1 <= variant <= len(variants):
            chosen = variants[variant - 1]
        else:
            raise ValueError(f"playlist has {len(variants)} variant(s), --variant {variant} is out of range")
        print(f"using {chosen[1]}, {chosen[0]} bit/s: {chosen[2]}", file=sys.stderr)
        return playlist_segments(chosen[2], headers, retries)

    segments = []
    for line in lines:
        if line.startswith("#EXT-X-KEY"):
            if parse_attributes(line.partition(":")[2]).get("METHOD", "NONE").upper() != "NONE":
                raise ValueError("playlist is encrypted (#EXT-X-KEY), this is not supported")
        elif line.startswith("#EXT-X-MAP"):
            raise ValueError("playlist uses fMP4 segments (#EXT-X-MAP), only MPEG-TS is supported")
        elif not line.startswith("#"):
            segments.append(urllib.parse.urljoin(url, line))
    if not segments:
        raise ValueError(f"no segments found in {url}")
    return segments


def download_list(urls, workdir, headers, retries):
    files = []
    for n, url in enumerate(urls, 1):
        dest = os.path.join(workdir, f"{n:06d}.ts")
        print(f"[{n}/{len(urls)}] {url}", file=sys.stderr)
        if not fetch(url, dest, headers, retries):
            raise urllib.error.HTTPError(url, 404, "segment listed in playlist not found", None, None)
        files.append(dest)
    return files


def download_segments(make_url, first, limit, workdir, headers, retries):
    files = []
    n = first
    while limit is None or len(files) < limit:
        url = make_url(n)
        dest = os.path.join(workdir, f"{n:06d}.ts")
        print(f"[{n}] {url}", file=sys.stderr)
        if not fetch(url, dest, headers, retries):
            print(f"[{n}] not found, stopping", file=sys.stderr)
            break
        files.append(dest)
        n += 1
    return files


def concat(files, output, workdir, ffmpeg):
    list_path = os.path.join(workdir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path in files:
            escaped = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output]
    subprocess.run(cmd, check=True)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Download all TS segments that sit next to the given one (or that an .m3u8 playlist lists) "
                    "and join them into one MP4 with FFmpeg.")
    p.add_argument("url", help="URL of any segment, e.g. https://host/path/seg-2-v1-a1.ts, "
                               "or of an .m3u8 playlist")
    p.add_argument("-o", "--output", default="output.mp4", help="output file (default: output.mp4)")
    p.add_argument("--start", type=int, default=1,
                   help="first segment number to download (default: 1); for a playlist, first entry")
    p.add_argument("--max", type=int, dest="limit", help="stop after this many segments")
    p.add_argument("--group", type=int,
                   help="which number in the file name is the segment index, counting from 1 "
                        "(default: guess, e.g. the 2 in seg-2-v1-a1.ts)")
    p.add_argument("--variant", type=int,
                   help="for a master playlist: which variant to download, counting from 1 "
                        "(default: highest bandwidth; the list is printed while running)")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'",
                   help="extra HTTP header, can be repeated (e.g. -H 'Referer: https://site/')")
    p.add_argument("--retries", type=int, default=3, help="attempts per segment on network errors (default: 3)")
    p.add_argument("--keep", metavar="DIR", help="keep downloaded segments in DIR instead of a temp folder")
    p.add_argument("--ffmpeg", default="ffmpeg", help="path to the ffmpeg binary (default: ffmpeg from PATH)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        sys.exit(f"error: ffmpeg not found ({args.ffmpeg!r}); install it or pass --ffmpeg /path/to/ffmpeg")

    headers = {"User-Agent": USER_AGENT}
    for h in args.header:
        name, sep, value = h.partition(":")
        if not sep:
            sys.exit(f"error: bad header {h!r}, expected 'Name: value'")
        headers[name.strip()] = value.strip()

    playlist = None
    try:
        if is_playlist(args.url):
            playlist = playlist_segments(args.url, headers, args.retries, args.variant)
            playlist = playlist[args.start - 1:][:args.limit]
            print(f"playlist has {len(playlist)} segment(s) to download", file=sys.stderr)
        else:
            make_url, given = build_template(args.url, args.group)
            print(f"segment number in URL: {given}; starting from {args.start}", file=sys.stderr)
    except ValueError as err:
        sys.exit(f"error: {err}")
    except urllib.error.HTTPError as err:
        sys.exit(f"error: HTTP {err.code} for {err.url}")
    except urllib.error.URLError as err:
        sys.exit(f"error: {err.reason}")

    if args.keep:
        os.makedirs(args.keep, exist_ok=True)
        workdir, cleanup = args.keep, False
    else:
        workdir, cleanup = tempfile.mkdtemp(prefix="ts-segments-"), True

    try:
        if playlist is not None:
            files = download_list(playlist, workdir, headers, args.retries)
        else:
            files = download_segments(make_url, args.start, args.limit, workdir, headers, args.retries)
        if not files:
            sys.exit(f"error: segment {args.start} was not found, nothing to join")
        print(f"downloaded {len(files)} segment(s), joining into {args.output}", file=sys.stderr)
        concat(files, args.output, workdir, ffmpeg)
    except urllib.error.HTTPError as err:
        sys.exit(f"error: HTTP {err.code} for {err.url}")
    except urllib.error.URLError as err:
        sys.exit(f"error: {err.reason}")
    except subprocess.CalledProcessError as err:
        sys.exit(f"error: ffmpeg failed with exit code {err.returncode}")
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"done: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
