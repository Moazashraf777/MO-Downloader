# MO Downloader - Backend (النسخة 2: تنزيل في الخلفية + نسبة التقدم)
#
# الفكرة:
#   1) POST /api/start          -> نبدأ شغل في الخلفية ونرجّع job_id
#   2) GET  /api/progress/<id>  -> الموقع بيسأل: وصلنا لفين؟ (نسبة، سرعة، وقت متبقي)
#   3) GET  /api/file/<id>      -> لما يخلص، المتصفح يحمّل الملف


# MO Downloader - Backend
# Background downloads + progress tracking

import os
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


# ---- Version ----
@app.route("/version")
def version():
    return jsonify(
        yt_dlp_version=yt_dlp.version.__version__
    )


# ---- Settings ----
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}

MAX_DURATION_SECONDS = int(
    os.environ.get("MAX_DURATION_SECONDS", "0")
)

MAX_FILE_BYTES = int(
    os.environ.get("MAX_FILE_BYTES", "0")
)

JOB_MAX_AGE_SECONDS = 60 * 60

jobs = {}
jobs_lock = threading.Lock()


def update_job(job_id, **changes):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(changes)


def cleanup_old_jobs():
    now = time.time()

    with jobs_lock:
        old_ids = [
            j for j, v in jobs.items()
            if now - v["created"] > JOB_MAX_AGE_SECONDS
        ]

        for j in old_ids:
            tmpdir = jobs[j].get("tmpdir")

            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

            del jobs[j]


def fail_job(job_id, tmpdir, message):
    shutil.rmtree(tmpdir, ignore_errors=True)

    update_job(
        job_id,
        status="error",
        error=message,
        tmpdir=None
    )


def build_format_options(fmt):
    if fmt == "mp4":
        return {
            "format": (
                "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]"
                "/b[ext=mp4]/bv*[height<=1080]+ba/b"
            ),
            "merge_output_format": "mp4",
        }

    return {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }


def run_job(job_id, url, fmt):

    tmpdir = tempfile.mkdtemp(prefix="mo_")
    update_job(job_id, tmpdir=tmpdir)

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    base_opts.update(build_format_options(fmt))

    try:
        # Read video information
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist" or "entries" in info:
            raise ValueError(
                "Playlists are not supported. Please use a single video link."
            )

        if info.get("is_live"):
            raise ValueError("Live streams are not supported.")

        if (
            MAX_DURATION_SECONDS
            and (info.get("duration") or 0) > MAX_DURATION_SECONDS
        ):
            raise ValueError(
                f"This video is too long. "
                f"The limit is {MAX_DURATION_SECONDS // 60} minutes."
            )

        # Calculate download progress
        streams = info.get("requested_formats") or [info]

        sizes = [
            s.get("filesize") or s.get("filesize_approx")
            for s in streams
        ]

        sizes_known = all(sizes)

        weights = (
            [float(x) for x in sizes]
            if sizes_known
            else [1.0] * len(streams)
        )

        total_weight = sum(weights)

        seen_files = []

        def progress_hook(d):
            if d["status"] not in ("downloading", "finished"):
                return

            name = d.get("filename")

            if name not in seen_files:
                seen_files.append(name)

            idx = min(
                seen_files.index(name),
                len(weights) - 1
            )

            if d["status"] == "finished":
                fraction = 1.0
            else:
                total = (
                    d.get("total_bytes")
                    or d.get("total_bytes_estimate")
                    or 0
                )

                fraction = (
                    (d.get("downloaded_bytes") or 0) / total
                    if total else 0.0
                )

                fraction = min(max(fraction, 0.0), 1.0)

            done_weight = (
                sum(weights[:idx])
                + weights[idx] * fraction
            )

            percent = round(
                done_weight / total_weight * 100,
                1
            )

            speed = d.get("speed")

            if sizes_known and speed:
                eta = int(
                    (total_weight - done_weight) / speed
                )
            else:
                eta = d.get("eta")

            update_job(
                job_id,
                status="downloading",
                percent=percent,
                speed=speed,
                eta=eta
            )

        def postprocessor_hook(d):
            if d["status"] == "started":
                update_job(
                    job_id,
                    status="processing",
                    percent=100.0,
                    speed=None,
                    eta=None
                )

        opts = dict(base_opts)

        opts["outtmpl"] = os.path.join(
            tmpdir,
            "%(title).80s.%(ext)s"
        )

        opts["progress_hooks"] = [progress_hook]
        opts["postprocessor_hooks"] = [postprocessor_hook]

        if MAX_FILE_BYTES:
            opts["max_filesize"] = MAX_FILE_BYTES

        update_job(
            job_id,
            status="downloading",
            percent=0.0
        )

        # Download
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [
            f for f in os.listdir(tmpdir)
            if f.lower().endswith("." + fmt)
        ]

        if not files:
            raise ValueError(
                "The file could not be prepared. Please try again."
            )

        update_job(
            job_id,
            status="done",
            percent=100.0,
            speed=None,
            eta=None,
            filename=files[0],
            path=os.path.join(tmpdir, files[0])
        )

    except ValueError as exc:
        fail_job(job_id, tmpdir, str(exc))

    except yt_dlp.utils.DownloadError:
        app.logger.exception(
            "yt-dlp download failed in job %s",
            job_id
        )

        fail_job(
            job_id,
            tmpdir,
            "Could not download this video. YouTube may have restricted the request."
        )

    except Exception:
        app.logger.exception(
            "Unexpected error in job %s",
            job_id
        )

        fail_job(
            job_id,
            tmpdir,
            "Something went wrong on the server. Please try again."
        )


