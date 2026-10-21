# -*- coding: utf-8 -*-
"""
GUI 端到端测试：取消后点「继续」能不能原地续传

这是用户最直接的抱怨 —— "想断点续传，发现没有这个功能"。
所以这里不走核心 API，而是真的建窗口、真的加任务、真的点「继续」。

用法:
    python tests/test_gui_resume.py
"""

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emlyn_dl_core as core            # noqa: E402
from test_resume import (               # noqa: E402
    FILENAME, MB, PAYLOAD, PAYLOAD_SHA, Server, parts_in,
)


class QuietLogger(core.Logger):
    """不写文件、只记内存的日志器"""

    def __init__(self, ui_queue=None, log_dir=None):
        self.ui_queue = None
        self.lock = threading.Lock()
        self.msgs = []

    def write(self, level, msg, detail=None):
        self.msgs.append('[%s] %s' % (level, msg))

    def path(self):
        return '(quiet)'


def wait_until(pred, app, root, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        root.update()
        if pred():
            return True
        time.sleep(0.02)
    return False


def main():
    try:
        import tkinter as tk
    except Exception as e:
        print('SKIP 没有 tkinter: %s' % e)
        return 0

    import EmlynDownload as gui

    server = Server()
    tmp = tempfile.mkdtemp(prefix='emlyn_gui_test_')
    outdir = os.path.join(tmp, 'out')
    os.makedirs(outdir, exist_ok=True)

    real_logger = core.Logger
    real_tasks_file = gui.TASKS_FILE
    core.Logger = QuietLogger          # 别把测试写进用户的 logs/
    gui.TASKS_FILE = os.path.join(tmp, 'tasks.json')   # 别动用户真正的下载列表

    root = None
    try:
        root = tk.Tk()
        root.withdraw()                # 不弹窗，但事件循环照常
        app = gui.App(root)
        app.cfg['save_dir'] = outdir
        app.cfg['threads'] = 4
        app.var_dir.set(outdir)
        app.var_threads.set(4)

        print('== GUI: 取消后点「继续」 ==')
        app._add_task(server.url)
        row = next(r for r in app.rows if r.dl.raw_input == server.url)

        ok = wait_until(lambda: row.dl.done_bytes >= 3 * MB, app, root)
        if not ok:
            print('  FAIL 没能下到 3MB（state=%s）' % row.dl.state)
            return 1
        row.cancel()
        ok = wait_until(lambda: row.dl.state == 'cancelled'
                        and row.dl not in app.active, app, root)
        print('  [1] 点「取消」后: state=%s 已下=%s 分块=%d'
              % (row.dl.state, core.fmt_size(row.dl.done_bytes),
                 len(parts_in(outdir))))
        if not ok:
            print('  FAIL 取消后没停下来')
            return 1

        got = sum(os.path.getsize(os.path.join(outdir, f))
                  for f in parts_in(outdir))
        out = os.path.join(outdir, FILENAME)

        server.reset_served()
        row.resume()                   # ← 相当于用户点了行里的「继续」
        ok = wait_until(lambda: row.dl.state in ('done', 'failed'), app, root)
        print('  [2] 点「继续」后: state=%s 这次只补下了 %s'
              % (row.dl.state, core.fmt_size(server.served())))
        if not ok:
            print('  FAIL 续传没有结束')
            return 1
        if row.dl.state != 'done':
            print('  FAIL 状态不是 done: %s' % (row.dl.error or ''))
            return 1
        if not os.path.exists(out) or os.path.getsize(out) != len(PAYLOAD):
            print('  FAIL 最终文件大小不对')
            return 1
        h = hashlib.sha256(open(out, 'rb').read()).hexdigest()
        if h != PAYLOAD_SHA:
            print('  FAIL 内容哈希不匹配')
            return 1
        if server.served() >= len(PAYLOAD):
            print('  FAIL 又重下了差不多整个文件（不是续传）')
            return 1

        print('  -> PASS 取消 → 点「继续」→ 只补下 %s，内容完整正确'
              % core.fmt_size(server.served()))
        print('\n1/1 通过')
        return 0
    finally:
        core.Logger = real_logger
        gui.TASKS_FILE = real_tasks_file
        try:
            if root is not None:
                root.quit()
                root.destroy()
        except Exception:
            pass
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
