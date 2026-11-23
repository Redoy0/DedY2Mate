# ytbulk — bulk YouTube downloader for your own videos

Reads a text file of YouTube URLs (public or **unlisted**) and downloads each video as an MP4 at the highest available quality. It shows progress, skips videos it has already downloaded, resumes interrupted downloads and writes a summary report.

The download engine is [yt-dlp](https://github.com/yt-dlp/yt-dlp). FFmpeg merges the separate video and audio streams that YouTube serves.

> Only download videos you own or have permission to download. The tool does not bypass logins, DRM or other access controls. Unlisted videos work because the URL itself grants access.

---

## 1a. Windows: quick start

1. Copy the `DedY2Mate` folder anywhere. A short path such as `C:\ytbulk` is best.
2. Double-click **`setup.bat`**. This is only needed once and takes a couple of minutes.
   - If Python 3.10+ is missing, it offers to install Python 3.12 with `winget`, with no admin rights needed. Answer **Y**, then run `setup.bat` again when it finishes.
   - It installs everything else into `.venv` inside the folder: yt-dlp, a bundled FFmpeg, and the Deno JavaScript runtime. You don't need FFmpeg, Node.js or admin rights.
3. Double-click **`ytbulk-ui.bat`**. Your browser opens the downloader.
   - Drop your `videos.txt` on the page, choose a folder (for example `D:\Videos`), and press **Start download**.
   - Keep the black console window open while downloading. Closing it stops the tool.

**Command line:** run `ytbulk.bat -i videos.txt -o D:\Videos --codec h264`. It takes the same options as `ytbulk.sh`.

Windows notes:
- **Cookies:** Chrome and Edge encrypt their cookies on Windows, so `--cookies-from-browser chrome` usually fails. Use `firefox`, or a `cookies.txt` file instead. Cookies are only needed for age-restricted or private videos.
- **Path length:** Windows paths are limited to 260 characters, so titles are trimmed to 120 characters on Windows. Use a short output folder.
- **Moving between computers:** if you copy the folder from Linux to Windows, `setup.bat` replaces the Linux `.venv` automatically.
- **Filename templates:** in `cmd`, `%` in `--filename` can be eaten by variable expansion. Put the template in `config.toml` instead.

## 1b. Install (Ubuntu)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg   # ffmpeg is optional, see below
cd DedY2Mate
./setup.sh
```

`setup.sh` creates `.venv/`, installs `requirements.txt` plus the optional Deno runtime, copies `videos.example.txt` to `videos.txt` if that file doesn't exist yet, and runs the environment checks.

- **FFmpeg:** the tool uses the system `ffmpeg` if there is one. Otherwise it uses the static build bundled with the `imageio-ffmpeg` pip package, so `apt install ffmpeg` is recommended but not required.
- **JS runtime:** YouTube now requires yt-dlp to run some JavaScript. The setup scripts install Deno into `.venv` with pip. A `deno`, `node` (20+) or `bun` on your `PATH` also works and is detected automatically.

Check the environment at any time:

```bash
./ytbulk.sh --check
```

## 2. Use

### Browser UI (easiest)

```bash
./ytbulk.sh --ui
```

This starts a small local server and opens `http://127.0.0.1:8765` in your browser. Keep the terminal open while you use the page, and press Ctrl+C in it to quit.

1. **URL list:** drop your `videos.txt` on the page, click to pick it, or paste the URLs. The page shows how many URLs are valid, duplicated or invalid before you start.
2. **Settings:** choose the output folder, the max quality and the format. *Web-compatible (H.264)* is the safest choice for website hosting.
3. **Start download:** shows overall progress plus the current video's title, speed and time remaining.
   - **Stop** keeps the partial file. Press Start again later to resume.
   - **Results** lists every video as Downloaded, Failed (with the reason and a hint) or Already had.
   - **Put failed URLs back in the list** loads only the failures, so you can retry them.

More details:
- The server listens on `127.0.0.1` only, so it can't be reached from other machines.
- Each uploaded list is saved to `<output>/_reports/input-<time>.txt`.
- Options: `--port 9000` picks another port, and `--no-browser` stops the browser from opening automatically.

### Terminal

```bash
# 1. put your URLs in videos.txt (one per line)
# 2. run
./ytbulk.sh                                   # videos.txt -> ./downloads
./ytbulk.sh -i my-list.txt -o /data/videos    # custom input/output
./ytbulk.sh -q 1080 --codec h264              # web-friendly H.264 MP4, max 1080p
./ytbulk.sh --dry-run                         # just validate the list, download nothing
```

**Stop:** press `Ctrl+C`. Partial data is kept in `downloads/.partial/`.
**Resume:** run the same command again. Finished videos are skipped and partial ones continue where they stopped.
**Retry failures:** each run that has failures writes `downloads/_reports/failed-<time>.txt`. Run `./ytbulk.sh -i downloads/_reports/failed-<time>.txt` to retry just those videos.

### What you see

```
✓ [1/3] My first video (1920x1080, 84.2 MiB)
✗ [2/3] https://www.youtube.com/watch?v=abc... private: Private video ...
    hint: Video is private. Make it unlisted, or pass --cookies ...
Overall  ━━━━━━━━━━━━━━━━━━━━          2/3 ✓ 1 ↷ 0 ✗ 1 0:01:12
    video [3/3] Another video  ━━━━━━━━  42%  51.0/121.3 MB  6.2 MiB/s  ETA 00:11
```

## 3. Input file format

```text
# comments and blank lines are ignored
https://www.youtube.com/watch?v=VIDEO_ID_1
https://youtu.be/VIDEO_ID_2?si=xyz        anything after the URL is ignored
https://www.youtube.com/shorts/VIDEO_ID_3
```

- **Accepted forms:** `watch?v=`, `youtu.be/`, `/shorts/`, `/embed/`, `/live/`, and `m.` or `music.` hosts.
- **Duplicates** are detected by video ID, so the same video in two URL forms is downloaded once. Duplicate lines are listed in the report.
- **Invalid lines** are reported and skipped. These include non-YouTube links, malformed IDs and playlist-only URLs.

## 4. Options

| Flag | Config key | Default | Meaning |
|---|---|---|---|
| `-i, --input` | `input` | `videos.txt` | URL list |
| `-o, --output` | `output` | `downloads` | Output directory |
| `-q, --quality` | `quality` | `best` | Max height: `best`, `2160`, `1440`, `1080`, `720`, `480`, `360` |
| `--codec` | `codec` | `any` | `any` = best quality (MP4 may contain AV1/VP9); `h264` = most compatible MP4 (H.264 + AAC, usually ≤1080p) |
| `--filename` | `filename` | `%(title)s [%(id)s].%(ext)s` | [yt-dlp output template](https://github.com/yt-dlp/yt-dlp#output-template) |
| `--safe-filenames` | `safe_filenames` | off | ASCII only, no spaces (nicer for web URLs) |
| `--cookies FILE` | `cookies` | – | Netscape `cookies.txt` (age-restricted/private videos you can access) |
| `--cookies-from-browser B[:PROFILE]` | `cookies_from_browser` | – | e.g. `firefox`, `chrome` |
| `--retries N` | `retries` | `3` | Attempts per video for network / rate-limit errors |
| `--sleep S` | `sleep` | `2` | Pause between videos |
| `--rate-limit R` | `rate_limit` | – | Speed cap, e.g. `5M` |
| `--ffmpeg PATH` | `ffmpeg` | auto | FFmpeg binary |
| `-c, --config FILE` | – | `./config.toml` | TOML config (see `config.example.toml`) |
| `-v, --verbose` | `verbose` | off | Debug-level log file |
| `--check` / `--dry-run` | – | – | Environment check only / list parse only |
| `--ui` [`--port N`] [`--no-browser`] | – | port `8765` | Browser UI instead of terminal mode |

Command-line flags take priority over `config.toml`, which takes priority over the built-in defaults.

**Hosting on a website?** `--codec any` gives the highest resolution, but the MP4 may contain AV1 or VP9 video, which some older browsers and devices (notably older iPhones) cannot play. If you need every visitor to be able to play the files, use `--codec h264`.

## 5. Output layout

```
downloads/
├── My first video [VIDEO_ID_1].mp4
├── Another video [VIDEO_ID_2].mp4
├── .partial/                 # in-progress fragments (resume data); safe to delete when idle
├── .ytbulk-archive.json      # finished IDs -> files (drives "skip already downloaded")
└── _reports/
    ├── report-YYYYmmdd-HHMMSS.txt   # human summary: counts, file paths, failures + hints
    ├── report-YYYYmmdd-HHMMSS.json  # same, machine-readable
    ├── failed-YYYYmmdd-HHMMSS.txt   # re-runnable list of unfinished URLs
    └── ytbulk.log                   # detailed log (cookies/tokens are redacted)
```

- **Filenames:** the original title, sanitized for any filesystem and trimmed to 150 characters, plus `[id]` so two videos with the same title can't overwrite each other. To drop the ID, use `--filename "%(title)s.%(ext)s"`.
- **Re-downloads:** a video is downloaded again only if its file was deleted.

## 6. Error handling

| Situation | Behaviour |
|---|---|
| Invalid URL / duplicate | Reported, skipped before any network call |
| Private / deleted / unavailable | Marked failed with category + hint, run continues |
| Age-restricted / members-only / bot check | Marked failed with a hint to pass `--cookies` |
| Network drop, HTTP 5xx | yt-dlp retries internally (10x), then the whole video is retried up to `--retries` times with backoff; partial data is resumed |
| HTTP 429 (rate limit) | Longer backoff (30s, 60s, …); consider a higher `--sleep` |
| Not enough disk for a video | Checked before download (estimated size ×2 + 200 MB); that video fails, smaller ones continue |
| Disk full / FFmpeg missing mid-run | Run stops cleanly, report is written |
| FFmpeg / yt-dlp / JS runtime missing | Caught by the pre-flight checks before anything starts |
| Ctrl+C | Current video marked interrupted, report + retry list written, exit code 130 |

**Exit codes:** `0` all good · `1` some videos failed · `2` setup/environment problem · `130` stopped by user.

## 7. Cookies (only if needed)

Unlisted videos do **not** need cookies. Cookies are only needed for age-restricted or private videos that your account is allowed to view.

- **Option A:** `--cookies-from-browser firefox` reads cookies directly from your local browser profile.
- **Option B:** export a `cookies.txt` (Netscape format) with a browser extension such as "Get cookies.txt LOCALLY", then:
  ```bash
  chmod 600 cookies.txt
  ./ytbulk.sh --cookies cookies.txt
  ```

Cookie contents are never printed or logged, and the log file redacts anything that looks like a cookie or auth header. `cookies*.txt` is in `.gitignore`.

## 8. Troubleshooting

- **Lots of "unknown", "format" or "bot_check" errors:** YouTube changes frequently. Update yt-dlp with `.venv/bin/pip install -U "yt-dlp[default]"`. `--check` warns when your yt-dlp is more than 60 days old.
- **Only 360p downloads, or "n challenge" warnings in the log:** install a JS runtime (see Install).
- **`python3 -m venv` fails:** run `sudo apt install python3-venv`.

## Project structure

```
DedY2Mate/
├── ytbulk/
│   ├── cli.py          # argument/config parsing, orchestration
│   ├── urls.py         # URL parsing, validation, de-duplication
│   ├── checks.py       # pre-flight checks (python, yt-dlp, ffmpeg, JS runtime, disk, cookies)
│   ├── downloader.py   # yt-dlp wrapper: retries, archive, disk check, error classification
│   ├── ui.py           # rich progress display (terminal)
│   ├── web.py          # local browser UI server (--ui), stdlib only
│   ├── web_ui.html     # the browser UI page
│   └── report.py       # JSON/text report, retry list, console summary
├── setup.sh / setup.bat         # one-time install into .venv (Linux / Windows)
├── ytbulk.sh / ytbulk.bat       # command-line runner
├── ytbulk-ui.bat                # Windows: double-click to open the browser UI
├── requirements.txt
├── config.example.toml
└── videos.example.txt
```
