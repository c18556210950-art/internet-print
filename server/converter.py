"""Convert office and Markdown uploads to cached PDFs as the service user."""
import fcntl
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

OFFICE_EXT = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
DOCUMENT_EXT = OFFICE_EXT | {'.md', '.markdown'}


def as_pdf(source: Path) -> Path:
    if source.suffix.lower() not in DOCUMENT_EXT:
        return source
    target = source.with_name(source.name + '.pdf')
    with open(source.parent / '.conversion.lock', 'a') as lock:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError('转换繁忙，请稍后重试')
                time.sleep(.2)
        if target.exists():
            return target
        with tempfile.TemporaryDirectory(prefix='print-convert-', dir=source.parent) as tmp:
            tmp = Path(tmp)
            profile = tmp / 'profile'
            user = profile / 'user'
            user.mkdir(parents=True)
            (user / 'registrymodifications.xcu').write_text('''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>''', encoding='utf-8')
            input_path = source
            if source.suffix.lower() in {'.md', '.markdown'}:
                from markdown_doc import render_markdown
                input_path = tmp / 'markdown.docx'
                render_markdown(source, input_path)
            proc = subprocess.Popen(['libreoffice', '-env:UserInstallation=' + profile.as_uri(),
                '--headless', '--nologo', '--nodefault', '--norestore',
                '--convert-to', 'pdf', '--outdir', str(tmp), str(input_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise RuntimeError('文件转换超时，请简化文件或先导出为 PDF')
            result = tmp / (input_path.stem + '.pdf')
            if proc.returncode or not result.is_file() or result.stat().st_size == 0:
                raise RuntimeError('文件转换失败：请检查文件是否损坏、加密，或先导出为 PDF')
            result.replace(target)
    return target
