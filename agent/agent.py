import json
import logging
import logging.handlers
import msvcrt
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

HTTP = requests.Session()
HTTP.trust_env = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "agent.log"
LOCK_PATH = BASE_DIR / "agent.lock"
WORK_DIR = BASE_DIR / "work"
WORK_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("print-agent")
logger.setLevel(logging.INFO)
handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(handler)

_lock_handle = None


def acquire_lock() -> bool:
    global _lock_handle
    _lock_handle = open(LOCK_PATH, "a+")
    try:
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("server_url", "token", "printer_name"):
        if not cfg.get(key):
            raise SystemExit(f"config.json missing {key}")
    cfg["poll_seconds"] = max(0.5, float(cfg.get("poll_seconds", 0.5)))
    cfg["server_url"] = cfg["server_url"].rstrip("/")
    return cfg


def http_json(url: str, token: str, method: str = "GET", payload: Optional[dict] = None, timeout: int = 30):
    response = HTTP.request(method, url, json=payload,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=(10, timeout))
    response.raise_for_status()
    return response.status_code, response.content


def download_file(url: str, token: str, dest: Path) -> Path:
    started = time.monotonic()
    with HTTP.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=(10, 120), stream=True) as resp:
        if resp.status_code == 422:
            raise RuntimeError("文档转换失败，请检查文件是否加密或损坏，或先导出为 PDF")
        resp.raise_for_status()
        if resp.headers.get('Content-Type', '').startswith('application/pdf'):
            dest = dest.with_suffix('.pdf')
        with open(dest, 'wb') as f:
            for chunk in resp.iter_content(256 * 1024):
                f.write(chunk)
    logger.info("download/convert %s bytes=%d seconds=%.3f", dest.name, dest.stat().st_size, time.monotonic()-started)
    return dest


def print_file(cfg: dict, path: Path, job: dict) -> dict:
    sides = job.get('sides', 'simplex')
    orientation = job.get('orientation', 'portrait')
    if sides not in {'simplex', 'duplexlong', 'duplexshort'} or orientation not in {'portrait', 'landscape'}:
        raise RuntimeError('打印选项无效')
    python = Path(sys.executable).with_name('python.exe')
    cmd = [str(python), str(BASE_DIR / 'print_worker.py'), str(path), cfg['printer_name'], sides, orientation]
    logger.info('submitting %s sides=%s orientation=%s', path.name, sides, orientation)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=540,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or '打印提交失败')[-450:])
    result = json.loads(proc.stdout)
    logger.info('spool result %s', result)
    return result


def report(cfg: dict, job_id: int, status: str, message: str) -> None:
    url = f"{cfg['server_url']}/api/agent/jobs/{job_id}/status"
    for attempt in range(1, 4):
        try:
            http_json(url, cfg["token"], "POST", {"status": status, "message": message[:500]})
            return
        except Exception as exc:
            logger.warning("status report attempt %d failed: %s", attempt, exc)
            time.sleep(2 * attempt)
    logger.error("could not report status for job %s", job_id)


def main() -> None:
    if not acquire_lock():
        logger.info("another agent instance is already running")
        return
    cfg = load_config()
    logger.info("agent started; server=%s printer=%s", cfg["server_url"], cfg["printer_name"])
    while True:
        try:
            status, body = http_json(f"{cfg['server_url']}/api/agent/next", cfg["token"])
        except requests.HTTPError as exc:
            logger.warning("next HTTP %s", exc.response.status_code)
            time.sleep(5)
            continue
        except Exception as exc:
            logger.warning("server unreachable: %s", exc)
            time.sleep(8)
            continue

        if status == 204 or not body:
            time.sleep(cfg["poll_seconds"])
            continue

        job = json.loads(body)
        job_id = int(job["id"])
        job_started = time.monotonic()
        filename = os.path.basename(job.get("filename") or f"job-{job_id}.pdf")
        suffix = Path(filename).suffix or ".pdf"
        dest = WORK_DIR / f"job-{job_id}{suffix}"
        print_path = dest
        try:
            file_url = urljoin(cfg["server_url"] + "/", job["file_url"])
            print_path = download_file(file_url, cfg["token"], dest)
            result = print_file(cfg, print_path, job)
            elapsed = time.monotonic() - job_started
            report(cfg, job_id, "done", f"已提交打印机 · {result['pages']} 页 · 处理 {elapsed:.1f} 秒")
            logger.info("job %s done", job_id)
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            report(cfg, job_id, "failed", str(exc))
        finally:
            for path in {dest, print_path}:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
