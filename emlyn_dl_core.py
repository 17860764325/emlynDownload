# -*- coding: utf-8 -*-
"""
================================================================================
  EmlynDownload —— 下载核心引擎
================================================================================

本模块负责"怎么把文件下下来"，不涉及界面。界面在 EmlynDownload.py 里。

【支持哪些链接】

  ① HuggingFace 链接（自动走国内镜像加速）
       https://huggingface.co/用户/仓库/resolve/main/文件
       https://huggingface.co/用户/仓库/blob/main/文件
       https://hf-mirror.com/...
       https://hf.co/...                        （短域名）
       用户/仓库                                  （会列出文件让你选）

  ② 任意直链（HTTP/HTTPS）
       https://github.com/xxx/releases/download/v1/model.bin
       https://example.com/files/data.zip
       https://xxx.oss-cn-hangzhou.aliyuncs.com/big.bin
       ... 只要点开能直接下载/能看到文件，就能用

【两种下载模式（自动判断）】

  分块下载    拿得到文件大小 → 切成 1~32 块下载（支持 Range 就并行）
              每块独立记录进度，中断/关机/崩溃后都能接着下
  流式下载    拿不到文件大小 → 只能单线程到底（这种没法可靠续传）

【断点续传是怎么保证不把文件下坏的】

  只"看 .partN 有多大"是不够的：分块布局一变（比如改了线程数），旧分块
  就会被套到错误的字节区间上，合并出来的文件**大小对、内容错**。
  所以：

    1) 每次下载都把这次用的分块布局写进 <输出文件名>.emlyn.json
    2) 下次优先按记录的布局续传（改了线程数也不影响）
    3) 没有记录（老版本留下的分块）时，取每块开头的几十字节，和服务器
       上对应偏移处的内容抽样比对 —— 对得上才敢接着下
    4) 校验请求打不通时**不动任何文件**（网络问题≠数据坏了），只提示重试

  链接指向网页（网盘分享页等）时，会明确告诉你拿不到真实直链、怎么拿。

================================================================================
"""

import json
import os
import re
import socket
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.client import IncompleteRead

# =============================================================================
#  常量
# =============================================================================

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36 EmlynDownload/2.0')

DEFAULT_HF_ENDPOINT = 'https://hf-mirror.com'

ENDPOINT_CHOICES = [
    ('hf-mirror 镜像（国内推荐）', 'https://hf-mirror.com'),
    ('huggingface 官方', 'https://huggingface.co'),
]

MIN_CHUNK_BYTES = 1024 * 1024              # 每块至少 1MB（小文件只开 1 个连接）
BUFFER = 128 * 1024

# 这些域名是"网页"而不是文件直链，需要用户去拿真实链接
WEB_HOSTS = (
    'pan.baidu.com', 'www.aliyundrive.com', 'aliyundrive.com', 'alipan.com',
    'drive.google.com', 'docs.google.com', 'pan.quark.cn', '115.com',
    'civitai.com', 'www.modelscope.cn/models', 'ollama.com/library',
)

# 已知的"反爬/需要特殊处理"的域名，给出更贴切的提示
SPECIAL_HINTS = {
    'drive.google.com': 'Google Drive 的文件链接需要先转成直链：\n'
                        '   打开分享链接 → 复制文件 ID → 用格式\n'
                        '   https://drive.google.com/uc?export=download&id=文件ID',
    'pan.baidu.com': '百度网盘的链接需要客户端或第三方工具，本下载器无法直接下载。',
    'www.aliyundrive.com': '阿里云盘的链接需要客户端，本下载器无法直接下载。',
    'alipan.com': '阿里云盘的链接需要客户端，本下载器无法直接下载。',
    'pan.quark.cn': '夸克网盘的链接需要客户端，本下载器无法直接下载。',
    'civitai.com': 'Civitai 需要在登录状态下拿直链（或带 API Token）。',
}

HF_HOSTS = ('huggingface.co', 'hf-mirror.com', 'hf.co')


# =============================================================================
#  日志（要求：所有报错都留痕）
# =============================================================================

class Logger:
    """写日志到文件，同时把消息推给界面"""

    def __init__(self, ui_queue=None, log_dir=None):
        self.ui_queue = ui_queue
        self.lock = threading.Lock()
        self.log_dir = log_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'logs')
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            self._cleanup_old_logs()
        except Exception:
            pass

    def _cleanup_old_logs(self, keep_days=14):
        try:
            now = time.time()
            for f in os.listdir(self.log_dir):
                if not f.startswith('emlyn_') or not f.endswith('.log'):
                    continue
                p = os.path.join(self.log_dir, f)
                if now - os.path.getmtime(p) > keep_days * 86400:
                    os.remove(p)
        except Exception:
            pass

    def _log_path(self):
        return os.path.join(self.log_dir,
                            'emlyn_%s.log' % datetime.now().strftime('%Y-%m-%d'))

    def write(self, level, msg, detail=None):
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = '[%s] [%-5s] %s' % (ts, level, msg)
        if detail:
            line += '\n' + '\n'.join('    ' + d for d in str(detail).splitlines())
        with self.lock:
            try:
                with open(self._log_path(), 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
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
        detail = None
        if exc is not None:
            detail = ''.join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
        self.write('ERROR', msg, detail)

    def path(self):
        return self._log_path()


# =============================================================================
#  链接识别与解析
# =============================================================================

def host_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ''


def is_hf_link(text):
    """是不是 HuggingFace 相关链接"""
    t = (text or '').strip()
    if any(h in t for h in HF_HOSTS):
        return True
    # 形如 用户/仓库 或 用户/仓库/resolve/...
    if re.match(r'^[\w.\-]+/[\w.\-]+(/|$)', t) and ' ' not in t \
            and not t.lower().startswith(('http://', 'https://')):
        return True
    return False


def is_url(text):
    return (text or '').strip().lower().startswith(('http://', 'https://'))


def is_direct_http(text):
    """是 http(s) 直链，且不是 HF、不是已知的网页型链接"""
    t = (text or '').strip()
    if not is_url(t):
        return False
    if is_hf_link(t):
        return False
    h = host_of(t)
    for w in WEB_HOSTS:
        if h == w or h.endswith('.' + w):
            return False
    return True


def detect_link_type(text):
    """返回 'hf' / 'direct' / 'web' / 'unknown'"""
    t = (text or '').strip()
    if not t:
        return 'unknown'
    if is_hf_link(t):
        return 'hf'
    if is_url(t):
        h = host_of(t)
        for w in WEB_HOSTS:
            if h == w or h.endswith('.' + w):
                return 'web'
        return 'direct'
    return 'unknown'


def parse_hf_input(text):
    """解析 HuggingFace 链接 → (repo, filename, revision)"""
    s = (text or '').strip().strip('"').strip("'")
    if not s:
        return None, None, None
    for pre in ('https://', 'http://'):
        if s.startswith(pre):
            s = s[len(pre):]
    for host in ('www.huggingface.co/', 'huggingface.co/', 'www.hf-mirror.com/',
                 'hf-mirror.com/', 'hf.co/'):
        if s.startswith(host):
            s = s[len(host):]
            break
    s = s.split('?')[0].split('#')[0]

    for sep in ('/resolve/', '/blob/', '/raw/'):
        if sep in s:
            repo, rest = s.split(sep, 1)
            parts = [p for p in rest.split('/') if p]
            if len(parts) < 2:
                return repo.strip('/'), None, parts[0] if parts else 'main'
            return repo.strip('/'), '/'.join(parts[1:]), parts[0]

    parts = [p for p in s.split('/') if p]
    if len(parts) == 2:
        return '%s/%s' % (parts[0], parts[1]), None, 'main'
    if len(parts) >= 3:
        return '%s/%s' % (parts[0], parts[1]), '/'.join(parts[2:]), 'main'
    return None, None, None


def build_hf_url(endpoint, repo, filename, revision='main'):
    return '%s/%s/resolve/%s/%s' % (endpoint.rstrip('/'), repo,
                                    revision or 'main', filename)


def filename_from_url(url, content_disposition=None):
    """从 URL 或 Content-Disposition 里推断文件名

    ★ 优先级：
       1) Content-Disposition 里的 filename（服务器最清楚）
       2) URL 路径最后一段（解码 %xx）
       3) 兜底 download.bin
    """
    # 1) Content-Disposition
    if content_disposition:
        cd = content_disposition
        # filename*=UTF-8''xxx  优先
        m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.I)
        if m:
            v = m.group(1).strip().strip('"')
            if "''" in v:
                v = v.split("''", 1)[1]
            try:
                v = urllib.parse.unquote(v)
            except Exception:
                pass
            if v:
                return _safe_name(v)
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            v = m.group(1).strip()
            try:
                v = v.encode('latin-1').decode('utf-8')     # 常见的中文乱码修复
            except Exception:
                pass
            if v:
                return _safe_name(v)

    # 2) URL 路径
    try:
        path = urllib.parse.urlparse(url).path
        base = os.path.basename(path.rstrip('/'))
        if base:
            base = urllib.parse.unquote(base)
            if '.' in base or len(base) > 2:
                return _safe_name(base)
    except Exception:
        pass

    return 'download.bin'


