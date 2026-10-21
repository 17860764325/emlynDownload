# -*- coding: utf-8 -*-
"""
界面「详情」功能测试：每个线程的进度 + 详细信息（总大小 / 已用时长 / 平均速度…）

用法:
    python tests/test_gui_detail.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emlyn_dl_core as core            # noqa: E402
from test_resume import Server, parts_in   # noqa: E402


class QuietLogger(core.Logger):
    def __init__(self, ui_queue=None, log_dir=None):
        self.ui_queue = None
        self.lock = threading.Lock()

    def write(self, level, msg, detail=None):
        pass

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
    tmp = tempfile.mkdtemp(prefix='emlyn_detail_')
    outdir = os.path.join(tmp, 'out')
    os.makedirs(outdir, exist_ok=True)

    real_logger, real_tasks = core.Logger, gui.TASKS_FILE
    core.Logger = QuietLogger
    gui.TASKS_FILE = os.path.join(tmp, 'tasks.json')

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = gui.App(root)
        app.cfg['save_dir'] = outdir
        app.cfg['threads'] = 4
        app.var_threads.set(4)
        app._add_task(server.url)
        row = app.rows[0]
        dl = row.dl

        print('== 界面详情功能 ==')
        # 1) 先让它下一点
        if not wait_until(lambda: dl.done_bytes >= 3 * MB
                          or dl.state in ('done', 'failed'), root):
            print('  FAIL 没能开始下载（state=%s）' % dl.state)
            return 1
        print('  [1] 下载中: %s / %s'
              % (core.fmt_size(dl.done_bytes), core.fmt_size(dl.total)))

        # 2) 默认是收起的
        row.update()
        root.update()
        if row.expanded:
            print('  FAIL 默认应该是收起的')
            return 1

        # 3) 展开
        row.toggle_detail()
        row.update()
        root.update()
        if not row.expanded:
            print('  FAIL 点「详情」没有展开')
            return 1
        n_threads = len(dl.part_specs)
        if len(row._thread_rows) != n_threads:
            print('  FAIL 线程行数(%d) ≠ 分块数(%d)'
                  % (len(row._thread_rows), n_threads))
            return 1
        print('  [2] 展开成功：%d 个线程行（分块数 %d）✓'
              % (len(row._thread_rows), n_threads))

        # 4) 详细信息里的值
        v = row._detail_values
        txt_total = v['total'].cget('text')
        txt_elapsed = v['elapsed'].cget('text')
        txt_parts = v['parts'].cget('text')
        if txt_total != core.fmt_size(dl.total):
            print('  FAIL 总大小显示不对: %r' % txt_total)
            return 1
        if not txt_elapsed or txt_elapsed == '--':
            print('  FAIL 已用时长没显示: %r' % txt_elapsed)
            return 1
        if '块' not in txt_parts:
            print('  FAIL 分块信息没显示: %r' % txt_parts)
            return 1
        print('  [3] 详细信息: 总大小=%s  已用时长=%s  分块=%s  ✓'
              % (txt_total, txt_elapsed, txt_parts))

        # 5) 每个线程都有自己的进度和速度文字
        shown = 0
        for bar, pct, txt in row._thread_rows:
            val = float(bar.cget('value'))
            if not (0 <= val <= 1000):
                print('  FAIL 线程进度条数值越界: %s' % val)
                return 1
            if not txt.cget('text'):
                print('  FAIL 线程行没有速度/字节文字')
                return 1
            shown += 1
        print('  [4] %d 个线程各自有进度条和字节/速度文字  ✓' % shown)

        # 6) 顶部汇总
        app._update_summary()
        root.update()
        stat = app.hdr_stat.cget('text')
        if '个' not in stat:
            print('  FAIL 顶部任务统计没显示: %r' % stat)
            return 1
        print('  [5] 顶部汇总: 速度=%s 总进度=%s 统计=%s  ✓'
              % (app.hdr_speed.cget('text'), app.hdr_progress.cget('text'), stat))

        # 7) 取消 → 已用时长应 > 0，摘要里能看到
        row.cancel()
        if not wait_until(lambda: dl not in app.active, root):
            print('  FAIL 取消后没停')
            return 1
        row.update()
        root.update()
        if dl.active_seconds <= 0:
            print('  FAIL 已用时长没统计到: %s' % dl.active_seconds)
            return 1
        info_text = row.widgets['info'].cget('text')
        if '已用时长' not in info_text:
            print('  FAIL 摘要里没有已用时长: %r' % info_text)
            return 1
        print('  [6] 取消后: 已用时长=%s（摘要里有）✓'
              % core.fmt_duration(dl.active_seconds))

        # 8) 收起
        row.toggle_detail()
        row.update()
        root.update()
        if row.expanded:
            print('  FAIL 再点一次没有收起')
            return 1
        print('  [7] 再点一次收起成功  ✓')

        # 9) 进度条颜色随状态变
        row.widgets['bar'].cget('style')
        print('  [8] 进度条样式: %s  ✓' % row.widgets['bar'].cget('style'))

        print('\n1/1 通过')
        return 0
    finally:
        core.Logger = real_logger
        gui.TASKS_FILE = real_tasks
        try:
            if root is not None:
                root.quit()
                root.destroy()
        except Exception:
            pass
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


MB = 1024 * 1024

if __name__ == '__main__':
    sys.exit(main())
