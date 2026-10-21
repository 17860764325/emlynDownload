# -*- coding: utf-8 -*-
"""
EmlynDownload 断点续传行为测试（本地 Range 服务器，不依赖外网）

用法:
    python tests/test_resume.py            # 跑全部
    python tests/test_resume.py T1 T5      # 只跑指定用例
"""

import hashlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emlyn_dl_core as core      # noqa: E402

FILENAME = 'payload.bin'
MB = 1024 * 1024
SIZE = 12 * MB

# 服务器节流：每 BLOCK 字节睡 DELAY 秒，好让"下到一半断掉"稳定可测
BLOCK = 128 * 1024
DELAY = 0.02


def make_payload(size):
    """确定性的伪随机内容，保证每次一致、可哈希比对"""
    buf = bytearray(size)
    seed = 0x12345678
    for i in range(size):
        seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
        buf[i] = (seed >> 16) & 0xFF
    return bytes(buf)


PAYLOAD = make_payload(SIZE)
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


class RangeHandler(BaseHTTPRequestHandler):
    """支持 Range 的 HTTP 服务器（带节流 + 流量统计 + 故障注入）"""

    served = 0
    served_lock = threading.Lock()
    # 故障注入：
    fail_range_after_probe = False   # 探测正常，但后续 Range 请求失败（模拟网络抖动）
    omit_size = False                # 不返回大小（Content-Length / Content-Range）
    ignore_range = False             # 完全不支持 Range：所有请求都回整个文件
    range_only_at_zero = False       # 只在 start=0 时回 206，其他偏移回整个文件
    shift_range_to_zero = False      # 回 206 且头是对的，但内容永远从文件头取

    def log_message(self, *a):
        pass

    def _range(self):
        h = self.headers.get('Range')
        if not h or not h.startswith('bytes='):
            return None
        spec = h[len('bytes='):].split(',')[0].strip()
        if '-' not in spec:
            return None
        a, b = spec.split('-', 1)
        try:
            start = int(a) if a else 0
            end = int(b) if b else len(PAYLOAD) - 1
        except ValueError:
            return None
        if start >= len(PAYLOAD):
            return 'unsatisfiable'
        end = min(end, len(PAYLOAD) - 1)
        return start, end

    def _send(self):
        rng = self._range()

        # 探测请求是 bytes=0-0（start 为 0），放它过去；之后的校验请求全部失败
        if (RangeHandler.fail_range_after_probe and isinstance(rng, tuple)
                and rng[0] > 0):
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        # 假装支持 Range：bytes=0-0 回 206，别的偏移却回整个文件
        if RangeHandler.ignore_range:
            rng = None
        elif (RangeHandler.range_only_at_zero and isinstance(rng, tuple)
                and rng[0] > 0):
            rng = None

        if rng == 'unsatisfiable':
            self.send_response(416)
            self.send_header('Content-Range', 'bytes */%d' % len(PAYLOAD))
            self.end_headers()
            return

        if rng is None:
            body = PAYLOAD
            self.send_response(200)
            if not RangeHandler.omit_size:
                self.send_header('Content-Range', 'bytes 0-%d/%d'
                                 % (len(PAYLOAD) - 1, len(PAYLOAD)))
        else:
            start, end = rng
            if RangeHandler.shift_range_to_zero and start > 0:
                # 头说是 start-end，内容却永远从文件头开始 —— 最阴的一种服务器
                body = PAYLOAD[:end - start + 1]
            else:
                body = PAYLOAD[start:end + 1]
            self.send_response(206)
            if not RangeHandler.omit_size:
                self.send_header('Content-Range', 'bytes %d-%d/%d'
                                 % (start, end, len(PAYLOAD)))

        self.send_header('Content-Type', 'application/octet-stream')
        if not RangeHandler.omit_size:
            self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Disposition',
                         'attachment; filename="%s"' % FILENAME)
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            for off in range(0, len(body), BLOCK):
                self.wfile.write(body[off:off + BLOCK])
                self.wfile.flush()
                with RangeHandler.served_lock:
                    RangeHandler.served += len(body[off:off + BLOCK])
                if DELAY:
                    time.sleep(DELAY)
        except Exception:
            pass

    def do_GET(self):
        self._send()

    def do_HEAD(self):
        self._send()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self):
        self.port = free_port()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', self.port), RangeHandler)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    @property
    def url(self):
        return 'http://127.0.0.1:%d/%s' % (self.port, FILENAME)

    def reset_served(self):
        with RangeHandler.served_lock:
            RangeHandler.served = 0

    def served(self):
        with RangeHandler.served_lock:
            return RangeHandler.served

    def inject_fail_range(self, on=True):
        RangeHandler.fail_range_after_probe = on

    def inject_omit_size(self, on=True):
        RangeHandler.omit_size = on

    def inject_ignore_range(self, on=True):
        RangeHandler.ignore_range = on

    def inject_range_only_at_zero(self, on=True):
        RangeHandler.range_only_at_zero = on

    def inject_shift_range(self, on=True):
        RangeHandler.shift_range_to_zero = on

    def stop(self):
        self.httpd.shutdown()


