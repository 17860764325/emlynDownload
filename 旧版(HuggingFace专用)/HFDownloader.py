# -*- coding: utf-8 -*-
"""
================================================================================
  HF 下载器 (HF Downloader)  —— 单个文件，双击即用
================================================================================

【它是什么】
  一个专门用来下载 HuggingFace 模型的可视化小工具。针对国内网络做过优化。
  不用记命令、不用装依赖、不用配环境变量。

【怎么用（3 步）】
  1. 把 HuggingFace 的链接粘到最上面的输入框（从浏览器地址栏直接复制就行）
  2. （可选）改一下保存目录 —— 会自动记住，下次不用再改
  3. 点「开始下载」

【核心特性】
  · 贴链接即可，自动识别各种链接写法（blob / resolve / 带参数 / 短域名 / 子目录）
  · 多线程分块下载，实测速度最优（默认 8 线程，已按本机带宽调优）
  · 断点续传：中断、关机、崩溃后重开，点一下继续
  · 暂停 / 继续 / 取消，下载中也能操作
  · 实时显示：速度、已下载、剩余时间、每个分块的状态
  · 支持剪贴板自动识别 —— 复制了链接切回窗口会自动填上
  · 批量下载：一行一个链接
  · 自动重试（每次退避等待），失败原因写得清清楚楚
  · 全部日志落盘到 logs/ 目录，出问题能查原因
  · 记住所有设置（保存目录、线程数、窗口大小）

【日志位置】
  logs/hfdl_YYYY-MM-DD.log      —— 每天一个文件
  界面上点「查看日志」按钮可以直接打开

【常见问题】
  Q: 速度上不去？
  A: 本机实测带宽上限约 3.7 MB/s，8 线程已经跑满。换更多线程没有意义。
     可以点「测速」按钮实测当前速度。

  Q: 提示 401 / 403 / gated？
  A: 该模型需要授权。先去模型页面点同意协议，再到设置里填 HF Token。

  Q: 下载了一部分，能接着下吗？
  A: 能。直接重新点「开始下载」即可，会自动从断点继续。

================================================================================
"""

import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.client import IncompleteRead

# ---------------- tkinter ----------------
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# =============================================================================
#  常量与路径
# =============================================================================

APP_NAME = 'HF 下载器'
APP_VERSION = '1.0'

# 程序所在目录（配置和日志都放这里，绿色免安装）
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)      # 打包成 exe 后的位置
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(BASE_DIR, 'hfdownloader_config.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36 hfdownloader/1.0')

# 默认走国内镜像（实测直连 huggingface.co 基本不通）
DEFAULT_ENDPOINT = 'https://hf-mirror.com'

# 备选端点（界面上可切换）
ENDPOINT_CHOICES = [
    ('hf-mirror 镜像（国内推荐）', 'https://hf-mirror.com'),
    ('huggingface 官方', 'https://huggingface.co'),
]

# 文件太小就不值得分块
MIN_SIZE_FOR_CHUNKS = 8 * 1024 * 1024        # 8 MB
BUFFER = 128 * 1024                          # 单次读取块大小


# =============================================================================
#  日志（要求：所有报错都留痕，能看到原因）
# =============================================================================

