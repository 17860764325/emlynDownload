# -*- coding: utf-8 -*-
"""
hfget.py —— HuggingFace 大文件多线程下载器（国内网络优化版）

【为什么需要它】
  实测本机网络：直连 huggingface.co ≈ 0.01 MB/s（基本不通），
  hf-mirror.com 单连接 ≈ 3.85 MB/s，而多线程分块能稳定跑满带宽。
  官方的 hf_xet 在 hf-mirror 上会退化（镜像不完整支持 Xet 的 CAS 协议），
  实测只有 0.5 MB/s，反而更慢。

【特性】
  · 多线程分块下载（Range 请求），线程数可调
  · 断点续传：中断后重跑会接着下，不重新开始
  · 自动走 hf-mirror（可用 --endpoint 覆盖）
  · 只依赖标准库，不需要 huggingface_hub
  · 自动处理 HTTP 重定向（HF 会 302 到 CDN）

【用法】
  ★ 最省事：直接贴完整链接
  python hfget.py https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors -o D:\\models\\gemma

  ★ 或者用 仓库名 + 文件名（推荐，更清晰）
  python hfget.py google/embeddinggemma-2 model.safetensors -o D:\\models\\gemma

  # 下载整个仓库的所有文件
  python hfget.py google/embeddinggemma-2 --all -o D:\\models\\gemma

  # 调整线程数（默认 8，本机带宽上限约 3.7MB/s，8 线程已够）
  python hfget.py <repo> <file> -t 8 -o D:\\models

  # 查看仓库文件列表和大小，不下载
  python hfget.py google/embeddinggemma-2 --list

  # 需要授权的模型（Llama/Gemma 等 gated）
  python hfget.py meta-llama/Llama-3.3-70B-Instruct <file> --token hf_xxxxx -o D:\\models

【链接的几种写法都支持】
  https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors
  https://hf-mirror.com/google/embeddinggemma-2/resolve/main/model.safetensors
  https://huggingface.co/google/embeddinggemma-2/blob/main/model.safetensors
  https://huggingface.co/google/embeddinggemma-2/blob/main/model.safetensors?download=true
  google/embeddinggemma-2/resolve/main/model.safetensors        （不带域名也行）
  链接里带子目录也能识别，例如：
  https://huggingface.co/Qwen/Qwen2.5-7B-Instruct/resolve/main/subdir/file.bin

【环境变量】
  HF_ENDPOINT         默认 https://hf-mirror.com
  HF_TOKEN            访问 gated 模型时用
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass

DEFAULT_ENDPOINT = os.environ.get('HF_ENDPOINT', 'https://hf-mirror.com')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) hfget/1.0'

# 全局统计（多线程共享，用锁保护）
_lock = threading.Lock()
_done_bytes = [0]
# 每个分块自己已经计入的字节数（用于把进度限制在 100% 以内）
_done_bytes_per_part = {}


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024:
            return '%.2f %s' % (n, unit)
        n /= 1024
    return '%.2f PB' % n


def api_url(endpoint, repo, revision='main'):
    return '%s/api/models/%s/revision/%s' % (endpoint.rstrip('/'), repo, revision)


def resolve_url(endpoint, repo, filename, revision='main'):
    return '%s/%s/resolve/%s/%s' % (endpoint.rstrip('/'), repo, revision, filename)


def make_request(url, headers=None, method='GET'):
    req = urllib.request.Request(url, method=method)
    req.add_header('User-Agent', UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def get_repo_files(endpoint, repo, token=None, revision='main'):
    """取仓库文件列表（含大小）。返回 [(filename, size), ...]"""
    url = api_url(endpoint, repo, revision)
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with urllib.request.urlopen(make_request(url, headers), timeout=30) as r:
        info = json.loads(r.read().decode('utf-8'))
    files = []
    for s in info.get('siblings', []):
        fn = s.get('rfilename')
        if fn:
            files.append(fn)
    # 补上大小（siblings 里通常没有 size，需要逐个 HEAD，量大时较慢）
    out = []
    for fn in files:
        try:
            sz = head_size(resolve_url(endpoint, repo, fn, revision), token)
        except Exception:
            sz = -1
        out.append((fn, sz))
    return out


def head_size(url, token=None):
    """通过 HEAD 拿文件大小（自动跟随重定向）"""
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = make_request(url, headers, method='HEAD')
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get('Content-Length', -1))


def parse_hf_url(text):
    """从 HuggingFace 链接里解析出 (repo, filename, revision)

    ★ 支持多种写法，让你可以直接把浏览器地址栏的链接粘进来：

        https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors
        https://hf-mirror.com/google/embeddinggemma-2/blob/main/model.safetensors
        https://huggingface.co/google/embeddinggemma-2/blob/main/subdir/x.bin?download=true
        google/embeddinggemma-2/resolve/main/model.safetensors     （不带域名）
        google/embeddinggemma-2/model.safetensors                  （不带 resolve）

    解析不出来时返回 (None, None, None)。
    """
    s = (text or '').strip().strip('"').strip("'")
    if not s:
        return None, None, None

    # 去掉协议和域名（hf.co 是 huggingface.co 的短域名）
    for pre in ('https://', 'http://'):
        if s.startswith(pre):
            s = s[len(pre):]
    for host in ('huggingface.co/', 'hf-mirror.com/', 'hf.co/',
                 'www.huggingface.co/', 'www.hf-mirror.com/'):
        if s.startswith(host):
            s = s[len(host):]
            break

    # 去掉查询串和锚点（?download=true / #xxx）
    s = s.split('?')[0].split('#')[0]

    # 用 resolve 或 blob 切分：前面是 repo，后面是 revision/filename
    for sep in ('/resolve/', '/blob/', '/raw/'):
        if sep in s:
            repo, rest = s.split(sep, 1)
            parts = [p for p in rest.split('/') if p]
            if len(parts) < 2:
                return None, None, None
            revision = parts[0]
            filename = '/'.join(parts[1:])      # 支持子目录
            return repo.strip('/'), filename, revision

    # 没有 resolve/blob：按 repo 是前两段来猜
    parts = [p for p in s.split('/') if p]
    if len(parts) >= 3:
        repo = '%s/%s' % (parts[0], parts[1])
        filename = '/'.join(parts[2:])
        return repo, filename, 'main'
    return None, None, None


def download_chunk(url, start, end, part_path, token, retries=6):
    """下载 [start, end] 区间的字节，写入 part_path（支持已存在时续传）

    ★ 进度统计要点：只把【本次真正写入的字节】计入 _done_bytes，
      否则断点续传时"已存在的部分"会被重复计数，进度会超过 100%。
    """
    total = end - start + 1
    for attempt in range(retries):
        already = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        if already >= total:
            return True
        try:
            headers = {'Range': 'bytes=%d-%d' % (start + already, end)}
            if token:
                headers['Authorization'] = 'Bearer ' + token
            with urllib.request.urlopen(make_request(url, headers), timeout=60) as r:
                mode = 'ab' if already else 'wb'
                with open(part_path, mode) as f:
                    while True:
                        buf = r.read(1024 * 128)     # 128KB 一块
                        if not buf:
                            break
                        f.write(buf)
                        # ★ 统计"实际写进去的字节数"，而不是"收到的字节数"。
                        #   有些 CDN/代理会忽略 Range、返回整个文件，
                        #   若按收到的字节统计，进度会超过 100%。
                        #   同时按分块上限截断，保证进度永远不超过 100%。
                        with _lock:
                            room = total - (_done_bytes_per_part.get(part_path, 0))
                            add = min(len(buf), max(room, 0))
                            _done_bytes_per_part[part_path] = \
                                _done_bytes_per_part.get(part_path, 0) + add
                            _done_bytes[0] += add
            if os.path.getsize(part_path) >= total:
                return True
        except Exception as e:
            if attempt == retries - 1:
                print('\n    [失败] 分块 %d-%d: %s' % (start, end, e))
                return False
            time.sleep(2 * (attempt + 1))       # 退避重试
    return False


def download_file(endpoint, repo, filename, out_dir, threads=8, token=None,
                  force=False, revision='main'):
    """多线程下载单个文件，支持断点续传"""
    url = resolve_url(endpoint, repo, filename, revision)
    out_path = os.path.join(out_dir, os.path.basename(filename))
    os.makedirs(out_dir, exist_ok=True)

    total = head_size(url, token)
    if total <= 0:
        print('  [错误] 拿不到文件大小，检查仓库/文件名/网络: %s' % url)
        return False

    name = os.path.basename(filename)

    # 已完成？
    if os.path.exists(out_path) and os.path.getsize(out_path) == total and not force:
        print('  [跳过] %s 已存在且完整（%s）' % (name, human(total)))
        return True

    # 小文件（<8MB）直接单线程，不值得分块
    if total < 8 * 1024 * 1024:
        print('  [单线程] %s (%s)' % (name, human(total)))
        tmp = out_path + '.part'
        with urllib.request.urlopen(make_request(url)) as r, open(tmp, 'wb') as f:
            while True:
                buf = r.read(1024 * 256)
                if not buf:
                    break
                f.write(buf)
        os.replace(tmp, out_path)
        print('  [完成] %s' % name)
        return True

    print('  [多线程 x%d] %s  (%s)' % (threads, name, human(total)))
    chunk = (total + threads - 1) // threads
    part_files = []
    jobs = []

    # ★ 断点续传：先把已存在的 .partN 的字节数计入进度，这样进度显示才真实
    _done_bytes_per_part.clear()
    _done_bytes[0] = 0
    for i in range(threads):
        start = i * chunk
        if start >= total:
            break
        pp = '%s.part%d' % (out_path, i)
        prev = os.path.getsize(pp) if os.path.exists(pp) else 0
        _done_bytes_per_part[pp] = prev
        _done_bytes[0] += prev

    for i in range(threads):
        start = i * chunk
        end = min(start + chunk - 1, total - 1)
        if start >= total:
            break
        pp = '%s.part%d' % (out_path, i)
        part_files.append((pp, start, end))
        t = threading.Thread(target=download_chunk,
                             args=(url, start, end, pp, token), daemon=True)
        t.start()
        jobs.append(t)

    # 进度显示
    t0 = time.time()
    while any(t.is_alive() for t in jobs):
        time.sleep(1)
        with _lock:
            d = _done_bytes[0]
        el = max(time.time() - t0, 0.001)
        pct = 100.0 * d / total
        spd = d / el / 1048576
        eta = (total - d) / max(d / el, 1)
        sys.stdout.write('\r    %5.1f%%  %s/%s  %.2f MB/s  剩余 %s   ' %
                         (pct, human(d), human(total), spd, human(eta)))
        sys.stdout.flush()

    for t in jobs:
        t.join()

    # 合并
    ok = all(os.path.exists(pp) and os.path.getsize(pp) == (e - s + 1)
             for pp, s, e in part_files)
    if not ok:
        print('\n  [未完成] 有分块没下完，已保留 .partN 文件，重跑本命令可续传')
        return False

    print('\n    合并分块...')
    with open(out_path, 'wb') as out:
        for pp, s, e in part_files:
            with open(pp, 'rb') as f:
                while True:
                    buf = f.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
            os.remove(pp)

    final = os.path.getsize(out_path)
    el = time.time() - t0
    print('  [完成] %s  %s  (%.2f MB/s)' % (name, human(final), final / el / 1048576))
    return final == total


def main():
    ap = argparse.ArgumentParser(
        description='HuggingFace 多线程下载器（国内网络优化）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('repo', help='仓库名（如 google/embeddinggemma-2），或直接贴完整链接')
    ap.add_argument('filename', nargs='?', help='要下载的文件名（贴链接或用 --all 时可省略）')
    ap.add_argument('-o', '--out', default='.', help='输出目录')
    ap.add_argument('-t', '--threads', type=int, default=8, help='线程数（默认 8）')
    ap.add_argument('--endpoint', default=DEFAULT_ENDPOINT, help='端点，默认 $HF_ENDPOINT 或 hf-mirror')
    ap.add_argument('--revision', default=None, help='分支/tag/commit，默认 main')
    ap.add_argument('--token', default=os.environ.get('HF_TOKEN'), help='HF token（gated 模型需要）')
    ap.add_argument('--all', action='store_true', help='下载仓库全部文件')
    ap.add_argument('--list', action='store_true', help='只列出文件，不下载')
    ap.add_argument('--force', action='store_true', help='强制重新下载')
    args = ap.parse_args()

    # ★ 如果第一个参数是链接，自动拆成 仓库 + 文件名 + revision
    repo, filename, revision = args.repo, args.filename, args.revision
    if '://' in args.repo or args.repo.startswith(('huggingface.co/', 'hf-mirror.com/',
                                                   'hf.co/', 'www.')):
        r, f, rev = parse_hf_url(args.repo)
        if r is None:
            print('[错误] 无法从链接中解析出仓库和文件名：')
            print('       %s' % args.repo)
            print('       支持的格式示例：')
            print('       https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors')
            return 1
        repo = r
        if filename is None:
            filename = f
        if revision is None:
            revision = rev
        print('  [已从链接解析] 仓库 = %s' % repo)
        print('                 文件 = %s' % filename)
        print('                 版本 = %s' % revision)

    revision = revision or 'main'

    print('=' * 70)
    print('hfget —— HuggingFace 多线程下载器')
    print('=' * 70)
    print('  仓库   : %s' % repo)
    if filename and not args.all:
        print('  文件   : %s' % filename)
    print('  版本   : %s' % revision)
    print('  端点   : %s' % args.endpoint)
    print('  线程数 : %d' % args.threads)
    print('  输出   : %s' % os.path.abspath(args.out))
    print()

    # 后续全部使用解析后的 repo / filename / revision
    args.repo = repo
    args.filename = filename
    args.revision = revision

    if args.list or args.all:
        print('正在获取文件列表...')
        try:
            files = get_repo_files(args.endpoint, args.repo, args.token, args.revision)
        except Exception as e:
            print('  [错误] 获取文件列表失败: %s' % e)
            return 1
        print('  %-58s %12s' % ('文件', '大小'))
        print('  ' + '-' * 72)
        total = 0
        for fn, sz in files:
            s = human(sz) if sz > 0 else '?'
            print('  %-58s %12s' % (fn[:58], s))
            if sz > 0:
                total += sz
        print('  ' + '-' * 72)
        print('  共 %d 个文件，合计 %s' % (len(files), human(total)))
        if args.list:
            return 0
        print()

    if args.all:
        ok_all = True
        for i, (fn, sz) in enumerate(files, 1):
            print('[%d/%d] %s' % (i, len(files), fn))
            if not download_file(args.endpoint, args.repo, fn, args.out,
                                 args.threads, args.token, args.force,
                                 args.revision):
                ok_all = False
            print()
        return 0 if ok_all else 1

    if not args.filename:
        print('[错误] 请指定文件名，或用 --all 下载全部，或用 --list 查看文件列表')
        return 1

    ok = download_file(args.endpoint, args.repo, args.filename, args.out,
                       args.threads, args.token, args.force, args.revision)
    print()
    print('结果: %s' % ('成功 ✅' if ok else '失败 ❌（重跑本命令可断点续传）'))
    return 0 if ok else 1


if __name__ == '__main__':
    # =================================================================
    # ★★★ 最省事的用法：把链接粘在下面，然后直接运行本文件 ★★★
    #
    #   1) 把 MODEL_URL 改成你要下载的链接（浏览器地址栏直接复制就行）
    #   2) 把 SAVE_DIR 改成你要保存的目录
    #   3) 运行时【不带任何命令行参数】，比如：
    #          python hfget.py
    #      直接双击运行、或在 PyCharm 里点运行也可以
    #
    #   如果 MODEL_URL 留空（''），就会走命令行参数模式，即：
    #          python hfget.py <repo> <file> -o <目录>
    # =================================================================

    MODEL_URL = ''                              # ← 把链接粘到这里
    SAVE_DIR = r'D:\models'                     # ← 保存目录
    THREADS = 8                                 # ← 线程数（本机 8 够用）

    # 示例（取消注释即可用）：
    # MODEL_URL = 'https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors'
    # MODEL_URL = 'https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-IQ3_S.gguf'

    if MODEL_URL.strip():
        repo, fn, rev = parse_hf_url(MODEL_URL)
        if repo is None:
            print('[错误] 无法从 MODEL_URL 解析出仓库和文件名：')
            print('       %s' % MODEL_URL)
            print('       正确格式示例：')
            print('       https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors')
            sys.exit(1)
        print('=' * 70)
        print('hfget —— 从链接下载（脚本内配置模式）')
        print('=' * 70)
        print('  链接   : %s' % MODEL_URL)
        print('  仓库   : %s' % repo)
        print('  文件   : %s' % fn)
        print('  版本   : %s' % (rev or 'main'))
        print('  线程数 : %d' % THREADS)
        print('  保存到 : %s' % SAVE_DIR)
        print()
        ok = download_file(DEFAULT_ENDPOINT, repo, fn, SAVE_DIR,
                           THREADS, os.environ.get('HF_TOKEN'), False, rev or 'main')
        print()
        print('结果: %s' % ('成功 ✅' if ok else '失败 ❌（重跑本文件可断点续传）'))
        sys.exit(0 if ok else 1)
    else:
        # 没填链接 → 走命令行参数
        sys.exit(main())