class QuietLogger(core.Logger):
    def __init__(self):
        self.ui_queue = None
        self.lock = threading.Lock()
        self.msgs = []

    def write(self, level, msg, detail=None):
        self.msgs.append('[%s] %s' % (level, msg))

    def path(self):
        return '(quiet)'


def partial_download(url, outdir, threads, target_bytes, logger):
    """跑到 target_bytes 就取消 —— 模拟"下到一半断了" """
    dl = core.Download(url, outdir, threads, logger=logger)
    t = threading.Thread(target=dl.run, daemon=True)
    t.start()
    deadline = time.time() + 90
    while time.time() < deadline:
        if dl.done_bytes >= target_bytes or dl.state in ('done', 'failed'):
            break
        time.sleep(0.005)
    dl.cancel()
    t.join(timeout=60)
    return dl


def fresh_dir(tmp, name):
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    return d


def parts_in(outdir):
    return sorted(f for f in os.listdir(outdir)
                  if '.part' in f and not f.endswith('.json'))


def sha_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(4 * MB)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def check_final(outdir, state=None):
    """返回 (是否正确, 说明)"""
    out = os.path.join(outdir, FILENAME)
    if not os.path.exists(out):
        return False, '没有生成最终文件'
    if os.path.getsize(out) != len(PAYLOAD):
        return False, '大小不对: %d != %d' % (os.path.getsize(out), len(PAYLOAD))
    if sha_of(out) != PAYLOAD_SHA:
        return False, '内容哈希不匹配（文件已损坏）'
    if state and state != 'done':
        return False, '内容是好的，但状态是 %s' % state
    return True, '内容完整正确'


# ---------------------------------------------------------------- 用例

def T1_multi_resume(server, tmp):
    """多线程中断后重开：应续传（不是从头下）且内容完全正确"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't1')

    dl1 = partial_download(server.url, outdir, 4, 5 * MB, logger)
    got1 = sum(os.path.getsize(os.path.join(outdir, f)) for f in parts_in(outdir))
    print('  [1] 中断: state=%s 已下=%s parts=%s'
          % (dl1.state, core.fmt_size(got1), parts_in(outdir)))
    if got1 < 3 * MB:
        return False, '没能在中途停下来，测试不可信（已下 %s）' % core.fmt_size(got1)

    server.reset_served()
    dl2 = core.Download(server.url, outdir, 4, logger=logger)
    dl2.run()
    served2 = server.served()
    print('  [2] 续传: state=%s 这次服务器只发了 %s'
          % (dl2.state, core.fmt_size(served2)))

    ok, detail = check_final(outdir, dl2.state)
    if not ok:
        return False, detail
    # 关键：续传只该下"缺的那部分"，不该从头再来
    if served2 >= len(PAYLOAD):
        return False, ('续传时又下了 %s（几乎等于整个文件）—— 说明是从头重下的'
                       % core.fmt_size(served2))
    return True, '续传成功，只补下了 %s（内容完整）' % core.fmt_size(served2)


def T2_threads_changed(server, tmp):
    """中途改了线程数：必须沿用上次的布局，绝不能静默损坏"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't2')

    dl1 = partial_download(server.url, outdir, 4, 5 * MB, logger)
    print('  [1] 4线程中断: state=%s' % dl1.state)

    dl2 = core.Download(server.url, outdir, 3, logger=logger)   # ← 改成 3
    dl2.run()
    out = os.path.join(outdir, FILENAME)
    size_ok = os.path.exists(out) and os.path.getsize(out) == len(PAYLOAD)
    hash_ok = os.path.exists(out) and sha_of(out) == PAYLOAD_SHA
    print('  [2] 3线程续传: state=%s 大小对=%s 内容对=%s'
          % (dl2.state, size_ok, hash_ok))
    if size_ok and not hash_ok:
        return False, '静默损坏：大小对了但内容错了（沿用旧分块时错位）'
    if not hash_ok:
        return False, '内容不正确: %s' % (dl2.error or dl2.state)
    return True, '线程数改变后仍然正确（按记录的布局续传）'