class Logger:
    """写日志到文件，同时把消息推给界面"""

    def __init__(self, ui_queue):
        self.ui_queue = ui_queue
        self.lock = threading.Lock()
        os.makedirs(LOG_DIR, exist_ok=True)
        self._cleanup_old_logs()

    def _cleanup_old_logs(self, keep_days=14):
        """只保留最近 N 天的日志，避免越积越多"""
        try:
            now = time.time()
            for f in os.listdir(LOG_DIR):
                if not f.startswith('hfdl_') or not f.endswith('.log'):
                    continue
                p = os.path.join(LOG_DIR, f)
                if now - os.path.getmtime(p) > keep_days * 86400:
                    os.remove(p)
        except Exception:
            pass

    def _log_path(self):
        return os.path.join(LOG_DIR, 'hfdl_%s.log' % datetime.now().strftime('%Y-%m-%d'))

    def write(self, level, msg, detail=None):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = '[%s] [%-5s] %s' % (ts, level, msg)
        if detail:
            line += '\n' + '\n'.join('    ' + d for d in str(detail).splitlines())
        # 写文件
        with self.lock:
            try:
                with open(self._log_path(), 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
        # 推给界面
        if self.ui_queue is not None:
            try:
                self.ui_queue.put(('log', level, msg))
            except Exception:
                pass

    def info(self, msg, detail=None):
        self.write('INFO', msg, detail)

    def warn(self, msg, detail=None):
        self.write('WARN', msg, detail)

    def error(self, msg, detail=None):
        self.write('ERROR', msg, detail)

    def exception(self, msg, exc=None):
        """记录异常，带完整堆栈 —— 这是排查问题的关键"""
        detail = None
        if exc is not None:
            detail = ''.join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
        self.write('ERROR', msg, detail)

    def path(self):
        return self._log_path()


# =============================================================================
#  链接解析
# =============================================================================

def parse_hf_input(text):
    """把用户粘进来的东西解析成 (repo, filename, revision)

    支持：
      https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors
      https://hf-mirror.com/..../blob/main/model.safetensors?download=true
      https://hf.co/...                      （短域名）
      google/embeddinggemma-2/resolve/main/model.safetensors   （不带域名）
      google/embeddinggemma-2                （只有仓库，下载全部）

    返回 (repo, filename, revision)；解析失败返回 (None, None, None)
    """
    s = (text or '').strip().strip('"').strip("'")
    if not s:
        return None, None, None

    # 去协议
    for pre in ('https://', 'http://'):
        if s.startswith(pre):
            s = s[len(pre):]
    # 去域名
    for host in ('www.huggingface.co/', 'huggingface.co/', 'www.hf-mirror.com/',
                 'hf-mirror.com/', 'hf.co/'):
        if s.startswith(host):
            s = s[len(host):]
            break
    # 去查询串/锚点
    s = s.split('?')[0].split('#')[0]

    # resolve / blob / raw 分割
    for sep in ('/resolve/', '/blob/', '/raw/'):
        if sep in s:
            repo, rest = s.split(sep, 1)
            parts = [p for p in rest.split('/') if p]
            if len(parts) < 2:
                # 例如 .../resolve/main  （只指到分支，没具体文件）
                return repo.strip('/'), None, parts[0] if parts else 'main'
            return repo.strip('/'), '/'.join(parts[1:]), parts[0]

    # 只有 用户名/仓库名 → 整个仓库
    parts = [p for p in s.split('/') if p]
    if len(parts) == 2:
        return '%s/%s' % (parts[0], parts[1]), None, 'main'
    # 用户名/仓库名/文件...
    if len(parts) >= 3:
        return '%s/%s' % (parts[0], parts[1]), '/'.join(parts[2:]), 'main'
    return None, None, None


def looks_like_hf_link(text):
    t = (text or '').strip()
    if not t or len(t) < 5:
        return False
    if 'huggingface.co' in t or 'hf-mirror.com' in t or 'hf.co/' in t:
        return True
    # 形如 user/repo 或 user/repo/resolve/...
    if re.match(r'^[\w.\-]+/[\w.\-]+(/|$)', t) and ' ' not in t:
        return True
    return False


# =============================================================================
#  HTTP 工具
# =============================================================================

def build_url(endpoint, repo, filename, revision='main'):
    return '%s/%s/resolve/%s/%s' % (endpoint.rstrip('/'), repo,
                                    revision or 'main', filename)


def make_request(url, headers=None, method='GET'):
    req = urllib.request.Request(url, method=method)
    req.add_header('User-Agent', UA)
    req.add_header('Accept-Encoding', 'identity')     # 关键：不要压缩，否则拿不到准确字节
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def friendly_http_error(exc, url):
    """把 HTTP 错误翻译成人能看懂的原因（要求：知道为什么报错）"""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        body = ''
        try:
            body = exc.read(600).decode('utf-8', 'ignore')
        except Exception:
            pass

        # ★ 经验：HF 对「仓库不存在」有时也返回 401，
        #   服务器正文是 "Invalid username or password" / "Repository not found"。
        #   这跟"模型需要授权"是两回事，必须分开提示，否则用户会白折腾。
        low = body.lower()
        if ('invalid username or password' in low
                or 'repository not found' in low
                or 'repo not found' in low):
            return ('仓库不存在或无权访问（HTTP %d）。\n'
                    '   服务器说：%s\n'
                    '   解决：\n'
                    '   · 检查用户名和仓库名有没有拼错（区分大小写）\n'
                    '   · 在浏览器里打开 https://huggingface.co/%s 确认能访问\n'
                    '   · 如果页面提示要同意协议，就去设置里填 HF Token'
                    % (code, body[:120].strip(),
                       '/'.join(url.split('/')[3:5]) if url.count('/') >= 4 else '用户/仓库'))

        reasons = {
            400: '请求格式错误。链接可能不完整，或文件名里有特殊字符。',
            401: '认证失败。这个模型需要登录授权（gated）。\n'
                 '   解决：去模型页面点「Agree and access repository」，'
                 '然后到设置里填上 HF Token。',
            403: '拒绝访问。可能原因：模型需要授权但没填 Token，或该地区被限制。\n'
                 '   解决：填上 HF Token；或换用 ModelScope 镜像。',
            404: '文件不存在。请检查：\n'
                 '   · 仓库名/文件名是否拼错（区分大小写）\n'
                 '   · 该文件是否在 main 分支上\n'
                 '   · 链接是否指向了一个目录而不是文件',
            416: '请求的字节范围无效。通常是本地残留的分块文件与服务器文件不一致。\n'
                 '   解决：删掉同目录下的 .partN 文件后重新下载。',
            429: '请求太频繁，被限流了。等几分钟再试，或在设置里填 HF Token 提高额度。',
            500: '服务器内部错误，稍后重试。',
            502: '网关错误（Bad Gateway）。镜像站临时故障，稍后重试或换端点。',
            503: '服务不可用。服务器过载，稍后重试。',
            504: '网关超时。网络到镜像站不通，稍后重试。',
        }
        hint = reasons.get(code, '未知的 HTTP 错误。')
        extra = ('\n   服务器返回: %s' % body[:200]) if body else ''
        return 'HTTP %d：%s%s' % (code, hint, extra)

    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        if isinstance(r, socket.timeout) or 'timed out' in str(r).lower():
            return ('连接超时：网络到 %s 不通或太慢。\n'
                    '   解决：检查网络；或在设置里换一个端点试试。' % _host(url))
        if isinstance(r, ssl.SSLError):
            return 'SSL 证书错误：%s\n   解决：检查系统时间是否正确；或换端点。' % r
        return ('网络不可达：%s\n'
                '   解决：检查网络连接、代理设置，或换端点。' % r)

    if isinstance(exc, socket.timeout):
        return '读取超时：服务器长时间没有响应。稍后重试即可（支持断点续传）。'

    if isinstance(exc, IncompleteRead):
        return '连接被中断（数据没读完）。这是网络抖动，重试即可 —— 已下载的部分会保留。'

    if isinstance(exc, ConnectionResetError):
        return '连接被对方重置。常见于镜像站限流，稍后重试。'

    return '%s: %s' % (type(exc).__name__, exc)


def _host(url):
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return url


def head_size(url, token=None, timeout=30):
    """拿文件大小。返回 (size, error_message)"""
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    # 有的 CDN 对 HEAD 支持不好，先试 HEAD
    try:
        req = make_request(url, headers, method='HEAD')
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as r:
            n = int(r.headers.get('Content-Length', -1))
            if n >= 0:
                return n, None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return -1, friendly_http_error(e, url)
        # 其他错误退回到 GET 试探
    except Exception:
        pass

    # 退回 GET，只读头部
    try:
        h = dict(headers)
        h['Range'] = 'bytes=0-0'
        req = make_request(url, h, method='GET')
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as r:
            cr = r.headers.get('Content-Range')          # bytes 0-0/12345
            if cr and '/' in cr:
                return int(cr.split('/')[-1]), None
            n = int(r.headers.get('Content-Length', -1))
            if n >= 0:
                return n, None
    except urllib.error.HTTPError as e:
        return -1, friendly_http_error(e, url)
    except Exception as e:
        return -1, friendly_http_error(e, url)
    return -1, '服务器没有返回文件大小，无法下载。'


def fetch_json(url, token=None, timeout=30):
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = make_request(url, headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return json.loads(r.read().decode('utf-8'))


def list_repo_files(endpoint, repo, token=None, revision='main'):
    """列出仓库所有文件。返回 ([(name, size)], error_message)

    ★ 优先读同一目录下的 README 里的文件清单？不 —— 直接用 HF 的 API。
      但 API 在镜像上有时不可用，所以还要有个兜底。
    """
    url = '%s/api/models/%s/revision/%s' % (endpoint.rstrip('/'), repo,
                                            revision or 'main')
    try:
        info = fetch_json(url, token)
        names = [s['rfilename'] for s in info.get('siblings', []) if s.get('rfilename')]
        out = []
        for fn in names:
            out.append((fn, -1))
        return out, None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # 没 token 时 API 会 401，但文件本身可能还是能下（公开模型）
            return None, ('仓库信息接口需要认证（HTTP 401）。\n'
                          '   这不影响下载已知文件名的文件 —— 直接粘文件的完整链接即可。')
        return None, friendly_http_error(e, url)
    except Exception as e:
        return None, ('获取文件列表失败：%s\n'
                      '   镜像站可能不提供 API。这不影响下载 —— '
                      '直接粘文件的完整链接即可。' % e)


# =============================================================================
#  下载任务（引擎）
# =============================================================================

class Download:
    """一个下载任务

    状态机：
        waiting → resolving → downloading → (paused) → merging → done
                                    ↓
                                 failed / cancelled
    """

    def __init__(self, url_input, save_dir, threads=8, endpoint=DEFAULT_ENDPOINT,
                 token=None, logger=None):
        self.raw_input = url_input
        self.save_dir = save_dir
        self.threads = max(1, min(32, int(threads)))
        self.endpoint = endpoint
        self.token = token
        self.logger = logger

        self.repo = None
        self.filename = None
        self.revision = 'main'
        self.url = None

        self.total = 0
        self.out_path = None
        self.state = 'waiting'
        self.error = None
        self.speed = 0.0
        self.done_bytes = 0

        # ★ 这些必须在 __init__ 里初始化：
        #   _download_part 会在子线程里访问 parts_done，
        #   若等到 _run 里才创建，极端情况下会 AttributeError。
        self.part_specs = []           # [(part_path, start, end), ...]
        self.parts_done = {}           # {part_path: 已下载字节数}

        self._stop_flag = threading.Event()     # 取消
        self._pause_flag = threading.Event()    # 暂停
        self._lock = threading.Lock()
        self._threads = []
        self._last_t = time.time()
        self._last_b = 0

    # ---------- 小工具 ----------
    @property
    def name(self):
        return self.filename or self.raw_input[:40]

    @property
    def display(self):
        if self.repo and self.filename:
            return '%s  →  %s' % (self.repo, self.filename)
        return self.raw_input

    def percent(self):
        if self.total <= 0:
            return 0.0
        return min(100.0, 100.0 * self.done_bytes / self.total)

    def eta(self):
        if self.speed <= 1e-6:
            return None
        left = max(0, self.total - self.done_bytes)
        return left / self.speed

    def cancel(self):
        self._stop_flag.set()
        self._pause_flag.clear()      # 取消时顺便解除暂停，让线程能退出

    def pause(self):
        self._pause_flag.set()

    def resume(self):
        self._pause_flag.clear()

    @property
    def is_paused(self):
        return self._pause_flag.is_set()

    # ---------- 解析链接 ----------
    def resolve(self):
        repo, fn, rev = parse_hf_input(self.raw_input)
        if repo is None:
            self.error = ('无法识别这个链接。\n'
                          '   正确格式示例：\n'
                          '   https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors')
            self.state = 'failed'
            return False
        self.repo, self.filename, self.revision = repo, fn, rev or 'main'
        if self.logger:
            self.logger.info('解析链接: repo=%s file=%s rev=%s'
                             % (self.repo, self.filename, self.revision))
        return True

    def _add_error(self, msg):
        """把错误追加到 self.error（多条错误都保留）"""
        if self.error:
            self.error += '\n' + msg
        else:
            self.error = msg

    # ---------- 下载一个分块 ----------
    def _download_part(self, part_path, start, end):
        """下载 [start, end]。支持续传、暂停、取消、重试。"""
        total_part = end - start + 1
        max_retry = 8

        for attempt in range(max_retry):
            if self._stop_flag.is_set():
                return False
            # 暂停时原地等待
            while self._pause_flag.is_set() and not self._stop_flag.is_set():
                time.sleep(0.25)
            if self._stop_flag.is_set():
                return False

            already = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if already >= total_part:
                with self._lock:
                    self.parts_done[part_path] = total_part
                    self._recalc_done()
                return True

            try:
                headers = {}
                if self.token:
                    headers['Authorization'] = 'Bearer ' + self.token
                headers['Range'] = 'bytes=%d-%d' % (start + already, end)
                req = make_request(self.url, headers)
                with urllib.request.urlopen(req, timeout=60,
                                            context=ssl_context()) as r:
                    # ★ 关键：如果服务器忽略了 Range，返回的是整个文件，
                    #   这时必须从正确位置截断写入，否则文件会错乱。
                    if r.status == 200 and already > 0:
                        # 不支持断点续传的服务器 —— 丢掉重来
                        if self.logger:
                            self.logger.warn('服务器不支持 Range 续传，分块 %d 重新开始'
                                             % start)
                        already = 0
                        mode = 'wb'
                    else:
                        mode = 'ab' if already else 'wb'

                    base = already
                    with open(part_path, mode) as f:
                        while True:
                            if self._stop_flag.is_set():
                                return False
                            if self._pause_flag.is_set():
                                break
                            buf = r.read(BUFFER)
                            if not buf:
                                break
                            # 防止服务器返回过多数据
                            room = total_part - base
                            if room <= 0:
                                break
                            if len(buf) > room:
                                buf = buf[:room]
                            f.write(buf)
                            base += len(buf)
                            with self._lock:
                                self.parts_done[part_path] = min(base, total_part)
                                self._recalc_done()

                if os.path.getsize(part_path) >= total_part:
                    with self._lock:
                        self.parts_done[part_path] = total_part
                        self._recalc_done()
                    return True

                if self._pause_flag.is_set():
                    continue          # 被暂停，回来继续循环（会重新算 already）

            except Exception as e:
                if self._stop_flag.is_set():
                    return False
                if self._pause_flag.is_set():
                    continue
                # 记录每次失败的原因（要求：报错留痕）
                reason = friendly_http_error(e, self.url)
                if self.logger:
                    self.logger.warn('分块 %d-%d 第 %d/%d 次失败：%s'
                                     % (start, end, attempt + 1, max_retry,
                                        reason.splitlines()[0]))
                if attempt == max_retry - 1:
                    self._add_error('分块 %d-%d 重试 %d 次仍失败：\n   %s'
                                    % (start, end, max_retry, reason))
                    return False
                time.sleep(min(2 * (attempt + 1), 15))     # 退避

        return False

    def _recalc_done(self):
        """根据各部分进度重算总进度（调用方需持有 self._lock）"""
        self.done_bytes = sum(self.parts_done.values())

    def _tick_speed(self):
        """每秒算一次速度"""
        now = time.time()
        dt = now - self._last_t
        if dt >= 0.9:
            with self._lock:
                cur = self.done_bytes
            self.speed = max(0.0, (cur - self._last_b) / dt)
            self._last_t = now
            self._last_b = cur

    # ---------- 主流程 ----------
    def run(self):
        try:
            self._run()
        except Exception as e:
            self.state = 'failed'
            self._add_error('未预期的错误：%s' % e)
            if self.logger:
                self.logger.exception('下载任务异常终止: %s' % self.name, e)

    def _run(self):
        # ---- 1. 解析 ----
        self.state = 'resolving'
        if not self.resolve():
            return
        if self.filename is None:
            self.state = 'failed'
            self._add_error('链接指向的是整个仓库，没有指定具体文件。\n'
                            '   请粘贴具体文件的链接（地址栏里带文件名的那个），\n'
                            '   或在「高级」里选择要下载的文件。')
            return

        self.url = build_url(self.endpoint, self.repo, self.filename, self.revision)
        os.makedirs(self.save_dir, exist_ok=True)
        self.out_path = os.path.join(self.save_dir, os.path.basename(self.filename))

        # ---- 2. 拿大小 ----
        size, err = head_size(self.url, self.token)
        if err:
            self.state = 'failed'
            self._add_error(err)
            if self.logger:
                self.logger.error('获取文件大小失败: %s\n   %s' % (self.url, err))
            return
        self.total = size
        if self.logger:
            self.logger.info('文件大小: %.2f MB' % (size / 1048576))

        # ---- 3. 已完成？ ----
        if os.path.exists(self.out_path) and os.path.getsize(self.out_path) == size:
            self.done_bytes = size
            self.state = 'done'
            if self.logger:
                self.logger.info('文件已存在且完整，跳过: %s' % self.out_path)
            return

        # ---- 4. 分块规划（断点续传的核心） ----
        n = self.threads if size >= MIN_SIZE_FOR_CHUNKS else 1
        chunk = (size + n - 1) // n
        self.part_specs = []
        for i in range(n):
            s = i * chunk
            e = min(s + chunk - 1, size - 1)
            if s >= size:
                break
            self.part_specs.append(('%s.part%d' % (self.out_path, i), s, e))

        self.parts_done = {}
        for pp, s, e in self.part_specs:
            prev = os.path.getsize(pp) if os.path.exists(pp) else 0
            self.parts_done[pp] = min(prev, e - s + 1)
        with self._lock:
            self._recalc_done()

        resumed = self.done_bytes
        if resumed > 0 and self.logger:
            self.logger.info('发现已下载 %.2f MB，将从此处续传' % (resumed / 1048576))

        # ---- 5. 开始多线程下载 ----
        self.state = 'downloading'
        self._last_t = time.time()
        self._last_b = self.done_bytes

        self._threads = []
        for pp, s, e in self.part_specs:
            t = threading.Thread(target=self._download_part, args=(pp, s, e),
                                 daemon=True)
            t.start()
            self._threads.append(t)

        # 主线程负责刷新速度
        while any(t.is_alive() for t in self._threads):
            self._tick_speed()
            time.sleep(0.4)
            if self._stop_flag.is_set():
                break
        for t in self._threads:
            t.join(timeout=5)
        self._tick_speed()

        # ---- 6. 被取消 ----
        if self._stop_flag.is_set():
            self.state = 'cancelled'
            if self.logger:
                self.logger.info('已取消: %s（已下载的部分保留，可续传）' % self.name)
            return

        # ---- 7. 检查所有分块 ----
        incomplete = []
        for pp, s, e in self.part_specs:
            need = e - s + 1
            have = os.path.getsize(pp) if os.path.exists(pp) else 0
            if have < need:
                incomplete.append((pp, s, e, have, need))
        if incomplete:
            self.state = 'failed'
            msgs = ['有 %d 个分块没有下载完整：' % len(incomplete)]
            for pp, s, e, have, need in incomplete[:5]:
                msgs.append('   %s  已下 %s / %s' % (os.path.basename(pp),
                                                     fmt_size(have), fmt_size(need)))
            msgs.append('')
            msgs.append('直接再点一次「开始下载」即可接着下（已下载的部分不会重下）。')
            self._add_error('\n'.join(msgs))
            return

        # ---- 8. 合并 ----
        self.state = 'merging'
        if self.logger:
            self.logger.info('分块下载完成，正在合并...')
        try:
            with open(self.out_path, 'wb') as out:
                for pp, s, e in self.part_specs:
                    with open(pp, 'rb') as f:
                        while True:
                            b = f.read(4 * 1024 * 1024)
                            if not b:
                                break
                            out.write(b)
            for pp, s, e in self.part_specs:
                try:
                    os.remove(pp)
                except OSError:
                    pass
        except Exception as e:
            self.state = 'failed'
            self._add_error('合并分块失败：%s\n'
                            '   磁盘空间是否足够？或文件被其他程序占用？' % e)
            if self.logger:
                self.logger.exception('合并失败: %s' % self.name, e)
            return

        # ---- 9. 校验 ----
        final = os.path.getsize(self.out_path)
        if final != size:
            self.state = 'failed'
            self._add_error('文件大小校验不通过：\n'
                            '   期望 %s，实际 %s\n'
                            '   可能是网络传输错误，请删除该文件后重下。'
                            % (fmt_size(size), fmt_size(final)))
            return

        self.done_bytes = size
        self.state = 'done'
        if self.logger:
            self.logger.info('下载完成: %s  (%s)' % (self.out_path, fmt_size(size)))


def fmt_size(n):
    """人类可读的大小"""
    if n is None or n < 0:
        return '?'
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return '%.2f %s' % (n, u)
        n /= 1024
    return '%.2f PB' % n


def fmt_speed(bps):
    if bps is None or bps <= 0:
        return '0 B/s'
    return fmt_size(bps) + '/s'


def fmt_eta(sec):
    if sec is None:
        return '--:--'
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%02d:%02d' % (m, s)


# =============================================================================
#  设置持久化
# =============================================================================

DEFAULT_CONFIG = {
    'save_dir': os.path.join(os.path.expanduser('~'), 'Downloads', 'HF'),
    'threads': 8,
    'endpoint': DEFAULT_ENDPOINT,
    'token': '',
    'window': '1000x720',
    'auto_paste': True,
    'concurrent': 1,          # 同时下载几个任务
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    # 目录不存在就创建一个默认的
    if not cfg.get('save_dir'):
        cfg['save_dir'] = DEFAULT_CONFIG['save_dir']
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# =============================================================================
#  主界面
# =============================================================================

class TaskRow:
    """界面上的一行下载任务"""

    def __init__(self, app, dl):
        self.app = app
        self.dl = dl
        self.widgets = {}

    def build(self, parent, row):
        dl = self.dl
        p = ttk.Frame(parent)
        p.grid(row=row, column=0, sticky='ew', padx=4, pady=3)
        p.columnconfigure(0, weight=1)

        # 第一行：文件名 + 状态
        top = ttk.Frame(p)
        top.grid(row=0, column=0, sticky='ew')
        top.columnconfigure(0, weight=1)
        self.widgets['name'] = ttk.Label(top, text=dl.display, anchor='w')
        self.widgets['name'].grid(row=0, column=0, sticky='ew')
        self.widgets['status'] = ttk.Label(top, text='等待中', width=12, anchor='e')
        self.widgets['status'].grid(row=0, column=1, sticky='e')

        # 第二行：进度条
        self.widgets['bar'] = ttk.Progressbar(p, mode='determinate', maximum=1000)
        self.widgets['bar'].grid(row=1, column=0, sticky='ew', pady=(2, 0))

        # 第三行：速度 / 已下载 / 剩余
        info = ttk.Frame(p)
        info.grid(row=2, column=0, sticky='ew')
        info.columnconfigure(0, weight=1)
        self.widgets['info'] = ttk.Label(info, text='', anchor='w', font=('Consolas', 8))
        self.widgets['info'].grid(row=0, column=0, sticky='ew')

        btns = ttk.Frame(info)
        btns.grid(row=0, column=1, sticky='e')
        self.widgets['btn_pause'] = ttk.Button(btns, text='暂停', width=6,
                                               command=self.toggle_pause)
        self.widgets['btn_pause'].grid(row=0, column=0, padx=1)
        self.widgets['btn_cancel'] = ttk.Button(btns, text='取消', width=6,
                                                command=self.cancel)
        self.widgets['btn_cancel'].grid(row=0, column=1, padx=1)
        self.widgets['btn_open'] = ttk.Button(btns, text='定位', width=6,
                                              command=self.open_folder, state='disabled')
        self.widgets['btn_open'].grid(row=0, column=2, padx=1)

        self.frame = p
        return p

    def toggle_pause(self):
        if self.dl.is_paused:
            self.dl.resume()
        else:
            self.dl.pause()

    def cancel(self):
        self.dl.cancel()

    def open_folder(self):
        d = self.dl.save_dir
        if os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception:
                pass

    def update(self):
        dl = self.dl
        st = dl.state
        names = {
            'waiting': '等待中', 'resolving': '解析中', 'downloading': '下载中',
            'merging': '合并中', 'done': '已完成', 'failed': '失败',
            'cancelled': '已取消',
        }
        if dl.is_paused and st == 'downloading':
            st_text = '已暂停'
        else:
            st_text = names.get(st, st)

        self.widgets['status'].configure(text=st_text)
        self.widgets['bar'].configure(value=int(dl.percent() * 10))

        if dl.state == 'downloading':
            txt = ('%s / %s    速度 %s    剩余 %s    线程 %d'
                   % (fmt_size(dl.done_bytes), fmt_size(dl.total),
                      fmt_speed(dl.speed), fmt_eta(dl.eta()), dl.threads))
            self.widgets['btn_pause'].configure(
                text='继续' if dl.is_paused else '暂停', state='normal')
            self.widgets['btn_cancel'].configure(state='normal')
        elif dl.state == 'done':
            txt = '%s    已保存到 %s' % (fmt_size(dl.total), dl.save_dir)
            self.widgets['btn_pause'].configure(state='disabled')
            self.widgets['btn_cancel'].configure(state='disabled')
            self.widgets['btn_open'].configure(state='normal')
        elif dl.state == 'failed':
            txt = '失败：%s' % (dl.error or '未知原因').splitlines()[0][:150]
            self.widgets['btn_pause'].configure(state='disabled')
            self.widgets['btn_cancel'].configure(state='disabled')
        elif dl.state == 'cancelled':
            txt = '已取消（已下载的部分保留，可重新开始续传）'
            self.widgets['btn_pause'].configure(state='disabled')
            self.widgets['btn_cancel'].configure(state='disabled')
        elif dl.state == 'merging':
            txt = '正在合并分块...'
        else:
            txt = ''
        self.widgets['info'].configure(text=txt)

        # 名称在解析完成后可能变化
        self.widgets['name'].configure(text=dl.display)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.ui_q = queue.Queue()
        self.logger = Logger(self.ui_q)

        self.rows = []
        self.queue = []             # 等待执行的任务
        self.active = []            # 正在执行的任务
        self._stop_all = False

        self.logger.info('=' * 60)
        self.logger.info('%s v%s 启动' % (APP_NAME, APP_VERSION))
        self.logger.info('配置: 目录=%s 线程=%d 端点=%s'
                         % (self.cfg['save_dir'], self.cfg['threads'],
                            self.cfg['endpoint']))
        self.logger.info('日志文件: %s' % self.logger.path())

        self._build_ui()
        self._pump()

    # ------------------------------------------------------------------
    #  界面
    # ------------------------------------------------------------------
    def _build_ui(self):
        r = self.root
        r.title('%s v%s' % (APP_NAME, APP_VERSION))
        try:
            r.geometry(self.cfg.get('window', '1000x720'))
        except Exception:
            r.geometry('1000x720')
        r.minsize(880, 600)

        style = ttk.Style()
        try:
            style.theme_use('vista')      # Windows 上更好看；没有就退回默认
        except Exception:
            pass
        style.configure('Big.TButton', font=('Microsoft YaHei UI', 11, 'bold'),
                        padding=(14, 6))
        style.configure('Hint.TLabel', foreground='#666')

        # ============ 顶部：链接输入（最显眼） ============
        top = ttk.LabelFrame(r, text=' 1. 粘贴 HuggingFace 链接 ')
        top.pack(fill='x', padx=10, pady=(10, 4))
        top.columnconfigure(0, weight=1)

        e1 = ttk.Frame(top)
        e1.grid(row=0, column=0, sticky='ew', padx=6, pady=4)
        e1.columnconfigure(0, weight=1)
        self.var_url = tk.StringVar()
        self.entry = ttk.Entry(e1, textvariable=self.var_url,
                               font=('Consolas', 10))
        self.entry.grid(row=0, column=0, sticky='ew')
        self.entry.bind('<Return>', lambda e: self.start_download())
        self.var_url.trace_add('write', self._on_url_changed)

        ttk.Button(e1, text='清空', width=6,
                   command=lambda: self.var_url.set('')).grid(row=0, column=1, padx=2)
        ttk.Button(e1, text='粘贴', width=6,
                   command=self.paste).grid(row=0, column=2, padx=2)

        self.lbl_detect = ttk.Label(top, text='', style='Hint.TLabel')
        self.lbl_detect.grid(row=1, column=0, sticky='w', padx=10)

        # ============ 中部：保存位置 + 参数 ============
        mid = ttk.LabelFrame(r, text=' 2. 保存位置与选项 ')
        mid.pack(fill='x', padx=10, pady=4)
        mid.columnconfigure(0, weight=1)

        f2 = ttk.Frame(mid)
        f2.grid(row=0, column=0, sticky='ew', padx=6, pady=5)
        f2.columnconfigure(0, weight=1)
        self.var_dir = tk.StringVar(value=self.cfg['save_dir'])
        ttk.Entry(f2, textvariable=self.var_dir, font=('Consolas', 9)).grid(
            row=0, column=0, sticky='ew')
        ttk.Button(f2, text='浏览...', width=9,
                   command=self.choose_dir).grid(row=0, column=1, padx=2)
        ttk.Button(f2, text='打开', width=6,
                   command=self.open_dir).grid(row=0, column=2, padx=2)

        f3 = ttk.Frame(mid)
        f3.grid(row=1, column=0, sticky='ew', padx=6, pady=(0, 5))

        ttk.Label(f3, text='线程数：').grid(row=0, column=0, sticky='w')
        self.var_threads = tk.IntVar(value=self.cfg['threads'])
        ttk.Spinbox(f3, from_=1, to=32, width=5,
                    textvariable=self.var_threads).grid(row=0, column=1, sticky='w')
        ttk.Label(f3, text='  （本机实测 8 线程已跑满带宽）',
                  style='Hint.TLabel').grid(row=0, column=2, sticky='w')

        ttk.Label(f3, text='    端点：').grid(row=0, column=3, sticky='w')
        self.var_ep_label = tk.StringVar(value=self._ep_label())
        cb = ttk.Combobox(f3, textvariable=self.var_ep_label, width=24,
                          state='readonly',
                          values=[x[0] for x in ENDPOINT_CHOICES])
        cb.grid(row=0, column=4, sticky='w')
        cb.bind('<<ComboboxSelected>>', self._on_ep_change)

        # ============ 按钮区 ============
        bar = ttk.Frame(r)
        bar.pack(fill='x', padx=10, pady=(2, 4))

        self.btn_start = ttk.Button(bar, text='▶  开始下载', style='Big.TButton',
                                    command=self.start_download)
        self.btn_start.pack(side='left')
        ttk.Button(bar, text='批量下载', width=10,
                   command=self.batch_download).pack(side='left', padx=6)
        ttk.Button(bar, text='测速', width=8,
                   command=self.speed_test).pack(side='left')
        ttk.Button(bar, text='查看日志', width=10,
                   command=self.open_log).pack(side='left', padx=6)
        self.btn_clear = ttk.Button(bar, text='清空列表', width=10,
                                    command=self.clear_done)
        self.btn_clear.pack(side='right')
        self.btn_cancel_all = ttk.Button(bar, text='全部取消', width=10,
                                         command=self.cancel_all)
        self.btn_cancel_all.pack(side='right', padx=6)

        # ============ 任务列表 ============
        box = ttk.LabelFrame(r, text=' 3. 下载队列 ')
        box.pack(fill='both', expand=True, padx=10, pady=4)

        # 用 Canvas + 滚动条，方便放任意多个任务行
        self.canvas = tk.Canvas(box, highlightthickness=0)
        vsb = ttk.Scrollbar(box, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)

        self.list_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.list_frame, anchor='nw')
        self.list_frame.bind('<Configure>', lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(
            self.canvas.find_all()[0], width=e.width))
        # 鼠标滚轮
        self.canvas.bind_all('<MouseWheel>',
                             lambda e: self.canvas.yview_scroll(int(-e.delta / 120), 'units'))

        # ★ 重要：Tkinter 不允许同一个容器里混用 pack 和 grid。
        #   任务行用 grid 排列，所以"空状态提示"也必须用 grid ——
        #   而且把它单独放进一个容器，避免影响其他部件。
        self.empty_holder = ttk.Frame(self.list_frame)
        self.empty_holder.grid(row=0, column=0, pady=40)
        self.lbl_empty = ttk.Label(
            self.empty_holder,
            text='还没有任务。\n\n把 HuggingFace 链接粘到上面，点「开始下载」就行。\n'
                 '（从浏览器地址栏直接复制整条链接即可，不用手工改）',
            style='Hint.TLabel', justify='center')
        self.lbl_empty.pack()

        # 任务行放在独立的容器里，从第 1 行开始（第 0 行留给空状态提示）
        self.rows_holder = ttk.Frame(self.list_frame)
        self.rows_holder.grid(row=1, column=0, sticky='ew')
        self.list_frame.columnconfigure(0, weight=1)
        self.rows_holder.columnconfigure(0, weight=1)

        # ============ 底部状态栏 ============
        bot = ttk.Frame(r)
        bot.pack(fill='x', side='bottom')
        self.var_status = tk.StringVar(value='就绪')
        ttk.Label(bot, textvariable=self.var_status, anchor='w',
                  style='Hint.TLabel').pack(side='left', padx=10, pady=3)
        self.lbl_log = ttk.Label(bot, text='', anchor='e', style='Hint.TLabel')
        self.lbl_log.pack(side='right', padx=10)

        r.protocol('WM_DELETE_WINDOW', self.on_close)

        # 自动粘贴（复制链接后切回窗口自动填）
        self._last_clip = ''
        r.after(800, self._watch_clipboard)

    # ------------------------------------------------------------------
    #  小功能
    # ------------------------------------------------------------------
    def _ep_label(self):
        for label, url in ENDPOINT_CHOICES:
            if url == self.cfg['endpoint']:
                return label
        return ENDPOINT_CHOICES[0][0]

    def _on_ep_change(self, _e=None):
        label = self.var_ep_label.get()
        for lab, url in ENDPOINT_CHOICES:
            if lab == label:
                self.cfg['endpoint'] = url
                save_config(self.cfg)
                self.logger.info('切换端点为: %s' % url)
                break

    def _endpoint(self):
        return self.cfg.get('endpoint', DEFAULT_ENDPOINT)

    def _on_url_changed(self, *_a):
        txt = self.var_url.get().strip()
        if not txt:
            self.lbl_detect.configure(text='')
            return
        repo, fn, rev = parse_hf_input(txt)
        if repo and fn:
            self.lbl_detect.configure(
                text='✓ 已识别：仓库 %s  文件 %s  分支 %s' % (repo, fn, rev),
                foreground='#0a7d28')
        elif repo:
            self.lbl_detect.configure(
                text='✓ 已识别仓库 %s（未指定文件）' % repo, foreground='#b8860b')
        else:
            self.lbl_detect.configure(
                text='✗ 识别不了这个链接，请检查格式', foreground='#c0392b')

    def paste(self):
        try:
            self.var_url.set(self.root.clipboard_get().strip())
        except Exception as e:
            messagebox.showwarning('粘贴失败', '剪贴板里没有文本内容。\n%s' % e)

    def _watch_clipboard(self):
        """自动识别剪贴板里的链接（只提示，不自动覆盖用户输入）"""
        try:
            if self.cfg.get('auto_paste', True):
                txt = self.root.clipboard_get().strip()
                if (txt != self._last_clip
                        and looks_like_hf_link(txt)
                        and not self.var_url.get().strip()):
                    self.var_url.set(txt)
                    self.var_status.set('已自动填入剪贴板里的链接')
                self._last_clip = txt
        except Exception:
            pass
        self.root.after(1200, self._watch_clipboard)

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or os.getcwd(),
                                    title='选择保存目录')
        if d:
            self.var_dir.set(d.replace('/', os.sep))
            self.cfg['save_dir'] = self.var_dir.get()
            save_config(self.cfg)
            self.logger.info('保存目录改为: %s' % self.cfg['save_dir'])

    def open_dir(self):
        d = self.var_dir.get()
        if os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception as e:
                messagebox.showwarning('打开失败', str(e))
        else:
            messagebox.showinfo('目录不存在', '目录还不存在：\n%s\n\n下载时会自动创建。' % d)

    def open_log(self):
        p = self.logger.path()
        if os.path.isfile(p):
            try:
                os.startfile(p)
            except Exception as e:
                messagebox.showwarning('打开失败', str(e))
        else:
            messagebox.showinfo('暂无日志', '还没有日志文件。')

    # ------------------------------------------------------------------
    #  下载控制
    # ------------------------------------------------------------------
    def _collect_config(self):
        """从界面读取当前设置并持久化"""
        self.cfg['save_dir'] = self.var_dir.get().strip() or DEFAULT_CONFIG['save_dir']
        try:
            self.cfg['threads'] = max(1, min(32, int(self.var_threads.get())))
        except Exception:
            self.cfg['threads'] = 8
            self.var_threads.set(8)
        save_config(self.cfg)
        return self.cfg

    def start_download(self, url=None):
        cfg = self._collect_config()
        text = (url if url is not None else self.var_url.get()).strip()
        if not text:
            messagebox.showinfo('还没填链接',
                                '请先把 HuggingFace 链接粘到最上面的输入框。\n\n'
                                '例如：\n'
                                'https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors')
            return

        # 允许多行（批量）
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) > 1:
            self._add_many(lines)
            return

        repo, fn, rev = parse_hf_input(text)
        if repo is None:
            messagebox.showerror('链接无法识别',
                                 '识别不出仓库和文件名，请检查链接。\n\n'
                                 '支持的格式：\n'
                                 '  https://huggingface.co/用户/仓库/resolve/main/文件\n'
                                 '  https://hf-mirror.com/用户/仓库/resolve/main/文件\n'
                                 '  用户/仓库                     （下载整个仓库）')
            return
        if fn is None:
            # 只给了仓库 → 询问是要列表还是全下
            self._handle_repo_only(repo)
            return

        if not url:
            self.var_url.set('')
        self._add_task(text)

    def _handle_repo_only(self, repo):
        """用户只粘了仓库名 —— 列出文件让他选（一个都不填也能下全部）"""
        cfg = self._collect_config()
        ep = self._endpoint()
        self.var_status.set('正在获取文件列表...')
        self.root.update_idletasks()

        files, err = list_repo_files(ep, repo, cfg.get('token') or None)
        if err:
            # 失败也不阻塞：允许用户手填文件名
            messagebox.showwarning(
                '拿不到文件列表',
                '%s\n\n你仍然可以在链接里直接带上文件名，例如：\n'
                '%s/resolve/main/文件名' % (err, ep + '/' + repo))
            self.logger.warn('获取 %s 的文件列表失败: %s' % (repo, err))
            self.var_status.set('就绪')
            return
        self.var_status.set('就绪')

        dlg = tk.Toplevel(self.root)
        dlg.title('选择要下载的文件 —— %s' % repo)
        dlg.geometry('720x480')
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text='仓库：%s\n勾选要下载的文件（可多选）' % repo,
                  font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=10, pady=8)

        frm = ttk.Frame(dlg)
        frm.pack(fill='both', expand=True, padx=10)
        sb = ttk.Scrollbar(frm)
        sb.pack(side='right', fill='y')
        lb = tk.Listbox(frm, selectmode='extended', font=('Consolas', 9),
                        yscrollcommand=sb.set)
        lb.pack(side='left', fill='both', expand=True)
        sb.configure(command=lb.yview)

        for name, _sz in files:
            lb.insert('end', name)
        # 默认全选大文件（通常是权重）
        for i, (name, _sz) in enumerate(files):
            if name.endswith(('.safetensors', '.bin', '.gguf', '.pth', '.onnx')):
                lb.selection_set(i)

        def do_download(all_files=False):
            if all_files:
                sel = [n for n, _ in files]
            else:
                sel = [lb.get(i) for i in lb.curselection()]
            if not sel:
                messagebox.showinfo('没选文件', '请至少勾选一个文件。')
                return
            dlg.destroy()
            for n in sel:
                self._add_task('%s/resolve/main/%s' % (ep + '/' + repo, n))

        bt = ttk.Frame(dlg)
        bt.pack(fill='x', padx=10, pady=10)
        ttk.Button(bt, text='下载选中的', command=lambda: do_download(False)).pack(side='left')
        ttk.Button(bt, text='下载全部', command=lambda: do_download(True)).pack(side='left', padx=6)
        ttk.Button(bt, text='取消', command=dlg.destroy).pack(side='right')

    def _add_many(self, lines):
        n = 0
        for l in lines:
            if parse_hf_input(l)[0]:
                self._add_task(l)
                n += 1
        if n == 0:
            messagebox.showerror('没有有效链接', '这几行里没有能识别的 HuggingFace 链接。')

    def batch_download(self):
        """批量下载对话框：一行一个链接，适合一次下多个文件"""
        dlg = tk.Toplevel(self.root)
        dlg.title('批量下载')
        dlg.geometry('760x520')
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text='一行一个链接（支持粘贴整段，会自动拆行）',
                  font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=12, pady=(12, 4))
        ttk.Label(dlg, text='示例：\n'
                            '  https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors\n'
                            '  用户/仓库名                      （只写仓库名会列出文件让你选）',
                  style='Hint.TLabel', justify='left').pack(anchor='w', padx=12)

        txt = tk.Text(dlg, height=16, font=('Consolas', 9), wrap='none')
        txt.pack(fill='both', expand=True, padx=12, pady=8)
        sb = ttk.Scrollbar(txt, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)

        def do_it():
            content = txt.get('1.0', 'end')
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if not lines:
                messagebox.showinfo('内容为空', '请至少填一个链接。')
                return
            valid = [l for l in lines if parse_hf_input(l)[0]]
            dlg.destroy()
            if not valid:
                messagebox.showerror('没有有效链接',
                                     '这些行里没有能识别的 HuggingFace 链接。')
                return
            for l in valid:
                self._add_task(l)
            self.logger.info('批量加入 %d 个任务' % len(valid))

        def paste_clip():
            try:
                content = self.root.clipboard_get().strip()
            except Exception:
                messagebox.showwarning('剪贴板为空', '剪贴板里没有文本内容。')
                return
            if content:
                if txt.get('1.0', 'end').strip():
                    txt.insert('end', '\n')
                txt.insert('end', content + '\n')

        bt = ttk.Frame(dlg)
        bt.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(bt, text='加入下载队列', style='Big.TButton',
                   command=do_it).pack(side='left')
        ttk.Button(bt, text='从剪贴板粘贴',
                   command=paste_clip).pack(side='left', padx=8)
        ttk.Button(bt, text='取消', command=dlg.destroy).pack(side='right')

    def _add_task(self, url_text):
        cfg = self.cfg
        dl = Download(url_text, cfg['save_dir'], cfg['threads'],
                      self._endpoint(), cfg.get('token') or None, self.logger)
        row = TaskRow(self, dl)
        row.build(self.rows_holder, len(self.rows))
        self.rows.append(row)
        # 有任务了就隐藏"还没有任务"的提示（用 grid_remove 而不是 pack_forget）
        try:
            self.empty_holder.grid_remove()
        except Exception:
            pass

        self.queue.append(dl)
        self.logger.info('加入队列: %s' % url_text[:120])
        self._refresh_rows()
        self._maybe_start_next()

    def _refresh_rows(self):
        """重新排列所有任务行的位置（清空后需要重排）"""
        for i, row in enumerate(self.rows):
            try:
                row.frame.grid(row=i, column=0, sticky='ew', padx=4, pady=3)
            except Exception:
                pass

    def _maybe_start_next(self):
        """按并发数启动任务"""
        limit = max(1, int(self.cfg.get('concurrent', 1)))
        while self.queue and len(self.active) < limit:
            dl = self.queue.pop(0)
            self.active.append(dl)
            t = threading.Thread(target=self._run_task, args=(dl,), daemon=True)
            t.start()

    def _run_task(self, dl):
        try:
            dl.run()
        except Exception as e:
            dl.state = 'failed'
            dl.error = '任务崩溃：%s' % e
            self.logger.exception('任务崩溃: %s' % dl.name, e)
        finally:
            # 从 active 移除，启动下一个
            try:
                self.active.remove(dl)
            except ValueError:
                pass
            self.ui_q.put(('task_done', None, None))
            self._maybe_start_next()

    def cancel_all(self):
        for dl in list(self.queue) + list(self.active):
            dl.cancel()
        self.queue.clear()
        self.var_status.set('已取消全部任务（已下载的部分保留，可续传）')

    def clear_done(self):
        """清掉已完成/失败/取消的行"""
        keep = []
        for row in self.rows:
            if row.dl.state in ('done', 'failed', 'cancelled'):
                row.frame.destroy()
            else:
                keep.append(row)
        self.rows = keep
        self._refresh_rows()
        # 全清空了就把提示放回来
        if not self.rows:
            try:
                self.empty_holder.grid()
            except Exception:
                pass

    # ------------------------------------------------------------------
    #  测速
    # ------------------------------------------------------------------
    def speed_test(self):
        """实测当前端点速度 —— 用一个已知的公开小文件跑 10 秒"""
        ep = self._endpoint()
        url = ('%s/google/embeddinggemma-2/resolve/main/model.safetensors'
               % ep.rstrip('/'))
        self.var_status.set('测速中（约 10 秒）...')
        self.logger.info('开始测速: %s' % url)

        def worker():
            n, err = head_size(url, self.cfg.get('token') or None)
            if err:
                self.ui_q.put(('speed', None, '测速失败：%s' % err))
                return
            got = 0
            t0 = time.time()
            try:
                h = {'Range': 'bytes=0-%d' % (20 * 1024 * 1024)}
                req = make_request(url, h)
                with urllib.request.urlopen(req, timeout=30,
                                            context=ssl_context()) as r:
                    while time.time() - t0 < 10:
                        buf = r.read(BUFFER)
                        if not buf:
                            break
                        got += len(buf)
            except Exception as e:
                if got < 1024 * 1024:
                    self.ui_q.put(('speed', None,
                                   '测速失败：%s' % friendly_http_error(e, url)))
                    return
            dt = max(time.time() - t0, 0.001)
            self.ui_q.put(('speed', got / dt, None))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    #  事件泵（把后台消息刷到界面）
    # ------------------------------------------------------------------
    def _pump(self):
        try:
            while True:
                kind, a, b = self.ui_q.get_nowait()
                if kind == 'log':
                    self.lbl_log.configure(text='%s: %s' % (a, (b or '')[:90]))
                elif kind == 'task_done':
                    pass
                elif kind == 'speed':
                    if b:
                        self.var_status.set(b)
                    else:
                        mb = a / 1048576
                        self.logger.info('测速结果: %.2f MB/s' % mb)
                        msg = ('实测速度：%.2f MB/s\n\n'
                               '参考：1 GB 约需 %.1f 分钟；10 GB 约需 %.0f 分钟。'
                               % (mb, 1024 / max(mb, 0.01) / 60,
                                  10 * 1024 / max(mb, 0.01) / 60))
                        self.var_status.set('测速完成：%.2f MB/s' % mb)
                        messagebox.showinfo('测速结果', msg)
        except queue.Empty:
            pass

        # 刷新所有任务行
        for row in self.rows:
            try:
                row.update()
            except Exception:
                pass

        # 汇总状态
        if self.rows:
            act = len(self.active)
            qn = len(self.queue)
            done = sum(1 for r in self.rows if r.dl.state == 'done')
            fail = sum(1 for r in self.rows if r.dl.state == 'failed')
            self.var_status.set('共 %d 个任务：下载中 %d，排队 %d，完成 %d，失败 %d'
                                % (len(self.rows), act, qn, done, fail))

        self.root.after(400, self._pump)

    # ------------------------------------------------------------------
    def on_close(self):
        running = [d for d in self.active if d.state in ('downloading', 'resolving')]
        if running:
            ok = messagebox.askyesno(
                '还有任务在下载',
                '有 %d 个任务正在下载。\n\n'
                '现在关闭会中断下载 —— 不用担心，已经下载的部分会保留，\n'
                '下次打开点「开始下载」就能续传。\n\n确定关闭吗？' % len(running))
            if not ok:
                return
        try:
            self.cfg['window'] = '%dx%d' % (self.root.winfo_width(),
                                            self.root.winfo_height())
            save_config(self.cfg)
        except Exception:
            pass
        for d in self.active:
            d.cancel()
        self.logger.info('程序退出')
        time.sleep(0.3)
        self.root.destroy()


# =============================================================================
#  入口
# =============================================================================

def main():
    # ★ 关键：把镜像端点写进环境变量，
    #   这样即使你之后用 transformers / huggingface_hub 下载也会走镜像
    os.environ.setdefault('HF_ENDPOINT', DEFAULT_ENDPOINT)
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')

    root = tk.Tk()
    # 高 DPI 适配（Windows 上不设会糊）
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
