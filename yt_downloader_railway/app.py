import os
import re
import time
import uuid
import shutil
import threading
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify, after_this_request
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path("/tmp/yt_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()


def cleanup_old_files(max_age_seconds: int = 1800):
    now = time.time()
    try:
        for item in DOWNLOAD_DIR.iterdir():
            try:
                if now - item.stat().st_mtime > max_age_seconds:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def get_audio_selector(lang: str) -> str:
    if lang == "hi":
        return "bestaudio[language^=hi]/bestaudio[language*=hi]/bestaudio[acodec^=mp4a]/bestaudio"
    elif lang == "en":
        return "bestaudio[language^=en]/bestaudio[language*=en]/bestaudio[acodec^=mp4a]/bestaudio"
    return "bestaudio[acodec^=mp4a]/bestaudio"


def safe_filename(title: str, lang: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]+', "", title).strip()
    if not cleaned:
        cleaned = "youtube_video"
    cleaned = cleaned[:80]
    suffix = {"hi": "Hindi", "en": "English", "original": "Original"}.get(lang, "Original")
    return f"{cleaned} [{suffix}] 1080p.mp4"


def run_download_job(job_id: str, url: str, lang: str):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def update_job(**kwargs):
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(kwargs)

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta")
            pct = (downloaded / total * 100.0) if total > 0 else 0.0
            pct = max(0.0, min(98.0, pct))
            speed_mb = f"{speed / (1024 * 1024):.1f} MB/s" if speed else "Calculating..."
            eta_str = f"{int(eta)}s" if eta is not None else "..."
            update_job(
                state="downloading",
                percent=round(pct, 1),
                message=f"Downloading ({speed_mb} • ETA {eta_str})..."
            )
        elif status == "finished":
            update_job(
                state="merging",
                percent=99.0,
                message="Merging 1080p video & audio with FFmpeg..."
            )

    audio_selector = get_audio_selector(lang)
    format_selector = (
        f"bestvideo[height<=1080][vcodec^=avc1]+{audio_selector}/"
        f"bestvideo[height<=1080][ext=mp4]+{audio_selector}/"
        f"bestvideo[height<=1080]+{audio_selector}/"
        f"best[height<=1080]/{audio_selector}"
    )

    outtmpl = str(job_dir / "video.%(ext)s")

    ydl_opts = {
        "format": format_selector,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 8,
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [progress_hook],
        "postprocessor_args": {
            "merger": ["-movflags", "+faststart"]
        },
    }

    try:
        update_job(state="downloading", percent=2.0, message="Fetching video & audio streams...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title") or "youtube_video"

        merged_candidates = list(job_dir.glob("*.mp4"))
        if not merged_candidates:
            all_files = [f for f in job_dir.iterdir() if f.is_file()]
            if not all_files:
                raise RuntimeError("No output file produced.")
            final_file = max(all_files, key=lambda p: p.stat().st_size)
        else:
            final_file = max(merged_candidates, key=lambda p: p.stat().st_size)

        download_name = safe_filename(title, lang)
        update_job(
            state="completed",
            percent=100.0,
            message="Ready! Starting fast download...",
            file_path=str(final_file),
            job_dir=str(job_dir),
            download_name=download_name,
        )
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        update_job(
            state="error",
            percent=0.0,
            message=f"Error: {str(exc)}"
        )


@app.route("/")
def index():
    cleanup_old_files()
    return render_template("index.html")


@app.route("/start-download", methods=["POST"])
def start_download():
    cleanup_old_files()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    lang = (data.get("language") or "original").strip().lower()

    if lang not in ("original", "hi", "en"):
        lang = "original"

    if not url or ("youtube.com" not in url and "youtu.be" not in url):
        return jsonify({"error": "Please paste a valid YouTube link."}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "state": "queued",
            "percent": 0.0,
            "message": "Starting job...",
            "file_path": None,
            "job_dir": None,
            "download_name": "video_1080p.mp4",
        }

    worker = threading.Thread(target=run_download_job, args=(job_id, url, lang), daemon=True)
    worker.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"state": "error", "percent": 0, "message": "Job not found or expired."}), 404

    return jsonify({
        "state": job["state"],
        "percent": job["percent"],
        "message": job["message"],
        "download_name": job["download_name"],
    })


@app.route("/file/<job_id>")
def get_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job or job.get("state") != "completed" or not job.get("file_path"):
        return jsonify({"error": "File not ready."}), 404

    file_path = Path(job["file_path"])
    job_dir_path = Path(job["job_dir"]) if job.get("job_dir") else None
    download_name = job.get("download_name") or "video_1080p.mp4"

    if not file_path.exists():
        return jsonify({"error": "File already removed or missing."}), 404

    @after_this_request
    def remove_after_send(response):
        def delayed_cleanup(target_dir: Path):
            time.sleep(10)
            if target_dir and target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            with jobs_lock:
                jobs.pop(job_id, None)

        if job_dir_path:
            threading.Thread(target=delayed_cleanup, args=(job_dir_path,), daemon=True).start()
        return response

    return send_file(
        file_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
