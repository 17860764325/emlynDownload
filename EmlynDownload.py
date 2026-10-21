# -*- coding: utf-8 -*-
"""
================================================================================
  EmlynDownload —— 通用下载器（图形界面）
================================================================================

【它能下载什么】
  · HuggingFace 模型（自动走国内镜像加速）
  · 任意 http/https 文件直链（GitHub Releases、官网文件、对象存储……）

【怎么用（3 步）】
  1. 把下载链接粘到最上面的输入框
  2. （可选）改保存目录 —— 会永久记住
  3. 点「开始下载」

【核心特性】
  · 贴链接即可，自动判断是 HF 链接还是普通直链
  · 多线程分块下载，国内镜像实测最优
  · 断点续传：中断、关机、崩溃后重开，点一下「继续」
  · **下载列表会存盘**：关掉软件重开，记录和进度都还在
  · 暂停 / 继续 / 取消，下载中也能操作
  · 实时速度、进度、剩余时间
  · 服务器不支持多线程时，自动降级为单线程，不会失败
  · 网页链接（网盘页等）会明确告诉你为什么下不了、怎么拿真实直链
  · 全部日志落盘到 logs/，出问题能查原因
  · 记住所有设置

【依赖】只用 Python 标准库 + tkinter，不需要 pip 装任何东西
================================================================================
"""

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import emlyn_dl_core as core         # noqa: E402

APP_NAME = 'EmlynDownload'
APP_VERSION = '2.0'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 下载列表存这里 —— 关掉软件再打开，记录还在
TASKS_FILE = os.path.join(BASE_DIR, 'emlyndownload_tasks.json')


def load_task_records():
    """读取上次的下载列表（读不到就当空的）"""
    try:
        with open(TASKS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, list):
            return d
    except Exception:
        pass
    return []


def save_task_records(recs):
    """原子地保存下载列表"""
    try:
        tmp = TASKS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(recs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TASKS_FILE)
    except Exception:
        pass

# 界面提示文本
HINT_HF = '✓ 识别为 HuggingFace 链接'
HINT_DIRECT = '✓ 识别为文件直链'
HINT_WEB = '⚠ 这是网页链接，不是文件直链 —— 点「开始下载」会告诉你如何拿到真实链接'
HINT_BAD = '✗ 认不出这个链接'


# =============================================================================
#  外观：配色 / 字体 / 小控件
# =============================================================================

class T:
    """统一的配色（想换风格只改这里）"""
    BG         = '#eef1f6'      # 窗口底色
    CARD       = '#ffffff'      # 卡片
    BORDER     = '#dce1ea'      # 卡片描边
    DETAIL_BG  = '#f7f9fc'      # 展开区底色
    HEADER_BG  = '#151c2c'      # 顶部标题栏
    HEADER_FG  = '#ffffff'
    HEADER_DIM = '#8f9bb3'
    TEXT       = '#1b2231'
    MUTED      = '#7c869b'
    FAINT      = '#aab3c5'
    ACCENT     = '#2f6fed'      # 主色
    ACCENT_DK  = '#2456c4'
    OK         = '#0f9d58'
    WARN       = '#d98324'
    ERR        = '#dc3b40'
    IDLE       = '#94a0b5'
    TRACK      = '#e8ecf3'      # 进度条底槽
    BTN        = '#f1f3f8'
    BTN_HOVER  = '#e3e7f0'


# 状态 → (中文名, 颜色)
STATUS_STYLE = {
    'waiting':     ('等待中', T.IDLE),
    'resolving':   ('解析中', T.ACCENT),
    'downloading': ('下载中', T.ACCENT),
    'merging':     ('合并中', T.ACCENT),
    'done':        ('已完成', T.OK),
    'failed':      ('失败', T.ERR),
    'cancelled':   ('已取消', T.WARN),
    'interrupted': ('未完成', T.WARN),
}

# 按钮配色：(底色, 字色, 按下/悬停底色)
BTN_KINDS = {
    'primary': (T.ACCENT, '#ffffff', T.ACCENT_DK),
    'ghost':   (T.BTN, T.TEXT, T.BTN_HOVER),
    'danger':  ('#fdecec', T.ERR, '#f8d9da'),
    'ok':      ('#e8f7ee', T.OK, '#d5f0df'),
}


def pick_font(root, candidates, default='TkDefaultFont'):
    """挑一个系统里真的存在的字体（免得中文变方块）"""
    try:
        from tkinter import font as tkfont
        fams = set(tkfont.families(root))
        for c in candidates:
            if c in fams:
                return c
    except Exception:
        pass
    return default


def make_btn(parent, text, command, kind='ghost', font=None, width=None,
             padx=11, pady=4):
    """扁平化的小按钮（tk.Button 比 ttk 好控制颜色）"""
    bg, fg, active = BTN_KINDS.get(kind, BTN_KINDS['ghost'])
    b = tk.Button(parent, text=text, command=command,
                  bg=bg, fg=fg, activebackground=active, activeforeground=fg,
                  relief='flat', bd=0, highlightthickness=0,
                  padx=padx, pady=pady, cursor='hand2',
                  disabledforeground=T.FAINT,
                  font=font or ('TkDefaultFont', 9))
    if width:
        b.configure(width=width)
    return b


def set_btn(btn, enabled, kind='ghost'):
    """启用/禁用按钮，并且把底色也换掉

    （tk.Button 禁用时不会自己变灰，不换底色会出现"灰字绿底"这种
      看着像能点、其实点不了的按钮）
    """
    bg, fg, active = BTN_KINDS.get(kind, BTN_KINDS['ghost'])
    if enabled:
        btn.configure(state='normal', bg=bg, fg=fg, cursor='hand2',
                      activebackground=active, activeforeground=fg)
    else:
        btn.configure(state='disabled', bg='#f4f5f8', fg=T.FAINT,
                      disabledforeground=T.FAINT)


