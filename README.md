# Internet Print · 网页远程打印

在网页上传文件，由 Windows 电脑上的代理取件并提交本地打印机。服务器和电脑之间使用 HTTPS，电脑无需开放入站端口。

## 功能

- PDF 和图片：JPG、PNG、WEBP、TIFF、BMP。
- Word：DOC、DOCX；Excel：XLS、XLSX；PowerPoint：PPT、PPTX。
- Markdown：MD、MARKDOWN；支持中文、标题、列表、加粗、表格和代码块。图片转换为替代说明文字。
- A4 单面、双面长边翻页、双面短边翻页；纵向和横向。
- 密码登录、任务列表、处理耗时和失败信息；上传文件最大 50 MB，任务保留七天并在后续上传时清理。
- Windows 原生打印队列，每个任务独立设置方向和双面，不修改系统打印默认值。

```text
浏览器 → HTTPS / Nginx → Flask + SQLite
                              ↓ Office / Markdown 转 PDF
Windows 打印代理 ← HTTPS 取件 ← 文件与任务队列
       ↓
Windows 打印驱动 → USB 或网络打印机
```

电脑和代理必须开机运行；打印机必须已在该电脑安装可用驱动。自动双面需要打印机和驱动支持。网页显示“已提交打印机”表示 Windows 已接收任务，不表示实际出纸完成。

## Linux 服务器部署

以下示例使用 Python 3.9+、Nginx、systemd 和 LibreOffice。源码中的 SQLite 数据目录与程序目录分开管理。

1. 安装系统组件及中文字体。例如 Alibaba Cloud Linux / RHEL 系：

   ```bash
   sudo dnf install python3 python3-pip nginx libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-headless wqy-microhei-fonts
   ```

   Ubuntu / Debian 可安装 `python3-venv nginx libreoffice-writer libreoffice-calc libreoffice-impress fonts-wqy-microhei`。

2. 创建服务用户，将 `server/` 内容复制到 `/opt/print-portal/`：

   ```bash
   sudo useradd --system --shell /usr/sbin/nologin printportal
   sudo mkdir -p /opt/print-portal
   sudo cp -r server/. /opt/print-portal/
   sudo python3 -m venv /opt/print-portal/venv
   sudo /opt/print-portal/venv/bin/pip install -r /opt/print-portal/requirements.txt
   sudo mkdir -p /opt/print-portal/data/uploads
   sudo chown -R printportal:printportal /opt/print-portal/data
   sudo chmod 750 /opt/print-portal/data /opt/print-portal/data/uploads
   ```

3. 复制环境变量示例，并填写自己的访问密码、代理令牌和会话密钥。令牌与会话密钥应分别生成：

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   sudo install -m 600 deploy/print-portal.env.example /etc/print-portal.env
   sudoedit /etc/print-portal.env
   sudo install -m 644 deploy/print-portal.service /etc/systemd/system/print-portal.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now print-portal
   curl http://127.0.0.1:8099/health
   ```

4. 在配置了自己域名及 HTTPS 证书的 Nginx `server` 块内加入 `deploy/nginx-print.conf` 中的两个 `location` 块，然后运行 `sudo nginx -t` 并重载 Nginx。

5. 打开 `https://你的域名/print/`。登录 Cookie 默认要求 HTTPS；服务固定挂载在 `/print/`，Nginx 会转发到本机 `8099` 端口。

## Windows 打印代理

在安装了目标打印机驱动的 Windows 用户会话中执行：

```powershell
cd agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.json config.json
Get-Printer | Select-Object Name
```

编辑 `config.json`：填写服务器 URL、与服务器 `PRINT_AGENT_TOKEN` 一致的令牌，以及准确的 Windows 打印机名称。接着运行：

```powershell
.\.venv\Scripts\python.exe agent.py
```

需要后台运行时，可在 Windows 任务计划程序中创建“用户登录时”触发的任务，程序填写虚拟环境内 `pythonw.exe` 的绝对路径，参数填写 `agent.py`，起始目录填写 `agent` 目录的绝对路径。使用安装了打印机的同一个 Windows 用户运行。

代理轮询间隔默认 0.5 秒，复用 HTTP 连接。日志保存在 `agent/agent.log`；临时文件保存在 `agent/work/`，任务结束后删除。无需 SumatraPDF。

## 排版与运行说明

- Office 由 LibreOffice 转换，字体、复杂排版、Excel 打印区域和分页可能与 Microsoft Office 不同。Excel 按文件原有打印设置导出，不自动把整个工作簿缩成一页。
- 方向选项控制 A4 纸张方向，内容等比例适配可打印区域，不裁切，也不重新排版已有 PDF 或幻灯片。
- Windows 端逐页以最高 300 DPI 渲染后提交打印。大文件和复杂文档耗时更长。
- 服务端禁用转换宏，Markdown 不加载外部图片。此项目是供受信任用户使用的小型打印服务，上传密码不应公开分享。
- 打印任务被领取十分钟后仍未回报时，会重新入队。如果纸张已经输出但回报丢失，可能重复打印，操作前先确认实际状态。
- 本仓库不包含真实服务器地址、访问密码、代理令牌、数据库、上传文档、日志或第三方二进制文件。示例配置中的占位符必须替换。

## 验证

Linux 环境安装 `server/requirements.txt` 后运行：

```bash
python -m unittest discover -s tests -v
```

接口测试覆盖认证、旧数据库迁移、任务选项保存与传递、非法选项拒绝。实际转换需要安装 LibreOffice，真实打印需要 Windows 和可用打印机。

## 依赖

Flask、Gunicorn、LibreOffice、Python-Markdown、Beautiful Soup、python-docx、Requests、PyMuPDF、Pillow、pywin32。依赖遵循各自许可证；PyMuPDF 的使用和分发应遵循其 AGPL 或商业许可条款。