def error(message, status=400):
    return jsonify(error=message), status


# ---- Health ----
@app.route("/health")
def health():
    return jsonify(
        status="ok",
        message="MO Downloader backend is running"
    )


# ---- Start download ----
@app.route("/api/start", methods=["POST"])
def start():

    data = request.get_json(silent=True) or {}

    url = str(data.get("url", "")).strip()
    fmt = str(data.get("format", "")).strip().lower()

    if fmt not in ("mp4", "mp3"):
        return error("Unsupported format. Choose MP4 or MP3.")

    parsed = urlparse(url)

    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ALLOWED_HOSTS
    ):
        return error("Please enter a valid YouTube link.")

    cleanup_old_jobs()

    job_id = uuid.uuid4().hex

    with jobs_lock:
        jobs[job_id] = {
            "status": "starting",
            "percent": 0.0,
            "speed": None,
            "eta": None,
            "error": None,
            "filename": None,
            "path": None,
            "tmpdir": None,
            "created": time.time(),
        }

    threading.Thread(
        target=run_job,
        args=(job_id, url, fmt),
        daemon=True
    ).start()

    return jsonify(job_id=job_id)


# ---- Progress ----
@app.route("/api/progress/<job_id>")
def progress(job_id):

    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return error(
                "Job not found. It may have expired.",
                404
            )

        return jsonify(
            status=job["status"],
            percent=job["percent"],
            speed=job["speed"],
            eta=job["eta"],
            error=job["error"],
            filename=job["filename"],
        )


# ---- Download file ----
@app.route("/api/file/<job_id>")
def get_file(job_id):

    with jobs_lock:
        job = jobs.get(job_id)

        ready = bool(
            job
            and job["status"] == "done"
            and job["path"]
        )

        path = job["path"] if ready else None
        filename = job["filename"] if ready else None
        tmpdir = job["tmpdir"] if ready else None

    if not ready:
        return error("File is not ready.", 404)

    def stream_and_cleanup():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)

                    if not chunk:
                        break

                    yield chunk

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

            with jobs_lock:
                jobs.pop(job_id, None)

    ext = os.path.splitext(filename)[1].lower()

    mimetype = (
        "audio/mpeg"
        if ext == ".mp3"
        else "video/mp4"
    )

    base = (
        os.path.splitext(filename)[0]
        .encode("ascii", "ignore")
        .decode()
        .replace('"', "")
        .strip()
    )

    ascii_name = (base or "download") + ext

    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )

    return Response(
        stream_and_cleanup(),
        mimetype=mimetype,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(os.path.getsize(path)),
        },
    )
