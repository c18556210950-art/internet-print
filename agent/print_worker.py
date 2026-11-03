"""Print PDFs/images using a per-job DEVMODE; leave printer defaults intact."""
import json
import sys
import time
from pathlib import Path

import fitz
import win32con
import win32gui
import win32print
import win32ui
from PIL import Image, ImageWin


def printer_dc(printer, sides, orientation):
    handle = win32print.OpenPrinter(printer)
    try:
        mode = win32print.GetPrinter(handle, 2)['pDevMode']
        mode.Fields |= win32con.DM_ORIENTATION | win32con.DM_PAPERSIZE | win32con.DM_DUPLEX | win32con.DM_COPIES
        mode.Orientation = 2 if orientation == 'landscape' else 1
        mode.PaperSize = 9  # DMPAPER_A4
        mode.Copies = 1
        mode.Duplex = {'simplex': 1, 'duplexlong': 2, 'duplexshort': 3}[sides]
        result = win32print.DocumentProperties(0, handle, printer, mode, mode, win32con.DM_IN_BUFFER | win32con.DM_OUT_BUFFER)
        if result < 0:
            raise RuntimeError('打印驱动不接受所选设置')
        expected_orientation = 2 if orientation == 'landscape' else 1
        if mode.Orientation != expected_orientation or mode.Duplex != {'simplex':1,'duplexlong':2,'duplexshort':3}[sides]:
            raise RuntimeError('打印驱动不支持所选方向或双面方式')
        raw = win32gui.CreateDC('WINSPOOL', printer, mode)
        return win32ui.CreateDCFromHandle(raw)
    finally:
        win32print.ClosePrinter(handle)


def print_document(path, printer, sides, orientation):
    started = time.monotonic()
    dc = printer_dc(printer, sides, orientation)
    opened = False
    try:
        dc.StartDoc('Web Print ' + Path(path).stem)
        opened = True
        width, height = dc.GetDeviceCaps(8), dc.GetDeviceCaps(10)
        dpi_x, dpi_y = dc.GetDeviceCaps(88), dc.GetDeviceCaps(90)
        pages = 0
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise RuntimeError('文件已加密，无法打印')
            if doc.page_count == 0:
                raise RuntimeError('文件没有可打印页面')
            for page in doc:
                # Fit without cropping or rotating text. Render at 300 dpi maximum.
                scale = min(width / dpi_x * 72 / page.rect.width,
                            height / dpi_y * 72 / page.rect.height)
                render_scale = min(300 / 72, scale * max(dpi_x, dpi_y) / 72)
                pix = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), colorspace=fitz.csRGB, alpha=False)
                image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                out_w = round(page.rect.width * scale / 72 * dpi_x)
                out_h = round(page.rect.height * scale / 72 * dpi_y)
                x, y = (width-out_w)//2, (height-out_h)//2
                dc.StartPage()
                ImageWin.Dib(image).draw(dc.GetHandleOutput(), (x, y, x+out_w, y+out_h))
                dc.EndPage()
                pages += 1
        dc.EndDoc()
        opened = False
        return {'pages': pages, 'submit_seconds': round(time.monotonic()-started, 3)}
    finally:
        if opened:
            dc.AbortDoc()
        dc.DeleteDC()


if __name__ == '__main__':
    result = print_document(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(json.dumps(result))