def T3_inplace_resume(server, tmp):
    """同一个任务对象 reset() 后原地续传（界面「继续」按钮走的就是这条路）"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't3')

    dl = core.Download(server.url, outdir, 4, logger=logger)
    t = threading.Thread(target=dl.run, daemon=True)
    t.start()
    deadline = time.time() + 90
    while time.time() < deadline:
        if dl.done_bytes >= 3 * MB or dl.state in ('done', 'failed'):
            break
        time.sleep(0.005)
    dl.cancel()
    t.join(timeout=60)
    print('  [1] 取消: state=%s done=%s' % (dl.state, core.fmt_size(dl.done_bytes)))

    dl.reset()                      # ← 界面点「继续」时会做这一步
    print('  [2] reset 后: state=%s stop_flag=%s'
          % (dl.state, dl._stop_flag.is_set()))
    dl.run()
    print('  [3] 重跑: state=%s' % dl.state)
    return check_final(outdir, dl.state)


def T4_oversized_part(server, tmp):
    """残留分块比该块需要的还长：不能被当成"已完成"直接合并"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't4')
    out_path = os.path.join(outdir, FILENAME)

    with open(out_path + '.part0', 'wb') as f:
        f.write(PAYLOAD[:6 * MB])          # 4 块布局下 part0 只需要 3MB
    with open(out_path + '.part1', 'wb') as f:
        f.write(PAYLOAD[3 * MB:6 * MB])

    dl = core.Download(server.url, outdir, 4, logger=logger)
    dl.run()
    print('  [1] state=%s error=%s'
          % (dl.state, (dl.error or '').splitlines()[:1]))
    ok, detail = check_final(outdir, dl.state)
    if not ok:
        return False, detail + '（残留分块长度异常时必须重下或拒绝）'
    return True, '内容正确'


