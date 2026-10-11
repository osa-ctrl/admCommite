#!/usr/bin/env python3
"""Download sibling TS segments from one segment URL and join them with FFmpeg.

Example:
    python3 ts_download.py "https://host/path/seg-2-v1-a1.ts" -o video.mp4

The script finds the segment number in the file name (seg-2-...), then
downloads seg-1, seg-2, seg-3, ... until the server answers 404, and
concatenates the pieces with FFmpeg's concat demuxer (-c copy, no re-encode).
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
        description="Download all TS segments that sit next to the given one and join them into one MP4 with FFmpeg.")
    p.add_argument("url", help="URL of any segment, e.g. https://host/path/seg-2-v1-a1.ts")
    p.add_argument("-o", "--output", default="output.mp4", help="output file (default: output.mp4)")
    p.add_argument("--start", type=int, default=1, help="first segment number to download (default: 1)")
    p.add_argument("--max", type=int, dest="limit", help="stop after this many segments")
    p.add_argument("--group", type=int,
                   help="which number in the file name is the segment index, counting from 1 "
                        "(default: guess, e.g. the 2 in seg-2-v1-a1.ts)")
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

    try:
        make_url, given = build_template(args.url, args.group)
    except ValueError as err:
        sys.exit(f"error: {err}")
    print(f"segment number in URL: {given}; starting from {args.start}", file=sys.stderr)

    if args.keep:
        os.makedirs(args.keep, exist_ok=True)
        workdir, cleanup = args.keep, False
    else:
        workdir, cleanup = tempfile.mkdtemp(prefix="ts-segments-"), True

    try:
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
