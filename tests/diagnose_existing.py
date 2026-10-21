# -*- coding: utf-8 -*-
"""
对已有断点分块做「只读体检」—— 不下载、不修改、不删除任何文件

回答三个问题：
  1. 服务器上那个文件多大？
  2. 磁盘上残留的分块属于哪套布局？能不能安全续传？
  3. 还差多少？

用法:
    python tests/diagnose_existing.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import emlyn_dl_core as core      # noqa: E402

REPO = 'Comfy-Org/MiniMax-Music-3'
SAVE_DIR = r'D:\EmlynDownload\_gui_out'
ENDPOINT = 'https://hf-mirror.com'
THREADS = 8

TARGETS = [
    'diffusion_models/minimax_music3_dit_fp16.safetensors',
    'text_encoders/minimax_music3_text_encoder_pruned_int8_convrot.safetensors',
]


def main():
    for fn in TARGETS:
        print('\n' + '=' * 72)
        print('文件: %s' % fn)
        dl = core.Download('%s/%s/resolve/main/%s' % (ENDPOINT, REPO, fn),
                           SAVE_DIR, THREADS)
        if not dl.resolve():
            print('  解析失败: %s' % (dl.error or '')[:200])
            continue

        probe = core.probe_direct(dl.url, None)
        if not probe.ok:
            print('  探测失败: %s' % (probe.error or '')[:200])
            continue

        dl.filename = os.path.basename(fn)
        dl.out_path = os.path.join(SAVE_DIR, dl.filename)
        dl.total = probe.size if probe.size > 0 else 0
        print('  服务器大小: %s   Range 支持: %s'
              % (core.fmt_size(dl.total), probe.supports_range))
        print('  输出路径: %s' % dl.out_path)

        if os.path.exists(dl.out_path):
            print('  ★ 最终文件已存在: %s'
                  % core.fmt_size(os.path.getsize(dl.out_path)))
            if dl.total > 0 and os.path.getsize(dl.out_path) == dl.total:
                print('    而且是完整的 —— 已经下好了')
            continue

        # ★ 只读保护：把"删除分块"这个动作挡住，体检绝不碰用户数据
        deleted = []

        def _no_delete(*_a, **_k):
            deleted.append(1)
            print('  （体检拦截了一次删除操作，未执行）')

        dl._discard_parts = _no_delete

        on_disk = dl._parts_on_disk()
        if not on_disk:
            print('  磁盘上没有残留分块 —— 会从头下载')
            continue
        have = sum(sz for _i, _p, sz in on_disk)
        print('  残留分块: %d 个，共 %s' % (len(on_disk), core.fmt_size(have)))
        for i, _p, sz in on_disk:
            print('     part%-2d  %s' % (i, core.fmt_size(sz)))

        saved = core.load_manifest(dl.out_path)
        print('  断点记录: %s' % ('有' if saved else '没有（老版本留下的分块）'))

        ok = dl._plan_parts(probe.supports_range)
        print('  -> 布局判定: %s' % ('可安全续传' if ok else '本次不做改动'))
        if dl.error:
            print('     说明: %s' % dl.error.replace('\n', '\n     '))
        print('  布局来源: %s   分块数: %d'
              % (dl._layout_source or '(无)', len(dl.part_specs)))

        if ok:
            done = 0
            for pp, s, e in dl.part_specs:
                need = e - s + 1
                sz = os.path.getsize(pp) if os.path.exists(pp) else 0
                done += min(sz, need)
            left = dl.total - done if dl.total > 0 else -1
            print('  已下载: %s / %s  (%.1f%%)'
                  % (core.fmt_size(done), core.fmt_size(dl.total),
                     100.0 * done / dl.total if dl.total else 0))
            print('  还需下载: %s' % (core.fmt_size(left) if left >= 0 else '未知'))
            print('  ★ 结论：点「继续」即可接着下，不会重下已完成的 %s'
                  % core.fmt_size(done))
        elif deleted:
            print('  ★ 注意：本该删除残留分块，被体检模式拦住了')
        else:
            print('  ★ 结论：本次没有改动任何文件，稍后重试即可')


if __name__ == '__main__':
    main()