def T5_old_layout_adopted(server, tmp):
    """★ 真实场景：老版本没有断点记录，磁盘上是 8 块布局，当前配置却是 4 线程

    这正是用户那个 4.58GB 文件的状态（part0-4/6/7 满、part5 只下一半）。
    必须靠"抽样比对"认出旧布局并接着下，而不是按 4 块去拼。
    """
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't5')
    out_path = os.path.join(outdir, FILENAME)

    specs = core.chunk_layout(len(PAYLOAD), 8)
    for i, (s, e) in enumerate(specs):
        need = e - s + 1
        data = PAYLOAD[s:s + (need // 2 if i == 5 else need)]
        with open(out_path + '.part%d' % i, 'wb') as f:
            f.write(data)
    print('  [1] 造出 8 块残留（part5 只下一半），part 文件数=%d'
          % len(parts_in(outdir)))

    dl = core.Download(server.url, outdir, 4, logger=logger)   # ← 4 线程
    dl.run()
    print('  [2] 续传: state=%s 布局来源=%s 分块数=%d'
          % (dl.state, dl._layout_source, len(dl.part_specs)))
    if dl._layout_source != 'verified':
        return False, '没能认出旧布局（来源=%s）' % dl._layout_source
    if len(dl.part_specs) != 8:
        return False, '没有沿用 8 块布局（用了 %d 块）' % len(dl.part_specs)
    return check_final(outdir, dl.state)


def T6_garbage_parts(server, tmp):
    """残留分块内容是垃圾：必须识别出来删掉重下，而不是拼进去"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't6')
    out_path = os.path.join(outdir, FILENAME)

    with open(out_path + '.part0', 'wb') as f:
        f.write(PAYLOAD[:1 * MB])                 # part0 开头永远"对"
    with open(out_path + '.part1', 'wb') as f:
        f.write(b'\xAA' * (1 * MB))               # part1 是垃圾

    dl = core.Download(server.url, outdir, 4, logger=logger)
    dl.run()
    print('  [1] state=%s 布局来源=%s error=%s'
          % (dl.state, dl._layout_source, (dl.error or '').splitlines()[:1]))
    ok, detail = check_final(outdir, dl.state)
    if not ok:
        return False, detail + '（垃圾分块被当成续传数据拼进去了）'
    return True, '识别出垃圾分块并重下，内容正确'


def T7_no_corrupt_output(server, tmp):
    """合并后大小不对时，不能在正式文件名下留下坏文件"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't7')
    out_path = os.path.join(outdir, FILENAME)

    # 造一个和服务器不一致的断点记录（模拟服务器上的文件被换过）
    core.save_manifest(out_path, {
        'url': server.url, 'total': len(PAYLOAD) + 1000, 'filename': FILENAME,
        'mode': 'multi', 'threads': 4,
        'parts': core.chunk_layout(len(PAYLOAD), 4),
    })
    with open(out_path + '.part0', 'wb') as f:
        f.write(PAYLOAD)

    dl = core.Download(server.url, outdir, 4, logger=logger)
    dl.run()
    print('  [1] state=%s error=%s' % (dl.state, (dl.error or '').splitlines()[:1]))
    if os.path.exists(out_path):
        # 只有当它确实完整正确时才算通过
        if sha_of(out_path) == PAYLOAD_SHA:
            return True, '最终文件正确'
        return False, ('正式文件名下留下了一个坏文件（%s）—— 必须删掉'
                       % core.fmt_size(os.path.getsize(out_path)))
    return True, '没有留下坏文件（已丢弃半成品）'


def seed_parts(outdir, n, fraction=3):
    """按 n 块布局造出残留分块，每块只下 1/fraction"""
    out_path = os.path.join(outdir, FILENAME)
    for i, (s, e) in enumerate(core.chunk_layout(len(PAYLOAD), n)):
        need = e - s + 1
        with open(out_path + '.part%d' % i, 'wb') as f:
            f.write(PAYLOAD[s:s + need // fraction])
    return out_path


def bytes_in(outdir):
    return sum(os.path.getsize(os.path.join(outdir, f)) for f in parts_in(outdir))


def T8_verify_unavailable_keeps_data(server, tmp):
    """★ 校验请求打不通时，绝不能把断点数据删掉

    网络抖动 ≠ 数据坏了。用户刚丢过一个 4GB 的下载，
    这种"因为一次请求失败就把几 GB 断点删了"的行为是绝对不能有的。
    """
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't8')
    seed_parts(outdir, 4)
    n_before, b_before = len(parts_in(outdir)), bytes_in(outdir)

    server.inject_fail_range(True)
    try:
        dl = core.Download(server.url, outdir, 4, logger=logger)
        dl.run()
    finally:
        server.inject_fail_range(False)

    n_after, b_after = len(parts_in(outdir)), bytes_in(outdir)
    print('  [1] state=%s 分块 %d->%d  字节 %s->%s'
          % (dl.state, n_before, n_after,
             core.fmt_size(b_before), core.fmt_size(b_after)))
    if b_after < b_before:
        return False, ('断点数据被删了（%s -> %s）'
                       % (core.fmt_size(b_before), core.fmt_size(b_after)))
    if dl.state != 'failed':
        return False, '应该是 failed 并提示重试，实际是 %s' % dl.state
    if '重试' not in (dl.error or ''):
        return False, '错误提示里没有告诉用户重试: %r' % (dl.error or '')[:80]
    return True, '完整保留断点数据，并提示稍后重试'


def T9_unknown_size_keeps_data(server, tmp):
    """服务器不报文件大小时：也不能删掉已有分块"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't9')
    seed_parts(outdir, 4)
    n_before, b_before = len(parts_in(outdir)), bytes_in(outdir)

    server.inject_omit_size(True)
    try:
        dl = core.Download(server.url, outdir, 4, logger=logger)
        dl.run()
    finally:
        server.inject_omit_size(False)

    n_after, b_after = len(parts_in(outdir)), bytes_in(outdir)
    print('  [1] state=%s 分块 %d->%d  字节 %s->%s'
          % (dl.state, n_before, n_after,
             core.fmt_size(b_before), core.fmt_size(b_after)))
    if b_after < b_before:
        return False, '断点数据被删了'
    if dl.state != 'failed':
        return False, '应该是 failed 并提示重试，实际是 %s' % dl.state
    return True, '完整保留断点数据'


def T10_server_ignores_range(server, tmp):
    """★ 服务器完全不支持 Range：必须只用 1 个连接，绝不能分块

    如果还按多块去下，服务器每次都从文件头给数据，第 2 块起就会拿到
    错误的字节 —— 结果"大小对、内容错"。
    """
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't10')

    server.inject_ignore_range(True)
    try:
        dl = core.Download(server.url, outdir, 4, logger=logger)
        dl.run()
    finally:
        server.inject_ignore_range(False)

    print('  [1] state=%s 模式=%s 分块数=%d'
          % (dl.state, dl.mode, len(dl.part_specs)))
    if len(dl.part_specs) > 1:
        return False, '服务器不支持 Range，却切成了 %d 块' % len(dl.part_specs)
    return check_final(outdir, dl.state)


def T11_fake_range_support(server, tmp):
    """★ 服务器"假装"支持 Range（只有 start=0 回 206）：也不能拼错"""
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't11')

    server.inject_range_only_at_zero(True)
    try:
        dl = core.Download(server.url, outdir, 4, logger=logger)
        dl.run()
    finally:
        server.inject_range_only_at_zero(False)

    print('  [1] state=%s 模式=%s 分块数=%d'
          % (dl.state, dl.mode, len(dl.part_specs)))
    out = os.path.join(outdir, FILENAME)
    if os.path.exists(out) and os.path.getsize(out) == len(PAYLOAD) \
            and sha_of(out) != PAYLOAD_SHA:
        return False, '静默损坏：大小对了但内容错了（被假 Range 骗了）'
    return check_final(outdir, dl.state)


def T12_hidden_wrong_offset(server, tmp):
    """★ 最阴的一种：Range 头返回 206 且完全正确，内容却永远从文件头取

    这种服务器光看状态码和长度是发现不了的 —— 必须靠"取两处对比"识破。
    """
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't12')

    server.inject_shift_range(True)
    try:
        dl = core.Download(server.url, outdir, 4, logger=logger)
        dl.run()
    finally:
        server.inject_shift_range(False)

    print('  [1] state=%s 模式=%s 分块数=%d'
          % (dl.state, dl.mode, len(dl.part_specs)))
    out = os.path.join(outdir, FILENAME)
    if os.path.exists(out) and os.path.getsize(out) == len(PAYLOAD) \
            and sha_of(out) != PAYLOAD_SHA:
        return False, '静默损坏：偏移被无视却没发现'
    return check_final(outdir, dl.state)


def T13_task_record_roundtrip(server, tmp):
    """★ 任务记录：存下来 → 读出来 → 进度能从磁盘算回来 → 接着下

    这就是"关掉软件再打开，列表和进度都还在"依赖的东西。
    """
    logger = QuietLogger()
    outdir = fresh_dir(tmp, 't13')
    dl1 = partial_download(server.url, outdir, 4, 5 * MB, logger)
    on_disk = bytes_in(outdir)
    print('  [1] 中断: state=%s 磁盘上=%s' % (dl1.state, core.fmt_size(on_disk)))

    rec = dl1.snapshot()                       # 相当于"写进列表文件"
    if rec.get('url') != server.url or not rec.get('total'):
        return False, '记录里缺少 url/大小: %r' % rec
    print('  [2] 记录: url=%s total=%s'
          % (rec['url'], core.fmt_size(rec['total'])))

    dl2 = core.Download.from_snapshot(rec, logger=logger)   # 相当于"重开软件"
    dl2.refresh_progress_from_disk()                        # 不联网，算回进度
    print('  [3] 重开后: state=%s 已下载=%s（磁盘上 %s）'
          % (dl2.state, core.fmt_size(dl2.done_bytes), core.fmt_size(on_disk)))
    if dl2.done_bytes < on_disk * 0.9:
        return False, ('重开后进度算不回来（%s vs 磁盘 %s）'
                       % (core.fmt_size(dl2.done_bytes), core.fmt_size(on_disk)))
    if dl2.state not in ('interrupted', 'cancelled'):
        return False, '重开后状态应是"未完成/可继续"，实际 %s' % dl2.state

    server.reset_served()
    dl2.run()
    served = server.served()
    print('  [4] 点继续: state=%s 只补下 %s' % (dl2.state, core.fmt_size(served)))
    ok, detail = check_final(outdir, dl2.state)
    if not ok:
        return False, detail
    if served >= len(PAYLOAD):
        return False, '重开后是从头重下的，不是续传'
    return True, '记录能存能读，进度算得回来，接着下只补 %s' % core.fmt_size(served)


CASES = [
    ('T1', '多线程中断后续传', T1_multi_resume),
    ('T2', '改变线程数后续传', T2_threads_changed),
    ('T3', '同一对象原地续传', T3_inplace_resume),
    ('T4', '异常长度的残留分块', T4_oversized_part),
    ('T5', '认出旧版分块布局', T5_old_layout_adopted),
    ('T6', '垃圾残留分块', T6_garbage_parts),
    ('T7', '不留坏文件', T7_no_corrupt_output),
    ('T8', '校验失败不删数据', T8_verify_unavailable_keeps_data),
    ('T9', '大小未知不删数据', T9_unknown_size_keeps_data),
    ('T10', '服务器不支持 Range', T10_server_ignores_range),
    ('T11', '服务器假 Range', T11_fake_range_support),
    ('T12', '服务器偏移被无视', T12_hidden_wrong_offset),
    ('T13', '任务记录存取与续传', T13_task_record_roundtrip),
]


def main():
    want = sys.argv[1:] or [c[0] for c in CASES]
    server = Server()
    tmp = tempfile.mkdtemp(prefix='emlyn_test_')
    results = []
    try:
        for key, title, fn in CASES:
            if key not in want:
                continue
            print('\n== %s %s ==' % (key, title))
            server.inject_fail_range(False)
            server.inject_omit_size(False)
            server.inject_ignore_range(False)
            server.inject_range_only_at_zero(False)
            server.inject_shift_range(False)
            server.reset_served()
            try:
                ok, detail = fn(server, tmp)
            except Exception as e:
                import traceback
                traceback.print_exc()
                ok, detail = False, '用例异常: %r' % e
            results.append((key, title, ok, detail))
            print('  -> %s  %s' % ('PASS' if ok else 'FAIL', detail))
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + '=' * 70)
    npass = sum(1 for r in results if r[2])
    for key, title, ok, detail in results:
        print('%-4s %-24s %s' % (key, title, 'PASS' if ok else 'FAIL'))
    print('%d/%d 通过' % (npass, len(results)))
    return 0 if npass == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
