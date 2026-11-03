import hmac
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from converter import as_pdf, DOCUMENT_EXT

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PRINT_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "print.db"

PASSWORD = os.environ.get("PRINT_PASSWORD", "")
AGENT_TOKEN = os.environ.get("PRINT_AGENT_TOKEN", "")
SECRET_KEY = os.environ.get("PRINT_SECRET", "")
MAX_BYTES = 50 * 1024 * 1024

ALLOWED_EXT = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}

ALLOWED_EXT.update({ext: "application/octet-stream" for ext in DOCUMENT_EXT})

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=MAX_BYTES,
    SESSION_COOKIE_PATH="/print",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
CN_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CN_TZ).replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def secure_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            finished_at TEXT
        )
        """
    )
    columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    for name, default in (("sides", "simplex"), ("orientation", "portrait")):
        if name not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN " + name + " TEXT NOT NULL DEFAULT '" + default + "'")
    db.commit()
    db.close()


def cleanup_old_jobs(days: int = 7) -> None:
    cutoff = (datetime.now(CN_TZ) - timedelta(days=days)).replace(microsecond=0).isoformat()
    db = get_db()
    rows = db.execute("SELECT id, stored_name FROM jobs WHERE created_at < ?", (cutoff,)).fetchall()
    for row in rows:
        path = UPLOAD_DIR / row["stored_name"]
        try:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".pdf").unlink(missing_ok=True)
        except OSError:
            pass
    db.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
    db.commit()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("login")
        return fn(*args, **kwargs)

    return wrapper


def agent_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {AGENT_TOKEN}"
        if not AGENT_TOKEN or not hmac.compare_digest(header, expected):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def sniff_content_type(head: bytes, ext: str) -> str:
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image/tiff"
    if head.startswith(b"BM"):
        return "image/bmp"
    return ALLOWED_EXT.get(ext, "")


@app.route("/health")
def health():
    return "ok", 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect("./")
    error = ""
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if PASSWORD and hmac.compare_digest(supplied, PASSWORD):
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            return redirect("./")
        time.sleep(0.6)
        error = "密码不对"
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("login")


@app.route("/")
@login_required
def index():
    jobs = get_db().execute(
        "SELECT id, original_name, size, status, message, created_at, sides, orientation FROM jobs ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return render_template("index.html", jobs=jobs)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    sides = request.form.get("sides", "simplex")
    orientation = request.form.get("orientation", "portrait")
    if sides not in {"simplex", "duplexlong", "duplexshort"} or orientation not in {"portrait", "landscape"}:
        return "打印选项无效", 400
    upload_file = request.files.get("file")
    if not upload_file or not upload_file.filename:
        return "没有选择文件", 400

    name = os.path.basename(upload_file.filename).replace("\x00", "").strip()
    if not name:
        return "文件名无效", 400
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        return "支持 PDF、图片、Word、Excel、PPT 和 Markdown。", 400

    head = upload_file.stream.read(16)
    upload_file.stream.seek(0)
    content_type = sniff_content_type(head, ext)
    if not content_type:
        return "文件内容与扩展名不匹配。", 400

    stored_name = f"{uuid.uuid4().hex}{ext}"
    target = UPLOAD_DIR / stored_name
    upload_file.save(target)
    size = target.stat().st_size
    if size == 0:
        target.unlink(missing_ok=True)
        return "空文件。", 400

    db = get_db()
    db.execute(
        "INSERT INTO jobs (original_name, stored_name, content_type, size, status, created_at, sides, orientation) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
        (name[:180], stored_name, content_type, size, now_iso(), sides, orientation),
    )
    db.commit()
    cleanup_old_jobs()
    return redirect("./")


@app.get("/api/jobs")
@login_required
def api_jobs():
    rows = get_db().execute(
        "SELECT id, original_name, status, message, created_at, sides, orientation FROM jobs ORDER BY id DESC LIMIT 30"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/agent/next")
@agent_required
def agent_next():
    db = get_db()
    stale_before = (datetime.now(CN_TZ) - timedelta(minutes=10)).replace(microsecond=0).isoformat()
    db.execute(
        "UPDATE jobs SET status='pending', claimed_at=NULL WHERE status='printing' AND claimed_at < ?",
        (stale_before,),
    )
    db.commit()

    db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        "SELECT id, original_name, stored_name, content_type, size, sides, orientation FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        db.commit()
        return "", 204
    db.execute(
        "UPDATE jobs SET status='printing', claimed_at=? WHERE id=?",
        (now_iso(), row["id"]),
    )
    db.commit()
    return jsonify(
        {
            "id": row["id"],
            "filename": row["original_name"],
            "content_type": row["content_type"],
            "size": row["size"],
            "sides": row["sides"],
            "orientation": row["orientation"],
            "file_url": f"/print/api/agent/jobs/{row['id']}/file",
        }
    )


@app.get("/api/agent/jobs/<int:job_id>/file")
@agent_required
def agent_file(job_id: int):
    row = get_db().execute(
        "SELECT stored_name, original_name, content_type FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if row is None:
        abort(404)
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        abort(404)
    started = time.monotonic()
    try:
        printable = as_pdf(path)
    except Exception as exc:
        app.logger.exception("conversion failed for job %s", job_id)
        return jsonify({"error": "文档转换失败，请检查文件是否加密或损坏，或先导出为 PDF"}), 422
    converted = printable != path
    app.logger.info("job %s prepare_seconds=%.3f", job_id, time.monotonic() - started)
    return send_file(printable, mimetype="application/pdf" if converted else row["content_type"],
                     as_attachment=True, download_name=Path(row["original_name"]).stem + ".pdf" if converted else row["original_name"])


@app.post("/api/agent/jobs/<int:job_id>/status")
@agent_required
def agent_status(job_id: int):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    message = str(payload.get("message", ""))[:500]
    if status not in ("done", "failed"):
        return jsonify({"error": "bad status"}), 400
    db = get_db()
    row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    db.execute(
        "UPDATE jobs SET status=?, message=?, finished_at=? WHERE id=?",
        (status, message, now_iso(), job_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_e):
    return f"文件太大，最大 {MAX_BYTES // (1024 * 1024)} MB。", 413


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8099, debug=False)
