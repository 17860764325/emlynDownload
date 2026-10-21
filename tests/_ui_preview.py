# -*- coding: utf-8 -*-
"""界面预览：造几个假任务，把窗口显示出来，好截图看效果（不下载任何东西）

用法:
    python tests/_ui_preview.py [秒数]
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emlyn_dl_core as core      # noqa: E402

MB = 1024 * 1024
GB = 1024 * MB


class QuietLogger(core.Logger):
    def __init__(self, *a, **k):
        self.ui_queue = None

    def write(self, level, msg, detail=None):
        pass

    def path(self):
        return '(quiet)'


def fake(app, name, total, threads, state, done, speed, elapsed,
         note=None, parts=None):
    dl = core.Download('https://huggingface.co/Comfy-Org/MiniMax-Music-3/'
                       'blob/main/x/' + name, app.cfg['save_dir'], threads)
    dl.filename = name
    dl.out_path = os.path.join(app.cfg['save_dir'], name)
    dl.total = total
    dl.done_bytes = int(done)
    dl.state = state
    dl.mode = 'multi' if threads > 1 else 'single'
    dl.speed = speed
    dl.error = note
    dl._base_elapsed = elapsed
    dl.created_at = time.time() - elapsed
    if state == 'done':
        dl.finished_at = time.time() - 30
    if parts == 0:                    # 还没点「继续」的恢复任务：没有分块信息
        dl.disk_parts = threads
        return dl
    n = parts or threads
    specs = core.chunk_layout(total, n)
    dl.part_specs = [('%s.part%d' % (dl.out_path, i), s, e)
                     for i, (s, e) in enumerate(specs)]
    dl.parts_done = {}
    dl.part_speed = {}
    left = int(done)
    for i, (p, s, e) in enumerate(dl.part_specs):
        need = e - s + 1
        give = min(need, left)
        dl.parts_done[p] = give
        left -= give
        if state == 'downloading':
            # 让每个线程速度不一样，看着真实
            dl.part_speed[p] = speed / n * (0.55 + 0.22 * ((i * 7) % 5))
        else:
            dl.part_speed[p] = 0.0
    return dl


def main():
    import tkinter as tk
    import EmlynDownload as gui

    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    scenario = sys.argv[2] if len(sys.argv) > 2 else 'all'
    tmp = tempfile.mkdtemp(prefix='emlyn_preview_')
    real_logger, real_tasks = core.Logger, gui.TASKS_FILE
    core.Logger = QuietLogger
    gui.TASKS_FILE = os.path.join(tmp, 'tasks.json')

    root = tk.Tk()
    app = gui.App(root)
    app.cfg['save_dir'] = os.path.join(r'D:\EmlynDownload\_gui_out')

    specs = [
        ('minimax_music3_dit_fp16.safetensors', int(4.58 * GB), 8,
         'downloading', 4.41 * GB, 4.6 * MB, 493),
        ('minimax_music3_text_encoder_pruned_int8_convrot.safetensors',
         int(8.57 * GB), 8, 'downloading', 2.41 * GB, 3.1 * MB, 812),
        ('minimax_music3_dav.safetensors', 216696128, 1, 'done',
         216696128, 0.0, 96),
        ('minimax_music3_dit_fp32.safetensors', int(9.15 * GB), 8,
         'failed', 1.2 * GB, 0.0, 240,
         '有 2 个分块没有下载完整：\n   part3  已下 585.82 MB / 1.14 GB\n'
         '   已下载的 1.20 GB 都保留着 —— 点这一行的「继续」就能接着下。'),
    ]
    if scenario == 'restored':
        # 刚重开软件、还没点「继续」的样子：有进度但没有分块信息
        specs = [
            ('minimax_music3_dit_fp16.safetensors', int(4.58 * GB), 8,
             'interrupted', 4.41 * GB, 0.0, 493),
            ('minimax_music3_text_encoder_pruned_int8_convrot.safetensors',
             int(8.57 * GB), 8, 'interrupted', 1.04 * GB, 0.0, 812),
        ]

    for name, total, th, state, done, spd, el, *rest in specs:
        note = rest[0] if rest else None
        parts = 0 if scenario == 'restored' else th
        dl = fake(app, name, total, th, state, done, spd, el, note, parts)
        row = gui.TaskRow(app, dl)
        row.build(app.rows_holder, len(app.rows))
        app.rows.append(row)
    try:
        app.empty_holder.grid_remove()
    except Exception:
        pass
    app._refresh_rows()

    if scenario != 'collapsed':          # 第一行展开：看每个线程 + 详细信息
        app.rows[0].toggle_detail()
    app.var_status.set('已恢复 %d 条下载记录，其中 %d 条没下完 —— '
                       '点那一行的「继续」接着下（已下的不会重下）'
                       % (len(app.rows), len(app.rows)))
    app.lbl_log.configure(text='INFO: 校验通过：磁盘上 8 个残留分块属于 8 块布局')

    root.update_idletasks()
    w, h = 1180, 920
    x = max(0, (root.winfo_screenwidth() - w) // 2)
    y = max(0, (root.winfo_screenheight() - h) // 2)
    root.geometry('%dx%d+%d+%d' % (w, h, x, y))
    root.deiconify()
    root.lift()
    root.update()
    root.update_idletasks()
    try:
        # 必须在窗口尺寸定下来之后再回顶，否则会被 Tk 记成中间位置
        app.canvas.configure(scrollregion=app.canvas.bbox('all'))
        app.canvas.yview_moveto(0.0)
    except Exception:
        pass
    root.update()

    def stop():
        try:
            app._closing = True
            core.Logger = real_logger
            gui.TASKS_FILE = real_tasks
            root.destroy()
        except Exception:
            pass

    root.after(int(seconds * 1000), stop)
    try:
        root.mainloop()
    except Exception:
        pass
    print('预览结束')


if __name__ == '__main__':
    main()