class TaskRow:
    """界面上的一张下载卡片

    收起时：文件名 / 状态 / 百分比 / 总进度条 / 一行摘要 / 操作按钮
    展开时：左边是详细信息（总大小、已用时长、平均速度…），
            右边是**每个线程（分块）各自**的进度和速度
    """

    def __init__(self, app, dl):
        self.app = app
        self.dl = dl
        self.widgets = {}
        self.expanded = False
        self._detail_built = False
        self._thread_rows = []
        self._built_parts = -1

    # ---------------- 构建 ----------------
    def build(self, parent, row):
        dl = self.dl
        app = self.app

        card = tk.Frame(parent, bg=T.CARD, highlightbackground=T.BORDER,
                        highlightthickness=1, bd=0)
        card.grid(row=row, column=0, sticky='ew', padx=3, pady=5)
        card.columnconfigure(0, weight=1)
        self.frame = card

        # 左侧状态色条（跟着状态变色，整卡高度）
        self.widgets['accent'] = tk.Frame(card, width=4, bg=T.IDLE)
        self.widgets['accent'].place(x=0, y=0, relheight=1.0, width=4)

        body = tk.Frame(card, bg=T.CARD)
        body.grid(row=0, column=0, sticky='ew', padx=(15, 13), pady=10)
        body.columnconfigure(0, weight=1)
        self._body = body

        # ---- 第一行：文件名 · 线程数 · 百分比 · 状态 ----
        head = tk.Frame(body, bg=T.CARD)
        head.grid(row=0, column=0, sticky='ew')
        head.columnconfigure(0, weight=1)
        self.widgets['name'] = tk.Label(head, text=dl.display, anchor='w',
                                        bg=T.CARD, fg=T.TEXT,
                                        font=(app.f_ui, 10, 'bold'))
        self.widgets['name'].grid(row=0, column=0, sticky='ew')
        self.widgets['mode'] = tk.Label(head, text='', anchor='e', bg=T.CARD,
                                        fg=T.MUTED, font=(app.f_ui, 8))
        self.widgets['mode'].grid(row=0, column=1, sticky='e', padx=(8, 10))
        self.widgets['percent'] = tk.Label(head, text='', anchor='e', width=5,
                                           bg=T.CARD, fg=T.TEXT,
                                           font=(app.f_mono, 13, 'bold'))
        self.widgets['percent'].grid(row=0, column=2, sticky='e', padx=(0, 10))
        self.widgets['status'] = tk.Label(head, text='等待中', bg=T.IDLE,
                                          fg='#ffffff', font=(app.f_ui, 8),
                                          padx=10, pady=2)
        self.widgets['status'].grid(row=0, column=3, sticky='e')

        # ---- 第二行：总进度条 ----
        self.widgets['bar'] = ttk.Progressbar(body, mode='determinate',
                                              maximum=1000,
                                              style='Bar.Horizontal.TProgressbar')
        self.widgets['bar'].grid(row=1, column=0, sticky='ew', pady=(9, 7))

        # ---- 第三行：一行摘要 + 操作按钮 ----
        line = tk.Frame(body, bg=T.CARD)
        line.grid(row=2, column=0, sticky='ew')
        line.columnconfigure(0, weight=1)
        self.widgets['info'] = tk.Label(line, text='', anchor='w', bg=T.CARD,
                                        fg=T.MUTED, font=(app.f_mono, 9),
                                        justify='left')
        self.widgets['info'].grid(row=0, column=0, sticky='ew')

        btns = tk.Frame(line, bg=T.CARD)
        btns.grid(row=0, column=1, sticky='e', padx=(12, 0))
        self.widgets['btn_toggle'] = make_btn(btns, '详情 ▾', self.toggle_detail,
                                              font=(app.f_ui, 9))
        self.widgets['btn_toggle'].grid(row=0, column=0, padx=(0, 5))
        self.widgets['btn_resume'] = make_btn(btns, '继续', self.resume, 'ok',
                                              font=(app.f_ui, 9))
        self.widgets['btn_resume'].grid(row=0, column=1, padx=(0, 5))
        self.widgets['btn_pause'] = make_btn(btns, '暂停', self.toggle_pause,
                                             font=(app.f_ui, 9))
        self.widgets['btn_pause'].grid(row=0, column=2, padx=(0, 5))
        self.widgets['btn_cancel'] = make_btn(btns, '取消', self.cancel, 'danger',
                                              font=(app.f_ui, 9))
        self.widgets['btn_cancel'].grid(row=0, column=3, padx=(0, 5))
        self.widgets['btn_open'] = make_btn(btns, '定位', self.open_folder,
                                            font=(app.f_ui, 9))
        self.widgets['btn_open'].grid(row=0, column=4)

        # 记住每个按钮属于哪种配色，禁用时要连底色一起换掉
        self._btn_kind = {'btn_toggle': 'ghost', 'btn_resume': 'ok',
                          'btn_pause': 'ghost', 'btn_cancel': 'danger',
                          'btn_open': 'ghost'}

        # ---- 展开区（默认不显示）----
        self.detail = tk.Frame(body, bg=T.DETAIL_BG,
                               highlightbackground=T.BORDER, highlightthickness=1)
        self.detail.columnconfigure(0, weight=1)
        self.detail.columnconfigure(1, weight=0)

        # 双击卡片空白处也能展开/收起
        for key in ('name', 'percent', 'info', 'bar'):
            try:
                self.widgets[key].bind('<Double-Button-1>',
                                       lambda e: self.toggle_detail())
            except Exception:
                pass
        return card

    # ---------------- 展开 / 收起 ----------------
    def toggle_detail(self):
        self.expanded = not self.expanded
        if self.expanded:
            self._build_detail()
            self.widgets['btn_toggle'].configure(text='收起 ▴')
            self.detail.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        else:
            self.widgets['btn_toggle'].configure(text='详情 ▾')
            self.detail.grid_remove()
        try:
            self.app.canvas.configure(scrollregion=self.app.canvas.bbox('all'))
        except Exception:
            pass

    def _build_detail(self):
        if self._detail_built:
            return
        self._detail_built = True
        app = self.app

        left = tk.Frame(self.detail, bg=T.DETAIL_BG)
        left.grid(row=0, column=0, sticky='nsew', padx=(14, 10), pady=11)
        right = tk.Frame(self.detail, bg=T.DETAIL_BG)
        right.grid(row=0, column=1, sticky='nsew', padx=(10, 14), pady=11)

        tk.Label(left, text='详细信息', bg=T.DETAIL_BG, fg=T.TEXT,
                 font=(app.f_ui, 9, 'bold')).grid(
            row=0, column=0, columnspan=4, sticky='w', pady=(0, 8))

        # (显示名, 取值用的键, 第几行, 第几列)
        fields = [
            ('总大小', 'total', 0, 0), ('已用时长', 'elapsed', 0, 2),
            ('已下载', 'done', 1, 0), ('当前速度', 'speed', 1, 2),
            ('剩余', 'left', 2, 0), ('平均速度', 'avg', 2, 2),
            ('分块', 'parts', 3, 0), ('剩余时间', 'eta', 3, 2),
            ('开始时间', 'started', 4, 0), ('完成时间', 'finished', 4, 2),
        ]
        self._detail_values = {}
        for label, key, r, c in fields:
            tk.Label(left, text=label, bg=T.DETAIL_BG, fg=T.MUTED,
                     font=(app.f_ui, 9), anchor='w').grid(
                row=r + 1, column=c, sticky='w', padx=(0, 7), pady=1)
            val = tk.Label(left, text='--', bg=T.DETAIL_BG, fg=T.TEXT,
                           font=(app.f_mono, 9), anchor='w')
            val.grid(row=r + 1, column=c + 1, sticky='w', padx=(0, 24), pady=1)
            self._detail_values[key] = val

        last = len(fields) + 1
        tk.Label(left, text='保存位置', bg=T.DETAIL_BG, fg=T.MUTED,
                 font=(app.f_ui, 9)).grid(row=last, column=0, sticky='w',
                                          pady=(7, 0))
        self._detail_values['path'] = tk.Label(
            left, text='--', bg=T.DETAIL_BG, fg=T.TEXT, anchor='w',
            justify='left', wraplength=440, font=(app.f_mono, 8))
        self._detail_values['path'].grid(row=last, column=1, columnspan=3,
                                         sticky='w', pady=(7, 0))
        self._detail_values['note'] = tk.Label(
            left, text='', bg=T.DETAIL_BG, fg=T.ERR, anchor='w',
            justify='left', wraplength=440, font=(app.f_ui, 8))
        self._detail_values['note'].grid(row=last + 1, column=0, columnspan=4,
                                         sticky='w', pady=(5, 0))

        # 右侧：每个线程一行
        self._thread_title = tk.Label(right, text='线程进度', bg=T.DETAIL_BG,
                                      fg=T.TEXT, font=(app.f_ui, 9, 'bold'))
        self._thread_title.grid(row=0, column=0, sticky='w', pady=(0, 8))
        self._thread_holder = tk.Frame(right, bg=T.DETAIL_BG)
        self._thread_holder.grid(row=1, column=0, sticky='nsew')

    def _ensure_thread_rows(self, n):
        """分块数变了就重建线程行（最多 32 行，不预先建）"""
        if self._built_parts == n and len(self._thread_rows) == n:
            return
        for w in self._thread_holder.winfo_children():
            w.destroy()
        self._thread_rows = []
        self._built_parts = n
        app = self.app
        if n == 0:
            # 还没开始跑（比如重开软件后还没点「继续」）—— 给个提示，
            # 别显示一个空白的"线程进度（0 个）"
            tk.Label(self._thread_holder,
                     text='点「继续」开始后，\n这里会显示每个线程的实时进度',
                     bg=T.DETAIL_BG, fg=T.FAINT, justify='left',
                     font=(app.f_ui, 8)).grid(row=0, column=0, sticky='w')
            return
        for i in range(n):
            f = tk.Frame(self._thread_holder, bg=T.DETAIL_BG)
            f.grid(row=i, column=0, sticky='ew', pady=1)
            tk.Label(f, text='%d' % (i + 1), width=3, anchor='e',
                     bg=T.DETAIL_BG, fg=T.MUTED,
                     font=(app.f_mono, 8)).grid(row=0, column=0, padx=(0, 7))
            bar = ttk.Progressbar(f, mode='determinate', maximum=1000,
                                  length=98, style='Thin.Horizontal.TProgressbar')
            bar.grid(row=0, column=1, padx=(0, 8))
            pct = tk.Label(f, text='0%', width=5, anchor='e', bg=T.DETAIL_BG,
                           fg=T.TEXT, font=(app.f_mono, 8))
            pct.grid(row=0, column=2, padx=(0, 8))
            txt = tk.Label(f, text='', anchor='w', bg=T.DETAIL_BG, fg=T.MUTED,
                           font=(app.f_mono, 8))
            txt.grid(row=0, column=3, sticky='w')
            self._thread_rows.append((bar, pct, txt))

    # ---------------- 每 400ms 刷新 ----------------
    def _mode_text(self):
        dl = self.dl
        n = len(dl.part_specs) or dl.threads
        if dl.mode == 'single':
            return '单线程'
        if dl.mode == 'multi':
            return '%d 线程' % n
        return ''

    def _info_text(self):
        dl = self.dl
        if dl.state == 'downloading':
            if dl.total > 0:
                return ('%s / %s    ·    %s    ·    剩余 %s    ·    %d 线程'
                        % (core.fmt_size(dl.done_bytes), core.fmt_size(dl.total),
                           core.fmt_speed(dl.speed), core.fmt_eta(dl.eta()),
                           len(dl.part_specs) or dl.threads))
            return ('已下载 %s    ·    %s    ·    服务器未提供文件大小'
                    % (core.fmt_size(dl.done_bytes), core.fmt_speed(dl.speed)))
        if dl.state == 'merging':
            return '正在合并分块...    （%d 块，合并完会自动删掉分块文件）' \
                % len(dl.part_specs)
        if dl.state == 'resolving':
            return '正在解析链接...'
        if dl.state == 'done':
            return ('已保存到 %s    ·    用时 %s    ·    平均 %s'
                    % (dl.out_path or '', core.fmt_duration(dl.active_seconds),
                       core.fmt_speed(dl.avg_speed())))
        if dl.state == 'interrupted':
            if dl.total > 0:
                return ('上次没下完：已下载 %s / %s（%d%%）    ·    已用时长 %s'
                        '    ·    点「继续」接着下'
                        % (core.fmt_size(dl.done_bytes), core.fmt_size(dl.total),
                           int(dl.percent()), core.fmt_duration(dl.active_seconds)))
            return '上次没下完：已下载 %s —— 点「继续」接着下' \
                % core.fmt_size(dl.done_bytes)
        if dl.state == 'failed':
            return '失败：%s' % (dl.error or '未知原因').splitlines()[0][:120]
        if dl.state == 'cancelled':
            return ('已取消：已下载 %s    ·    已用时长 %s'
                    '    ·    点「继续」接着下，不会重下'
                    % (core.fmt_size(dl.done_bytes),
                       core.fmt_duration(dl.active_seconds)))
        return '等待开始'

    def update(self):
        dl = self.dl
        label, color = STATUS_STYLE.get(dl.state, (dl.state, T.IDLE))
        if dl.is_paused and dl.state == 'downloading':
            label, color = '已暂停', T.WARN
        self.widgets['status'].configure(text=label, bg=color)
        self.widgets['accent'].configure(bg=color)

        pct = dl.percent()
        self.widgets['percent'].configure(
            text=('%d%%' % int(pct)) if dl.total > 0 else '--',
            fg=(T.OK if dl.state == 'done' else T.TEXT))
        self.widgets['bar'].configure(value=int(pct * 10))
        if dl.state == 'done':
            bar_style = 'Done.Horizontal.TProgressbar'
        elif dl.state in ('failed', 'cancelled', 'interrupted'):
            bar_style = 'Warn.Horizontal.TProgressbar'
        else:
            bar_style = 'Bar.Horizontal.TProgressbar'
        try:
            self.widgets['bar'].configure(style=bar_style)
        except Exception:
            pass

        self.widgets['name'].configure(text=dl.display)
        self.widgets['mode'].configure(text=self._mode_text())
        # 摘要文字按状态上色：失败红、未完成/取消橙、其它灰
        if dl.state == 'failed':
            info_fg = T.ERR
        elif dl.state in ('interrupted', 'cancelled'):
            info_fg = T.WARN
        elif dl.state == 'done':
            info_fg = T.OK
        else:
            info_fg = T.MUTED
        self.widgets['info'].configure(text=self._info_text(), fg=info_fg)

        # 按钮可用性（连底色一起换，避免"灰字绿底"的假可用）
        w = self.widgets
        if dl.state == 'downloading':
            w['btn_pause'].configure(text='继续' if dl.is_paused else '暂停')
            set_btn(w['btn_pause'], True, 'ghost')
            set_btn(w['btn_cancel'], True, 'danger')
            set_btn(w['btn_resume'], False, 'ok')
            set_btn(w['btn_open'], False, 'ghost')
        elif dl.state == 'done':
            set_btn(w['btn_pause'], False, 'ghost')
            set_btn(w['btn_cancel'], False, 'danger')
            set_btn(w['btn_resume'], False, 'ok')
            set_btn(w['btn_open'], True, 'ghost')
        else:
            set_btn(w['btn_pause'], False, 'ghost')
            set_btn(w['btn_cancel'], False, 'danger')
            set_btn(w['btn_open'], False, 'ghost')
            can_resume = (dl.state in ('failed', 'cancelled', 'interrupted')
                          and dl not in self.app.active
                          and dl not in self.app.queue)
            set_btn(w['btn_resume'], can_resume, 'ok')

        if self.expanded:
            self._update_detail()

    def _update_detail(self):
        dl = self.dl
        v = self._detail_values
        v['total'].configure(text=core.fmt_size(dl.total) if dl.total > 0 else '未知')
        v['done'].configure(text=core.fmt_size(dl.done_bytes))
        if dl.total > 0:
            v['left'].configure(text=core.fmt_size(max(0, dl.total - dl.done_bytes)))
        else:
            v['left'].configure(text='--')
        v['elapsed'].configure(text=core.fmt_duration(dl.active_seconds))
        live = dl.state in ('downloading', 'merging')
        v['speed'].configure(text=core.fmt_speed(dl.speed) if live else '—')
        v['avg'].configure(text=core.fmt_speed(dl.avg_speed()))
        v['eta'].configure(text=core.fmt_eta(dl.eta()) if live else '—')
        v['started'].configure(text=core.fmt_clock(dl.created_at))
        v['finished'].configure(text=core.fmt_clock(dl.finished_at))
        v['path'].configure(text=dl.out_path or '--')
        v['note'].configure(text=(dl.error or '').strip(),
                            fg=(T.ERR if dl.state == 'failed' else T.MUTED))

        info = dl.threads_info()
        n_done = sum(1 for t in info if t['finished'])
        if info:
            v['parts'].configure(text='%d 块（已完成 %d）' % (len(info), n_done))
        elif dl.disk_parts:
            # 还没点「继续」：分块还在磁盘上，先把它数出来给用户看个安心
            v['parts'].configure(text='%d 块（在磁盘上，待继续）' % dl.disk_parts)
        else:
            v['parts'].configure(text='--')
        self._thread_title.configure(
            text=('线程进度（%d 个）' % len(info)) if info else '线程进度')
        self._ensure_thread_rows(len(info))
        for (bar, pct, txt), t in zip(self._thread_rows, info):
            try:
                bar.configure(value=int(t['percent'] * 10),
                              style=('ThinDone.Horizontal.TProgressbar'
                                     if t['finished']
                                     else 'Thin.Horizontal.TProgressbar'))
            except Exception:
                pass
            pct.configure(text='%d%%' % int(t['percent']),
                          fg=(T.OK if t['finished'] else T.TEXT))
            txt.configure(text='%s / %s   %s'
                               % (core.fmt_size(t['done']),
                                  core.fmt_size(t['total']),
                                  core.fmt_speed(t['speed'])
                                  if not t['finished'] else '完成'))

    # ---------------- 按钮动作 ----------------
    def toggle_pause(self):
        if self.dl.is_paused:
            self.dl.resume()
        else:
            self.dl.pause()

    def cancel(self):
        self.dl.cancel()

    def resume(self):
        """点「继续」→ 让主窗口把这个任务重新排进队列（原地续传）"""
        self.app.resume_task(self)

    def open_folder(self):
        d = self.dl.save_dir
        if os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception:
                pass


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = core.load_config()
        self.ui_q = queue.Queue()
        self.logger = core.Logger(self.ui_q, os.path.join(BASE_DIR, 'logs'))

        self.rows = []
        self.queue = []
        self.active = []
        self._last_save = time.time()  # 上次保存下载列表的时间
        self._closing = False          # 关窗口时用来停掉定时任务
        self._pump_id = None
        self._clip_id = None

        self.logger.info('=' * 62)
        self.logger.info('%s v%s 启动' % (APP_NAME, APP_VERSION))
        self.logger.info('默认目录=%s 线程=%d 端点=%s'
                         % (self.cfg['save_dir'], self.cfg['threads'],
                            self.cfg['endpoint']))
        self.logger.info('日志文件: %s' % self.logger.path())

        self._build_ui()
        self._restore_tasks()
        self._pump()

    # ------------------------------------------------------------------
    def _init_style(self):
        """统一 ttk 控件的外观（clam 主题最好改颜色）"""
        st = ttk.Style()
        try:
            st.theme_use('clam')
        except Exception:
            pass
        st.configure('.', background=T.BG, foreground=T.TEXT,
                     font=(self.f_ui, 9))
        st.configure('TFrame', background=T.BG)
        st.configure('TLabel', background=T.BG, foreground=T.TEXT)
        st.configure('TEntry', fieldbackground='#ffffff', foreground=T.TEXT,
                     bordercolor=T.BORDER, lightcolor=T.BORDER,
                     darkcolor=T.BORDER, insertcolor=T.TEXT, padding=4)
        st.configure('TSpinbox', fieldbackground='#ffffff', foreground=T.TEXT,
                     bordercolor=T.BORDER, arrowcolor=T.MUTED, padding=3)
        st.configure('TCombobox', fieldbackground='#ffffff', foreground=T.TEXT,
                     bordercolor=T.BORDER, arrowcolor=T.MUTED, padding=3)
        st.map('TCombobox', fieldbackground=[('readonly', '#ffffff')])
        st.configure('TScrollbar', background='#cfd6e2', troughcolor=T.BG,
                     bordercolor=T.BG, arrowcolor=T.MUTED)
        for name, thickness, color in (
                ('Bar', 10, T.ACCENT), ('Done', 10, T.OK), ('Warn', 10, T.WARN),
                ('Thin', 6, T.ACCENT), ('ThinDone', 6, T.OK)):
            st.configure('%s.Horizontal.TProgressbar' % name,
                         thickness=thickness, troughcolor=T.TRACK,
                         background=color, bordercolor=T.TRACK,
                         lightcolor=color, darkcolor=color)

    def _stat_block(self, parent, title):
        """顶部标题栏里的一个小统计块，返回可以改文字的 Label"""
        f = tk.Frame(parent, bg=T.HEADER_BG)
        f.pack(side='left', padx=(0, 24))
        tk.Label(f, text=title, bg=T.HEADER_BG, fg=T.HEADER_DIM,
                 font=(self.f_ui, 8)).pack(anchor='e')
        v = tk.Label(f, text='—', bg=T.HEADER_BG, fg=T.HEADER_FG,
                     font=(self.f_mono, 12, 'bold'))
        v.pack(anchor='e')
        return v

    def _card(self, parent, title=None):
        """一张白底卡片，返回可以往里放东西的 Frame"""
        card = tk.Frame(parent, bg=T.CARD, highlightbackground=T.BORDER,
                        highlightthickness=1, bd=0)
        card.pack(fill='x', pady=(0, 10))
        inner = tk.Frame(card, bg=T.CARD)
        inner.pack(fill='x', padx=15, pady=13)
        if title:
            tk.Label(inner, text=title, bg=T.CARD, fg=T.MUTED,
                     font=(self.f_ui, 8, 'bold')).pack(anchor='w', pady=(0, 8))
        return inner

    def _build_ui(self):
        r = self.root
        r.title('%s v%s  —— 通用下载器' % (APP_NAME, APP_VERSION))
        # 窗口尺寸：尊重上次保存的大小，但旧版的小窗口装不下新界面，
        # 而且不能超出屏幕
        try:
            sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
            m = re.match(r'^(\d+)x(\d+)$', str(self.cfg.get('window') or ''))
            w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            if w < 1020 or h < 720:          # 太小的（含旧版默认 1040x740）
                w, h = 1180, 900
            w = max(1020, min(w, sw - 40))
            h = max(720, min(h, sh - 70))
            r.geometry('%dx%d+%d+%d' % (w, h, max(0, (sw - w) // 2),
                                        max(0, (sh - h) // 3)))
        except Exception:
            r.geometry('1180x900')
        r.minsize(1020, 700)
        r.configure(bg=T.BG)

        self.f_ui = pick_font(r, ('Microsoft YaHei UI', 'Microsoft YaHei',
                                  'PingFang SC', 'Segoe UI', 'Arial'))
        self.f_mono = pick_font(r, ('Consolas', 'Cascadia Mono', 'Courier New'),
                                'TkFixedFont')
        self._init_style()

        # ============ 顶部标题栏 ============
        hdr = tk.Frame(r, bg=T.HEADER_BG)
        hdr.pack(fill='x', side='top')
        hl = tk.Frame(hdr, bg=T.HEADER_BG)
        hl.pack(side='left', padx=(18, 0), pady=(13, 13))
        tk.Label(hl, text='⬇  EmlynDownload', bg=T.HEADER_BG,
                 fg=T.HEADER_FG, font=(self.f_ui, 15, 'bold')).pack(anchor='w')
        tk.Label(hl, text='通用下载器 · HuggingFace 镜像 + 任意直链 · 多线程断点续传',
                 bg=T.HEADER_BG, fg=T.HEADER_DIM,
                 font=(self.f_ui, 8)).pack(anchor='w', pady=(2, 0))
        hr = tk.Frame(hdr, bg=T.HEADER_BG)
        hr.pack(side='right', padx=(0, 18), pady=(13, 13))
        self.hdr_speed = self._stat_block(hr, '当前速度')
        self.hdr_progress = self._stat_block(hr, '总进度')
        self.hdr_stat = self._stat_block(hr, '任务统计')

        # ============ 底部状态栏（先占位，免得被内容挤掉）============
        bot = tk.Frame(r, bg=T.BG)
        bot.pack(fill='x', side='bottom', padx=16, pady=(0, 9))
        self.var_status = tk.StringVar(value='就绪')
        tk.Label(bot, textvariable=self.var_status, bg=T.BG, fg=T.MUTED,
                 font=(self.f_ui, 9), anchor='w').pack(side='left')
        self.lbl_log = tk.Label(bot, text='', bg=T.BG, fg=T.FAINT,
                                font=(self.f_mono, 8), anchor='e')
        self.lbl_log.pack(side='right')

        # ============ 内容区 ============
        wrap = tk.Frame(r, bg=T.BG)
        wrap.pack(fill='both', expand=True, padx=16, pady=(13, 0))

        # ---- 1. 链接输入 ----
        c1 = self._card(wrap, '1. 粘贴下载链接')
        e1 = tk.Frame(c1, bg=T.CARD)
        e1.pack(fill='x')
        e1.columnconfigure(0, weight=1)
        self.var_url = tk.StringVar()
        self.entry = ttk.Entry(e1, textvariable=self.var_url,
                               font=(self.f_mono, 10))
        self.entry.grid(row=0, column=0, sticky='ew', ipady=5)
        self.entry.bind('<Return>', lambda e: self.start_download())
        self.var_url.trace_add('write', self._on_url_changed)
        make_btn(e1, '粘贴', self.paste,
                 font=(self.f_ui, 9)).grid(row=0, column=1, padx=(9, 0))
        make_btn(e1, '清空', lambda: self.var_url.set(''),
                 font=(self.f_ui, 9)).grid(row=0, column=2, padx=(6, 0))

        self.lbl_detect = tk.Label(c1, text='', bg=T.CARD, fg=T.MUTED,
                                   font=(self.f_ui, 9), anchor='w',
                                   justify='left')
        self.lbl_detect.pack(fill='x', pady=(9, 0))
        tk.Label(c1, text='支持：HuggingFace 链接（自动走国内镜像） · '
                          '任意 http/https 文件直链',
                 bg=T.CARD, fg=T.FAINT, font=(self.f_ui, 8)).pack(
            anchor='w', pady=(4, 0))

        # ---- 2. 保存位置与选项 ----
        c2 = self._card(wrap, '2. 保存位置与选项')
        f2 = tk.Frame(c2, bg=T.CARD)
        f2.pack(fill='x')
        f2.columnconfigure(0, weight=1)
        self.var_dir = tk.StringVar(value=self.cfg['save_dir'])
        ttk.Entry(f2, textvariable=self.var_dir,
                  font=(self.f_mono, 9)).grid(row=0, column=0, sticky='ew',
                                              ipady=4)
        make_btn(f2, '浏览...', self.choose_dir,
                 font=(self.f_ui, 9)).grid(row=0, column=1, padx=(9, 0))
        make_btn(f2, '打开', self.open_dir,
                 font=(self.f_ui, 9)).grid(row=0, column=2, padx=(6, 0))

        f3 = tk.Frame(c2, bg=T.CARD)
        f3.pack(fill='x', pady=(11, 0))
        tk.Label(f3, text='线程数', bg=T.CARD, fg=T.MUTED,
                 font=(self.f_ui, 9)).pack(side='left')
        self.var_threads = tk.IntVar(value=self.cfg['threads'])
        ttk.Spinbox(f3, from_=1, to=32, width=5,
                    textvariable=self.var_threads).pack(side='left',
                                                        padx=(9, 7))
        tk.Label(f3, text='（本机实测 8 线程已跑满带宽）', bg=T.CARD,
                 fg=T.FAINT, font=(self.f_ui, 8)).pack(side='left')
        tk.Label(f3, text='HF 端点', bg=T.CARD, fg=T.MUTED,
                 font=(self.f_ui, 9)).pack(side='left', padx=(30, 0))
        self.var_ep_label = tk.StringVar(value=self._ep_label())
        cb = ttk.Combobox(f3, textvariable=self.var_ep_label, width=24,
                          state='readonly',
                          values=[x[0] for x in core.ENDPOINT_CHOICES])
        cb.pack(side='left', padx=(9, 0))
        cb.bind('<<ComboboxSelected>>', self._on_ep_change)

        # ---- 工具栏 ----
        bar = tk.Frame(wrap, bg=T.BG)
        bar.pack(fill='x', pady=(2, 11))
        make_btn(bar, '▶  开始下载', self.start_download, 'primary',
                 font=(self.f_ui, 10, 'bold'), padx=18,
                 pady=7).pack(side='left')
        make_btn(bar, '批量下载', self.batch_download,
                 font=(self.f_ui, 9)).pack(side='left', padx=7)
        make_btn(bar, '测速', self.speed_test,
                 font=(self.f_ui, 9)).pack(side='left')
        make_btn(bar, '查看日志', self.open_log,
                 font=(self.f_ui, 9)).pack(side='left', padx=7)
        make_btn(bar, '清空列表', self.clear_done,
                 font=(self.f_ui, 9)).pack(side='right')
        make_btn(bar, '全部取消', self.cancel_all,
                 font=(self.f_ui, 9)).pack(side='right', padx=7)

        # ---- 3. 下载队列 ----
        tk.Label(wrap, text='3. 下载队列     （点「详情」或双击卡片，'
                            '可以看到每个线程的进度和详细信息）',
                 bg=T.BG, fg=T.MUTED, font=(self.f_ui, 8, 'bold')).pack(
            anchor='w', pady=(0, 6))

        box = tk.Frame(wrap, bg=T.BG)
        box.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(box, bg=T.BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(box, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)

        self.list_frame = tk.Frame(self.canvas, bg=T.BG)
        self._list_window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor='nw')
        self.list_frame.bind(
            '<Configure>',
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind(
            '<Configure>',
            lambda e: self.canvas.itemconfig(self._list_window, width=e.width))
        self.canvas.bind_all(
            '<MouseWheel>',
            lambda e: self.canvas.yview_scroll(int(-e.delta / 120), 'units'))

        # Tk 不允许同一容器混用 pack 和 grid：任务行用 grid，空提示也用 grid
        self.empty_holder = tk.Frame(self.list_frame, bg=T.BG)
        self.empty_holder.grid(row=0, column=0, pady=64)
        tk.Label(self.empty_holder, text='还没有任务', bg=T.BG, fg=T.MUTED,
                 font=(self.f_ui, 13, 'bold')).pack()
        tk.Label(self.empty_holder,
                 text='把下载链接粘到上面，点「开始下载」就行\n'
                      '（HuggingFace 链接和普通文件直链都支持）',
                 bg=T.BG, fg=T.FAINT, font=(self.f_ui, 9),
                 justify='center').pack(pady=(8, 0))

        self.rows_holder = tk.Frame(self.list_frame, bg=T.BG)
        self.rows_holder.grid(row=1, column=0, sticky='ew')
        self.list_frame.columnconfigure(0, weight=1)
        self.rows_holder.columnconfigure(0, weight=1)

        r.protocol('WM_DELETE_WINDOW', self.on_close)

        self._last_clip = ''
        self._clip_id = r.after(800, self._watch_clipboard)

    def _update_summary(self):
        """顶部那三个统计数字"""
        try:
            rows = self.rows
            if not rows:
                self.hdr_speed.configure(text='—')
                self.hdr_progress.configure(text='—')
                self.hdr_stat.configure(text='还没有任务')
                return
            active = [r.dl for r in rows if r.dl.state == 'downloading']
            spd = sum(d.speed for d in active)
            done = sum(1 for r in rows if r.dl.state == 'done')
            fail = sum(1 for r in rows if r.dl.state == 'failed')
            tot = sum(r.dl.total for r in rows if r.dl.total > 0)
            got = sum(min(r.dl.done_bytes, r.dl.total)
                      for r in rows if r.dl.total > 0)
            self.hdr_speed.configure(
                text=core.fmt_speed(spd) if spd > 0 else '—')
            self.hdr_progress.configure(
                text=('%d%%' % int(100.0 * got / tot)) if tot else '—')
            self.hdr_stat.configure(
                text='%d 个 · 下载中 %d · 完成 %d · 失败 %d'
                     % (len(rows), len(active), done, fail))
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  下载列表的保存 / 恢复
    # ------------------------------------------------------------------
    def _save_tasks(self):
        """把当前下载列表写到磁盘（关软件/崩溃后还在）"""
        recs = []
        for row in self.rows:
            try:
                recs.append(row.dl.snapshot())
            except Exception:
                pass
        save_task_records(recs)

    def _restore_tasks(self):
        """启动时把上次的下载列表恢复出来

        ★ 这正是"关机重开、点一下继续"原先缺的那一环：列表以前只存在
          内存里，程序一关就没了，用户根本没有可以点「继续」的那一行。
        """
        recs = load_task_records()
        if not recs:
            return
        unfinished = 0
        for rec in recs:
            if not isinstance(rec, dict) or not rec.get('url'):
                continue
            try:
                dl = core.Download.from_snapshot(
                    rec, self.cfg.get('token') or None, self.logger)
            except Exception:
                continue
            # 按磁盘上的分块把"已下载多少"算回来（不联网）
            try:
                dl.refresh_progress_from_disk()
            except Exception:
                pass
            if dl.state == 'done':
                # 记的是已完成，但文件可能已被移走/删掉 → 那就当没下完
                still_ok = bool(dl.out_path and os.path.exists(dl.out_path)
                                and (dl.total <= 0
                                     or os.path.getsize(dl.out_path) == dl.total))
                if not still_ok:
                    dl.state = 'interrupted'
            if dl.state not in ('done', 'interrupted'):
                dl.state = 'interrupted'
            row = TaskRow(self, dl)
            row.build(self.rows_holder, len(self.rows))
            self.rows.append(row)
            if dl.state == 'interrupted':
                unfinished += 1

        if not self.rows:
            return
        try:
            self.empty_holder.grid_remove()
        except Exception:
            pass
        self._refresh_rows()
        self.logger.info('恢复下载列表：共 %d 条，其中 %d 条没下完'
                         % (len(self.rows), unfinished))
        if unfinished:
            self.var_status.set('已恢复 %d 条下载记录，其中 %d 条没下完 —— '
                                '点那一行的「继续」接着下（已下的不会重下）'
                                % (len(self.rows), unfinished))
        else:
            self.var_status.set('已恢复 %d 条下载记录' % len(self.rows))

    # ------------------------------------------------------------------
    def _ep_label(self):
        for label, url in core.ENDPOINT_CHOICES:
            if url == self.cfg['endpoint']:
                return label
        return core.ENDPOINT_CHOICES[0][0]

    def _on_ep_change(self, _e=None):
        for lab, url in core.ENDPOINT_CHOICES:
            if lab == self.var_ep_label.get():
                self.cfg['endpoint'] = url
                core.save_config(self.cfg)
                self.logger.info('切换 HF 端点为: %s' % url)
                break

    def _endpoint(self):
        return self.cfg.get('endpoint', core.DEFAULT_HF_ENDPOINT)

    def _on_url_changed(self, *_a):
        txt = self.var_url.get().strip()
        if not txt:
            self.lbl_detect.configure(text='')
            return
        t = core.detect_link_type(txt)
        if t == 'hf':
            repo, fn, rev = core.parse_hf_input(txt)
            if repo and fn:
                self.lbl_detect.configure(
                    text='%s\n   仓库 %s · 文件 %s · 分支 %s'
                         % (HINT_HF, repo, fn, rev), foreground='#0a7d28')
            elif repo:
                self.lbl_detect.configure(
                    text='%s\n   仓库 %s（会列出文件供你选择）' % (HINT_HF, repo),
                    foreground='#b8860b')
            else:
                self.lbl_detect.configure(text=HINT_BAD, foreground='#c0392b')
        elif t == 'direct':
            self.lbl_detect.configure(
                text='%s\n   将用多线程下载（若服务器不支持则自动降级为单线程）'
                     % HINT_DIRECT, foreground='#0a7d28')
        elif t == 'web':
            self.lbl_detect.configure(text=HINT_WEB, foreground='#b8860b')
        else:
            self.lbl_detect.configure(
                text='%s\n   请检查链接是否完整（应以 http:// 或 https:// 开头）'
                     % HINT_BAD, foreground='#c0392b')

    def paste(self):
        try:
            self.var_url.set(self.root.clipboard_get().strip())
        except Exception as e:
            messagebox.showwarning('粘贴失败', '剪贴板里没有文本内容。\n%s' % e)

    def _watch_clipboard(self):
        """剪贴板里出现链接时自动填入（只在输入框为空时）"""
        if self._closing:
            return
        try:
            if self.cfg.get('auto_paste', True):
                txt = self.root.clipboard_get().strip()
                if (txt != self._last_clip
                        and core.detect_link_type(txt) in ('hf', 'direct', 'web')
                        and not self.var_url.get().strip()):
                    self.var_url.set(txt)
                    self.var_status.set('已自动填入剪贴板里的链接')
                self._last_clip = txt
        except Exception:
            pass
        if not self._closing:
            self._clip_id = self.root.after(1200, self._watch_clipboard)

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or os.getcwd(),
                                    title='选择保存目录')
        if d:
            self.var_dir.set(d.replace('/', os.sep))
            self.cfg['save_dir'] = self.var_dir.get()
            core.save_config(self.cfg)
            self.logger.info('保存目录改为: %s' % self.cfg['save_dir'])

    def open_dir(self):
        d = self.var_dir.get()
        if os.path.isdir(d):
            try:
                os.startfile(d)
            except Exception as e:
                messagebox.showwarning('打开失败', str(e))
        else:
            messagebox.showinfo('目录不存在',
                                '目录还不存在：\n%s\n\n下载时会自动创建。' % d)

    def open_log(self):
        p = self.logger.path()
        if os.path.isfile(p):
            try:
                os.startfile(p)
            except Exception as e:
                messagebox.showwarning('打开失败', str(e))
        else:
            messagebox.showinfo('暂无日志', '还没有日志文件。')

    # ------------------------------------------------------------------
    def _collect_config(self):
        self.cfg['save_dir'] = self.var_dir.get().strip() or core.DEFAULT_CONFIG['save_dir']
        try:
            self.cfg['threads'] = max(1, min(32, int(self.var_threads.get())))
        except Exception:
            self.cfg['threads'] = 8
            self.var_threads.set(8)
        core.save_config(self.cfg)
        return self.cfg

    def start_download(self, url=None):
        self._collect_config()
        text = (url if url is not None else self.var_url.get()).strip()
        if not text:
            messagebox.showinfo(
                '还没填链接',
                '请先把下载链接粘到最上面的输入框。\n\n'
                '支持两种：\n'
                '  · HuggingFace 链接\n'
                '    https://huggingface.co/用户/仓库/resolve/main/文件\n'
                '  · 任意文件直链\n'
                '    https://github.com/xxx/releases/download/v1/文件')
            return

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) > 1:
            self._add_many(lines)
            return

        t = core.detect_link_type(text)
        if t == 'unknown':
            messagebox.showerror(
                '认不出这个链接',
                '请检查链接是否完整。\n\n'
                '本工具支持：\n'
                '  · HuggingFace 链接（自动走国内镜像）\n'
                '  · 任意 http/https 文件直链')
            return

        if t == 'hf':
            repo, fn, rev = core.parse_hf_input(text)
            if repo and fn is None:
                self._handle_repo_only(repo)
                return

        if url is None:
            self.var_url.set('')
        self._add_task(text)

    def _handle_repo_only(self, repo):
        """只粘了仓库名 → 列出文件让他选"""
        cfg = self._collect_config()
        ep = self._endpoint()
        self.var_status.set('正在获取文件列表...')
        self.root.update_idletasks()

        files, err = core.list_hf_repo_files(ep, repo, cfg.get('token') or None)
        self.var_status.set('就绪')
        if err:
            messagebox.showwarning('拿不到文件列表',
                                   '%s\n\n你仍然可以在链接里直接带上文件名，例如：\n'
                                   '%s/%s/resolve/main/文件名' % (err, ep, repo))
            self.logger.warn('获取 %s 文件列表失败: %s' % (repo, err))
            return

        dlg = tk.Toplevel(self.root)
        dlg.title('选择要下载的文件 —— %s' % repo)
        dlg.geometry('740x500')
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text='仓库：%s\n可按住 Ctrl / Shift 多选' % repo,
                  font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=10, pady=8)

        frm = ttk.Frame(dlg)
        frm.pack(fill='both', expand=True, padx=10)
        sb = ttk.Scrollbar(frm)
        sb.pack(side='right', fill='y')
        lb = tk.Listbox(frm, selectmode='extended', font=('Consolas', 9),
                        yscrollcommand=sb.set)
        lb.pack(side='left', fill='both', expand=True)
        sb.configure(command=lb.yview)

        for name, _sz in files:
            lb.insert('end', name)
        for i, (name, _sz) in enumerate(files):
            if name.endswith(('.safetensors', '.bin', '.gguf', '.pth', '.onnx',
                              '.pt', '.zip')):
                lb.selection_set(i)

        def do_download(all_files=False):
            sel = [n for n, _ in files] if all_files \
                else [lb.get(i) for i in lb.curselection()]
            if not sel:
                messagebox.showinfo('没选文件', '请至少勾选一个文件。')
                return
            dlg.destroy()
            for n in sel:
                self._add_task('%s/%s/resolve/main/%s' % (ep, repo, n))

        bt = ttk.Frame(dlg)
        bt.pack(fill='x', padx=10, pady=10)
        ttk.Button(bt, text='下载选中的',
                   command=lambda: do_download(False)).pack(side='left')
        ttk.Button(bt, text='下载全部',
                   command=lambda: do_download(True)).pack(side='left', padx=6)
        ttk.Button(bt, text='取消', command=dlg.destroy).pack(side='right')

    def _add_many(self, lines):
        n = 0
        for l in lines:
            if core.detect_link_type(l) in ('hf', 'direct', 'web'):
                self._add_task(l, quiet=True)
                n += 1
        if n == 0:
            messagebox.showerror('没有有效链接', '这些行里没有能识别的链接。')

    def batch_download(self):
        dlg = tk.Toplevel(self.root)
        dlg.title('批量下载')
        dlg.geometry('780x540')
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text='一行一个链接（HuggingFace 或普通直链都可以混着写）',
                  font=('Microsoft YaHei UI', 10)).pack(anchor='w', padx=12,
                                                        pady=(12, 4))
        ttk.Label(dlg, text='示例：\n'
                            '  https://huggingface.co/google/embeddinggemma-2/resolve/main/model.safetensors\n'
                            '  https://github.com/xxx/releases/download/v1/model.bin\n'
                            '  用户/仓库名                （只写仓库名会列出文件让你选）',
                  style='Hint.TLabel', justify='left').pack(anchor='w', padx=12)

        txt = tk.Text(dlg, height=17, font=('Consolas', 9), wrap='none')
        txt.pack(fill='both', expand=True, padx=12, pady=8)

        def paste_clip():
            try:
                c = self.root.clipboard_get().strip()
            except Exception:
                messagebox.showwarning('剪贴板为空', '剪贴板里没有文本内容。')
                return
            if c:
                if txt.get('1.0', 'end').strip():
                    txt.insert('end', '\n')
                txt.insert('end', c + '\n')

        def do_it():
            lines = [l.strip() for l in txt.get('1.0', 'end').splitlines()
                     if l.strip()]
            if not lines:
                messagebox.showinfo('内容为空', '请至少填一个链接。')
                return
            valid = [l for l in lines
                     if core.detect_link_type(l) in ('hf', 'direct', 'web')]
            dlg.destroy()
            if not valid:
                messagebox.showerror('没有有效链接', '这些行里没有能识别的链接。')
                return
            for l in valid:
                self._add_task(l, quiet=True)
            self.logger.info('批量加入 %d 个任务' % len(valid))

        bt = ttk.Frame(dlg)
        bt.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(bt, text='加入下载队列', style='Big.TButton',
                   command=do_it).pack(side='left')
        ttk.Button(bt, text='从剪贴板粘贴',
                   command=paste_clip).pack(side='left', padx=8)
        ttk.Button(bt, text='取消', command=dlg.destroy).pack(side='right')

    def _find_row(self, url_text):
        """列表里是不是已经有这个链接了"""
        t = (url_text or '').strip()
        for row in self.rows:
            if row.dl.raw_input.strip() == t:
                return row
        return None

    def _add_task(self, url_text, quiet=False):
        # 同一个链接不重复添加 —— 否则两个任务会写同一批分块文件
        old = self._find_row(url_text)
        if old is not None:
            st = old.dl.state
            if st in ('failed', 'cancelled', 'interrupted') \
                    and old.dl not in self.active and old.dl not in self.queue:
                self.var_status.set('这个链接已在列表里，直接帮你继续下载')
                self.resume_task(old)
                return
            self.logger.info('链接已在列表中，跳过重复添加: %s' % url_text[:120])
            self.var_status.set('这个链接已经在列表里了，点那一行的「继续」即可')
            if not quiet:
                messagebox.showinfo(
                    '这个链接已经在列表里了',
                    '不用重复添加。\n\n'
                    '当前状态：%s\n'
                    '在那一行上点「继续」就能接着下（已下的不会重下）。' % st)
            return

        cfg = self.cfg
        dl = core.Download(url_text, cfg['save_dir'], cfg['threads'],
                           self._endpoint(), cfg.get('token') or None, self.logger)
        row = TaskRow(self, dl)
        row.build(self.rows_holder, len(self.rows))
        self.rows.append(row)
        try:
            self.empty_holder.grid_remove()
        except Exception:
            pass

        self.queue.append(dl)
        self.logger.info('加入队列: %s' % url_text[:140])
        self._refresh_rows()
        self._save_tasks()
        self._maybe_start_next()

    def _refresh_rows(self):
        for i, row in enumerate(self.rows):
            try:
                row.frame.grid(row=i, column=0, sticky='ew', padx=4, pady=3)
            except Exception:
                pass

    def _maybe_start_next(self):
        limit = max(1, int(self.cfg.get('concurrent', 1)))
        while self.queue and len(self.active) < limit:
            dl = self.queue.pop(0)
            self.active.append(dl)
            threading.Thread(target=self._run_task, args=(dl,), daemon=True).start()

    def _run_task(self, dl):
        try:
            dl.run()
        except Exception as e:
            dl.state = 'failed'
            dl.error = '任务崩溃：%s' % e
            self.logger.exception('任务崩溃: %s' % dl.name, e)
        finally:
            try:
                self.active.remove(dl)
            except ValueError:
                pass
            self._save_tasks()
            self.ui_q.put(('task_done', None, None))
            self._maybe_start_next()

    def resume_task(self, row):
        """点某一行的「继续」→ 原地续传，不用重新粘链接

        ★ 这是旧版缺的那一环：Download 对象以前是一次性的
          （取消后 _stop_flag 一直是 set，再跑会立刻退出），所以
          「断点续传」只能靠用户自己重新粘贴链接，等于没有。
        """
        dl = row.dl
        if dl.state in ('downloading', 'resolving', 'merging'):
            return
        if dl in self.active or dl in self.queue:
            self.var_status.set('这个任务已经在队列里了')
            return
        dl.reset()
        self.logger.info('继续下载: %s（已下载的部分不会重下）' % dl.name)
        self.var_status.set('继续下载：%s（已下载的部分不会重下）' % dl.name)
        self.queue.append(dl)
        self._refresh_rows()
        self._save_tasks()
        self._maybe_start_next()

    def cancel_all(self):
        for dl in list(self.queue) + list(self.active):
            dl.cancel()
        self.queue.clear()
        self.var_status.set('已取消全部任务（已下载的部分保留，可续传）')
        self._save_tasks()

    def clear_done(self):
        keep = []
        for row in self.rows:
            if row.dl.state in ('done', 'failed', 'cancelled'):
                row.frame.destroy()
            else:
                keep.append(row)
        self.rows = keep
        self._refresh_rows()
        self._save_tasks()
        if not self.rows:
            try:
                self.empty_holder.grid()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def speed_test(self):
        """实测当前网络速度 —— 用 HF 镜像上的一个公开文件跑 10 秒"""
        ep = self._endpoint()
        url = ('%s/google/embeddinggemma-2/resolve/main/model.safetensors'
               % ep.rstrip('/'))
        self.var_status.set('测速中（约 10 秒）...')
        self.logger.info('开始测速: %s' % url)

        def worker():
            r = core.probe_direct(url, self.cfg.get('token') or None)
            if not r.ok:
                self.ui_q.put(('speed', None,
                               '测速失败：%s' % (r.error or '无法访问')))
                return
            got = 0
            t0 = time.time()
            try:
                h = {'Range': 'bytes=0-%d' % (20 * 1024 * 1024)}
                req = core.make_request(url, h)
                with core.urllib.request.urlopen(
                        req, timeout=30, context=core.ssl_context()) as resp:
                    while time.time() - t0 < 10:
                        buf = resp.read(core.BUFFER)
                        if not buf:
                            break
                        got += len(buf)
            except Exception as e:
                if got < 1024 * 1024:
                    self.ui_q.put(('speed', None, '测速失败：%s'
                                   % core.friendly_http_error(e, url)))
                    return
            dt = max(time.time() - t0, 0.001)
            self.ui_q.put(('speed', got / dt, None))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    def _pump(self):
        if self._closing:
            return
        try:
            while True:
                kind, a, b = self.ui_q.get_nowait()
                if kind == 'log':
                    self.lbl_log.configure(text='%s: %s' % (a, (b or '')[:90]))
                elif kind == 'speed':
                    if b:
                        self.var_status.set(b)
                    else:
                        mb = a / 1048576
                        self.logger.info('测速结果: %.2f MB/s' % mb)
                        self.var_status.set('测速完成：%.2f MB/s' % mb)
                        messagebox.showinfo(
                            '测速结果',
                            '实测速度：%.2f MB/s\n\n'
                            '参考：\n'
                            '  1 GB  约需 %.1f 分钟\n'
                            '  10 GB 约需 %.0f 分钟\n'
                            '  30 GB 约需 %.0f 分钟'
                            % (mb, 1024 / max(mb, 0.01) / 60,
                               10 * 1024 / max(mb, 0.01) / 60,
                               30 * 1024 / max(mb, 0.01) / 60))
        except queue.Empty:
            pass

        for row in self.rows:
            try:
                row.update()
            except Exception:
                pass

        if self.rows:
            done = sum(1 for r in self.rows if r.dl.state == 'done')
            fail = sum(1 for r in self.rows if r.dl.state == 'failed')
            self.var_status.set(
                '共 %d 个任务：下载中 %d，排队 %d，完成 %d，失败 %d'
                % (len(self.rows), len(self.active), len(self.queue), done, fail))

        self._update_summary()

        # 定期把下载列表落盘（不用每 400ms 都写一次）
        now = time.time()
        if now - self._last_save >= 5.0:
            self._last_save = now
            if self.rows:
                self._save_tasks()

        self._pump_id = self.root.after(400, self._pump)

    def on_close(self):
        running = [d for d in self.active
                   if d.state in ('downloading', 'resolving', 'merging')]
        if running:
            ok = messagebox.askyesno(
                '还有任务在下载',
                '有 %d 个任务正在下载。\n\n'
                '现在关闭会中断下载 —— 不用担心，已下载的部分会保留，\n'
                '下载列表也会存下来；下次打开点那一行的「继续」就能接着下。\n\n'
                '确定关闭吗？' % len(running))
            if not ok:
                return
        try:
            self.cfg['window'] = '%dx%d' % (self.root.winfo_width(),
                                            self.root.winfo_height())
            core.save_config(self.cfg)
        except Exception:
            pass
        # 先把列表存下来：正在下载的会被记成"下载中"，重开时还原为"未完成"
        self._save_tasks()
        for d in self.active:
            d.cancel()
        # 停掉定时任务，避免窗口销毁后回调还在跑（Tk 会报 invalid command name）
        self._closing = True
        for aid in (self._pump_id, self._clip_id):
            if aid:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
        self.logger.info('程序退出')
        time.sleep(0.3)
        self.root.destroy()


def main():
    # 顺手把 HF 相关环境变量设好，方便之后在代码里用 transformers
    os.environ.setdefault('HF_ENDPOINT', core.DEFAULT_HF_ENDPOINT)
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')

    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
