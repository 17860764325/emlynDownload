# -*- coding: utf-8 -*-
"""
反证：如果不做"服务器到底支不支持按偏移取数据"的检查，直接按多分块下，
      会得到什么？

这里**不调用下载器**，而是直接照着"无保护的朴素做法"发请求：
    对第 i 块发 Range: bytes=start-end
    把响应里的前 need 字节存成 part_i
    按顺序拼起来
然后对比大小和哈希。

用来证明 tests/test_resume.py 里 T10/T11/T12 这三个用例不是摆设。

用法:
    python tests/_negative_control.py
"""

import hashlib
import os
import socket
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from http.server import ThreadingHTTPServer            # noqa: E402
from test_resume import (                              # noqa: E402
    FILENAME, PAYLOAD, PAYLOAD_SHA, RangeHandler, free_port, MB,
)

N_PARTS = 4


def naive_download(url, n_parts=N_PARTS):
    """朴素做法：不看服务器是否真的支持 Range，直接切块"""
    total = len(PAYLOAD)
    chunk = (total + n_parts - 1) // n_parts
    parts = []
    for i in range(n_parts):
        start = i * chunk
        end = min(start + chunk - 1, total - 1)
        need = end - start + 1
        req = urllib.request.Request(url)
        req.add_header('Range', 'bytes=%d-%d' % (start, end))
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(need)          # ← 旧写法：拿到多少就存多少
        parts.append(data + b'\x00' * (need - len(data)))
    return b''.join(parts)


def run_case(label, **flags):
    for k, v in flags.items():
        setattr(RangeHandler, k, v)
    port = free_port()
    httpd = ThreadingHTTPServer(('127.0.0.1', port), RangeHandler)
    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:%d/%s' % (port, FILENAME)
    try:
        data = naive_download(url)
        size_ok = len(data) == len(PAYLOAD)
        hash_ok = hashlib.sha256(data).hexdigest() == PAYLOAD_SHA
        verdict = '正常' if hash_ok else '★ 静默损坏（大小对、内容错）'
        print('  %-28s 大小对=%-5s 内容对=%-5s  %s'
              % (label, size_ok, hash_ok, verdict))
        return size_ok, hash_ok
    finally:
        httpd.shutdown()
        for k in flags:
            setattr(RangeHandler, k, False)


def main():
    print('朴素多分块做法（无保护）在这几种服务器上的结果：\n')
    ok_normal = run_case('正常服务器')
    bad1 = run_case('不支持 Range', ignore_range=True)
    bad2 = run_case('只在 start=0 回 206', range_only_at_zero=True)
    bad3 = run_case('206 但内容永远从头取', shift_range_to_zero=True)

    print()
    if ok_normal[1]:
        print('对照组成立：正常服务器上朴素做法是对的（说明这个模拟是可信的）')
    else:
        print('★ 对照组就不对，模拟有问题，需要重新检查')
    corrupt = [lab for lab, (s, h) in
               [('不支持 Range', bad1), ('只在 start=0 回 206', bad2),
                ('206 但偏移被无视', bad3)] if s and not h]
    if corrupt:
        print('证实：%s 这几种服务器会让朴素做法产出"大小对、内容错"的坏文件'
              % '、'.join(corrupt))
        print('=> 所以下载器必须先实测 Range 是否真的可用（T10/T11/T12 就是守这个）')
    else:
        print('没有复现损坏，需重新检查这几个测试的意义')


if __name__ == '__main__':
    main()
