# -*- coding: utf-8 -*-
"""
GUI 重启测试：关掉软件再打开，下载记录应该还在，并且能点「继续」接着下

对应用户的实际反馈："我重新打开软件，我之前的下载记录全没了"

做法：真的建两次窗口（第一次加任务并关掉，第二次重新打开），
      检查列表有没有恢复、进度算不算得回来、「继续」能不能接着下。

用法:
    python tests/test_gui_restart.py
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
    FILENAME, MB, PAYLOAD, PAYLOAD_SHA, Server, bytes_in, parts_in,
)


class QuietLogger(core.Logger):
    def __init__(self, ui_queue=None, log_dir=None):
        self.ui_queue = None
        self.lock = threading.Lock()
        self.msgs = []

    def write(self, level, msg, detail=None):
        self.msgs.append('[%s] %s' % (level, msg))

    def path(self):
        return '(quiet)'


def wait_until(pred, root, timeout=120):
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
    tmp = tempfile.mkdtemp(prefix='emlyn_restart_')
    outdir = os.path.join(tmp, 'out')
    os.makedirs(outdir, exist_ok=True)
    tasks_file = os.path.join(tmp, 'tasks.json')

    real_logger = core.Logger
    real_tasks_file = gui.TASKS_FILE
    core.Logger = QuietLogger
    gui.TASKS_FILE = tasks_file          # 别动用户真正的列表文件

    root1 = root2 = None
    try:
        # ---------------- 第一次打开 ----------------
        print('== 第 1 次打开：加任务、下到一半、关掉软件 ==')
        root1 = tk.Tk()
        root1.withdraw()
        app1 = gui.App(root1)
        app1.cfg['save_dir'] = outdir
        app1.cfg['threads'] = 4
        app1.var_threads.set(4)
        app1.var_dir.set(outdir)

        app1._add_task(server.url)
        row = app1.rows[0]
        if not wait_until(lambda: row.dl.done_bytes >= 4 * MB, root1):
            print('  FAIL 没能下到 4MB（state=%s）' % row.dl.state)
            return 1
        row.cancel()
        if not wait_until(lambda: row.dl not in app1.active, root1):
            print('  FAIL 取消后没停下来')
            return 1
        got = bytes_in(outdir)
        app1.on_close()                  # 走真正的"关窗口"路径（内部会保存列表）
        root1 = None
        print('  [1] 已下 %s，列表已保存' % core.fmt_size(got))

        if not os.path.exists(tasks_file):
            print('  FAIL 关掉后没有生成列表文件 %s' % tasks_file)
            return 1
        recs = gui.load_task_records()
        if len(recs) != 1 or recs[0].get('url') != server.url:
            print('  FAIL 列表文件内容不对: %r' % recs)
            return 1

        # ---------------- 第二次打开 ----------------
        print('== 第 2 次打开：记录应该还在 ==')
        root2 = tk.Tk()
        root2.withdraw()
        app2 = gui.App(root2)
        if len(app2.rows) != 1:
            print('  FAIL 重开后列表是空的（恢复了 %d 条）' % len(app2.rows))
            return 1
        row2 = app2.rows[0]
        dl2 = row2.dl
        print('  [2] 恢复了 %d 条：state=%s 已下载=%s / %s'
              % (len(app2.rows), dl2.state,
                 core.fmt_size(dl2.done_bytes), core.fmt_size(dl2.total)))
        if dl2.state != 'interrupted':
            print('  FAIL 状态应是 interrupted（未完成），实际 %s' % dl2.state)
            return 1
        if dl2.done_bytes < got * 0.9:
            print('  FAIL 进度没算回来: %s vs %s'
                  % (core.fmt_size(dl2.done_bytes), core.fmt_size(got)))
            return 1
        root2.update()
        btn_state = str(row2.widgets['btn_resume'].cget('state'))
        if btn_state == 'disabled':
            print('  FAIL 「继续」按钮是灰的，用户点不了')
            return 1
        print('  [3] 「继续」按钮可用 ✓')

        # ---------------- 点继续 ----------------
        server.reset_served()
        row2.resume()
        if not wait_until(lambda: dl2.state in ('done', 'failed'), root2):
            print('  FAIL 续传没有结束')
            return 1
        served = server.served()
        out = os.path.join(outdir, FILENAME)
        print('  [4] 点「继续」后: state=%s 只补下 %s' % (dl2.state, core.fmt_size(served)))
        if dl2.state != 'done':
            print('  FAIL 状态不是 done: %s' % (dl2.error or ''))
            return 1
        if not os.path.exists(out) or os.path.getsize(out) != len(PAYLOAD):
            print('  FAIL 最终文件大小不对')
            return 1
        if hashlib.sha256(open(out, 'rb').read()).hexdigest() != PAYLOAD_SHA:
            print('  FAIL 内容哈希不匹配')
            return 1
        if served >= len(PAYLOAD):
            print('  FAIL 是从头重下的（不是续传）')
            return 1

        print('  -> PASS 重开后记录还在、进度算得回来、「继续」只补 %s，内容完整'
              % core.fmt_size(served))
        print('\n1/1 通过')
        return 0
    finally:
        core.Logger = real_logger
        gui.TASKS_FILE = real_tasks_file
        for r in (root1, root2):
            try:
                if r is not None:
                    r.quit()
                    r.destroy()
            except Exception:
                pass
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
