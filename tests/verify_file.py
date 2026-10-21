# -*- coding: utf-8 -*-
"""
校验已下载的文件是否和 HuggingFace 上的完全一致

下载器平时只做"大小 + 抽样"检查；这个工具做**完整 SHA-256 校验**
（拿 HuggingFace 仓库里公布的哈希来比对），是最彻底的确认方式。

用法:
    python tests/verify_file.py                        # 检查下面 LIST 里的文件
    python tests/verify_file.py <本地文件> <仓库> <仓库内路径> [分支]
"""

import hashlib
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENDPOINT = 'https://hf-mirror.com'
SAVE_DIR = r'D:\EmlynDownload\_gui_out'
REPO = 'Comfy-Org/MiniMax-Music-3'

LIST = [
    ('minimax_music3_dav.safetensors',
     'vae/minimax_music3_dav.safetensors'),
    ('minimax_music3_dit_fp16.safetensors',
     'diffusion_models/minimax_music3_dit_fp16.safetensors'),
]

CHUNK = 8 * 1024 * 1024


def declared_sha256(repo, path, revision='main'):
    """从 HuggingFace 拿"这个文件应该是什么哈希、多大"

    LFS 文件的 oid 就是它的 sha256；返回 (sha256, size)，拿不到就 (None, None)。
    """
    parent = os.path.dirname(path)
    url = '%s/api/models/%s/tree/%s/%s' % (ENDPOINT.rstrip('/'), repo,
                                           revision, parent)
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'EmlynDownload-verify/1.0')
    with urllib.request.urlopen(req, timeout=30) as r:
        entries = json.loads(r.read().decode('utf-8'))
    for e in entries:
        if e.get('path') == path:
            lfs = e.get('lfs') or {}
            return lfs.get('oid'), e.get('size')
    return None, None


def local_sha256(path):
    h = hashlib.sha256()
    total = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            total += len(b)
            h.update(b)
    return h.hexdigest(), total


def safetensors_header_ok(path):
    """额外做一次格式体检：safetensors 开头是 8 字节小端头长度 + JSON"""
    try:
        import struct
        with open(path, 'rb') as f:
            n = struct.unpack('<Q', f.read(8))[0]
            if n <= 0 or n > 100 * 1024 * 1024:
                return False, '头部长度不合理: %d' % n
            meta = json.loads(f.read(n).decode('utf-8'))
        return True, '头部正常（%d 个张量）' % max(0, len(meta) - 1)
    except Exception as e:
        return False, '头部解析失败: %s' % e


def verify(local_path, repo, repo_path, revision='main'):
    name = os.path.basename(local_path)
    print('\n' + '=' * 72)
    print('文件: %s' % name)
    if not os.path.exists(local_path):
        print('  ✗ 本地没有这个文件')
        return None

    size = os.path.getsize(local_path)
    print('  本地大小: %d 字节 (%.2f GB)' % (size, size / 2**30))

    try:
        want, want_size = declared_sha256(repo, repo_path, revision)
    except Exception as e:
        print('  拿不到官方哈希（网络问题）: %s' % e)
        want = want_size = None

    if want:
        print('  官方大小: %d 字节' % want_size)
        print('  官方哈希: %s' % want)
    else:
        print('  官方没公布哈希，只能做格式体检')

    print('  正在计算本地完整 SHA-256（%s，需要一会儿）...' % name)
    got, got_size = local_sha256(local_path)
    print('  本地哈希: %s' % got)

    ok_fmt, msg = safetensors_header_ok(local_path)
    print('  格式体检: %s' % msg)

    if not want:
        return None
    if got == want:
        print('  ✓✓ 与官方完全一致（逐字节相同），文件是好的')
        return True
    print('  ✗✗ 与官方不一致！这个文件是坏的')
    return False


def main():
    if len(sys.argv) >= 4:
        sys.exit(0 if verify(sys.argv[1], sys.argv[2], sys.argv[3],
                             sys.argv[4] if len(sys.argv) > 4 else 'main')
                 else 1)

    results = []
    for local_name, repo_path in LIST:
        r = verify(os.path.join(SAVE_DIR, local_name), REPO, repo_path)
        results.append((local_name, r))

    print('\n' + '=' * 72)
    for name, r in results:
        print('%-58s %s' % (name[:58],
                            {True: '一致', False: '不一致', None: '未判定'}[r]))
    return 0 if all(r is not False for _n, r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
