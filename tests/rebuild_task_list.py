# -*- coding: utf-8 -*-
"""
按 _gui_out 里实际存在的文件，重建下载列表（emlyndownload_tasks.json）

背景：旧版本把下载列表只放在内存里，关掉软件就没了。升级后列表虽然会
自动保存，但"以前那一次"的记录找不回来 —— 所以按磁盘上的残留文件把
列表重建一次，用户重开软件就能直接看到并点「继续」。

★ 只读探测 + 只写列表文件，不下载、不删除任何分块。

用法:
    python tests/rebuild_task_list.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emlyn_dl_core as core      # noqa: E402

REPO = 'Comfy-Org/MiniMax-Music-3'
SAVE_DIR = r'D:\EmlynDownload\_gui_out'
ENDPOINT = 'https://hf-mirror.com'
THREADS = 8
HF = 'https://huggingface.co/%s/blob/main/%s'

# (仓库内路径, 备注)  —— 都是 _gui_out 里确实有文件的
TARGETS = [
    'vae/minimax_music3_dav.safetensors',
    'diffusion_models/minimax_music3_dit_fp16.safetensors',
    'text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors',
]


def main():
    # 延迟导入，避免把 GUI 依赖带进来
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import EmlynDownload as gui

    recs = []
    for rel in TARGETS:
        name = os.path.basename(rel)
        url = HF % (REPO, rel)
        dl = core.Download(url, SAVE_DIR, THREADS, endpoint=ENDPOINT)
        if not dl.resolve():
            print('  跳过 %s: %s' % (name, (dl.error or '')[:80]))
            continue
        probe = core.probe_direct(dl.url, None)
        if not probe.ok:
            print('  跳过 %s: 探测失败 %s' % (name, (probe.error or '')[:80]))
            continue

        rec = {
            'url': url,
            'save_dir': SAVE_DIR,
            'threads': THREADS,
            'endpoint': ENDPOINT,
            'filename': name,
            'out_path': os.path.join(SAVE_DIR, name),
            'total': probe.size,
            'done_bytes': 0,
            'state': 'interrupted',
            'error': None,
            'mode': 'multi',
        }

        # 用真实代码走一遍"重开软件"的流程，确认这条记录是好的
        dl2 = core.Download.from_snapshot(rec, logger=None)
        dl2.refresh_progress_from_disk()
        rec['state'] = dl2.state
        rec['done_bytes'] = dl2.done_bytes
        rec['mode'] = dl2.mode
        recs.append(rec)

        pct = (100.0 * dl2.done_bytes / probe.size) if probe.size else 0
        print('  %-58s %s / %s (%.1f%%)  %s'
              % (name[:58], core.fmt_size(dl2.done_bytes),
                 core.fmt_size(probe.size), pct, dl2.state))

    if not recs:
        print('没有可重建的记录')
        return 1

    gui.save_task_records(recs)
    print('\n已写入 %s（%d 条）' % (gui.TASKS_FILE, len(recs)))
    print('关掉下载器、重新打开，列表就会带上这些记录，点「继续」即可接着下。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