def _safe_name(name):
    """去掉 Windows 不允许的字符"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().strip('.')
    if not name:
        return 'download.bin'
    return name[:180]


# =============================================================================
#  HTTP 工具
# =============================================================================

def make_request(url, headers=None, method='GET'):
    req = urllib.request.Request(url, method=method)
    req.add_header('User-Agent', UA)
    req.add_header('Accept-Encoding', 'identity')   # 必须：否则拿不到准确字节
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def friendly_http_error(exc, url):
    """把底层异常翻译成人能看懂的原因（要求：知道为什么报错）"""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        body = ''
        try:
            body = exc.read(600).decode('utf-8', 'ignore')
        except Exception:
            pass
        low = body.lower()

        # 仓库/用户不存在时 HF 也返回 401
        if ('invalid username or password' in low
                or 'repository not found' in low or 'repo not found' in low):
            return ('仓库不存在或无权访问（HTTP %d）。\n'
                    '   服务器说：%s\n'
                    '   解决：\n'
                    '   · 检查用户名和仓库名有没有拼错（区分大小写）\n'
                    '   · 在浏览器里打开确认能访问\n'
                    '   · 如果页面提示要同意协议，就去填 HF Token'
                    % (code, body[:120].strip()))

        reasons = {
            400: '请求格式错误。链接可能不完整，或文件名里有特殊字符。',
            401: '认证失败（HTTP 401）。\n'
                 '   · 如果是 HuggingFace 上的 gated 模型：\n'
                 '     去模型页面点「Agree and access repository」，再填 HF Token\n'
                 '   · 如果是其他网站：该文件需要登录，本工具拿不到直链',
            403: '拒绝访问（HTTP 403）。\n'
                 '   · 可能是防盗链、地区限制，或需要登录\n'
                 '   · 有些站点会检查 Referer，本工具无法绕过',
            404: '文件不存在（HTTP 404）。请检查：\n'
                 '   · 地址是否拼错（区分大小写）\n'
                 '   · 文件是否已被删除或改名\n'
                 '   · 链接是否指向了一个目录/页面而不是文件',
            416: '请求的字节范围无效（HTTP 416）。\n'
                 '   通常是因为本地残留的分块文件与服务器上的文件不一致。\n'
                 '   解决：删掉同目录下的 .partN 文件后重新下载。',
            429: '请求太频繁，被限流了（HTTP 429）。\n'
                 '   等待几分钟再试；HuggingFace 可以填 Token 提高额度。',
            500: '服务器内部错误（HTTP 500），稍后重试。',
            502: '网关错误（HTTP 502），服务器临时故障，稍后重试。',
            503: '服务不可用（HTTP 503），服务器过载，稍后重试。',
            504: '网关超时（HTTP 504），网络到服务器不通，稍后重试。',
        }
        hint = reasons.get(code, '未知的 HTTP 错误。')
        extra = ('\n   服务器返回: %s' % body[:200]) if body else ''
        return 'HTTP %d：%s%s' % (code, hint, extra)

    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        s = str(r).lower()
        if isinstance(r, socket.timeout) or 'timed out' in s:
            return ('连接超时：网络到 %s 不通或太慢。\n'
                    '   解决：检查网络；或换个镜像源再试。' % host_of(url))
        if isinstance(r, ssl.SSLError):
            return 'SSL 证书错误：%s\n   解决：检查系统时间；或换 http 试试。' % r
        if 'name or service not known' in s or 'getaddrinfo' in s:
            return ('域名解析失败：找不到 %s\n'
                    '   解决：检查网址拼写；确认网络和 DNS 正常。'
                    % host_of(url))
        return '网络不可达：%s\n   解决：检查网络连接或代理设置。' % r

    if isinstance(exc, socket.timeout):
        return '读取超时：服务器长时间没响应。稍后重试即可（支持断点续传）。'
    if isinstance(exc, IncompleteRead):
        return '连接被中断（数据没读完）。网络抖动，重试即可 —— 已下载的会保留。'
    if isinstance(exc, ConnectionResetError):
        return '连接被对方重置。常见于服务器限流，稍后重试。'
    return '%s: %s' % (type(exc).__name__, exc)


class ProbeResult:
    """探测直链后的结果"""

    def __init__(self):
        self.ok = False
        self.size = -1                 # 文件大小，-1 表示未知
        self.supports_range = False    # 是否支持 Range（能否多线程/续传）
        self.filename = None
        self.final_url = None          # 跟随重定向后的真实地址
        self.content_type = ''
        self.is_webpage = False        # 看起来是个网页而不是文件
        self.error = None


def probe_direct(url, token=None, timeout=25):
    """探测一个直链：拿大小、是否支持 Range、文件名

    ★ 做法：发一个 `Range: bytes=0-0` 的 GET。
       · 返回 206 → 支持 Range，能从 Content-Range 拿到总大小
       · 返回 200 → 不支持 Range，看 Content-Length
    """
    r = ProbeResult()
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    headers['Range'] = 'bytes=0-0'

    try:
        req = make_request(url, headers)
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as resp:
            r.ok = True
            r.final_url = resp.geturl()
            r.content_type = resp.headers.get('Content-Type', '')
            r.filename = filename_from_url(r.final_url,
                                           resp.headers.get('Content-Disposition'))

            status = resp.status
            if status == 206:
                r.supports_range = True
                cr = resp.headers.get('Content-Range')      # bytes 0-0/12345
                if cr and '/' in cr:
                    try:
                        r.size = int(cr.split('/')[-1])
                    except ValueError:
                        r.size = -1
            else:
                # 200：服务器忽略了 Range
                r.supports_range = False
                cl = resp.headers.get('Content-Length')
                if cl:
                    try:
                        r.size = int(cl)
                    except ValueError:
                        r.size = -1

            # 判断是不是网页
            ct = r.content_type.lower()
            if 'text/html' in ct and (r.size < 0 or r.size < 8 * 1024 * 1024):
                r.is_webpage = True
            return r

    except urllib.error.HTTPError as e:
        # 416：文件可能是 0 字节，或 Range 不被接受 —— 退化成普通 HEAD
        if e.code == 416:
            return _probe_by_head(url, token, timeout)
        # 其他错误：有些服务器对 Range 请求更严格，退化成 HEAD 再试一次
        head = _probe_by_head(url, token, timeout)
        if head.ok:
            return head
        r.error = friendly_http_error(e, url)
        return r
    except Exception as e:
        r.error = friendly_http_error(e, url)
        return r


def _probe_by_head(url, token=None, timeout=25):
    """退化方案：用 HEAD 请求探测"""
    r = ProbeResult()
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        req = make_request(url, headers, method='HEAD')
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as resp:
            r.ok = True
            r.final_url = resp.geturl()
            r.content_type = resp.headers.get('Content-Type', '')
            r.filename = filename_from_url(r.final_url,
                                           resp.headers.get('Content-Disposition'))
            ar = (resp.headers.get('Accept-Ranges') or '').lower()
            r.supports_range = ('bytes' in ar)
            cl = resp.headers.get('Content-Length')
            if cl:
                try:
                    r.size = int(cl)
                except ValueError:
                    r.size = -1
            ct = r.content_type.lower()
            if 'text/html' in ct and (r.size < 0 or r.size < 8 * 1024 * 1024):
                r.is_webpage = True
            return r
    except urllib.error.HTTPError as e:
        r.error = friendly_http_error(e, url)
    except Exception as e:
        r.error = friendly_http_error(e, url)
    return r


def head_size(url, token=None, timeout=30):
    """兼容旧接口：返回 (size, error)"""
    r = probe_direct(url, token, timeout)
    if r.ok:
        return r.size, None
    return -1, r.error or '探测失败'


def fetch_json(url, token=None, timeout=30):
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = make_request(url, headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return json.loads(r.read().decode('utf-8'))


def list_hf_repo_files(endpoint, repo, token=None, revision='main'):
    """列出 HF 仓库的文件。返回 ([(name, size)], error)"""
    url = '%s/api/models/%s/revision/%s' % (endpoint.rstrip('/'), repo,
                                            revision or 'main')
    try:
        info = fetch_json(url, token)
        names = [s['rfilename'] for s in info.get('siblings', [])
                 if s.get('rfilename')]
        return [(n, -1) for n in names], None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, ('仓库信息接口需要认证（HTTP 401）。\n'
                          '   这不影响下载 —— 直接粘具体文件的完整链接即可。')
        return None, friendly_http_error(e, url)
    except Exception as e:
        return None, ('获取文件列表失败：%s\n'
                      '   不影响下载 —— 直接粘具体文件的完整链接即可。' % e)


# =============================================================================
#  断点续传记录（清单文件）
# =============================================================================
#
#  ★ 为什么需要它？
#
#  续传如果只靠"看 .partN 有多大"，分块布局一变（比如你把线程数从 8 改成
#  4），旧分块就会被套到错误的字节区间上 —— 合并出来的文件**大小对、内容错**，
#  程序还查不出来。这是最危险的情况：你拿到一个坏文件却以为下好了。
#
#  所以每次下载都把"这次用的分块布局"写进一个清单文件，下次照着来：
#      <输出文件>.emlyn.json
#
#  万一清单丢了（老版本留下的分块），就用"抽样比对"来验证：取每块开头的
#  几十字节，跟服务器上对应偏移处的内容比对，对得上才敢接着下。
#

MANIFEST_VERSION = 1
MAX_PARTS = 32                 # 分块数上限（和线程数上限一致）
SPOTCHECK_BYTES = 64           # 抽样校验取多少字节


def manifest_path(out_path):
    return out_path + '.emlyn.json'


def load_manifest(out_path):
    """读取断点记录，读不到或版本不对就返回 None"""
    p = manifest_path(out_path)
    try:
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict) and int(d.get('version', 0)) == MANIFEST_VERSION:
                return d
    except Exception:
        pass
    return None


def save_manifest(out_path, data):
    """原子地写断点记录（先写临时文件再改名）"""
    try:
        d = dict(data)
        d['version'] = MANIFEST_VERSION
        p = manifest_path(out_path)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


def clear_manifest(out_path):
    try:
        os.remove(manifest_path(out_path))
    except OSError:
        pass


def chunk_layout(total, n):
    """把 total 字节切成 n 块，返回 [(start, end), ...]"""
    n = max(1, min(MAX_PARTS, int(n)))
    chunk = (total + n - 1) // n
    out = []
    for i in range(n):
        s = i * chunk
        if s >= total:
            break
        out.append((s, min(s + chunk - 1, total - 1)))
    return out


# 同一个输出文件，同时只允许一个任务在写 ——
# 防止"重复添加同一个链接"或"恢复的记录又手动加一遍"把分块写乱
_ACTIVE_OUT_PATHS = set()
_OUT_PATH_LOCK = threading.Lock()


def claim_out_path(path):
    with _OUT_PATH_LOCK:
        if path in _ACTIVE_OUT_PATHS:
            return False
        _ACTIVE_OUT_PATHS.add(path)
        return True


def release_out_path(path):
    with _OUT_PATH_LOCK:
        _ACTIVE_OUT_PATHS.discard(path)


# =============================================================================
#  下载任务
# =============================================================================

class Download:
    """一个下载任务

    状态机：
        waiting → resolving → downloading → (paused) → merging → done
                                    ↓
                                 failed / cancelled
    """

    def __init__(self, url_input, save_dir, threads=8,
                 endpoint=DEFAULT_HF_ENDPOINT, token=None, logger=None):
        self.raw_input = (url_input or '').strip()
        self.save_dir = save_dir
        self.threads = max(1, min(32, int(threads)))
        self.endpoint = endpoint
        self.token = token
        self.logger = logger

        self.link_type = detect_link_type(self.raw_input)
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
        self.mode = ''                 # 'multi' / 'single'，用于界面提示

        self.part_specs = []
        self.parts_done = {}
        self._layout_source = ''       # 'manifest' / 'verified' / 'new' / 'fresh'
        self._fatal = None             # 致命错误：数据没法安全落位，必须停
        self._claimed = False          # 是否已经占用这个输出文件

        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._lock = threading.Lock()
        self._threads = []
        self._last_t = time.time()
        self._last_b = 0

        # ---- 界面展示用的统计（每个分块/线程的实时速度、耗时） ----
        self.part_speed = {}           # part 路径 -> 该线程当前速度 B/s
        self._part_last_b = {}         # part 路径 -> 上次采样时的字节数
        self._base_elapsed = 0.0       # 以前几次跑累计用掉的秒数
        self._run_started = None       # 本次开始跑的时刻
        self.created_at = time.time()  # 任务建立时间
        self.finished_at = None        # 下载完成时间
        self.disk_parts = 0            # 磁盘上还剩几个分块（重开后显示用）

    # ---------- 展示 ----------
    @property
    def name(self):
        return self.filename or self.raw_input[:60]

    @property
    def display(self):
        if self.repo and self.filename:
            return '%s  →  %s' % (self.repo, self.filename)
        if self.filename:
            return self.filename
        return self.raw_input[:80]

    def percent(self):
        if self.total <= 0:
            return 0.0
        return min(100.0, 100.0 * self.done_bytes / self.total)

    def eta(self):
        if self.speed <= 1e-6:
            return None
        return max(0, self.total - self.done_bytes) / self.speed

    @property
    def active_seconds(self):
        """这个任务一共下了多少秒（中途关掉软件的空白时间不算）"""
        t = self._base_elapsed
        if self._run_started:
            t += time.time() - self._run_started
        return t

    def avg_speed(self):
        """平均速度（用于详情面板）"""
        sec = self.active_seconds
        if sec <= 0.5:
            return 0.0
        return self.done_bytes / sec

    def threads_info(self):
        """每个分块（也就是每个线程）的进度，给界面的"详情"展开用

        返回 [{index, start, end, total, done, percent, speed, finished}, ...]
        """
        with self._lock:
            done_map = dict(self.parts_done)
            speed_map = dict(self.part_speed)
        out = []
        for i, (pp, s, e) in enumerate(self.part_specs):
            need = e - s + 1
            have = done_map.get(pp, 0)
            if have > need:
                have = need
            out.append({
                'index': i,
                'start': s,
                'end': e,
                'total': need,
                'done': have,
                'percent': (100.0 * have / need) if need else 0.0,
                'speed': speed_map.get(pp, 0.0),
                'finished': have >= need,
            })
        return out

    # ---------- 控制 ----------
    def cancel(self):
        self._stop_flag.set()
        self._pause_flag.clear()

    def pause(self):
        self._pause_flag.set()

    def resume(self):
        self._pause_flag.clear()

    @property
    def is_paused(self):
        return self._pause_flag.is_set()

    def _add_error(self, msg):
        self.error = (self.error + '\n' + msg) if self.error else msg

    def reset(self):
        """把任务复位，让它可以再跑一次

        ★ 界面上点「继续 / 重试」靠的就是它。
        以前 Download 是一次性的：取消后 _stop_flag 一直是 set，
        同一个任务对象再跑会立刻退出 —— 所以旧版只能让用户重新粘链接。
        已下载的分块和断点记录都保留。
        """
        self._stop_flag.clear()
        self._pause_flag.clear()
        self.error = None
        self._fatal = None
        self.speed = 0.0
        self.done_bytes = 0
        self._last_t = time.time()
        self._last_b = 0
        self._threads = []
        self.part_speed = {}
        self._part_last_b = {}
        if self.state != 'waiting':
            self.state = 'waiting'

    # ---------- 任务记录（关掉软件后还能恢复列表） ----------
    def snapshot(self):
        """导出这条任务的记录，用于关软件/崩溃后恢复列表

        只存"能重建这个任务"的信息；已下载多少由磁盘上的分块决定，
        所以这里存的进度只是个备份（重开后会用 refresh_progress_from_disk 重算）。
        """
        return {
            'url': self.raw_input,
            'save_dir': self.save_dir,
            'threads': self.threads,
            'endpoint': self.endpoint,
            'filename': self.filename,
            'out_path': self.out_path,
            'total': self.total,
            'done_bytes': self.done_bytes,
            'state': self.state,
            'error': self.error,
            'mode': self.mode,
            'elapsed': round(self._base_elapsed, 1),
            'created_at': self.created_at,
            'finished_at': self.finished_at,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

    @staticmethod
    def from_snapshot(rec, token=None, logger=None):
        """按记录重建一个任务（不会自动开始下载）"""
        dl = Download(rec.get('url', ''), rec.get('save_dir', ''),
                      rec.get('threads', 8),
                      rec.get('endpoint', DEFAULT_HF_ENDPOINT),
                      token, logger)
        dl.filename = rec.get('filename')
        dl.out_path = rec.get('out_path') or (
            os.path.join(dl.save_dir, dl.filename) if dl.filename else None)
        try:
            dl.total = int(rec.get('total') or 0)
        except (TypeError, ValueError):
            dl.total = 0
        try:
            dl.done_bytes = int(rec.get('done_bytes') or 0)
        except (TypeError, ValueError):
            dl.done_bytes = 0
        dl.error = rec.get('error')
        dl.mode = rec.get('mode') or ''
        try:
            dl._base_elapsed = float(rec.get('elapsed') or 0)
        except (TypeError, ValueError):
            dl._base_elapsed = 0.0
        try:
            dl.created_at = float(rec.get('created_at') or time.time())
        except (TypeError, ValueError):
            dl.created_at = time.time()
        try:
            fa = rec.get('finished_at')
            dl.finished_at = float(fa) if fa else None
        except (TypeError, ValueError):
            dl.finished_at = None
        st = rec.get('state') or 'interrupted'
        # 上次记的是"下载中/解析中/合并中"→ 说明是被关掉/崩掉的，标成"未完成"
        if st in ('downloading', 'resolving', 'merging', 'waiting'):
            st = 'interrupted'
        dl.state = st
        return dl

    def refresh_progress_from_disk(self):
        """不联网，按磁盘上的文件重算"已下载多少"

        ★ 关掉软件重开时靠这个把进度显示回来 —— 让用户一眼看到
          "上次下的 4.41GB 还在"，而不是一个空空的任务。
        """
        if not self.out_path or self.total <= 0:
            return
        if os.path.exists(self.out_path) \
                and os.path.getsize(self.out_path) == self.total:
            self.done_bytes = self.total
            self.state = 'done'
            return
        # 有断点记录 → 按记录的分块区间精确统计
        # （刚重开时 self.url 还没解析出来，所以 url 为空时按大小匹配即可）
        saved = load_manifest(self.out_path)
        if saved and int(saved.get('total', -1)) == self.total \
                and (not self.url or saved.get('url') == self.url):
            done = 0
            parts = saved.get('parts') or []
            for i, se in enumerate(parts):
                need = int(se[1]) - int(se[0]) + 1
                p = '%s.part%d' % (self.out_path, i)
                sz = os.path.getsize(p) if os.path.exists(p) else 0
                done += min(sz, need)
            self.disk_parts = len(parts)
            self.done_bytes = min(done, self.total)
            return
        # 没有记录（老版本留下的分块）→ 只能按分块大小估个大概，仅供显示
        est = 0
        for i in range(MAX_PARTS):
            p = '%s.part%d' % (self.out_path, i)
            if not os.path.exists(p):
                break
            est += os.path.getsize(p)
        self.disk_parts = len(self._parts_on_disk())
        self.done_bytes = min(est, self.total)

    # ---------- 断点续传：布局规划 ----------
    def _parts_on_disk(self):
        """扫描从 part0 开始连续存在的分块 → [(序号, 路径, 大小)]"""
        found = []
        for i in range(MAX_PARTS):
            p = '%s.part%d' % (self.out_path, i)
            if not os.path.exists(p):
                break
            found.append((i, p, os.path.getsize(p)))
        return found

    def _discard_parts(self):
        """把残留分块和断点记录都删掉"""
        for i in range(MAX_PARTS):
            p = '%s.part%d' % (self.out_path, i)
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        clear_manifest(self.out_path)
        self.parts_done = {}

    def _sample(self, start, n=SPOTCHECK_BYTES):
        """从服务器取 [start, start+n) 这一段，用来校验分块边界

        ★ 返回 None 表示"没取到"（服务器不支持 Range，或网络问题）。
          调用方必须把 None 当成"没验成"，绝不能当成"验出来是错的" ——
          否则一次网络抖动就会把用户几 GB 的断点数据判成垃圾删掉。
        """
        headers = {'Range': 'bytes=%d-%d' % (start, start + n - 1)}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        for attempt in range(3):
            if self._stop_flag.is_set():
                return None
            try:
                req = make_request(self.url, headers)
                with urllib.request.urlopen(req, timeout=30,
                                            context=ssl_context()) as r:
                    if r.status != 206:
                        return None
                    data = r.read(n)
                    return data or None
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        return None

    def _layout_verified(self, specs, on_disk):
        """校验"这套布局"和"磁盘上已有的分块"是不是一回事

        ★ 关键点：分块文件永远是从自己的 start 一直往后追加写的，
          所以只要它开头的几十字节 == 服务器上 start 处的内容，
          就能确定这块是文件在该区间的开头 —— 接着往后下就不会错位。

        返回 True / False / None（None = 服务器不给 Range，验不了）
        """
        if len(specs) < len(on_disk):
            return False
        checks = 0
        for i, path, size in on_disk:
            _p, start, end = specs[i]
            need = end - start + 1
            if size > need:
                return False                     # 旧布局比现在这块还大 → 不对
            if start == 0 or size < SPOTCHECK_BYTES:
                continue                         # 开头永远对；太小没得比
            if checks >= 12:
                break
            want = self._sample(start)
            if want is None:
                return None
            try:
                with open(path, 'rb') as f:
                    have = f.read(len(want))
            except OSError:
                return False
            if have != want:
                return False
            checks += 1
        return True

    def _range_usable(self):
        """服务器是不是**真的**支持"从文件中间某处开始取"？

        有些服务器对 bytes=0-0 会回 206，但对真正的中间偏移却回整个文件、
        或者不管你要哪段都从头给（有些 CDN 就是这样）。不实测一下，分块
        就会被放到错误的偏移上 —— 大小对、内容错。

        做法：分别取开头和正中间各 64 字节
          · 中间取不到（状态不是 206）        → 不支持
          · 两段居然一模一样（不管要哪段都从头给）→ 不支持
        """
        if self.total <= MIN_CHUNK_BYTES:
            return True                  # 只有一块，用不到中间偏移
        mid = self._sample(self.total // 2)
        if mid is None:
            return False
        first = self._sample(0)
        if first is not None and first == mid:
            return False                 # 服务器无视偏移，每次都从头给
        return True

    def _default_chunks(self, range_ok=True):
        """默认分块数：受线程数限制，且每块至少 1MB

        · 小文件只开 1 个连接，别为了几百 KB 去开 8 个
        · 服务器不支持 Range 时必须只用 1 块 —— 否则第 2 块开始
          拿到的数据并不是它那一段（服务器每次都从文件头给）
        """
        if not range_ok:
            return 1
        by_size = self.total // MIN_CHUNK_BYTES
        return max(1, min(self.threads, max(1, int(by_size))))

    def _candidate_counts(self, count):
        """可能的分块数：先猜默认值，再从"磁盘上现有块数"往上试"""
        cands = []
        default = self._default_chunks()
        if default >= count:
            cands.append(default)
        for n in range(count, MAX_PARTS + 1):
            if n not in cands:
                cands.append(n)
        return cands

    def _plan_parts(self, range_ok):
        """决定这次用哪套分块布局 —— 断点续传对不对，全看这一步

        返回 False 表示"这次没法安全地决定"（已设好 error），调用方应停手。
        """
        usable = bool(range_ok) and self._range_usable()
        if range_ok and not usable and self.logger:
            self.logger.warn('服务器声称支持 Range，但实测从文件中间取不到数据；'
                             '本次只用 1 个连接下载')

        # ① 有断点记录，且是同一个文件 → 直接用上次记录的布局（最可靠）
        saved = load_manifest(self.out_path)
        if saved and saved.get('url') == self.url \
                and int(saved.get('total', -1)) == self.total:
            specs = []
            for i, se in enumerate(saved.get('parts') or []):
                specs.append(('%s.part%d' % (self.out_path, i),
                              int(se[0]), int(se[1])))
            if specs and (usable or len(specs) == 1):
                self.mode = 'multi' if len(specs) > 1 else 'single'
                self.part_specs = specs
                self._layout_source = 'manifest'
                if self.logger:
                    self.logger.info('找到上次的断点记录：%d 个分块 / 共 %s，'
                                     '按记录的布局续传'
                                     % (len(specs), fmt_size(self.total)))
                return True
            if specs and self.logger:
                self.logger.warn('有断点记录，但服务器现在不支持 Range，'
                                 '没法只下缺的那部分；改为整文件重下')

        # ② 没有记录，但磁盘上有残留分块 → 必须验证，不能瞎猜
        on_disk = self._parts_on_disk()
        if on_disk and usable:
            count = len(on_disk)
            have_bytes = sum(sz for _i, _p, sz in on_disk)
            tried = []
            unverifiable = 0
            for n in self._candidate_counts(count):
                specs = [('%s.part%d' % (self.out_path, i), s, e)
                         for i, (s, e) in enumerate(chunk_layout(self.total, n))]
                if len(specs) < count:
                    continue
                tried.append(n)
                verdict = self._layout_verified(specs, on_disk)
                if verdict:
                    self.mode = 'multi'
                    self.part_specs = specs
                    self._layout_source = 'verified'
                    if self.logger:
                        self.logger.info('校验通过：磁盘上 %d 个残留分块属于 %d 块布局'
                                         '（已抽样比对），继续续传'
                                         % (count, n))
                    return True
                if verdict is None:
                    # 取不到校验数据 → 换个分块数也一样取不到，直接停下来
                    unverifiable += 1
                    break

            # ③ 一次都没验成 → 说明是网络/服务器的问题，不是数据的问题。
            #    ★ 这种情况绝对不删断点数据：只报错，让用户重试。
            if unverifiable:
                self.error = ('暂时没法校验磁盘上已有的 %d 个分块（共 %s）。\n'
                              '   原因：向服务器请求校验数据时没有正常返回'
                              '（网络抖动或服务器限流）。\n'
                              '   为了不误删你的断点数据，这次没有改动任何文件。\n'
                              '   请等网络恢复后点「继续」重试，会自动接着下。'
                              % (count, fmt_size(have_bytes)))
                if self.logger:
                    self.logger.warn('校验残留分块失败（%d 个候选布局都取不到数据），'
                                     '已保留 %s 断点数据不做改动'
                                     % (unverifiable, fmt_size(have_bytes)))
                self.done_bytes = have_bytes
                return False

            # ④ 确认对不上任何布局 → 旧分块确实不可用。宁可重下，
            #    也不能把它们拼成一个内容错误的文件。
            if self.logger:
                self.logger.warn('磁盘上的残留分块与 %s 块布局都对不上；'
                                 '为避免合成出内容错误的文件，删掉重下'
                                 % (tried or '任何'))
            self._discard_parts()
        elif on_disk and self.logger:
            self.logger.warn('服务器不支持 Range，原有的多分块没法只补缺的部分；'
                             '改为整文件重下（多余的分块下载完成后会清理）')

        # ⑤ 干净开始：不支持 Range 时只用 1 块
        n = self._default_chunks(usable)
        specs = [('%s.part%d' % (self.out_path, i), s, e)
                 for i, (s, e) in enumerate(chunk_layout(self.total, n))]
        self.mode = 'multi' if n > 1 else 'single'
        self.part_specs = specs
        self._layout_source = 'new' if not on_disk else 'fresh'
        return True

    # ---------- 解析 ----------
    def resolve(self):
        """确定最终要下载的 URL 和文件名"""
        t = self.link_type

        if t == 'hf':
            repo, fn, rev = parse_hf_input(self.raw_input)
            if repo is None:
                self.error = ('无法识别这个 HuggingFace 链接。\n'
                              '   正确格式示例：\n'
                              '   https://huggingface.co/用户/仓库/resolve/main/文件')
                self.state = 'failed'
                return False
            self.repo, self.filename, self.revision = repo, fn, rev or 'main'
            if self.filename is None:
                self.error = ('这个链接只指向了仓库，没有指定具体文件。\n'
                              '   请粘贴具体文件的链接（地址栏里带文件名的那个）。')
                self.state = 'failed'
                return False
            self.url = build_hf_url(self.endpoint, self.repo, self.filename,
                                    self.revision)
            if self.logger:
                self.logger.info('HuggingFace 链接: repo=%s file=%s rev=%s'
                                 % (self.repo, self.filename, self.revision))
            return True

        if t == 'direct':
            self.url = self.raw_input
            if self.logger:
                self.logger.info('直链模式: %s' % self.url[:150])
            return True

        if t == 'web':
            h = host_of(self.raw_input)
            hint = SPECIAL_HINTS.get(h, '')
            self.error = ('这个链接看起来是【网页】而不是文件直链，无法直接下载。\n'
                          '   地址：%s\n' % self.raw_input +
                          (('   %s\n' % hint) if hint else '') +
                          '   怎么拿到真正的直链：\n'
                          '   1. 在浏览器里打开这个链接\n'
                          '   2. 按 F12 打开开发者工具 → 切到「网络 / Network」标签\n'
                          '   3. 点一次下载，找到体积最大的那条请求\n'
                          '   4. 右键 → Copy → Copy link address\n'
                          '   5. 把那个地址粘回来（通常在 .zip/.bin/.safetensors 之类的结尾）')
            self.state = 'failed'
            return False

        self.error = ('认不出这是什么链接。\n'
                      '   本工具支持：\n'
                      '   · HuggingFace 链接（自动走国内镜像）\n'
                      '   · 任意 http/https 文件直链')
        self.state = 'failed'
        return False

    # ---------- 分块下载 ----------
    def _download_part(self, part_path, start, end):
        """下一块。★ 只有"一点都没下动"才计入失败次数

        旧版是"每块最多试 8 次"，不看有没有进展。大文件在慢镜像上必然
        超时几次，于是试满 8 次就被判死刑 —— 这就是 4GB 文件反复失败的
        原因。现在改成：只要这一轮还下到东西，就继续下；连续 8 轮一点都
        没下动，才放弃。
        """
        total_part = end - start + 1
        max_stall = 8                    # 连续 8 轮毫无进展才放弃
        timeout = 90                     # 单次读取的超时（每读到数据就重置）
        stall = 0
        last_reason = '网络或服务器不稳定'

        while not self._stop_flag.is_set():
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

            before = already
            try:
                headers = {'Range': 'bytes=%d-%d' % (start + already, end)}
                if self.token:
                    headers['Authorization'] = 'Bearer ' + self.token
                req = make_request(self.url, headers)
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=ssl_context()) as r:
                    # ★ 服务器忽略了 Range，而这块要的不是文件开头 ——
                    #   响应里的字节是"从文件头开始"的，放进这块就会错位。
                    #   宁可停下来，也不能拼出一个大小对、内容错的文件。
                    if r.status == 200 and start > 0:
                        self._fatal = (
                            '服务器忽略了 Range 请求（分块要从第 %d 字节开始，'
                            '它却从头返回整个文件）。\n'
                            '   这块的数据没法放到正确位置，已停止，'
                            '以免拼出内容错误的文件。\n'
                            '   已下载的分块都保留着。请稍后点「继续」重试；'
                            '若一直这样，说明该服务器不支持多线程下载。'
                            % start)
                        if self.logger:
                            self.logger.error(self._fatal)
                        self._stop_flag.set()      # 别的分块也没必要继续了
                        return False

                    # 服务器忽略 Range 返回整个文件 → 丢弃重来，避免文件错乱
                    if r.status == 200 and already > 0:
                        if self.logger:
                            self.logger.warn('服务器未按 Range 返回，分块 %d 重下' % start)
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
                    continue

            except Exception as e:
                if self._stop_flag.is_set():
                    return False
                if self._pause_flag.is_set():
                    continue
                last_reason = friendly_http_error(e, self.url)
                if self.logger:
                    self.logger.warn('分块 %d-%d 出错（已下 %s / %s）：%s'
                                     % (start, end,
                                        fmt_size(os.path.getsize(part_path)
                                                 if os.path.exists(part_path) else 0),
                                        fmt_size(total_part),
                                        last_reason.splitlines()[0]))

            # ★ 判断这一轮到底有没有下动
            now_size = os.path.getsize(part_path) if os.path.exists(part_path) else 0
            if now_size > before:
                stall = 0                      # 有进展 → 失败计数清零，接着下
            else:
                stall += 1
                if self.logger:
                    self.logger.warn('分块 %d-%d 连续 %d/%d 次没有进展'
                                     % (start, end, stall, max_stall))
                if stall >= max_stall:
                    self._add_error('分块 %d-%d 连续 %d 次没下到任何数据，已放弃：\n   %s'
                                    % (start, end, max_stall,
                                       last_reason.splitlines()[0]))
                    return False
                time.sleep(min(2 * stall, 15))
        return False

    # ---------- 单线程流式下载 ----------
    def _download_single(self):
        """服务器不支持 Range 时用这个。也支持"已下部分继续"（通过字节跳过）。"""
        tmp = self.out_path + '.part0'
        total = self.total if self.total > 0 else 0

        max_stall = 8
        stall = 0
        last_reason = '网络或服务器不稳定'
        while not self._stop_flag.is_set():
            while self._pause_flag.is_set() and not self._stop_flag.is_set():
                time.sleep(0.25)
            if self._stop_flag.is_set():
                return False
            already = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if total > 0 and already >= total:
                break
            before = already
            try:
                headers = {}
                if self.token:
                    headers['Authorization'] = 'Bearer ' + self.token
                if already > 0:
                    headers['Range'] = 'bytes=%d-' % already
                req = make_request(self.url, headers)
                with urllib.request.urlopen(req, timeout=90,
                                            context=ssl_context()) as r:
                    if already > 0 and r.status == 200:
                        # 服务器不支持续传 → 从头来
                        already = 0
                        mode = 'wb'
                        if self.logger:
                            self.logger.warn('服务器不支持续传，从头下载')
                    else:
                        mode = 'ab' if already else 'wb'
                    base = already
                    with open(tmp, mode) as f:
                        while True:
                            if self._stop_flag.is_set():
                                return False
                            if self._pause_flag.is_set():
                                break
                            buf = r.read(BUFFER)
                            if not buf:
                                break
                            f.write(buf)
                            base += len(buf)
                            with self._lock:
                                self.parts_done[tmp] = base
                                self.done_bytes = base
                break   # 读完了
            except Exception as e:
                if self._stop_flag.is_set():
                    return False
                if self._pause_flag.is_set():
                    continue
                last_reason = friendly_http_error(e, self.url)
                if self.logger:
                    self.logger.warn('单线程出错（已下 %s）：%s'
                                     % (fmt_size(os.path.getsize(tmp)
                                                 if os.path.exists(tmp) else 0),
                                        last_reason.splitlines()[0]))

            # 只有"一点都没下动"才算一次失败
            now = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if now > before:
                stall = 0
            else:
                stall += 1
                if self.logger:
                    self.logger.warn('单线程连续 %d/%d 次没有进展' % (stall, max_stall))
                if stall >= max_stall:
                    self._add_error('下载连续 %d 次没有进展，已放弃：\n   %s'
                                    % (max_stall, last_reason.splitlines()[0]))
                    return False
                time.sleep(min(2 * stall, 15))

        try:
            os.replace(tmp, self.out_path)
        except OSError as e:
            self._add_error('重命名失败：%s' % e)
            return False
        if total > 0 and os.path.getsize(self.out_path) != total:
            self._add_error('大小校验不通过：期望 %s，实际 %s'
                            % (fmt_size(total), fmt_size(os.path.getsize(self.out_path))))
            return False
        self.done_bytes = self.total = os.path.getsize(self.out_path)
        return True

    def _recalc_done(self):
        self.done_bytes = sum(self.parts_done.values())

    def _tick_speed(self):
        now = time.time()
        dt = now - self._last_t
        if dt >= 0.9:
            with self._lock:
                cur = self.done_bytes
                cur_parts = dict(self.parts_done)
            self.speed = max(0.0, (cur - self._last_b) / dt)
            # 顺带算每个分块（线程）自己的速度，供界面详情展示
            for pp, b in cur_parts.items():
                prev = self._part_last_b.get(pp, b)
                self.part_speed[pp] = max(0.0, (b - prev) / dt)
                self._part_last_b[pp] = b
            self._last_t = now
            self._last_b = cur

    # ---------- 主流程 ----------
    def run(self):
        self._run_started = time.time()
        try:
            self._run()
        except Exception as e:
            self.state = 'failed'
            self._add_error('未预期的错误：%s' % e)
            if self.logger:
                self.logger.exception('任务异常终止: %s' % self.name, e)
        finally:
            # 累计"真正在下"的时长（界面详情里显示）
            if self._run_started:
                self._base_elapsed += max(0.0, time.time() - self._run_started)
                self._run_started = None
            if self.state == 'done' and self.finished_at is None:
                self.finished_at = time.time()
            if self._claimed and self.out_path:
                release_out_path(self.out_path)
                self._claimed = False

    def _run(self):
        # 1. 解析
        self.state = 'resolving'
        if not self.resolve():
            return

        os.makedirs(self.save_dir, exist_ok=True)

        # 2. 探测（拿大小 / 是否支持 Range / 文件名）
        probe = probe_direct(self.url, self.token)
        if not probe.ok:
            self.state = 'failed'
            self._add_error(probe.error or '无法访问这个链接。')
            if self.logger:
                self.logger.error('探测失败: %s\n   %s'
                                  % (self.url, probe.error))
            return

        if probe.is_webpage:
            self.state = 'failed'
            h = host_of(self.url)
            self._add_error('这个链接返回的是网页（HTML），不是文件。\n'
                            '   地址：%s\n'
                            '   怎么拿真实直链：\n'
                            '   1. 浏览器打开链接 → F12 → Network 标签\n'
                            '   2. 点下载，找到体积最大的那条请求\n'
                            '   3. 右键 → Copy → Copy link address\n'
                            '   4. 粘回本工具%s'
                            % (self.url, ('\n   ' + SPECIAL_HINTS[h]) if h in SPECIAL_HINTS else ''))
            return

        # 文件名：HF 用解析出来的，直链用服务器给的
        if self.link_type == 'hf' and self.filename:
            self.filename = os.path.basename(self.filename)
        elif probe.filename:
            self.filename = probe.filename
        self.out_path = os.path.join(self.save_dir, self.filename)

        # 同一个文件同时只让一个任务写（重复添加 / 恢复后重复添加都挡住）
        if not claim_out_path(self.out_path):
            self.state = 'failed'
            self._add_error('已经有一个任务在下载同一个文件了。\n'
                            '   目标：%s\n'
                            '   请在那个任务上点「继续」，不要重复添加同一个文件。'
                            % self.out_path)
            if self.logger:
                self.logger.warn('拒绝并发写同一个文件: %s' % self.out_path)
            return
        self._claimed = True

        saved = load_manifest(self.out_path)
        self.total = probe.size if probe.size > 0 else 0
        # 服务器这次没给大小，但上次记录过 → 沿用，免得把断点丢了
        if self.total <= 0 and saved and saved.get('url') == self.url \
                and int(saved.get('total', 0)) > 0:
            self.total = int(saved['total'])
            if self.logger:
                self.logger.warn('服务器这次没报文件大小，沿用上次记录的 %s'
                                 % fmt_size(self.total))

        if self.logger:
            self.logger.info('文件: %s' % self.filename)
            self.logger.info('大小: %s   支持多线程: %s'
                             % (fmt_size(self.total) if self.total else '未知',
                                '是' if probe.supports_range else '否'))

        # 3. 已完成？
        if self.total > 0 and os.path.exists(self.out_path) \
                and os.path.getsize(self.out_path) == self.total:
            self.done_bytes = self.total
            self.state = 'done'
            self._cleanup_after_done()
            if self.logger:
                self.logger.info('文件已存在且完整，跳过: %s' % self.out_path)
            return

        # 4. 拿不到大小 → 只能单线程流式（无法可靠续传）
        if self.total <= 0:
            if self._parts_on_disk():
                # ★ 大小未知时无法判断残留分块属于哪套布局。
                #   这里也选择"什么都不动"：万一这次只是探测抽风，
                #   删掉就等于把用户几 GB 的断点数据白白扔了。
                self.state = 'failed'
                self._add_error('服务器这次没有提供文件大小，无法校验磁盘上已有的'
                                '断点分块。\n'
                                '   为免误删数据，本次没有改动任何文件。\n'
                                '   请稍后点「继续」重试（多半是临时的探测失败）。')
                if self.logger:
                    self.logger.warn('未拿到文件大小，但磁盘上有残留分块，'
                                     '已保留不做改动')
                return
            self.mode = 'single'
            self.state = 'downloading'
            self._run_single()
            return

        # 5. 分块布局：优先用断点记录，其次抽样校验残留分块，最后才新建
        if not self._plan_parts(probe.supports_range):
            self.state = 'failed'
            return
        self.state = 'downloading'
        self._run_multi()
        return

    def _safe_remove(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _cleanup_after_done(self):
        """文件已经是完整的 → 清掉残留分块和断点记录"""
        for pp, _s, _e in self.part_specs:
            self._safe_remove(pp)
        for i in range(MAX_PARTS):
            self._safe_remove('%s.part%d' % (self.out_path, i))
        clear_manifest(self.out_path)

    def _verify_file(self, path):
        """成品抽查：从服务器取 3 小段，和本地成品比对

        ★ 长度对不代表内容对。这一步能抓住"分块错位""服务器返回了错误的
          偏移"这类问题 —— 它们合并出来大小刚好对，但内容是坏的。

        返回 (是否通过, 说明)。取不到数据时按"没验成"放过（网络问题
        不应该把一个本来正确的文件判死），只有**真的比对不一致**才判失败。
        """
        if self.total <= MIN_CHUNK_BYTES:
            return True, '文件很小，跳过抽查'
        if len(self.part_specs) <= 1:
            # 只有 1 块：数据就是从文件头连续下来的，不存在"放错位置"，
            # 而且这种服务器往往正好不支持按偏移取数据 —— 抽查会误判，
            # 白白把一个正确的文件判死。所以跳过。
            return True, '只有 1 个分块，无需抽查'
        offsets = [self.total // 4, self.total // 2, (self.total * 3) // 4]
        checked = 0
        for off in offsets:
            want = self._sample(off)
            if want is None:
                continue                      # 取不到 → 这处跳过
            try:
                with open(path, 'rb') as f:
                    f.seek(off)
                    have = f.read(len(want))
            except OSError:
                return False, '读不出成品文件'
            if have != want:
                return False, ('第 %s 字节处的内容和服务器不一致'
                               % fmt_size(off))
            checked += 1
        if checked == 0:
            return True, '服务器取不到抽查数据，跳过（不因此判失败）'
        return True, '抽查 %d 处一致' % checked

    def _run_multi(self):
        # 统计已有进度（顺带把"比这块需要还长"的残留截断掉）
        self.parts_done = {}
        for pp, s, e in self.part_specs:
            need = e - s + 1
            have = os.path.getsize(pp) if os.path.exists(pp) else 0
            if have > need:
                try:
                    with open(pp, 'r+b') as f:
                        f.truncate(need)
                    if self.logger:
                        self.logger.warn('%s 比该分块需要的还大，已截断到 %s'
                                         % (os.path.basename(pp), fmt_size(need)))
                except OSError:
                    pass
                have = need
            self.parts_done[pp] = have
        with self._lock:
            self._recalc_done()

        # ★ 先把布局写进断点记录：万一中途崩溃/关机/断电，下次能原样接上
        save_manifest(self.out_path, {
            'url': self.url,
            'total': self.total,
            'filename': self.filename,
            'mode': 'multi',
            'threads': self.threads,
            'parts': [[s, e] for _pp, s, e in self.part_specs],
            'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })

        if self.done_bytes > 0 and self.logger:
            self.logger.info('发现已下载 %s（%d 个分块），从这里继续'
                             % (fmt_size(self.done_bytes), len(self.part_specs)))

        self._last_t = time.time()
        self._last_b = self.done_bytes
        self.part_speed = {}
        self._part_last_b = dict(self.parts_done)
        self._threads = []
        for pp, s, e in self.part_specs:
            t = threading.Thread(target=self._download_part, args=(pp, s, e),
                                 daemon=True)
            t.start()
            self._threads.append(t)

        while any(t.is_alive() for t in self._threads):
            self._tick_speed()
            time.sleep(0.4)
            if self._stop_flag.is_set():
                break
        for t in self._threads:
            t.join(timeout=5)
        self._tick_speed()

        # ★ 致命错误（数据没法安全落位）优先于"已取消"，否则会被当成普通取消
        if self._fatal:
            self.state = 'failed'
            self._add_error(self._fatal)
            return

        if self._stop_flag.is_set():
            self.state = 'cancelled'
            if self.logger:
                self.logger.info('已暂停/取消: %s（已下载 %s 保留，点「继续」接着下）'
                                 % (self.name, fmt_size(self.done_bytes)))
            return

        # 检查分块完整性
        incomplete = []
        for pp, s, e in self.part_specs:
            need = e - s + 1
            have = os.path.getsize(pp) if os.path.exists(pp) else 0
            if have < need:
                incomplete.append((pp, have, need))
        if incomplete:
            self.state = 'failed'
            msgs = ['有 %d 个分块没有下载完整：' % len(incomplete)]
            for pp, have, need in incomplete[:5]:
                msgs.append('   %s  已下 %s / %s'
                            % (os.path.basename(pp), fmt_size(have), fmt_size(need)))
            msgs.append('')
            msgs.append('已下载的 %s 都保留着 —— 点这一行的「继续」就能接着下。'
                        % fmt_size(self.done_bytes))
            self._add_error('\n'.join(msgs))
            return

        # 合并：先写临时文件，校验通过才改名
        # （旧版直接写正式文件名，一旦校验失败，磁盘上就留下一个
        #   "名字是正式名、内容却是坏的" 文件 —— 这是必须避免的）
        self.state = 'merging'
        if self.logger:
            self.logger.info('分块下载完成，正在合并 %d 个分块...' % len(self.part_specs))
        tmp_out = self.out_path + '.merging'
        try:
            with open(tmp_out, 'wb') as out:
                for pp, s, e in self.part_specs:
                    with open(pp, 'rb') as f:
                        while True:
                            b = f.read(4 * 1024 * 1024)
                            if not b:
                                break
                            out.write(b)
        except Exception as e:
            self.state = 'failed'
            self._add_error('合并分块失败：%s\n   磁盘空间是否足够？文件是否被占用？' % e)
            if self.logger:
                self.logger.exception('合并失败: %s' % self.name, e)
            self._safe_remove(tmp_out)
            return

        final = os.path.getsize(tmp_out)
        if self.total > 0 and final != self.total:
            self.state = 'failed'
            self._safe_remove(tmp_out)
            self._add_error('文件大小校验不通过：期望 %s，实际 %s\n'
                            '   分块内容可能已经错位。为了不给你一个坏文件，'
                            '半成品已经删掉。\n'
                            '   请把同目录下的 %s.part* 删掉后重新下载。'
                            % (fmt_size(self.total), fmt_size(final),
                               os.path.basename(self.out_path)))
            if self.logger:
                self.logger.error('合并后大小不符，已丢弃半成品: %s (%s)'
                                  % (self.out_path, fmt_size(final)))
            return

        # ★ 成品抽查：长度对不代表内容对
        ok, why = self._verify_file(tmp_out)
        if not ok:
            self.state = 'failed'
            self._safe_remove(tmp_out)
            self._add_error('成品抽查没通过：%s\n'
                            '   合并出来的长度是对的，但内容和服务器不一致，'
                            '说明分块位置错了。\n'
                            '   为了不给你一个坏文件，半成品已经删掉。\n'
                            '   请把同目录下的 %s.part* 删掉后重新下载。'
                            % (why, os.path.basename(self.out_path)))
            if self.logger:
                self.logger.error('成品抽查失败（%s），已丢弃半成品: %s'
                                  % (why, self.out_path))
            return
        if self.logger:
            self.logger.info('成品抽查: %s' % why)

        try:
            os.replace(tmp_out, self.out_path)
        except OSError as e:
            self.state = 'failed'
            self._safe_remove(tmp_out)
            self._add_error('重命名失败：%s' % e)
            return

        for pp, _s, _e in self.part_specs:
            self._safe_remove(pp)
        # 顺带清掉不在本次布局里的残留分块（例如服务器不再支持 Range 时
        # 留下的旧分块）—— 此时成品已经校验通过，清掉是安全的
        kept = set(pp for pp, _s, _e in self.part_specs)
        for i in range(MAX_PARTS):
            p = '%s.part%d' % (self.out_path, i)
            if p not in kept:
                self._safe_remove(p)
        clear_manifest(self.out_path)

        self.done_bytes = final
        self.state = 'done'
        if self.logger:
            self.logger.info('下载完成: %s  (%s)' % (self.out_path, fmt_size(final)))

    def _run_single(self):
        if self.total <= 0:
            if self.logger:
                self.logger.warn('服务器未提供文件大小，只能单线程下载（无法显示进度百分比）')
        else:
            # 已有部分文件时更新进度基线
            tmp = self.out_path + '.part0'
            if os.path.exists(tmp):
                with self._lock:
                    self.done_bytes = os.path.getsize(tmp)
        self._last_t = time.time()
        self._last_b = self.done_bytes

        mon = threading.Event()

        def monitor():
            while not mon.is_set():
                self._tick_speed()
                time.sleep(0.4)
        threading.Thread(target=monitor, daemon=True).start()

        try:
            ok = self._download_single()
        finally:
            mon.set()

        if self._stop_flag.is_set():
            self.state = 'cancelled'
            if self.logger:
                self.logger.info('已取消: %s（已下载的保留，可续传）' % self.name)
            return
        if not ok:
            self.state = 'failed'
            return
        self.state = 'done'
        if self.logger:
            self.logger.info('下载完成: %s  (%s)'
                             % (self.out_path, fmt_size(self.done_bytes)))


# =============================================================================
#  格式化
# =============================================================================

def fmt_size(n):
    if n is None or n < 0:
        return '未知'
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


def fmt_duration(sec):
    """把秒数说成人话：不到 1 秒 / 45 秒 / 2 分 15 秒 / 1 小时 03 分"""
    if sec is None or sec < 0:
        return '--'
    if sec <= 0:
        return '—'
    if sec < 1:
        return '不到 1 秒'
    sec = int(sec)
    if sec < 60:
        return '%d 秒' % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return '%d 分 %02d 秒' % (m, s)
    h, m = divmod(m, 60)
    return '%d 小时 %02d 分' % (h, m)


def fmt_clock(ts):
    """时间戳 → 2026-10-13 22:30:45"""
    if not ts:
        return '--'
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '--'


# =============================================================================
#  配置
# =============================================================================

DEFAULT_CONFIG = {
    'save_dir': r'D:\models',
    'threads': 8,
    'endpoint': DEFAULT_HF_ENDPOINT,
    'token': '',
    'window': '1040x740',
    'auto_paste': True,
    'concurrent': 1,
}


def config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'emlyndownload_config.json')


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        p = config_path()
        if os.path.isfile(p):
            with open(p, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg.get('save_dir'):
        cfg['save_dir'] = DEFAULT_CONFIG['save_dir']
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
