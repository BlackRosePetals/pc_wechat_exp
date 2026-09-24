# -*- coding: utf-8 -*-
"""顶层编排：采集 → 解码 → 合并计划 → 写盘 → HTML → 清单 → zip → 报告。

设计要点（见设计文档 §4~§7）：

* 格式能力先探测：选 M4A 而没有 ffmpeg ⇒ **报错**（不静默换格式）；
  MP3 缺 lameenc ⇒ 降级 WAV 并在进度里说明；
* 取不到的语音进 ``missing``，由 layout 写 ``missing.csv``，报告里给出"成功/缺失"；
* 单文件内嵌 HTML 失败（体积超限等）**不阻塞**其余产物，记入 ``errors``；
* 取消只影响后续阶段，已写出的文件与清单仍然保留（清单会标明）。
"""

import os
from datetime import datetime

from engine.constants import TZ
from . import audio, html, layout
from .collect import collect_voice_items
from .model import plan_merge, safe_filename

DEFAULT_LAYOUTS = ('html-folder', 'html-inline', 'files', 'merged')


def _output_label(ok_items, chats, senders):
    """给导出目录起个人能读的名字：单个发送者 → 他的名字；否则会话名；再否则参数原样。"""
    names = sorted({item.sender_name for item in ok_items if item.sender_name})
    if len(names) == 1:
        return safe_filename(names[0]) or 'voice'
    if ok_items and ok_items[0].chat_name and ok_items[0].chat_name != ok_items[0].chat_id:
        return safe_filename(ok_items[0].chat_name) or 'voice'
    if chats:
        return safe_filename(str(chats[0])) or 'voice'
    if senders:
        return safe_filename('、'.join(str(s) for s in senders[:2])) or 'voice'
    return '全部'


def export_voices(decrypted_dir, out_root, *, chats=None, senders=None, start_ts=None,
                  end_ts=None, include_other_chats=False, own_wxid='', own_names=None,
                  name_lookup=None, fmt='mp3', layouts=DEFAULT_LAYOUTS,
                  merge_by='person', split='single', gap_s=1.0, per_n=200,
                  keep_silk=False, workers=4, zip_output=True,
                  inline_max_mb=200.0, max_bytes=layout.DEFAULT_MAX_BYTES,
                  progress_fn=None, cancel=None):
    """把语音按人（或按会话）批量导出，返回报告字典。

    Returns:
        ``{'count','missing','duration_total_s','out_dir','zip','merged','capabilities','errors'}``
    """
    def _progress(stage, message):
        if progress_fn:
            progress_fn(stage, message)

    layouts = tuple(layouts or DEFAULT_LAYOUTS)
    caps = audio.capabilities()
    if fmt == 'm4a' and not caps['m4a']:
        raise RuntimeError('未检测到 ffmpeg：M4A 需要 ffmpeg。可点「一键安装 ffmpeg」，或把 '
                           'ffmpeg.exe 放进程序目录的 tools/ 后重试；也可改用 mp3 / wav。')
    if fmt == 'mp3' and not caps['mp3']:
        _progress('write', 'lameenc 不可用，MP3 将降级为 WAV')
        fmt = 'wav'

    stamp = datetime.now(tz=TZ).strftime('%Y%m%d-%H%M%S')

    _progress('collect', '正在收集语音消息...')
    items, missing = collect_voice_items(
        decrypted_dir, chats=chats, senders=senders, start_ts=start_ts, end_ts=end_ts,
        include_other_chats=include_other_chats, own_wxid=own_wxid, own_names=own_names,
        name_lookup=name_lookup, progress_fn=progress_fn, extract=True, workers=workers)

    ok_items, failed = [], []
    for item in items:
        (failed if item.error else ok_items).append(item)
    for item in failed:
        missing.append({'sender_name': item.sender_name, 'chat_name': item.chat_name,
                        'datetime': item.datetime_text, 'reason': item.error})

    # 输出目录名用**人能读的名字**（只有一个发送者就用他，否则用会话名），而不是 wxid_xxx。
    out_dir = os.path.join(out_root, '%s_%s' % (_output_label(ok_items, chats, senders), stamp))
    os.makedirs(out_dir, exist_ok=True)

    report = {'count': 0, 'missing': len(missing), 'duration_total_s': 0.0,
              'out_dir': out_dir, 'zip': '', 'merged': [], 'capabilities': caps, 'errors': []}
    cancelled = bool(cancel and cancel())
    if cancelled:
        _progress('write', '已请求取消：将只处理到当前阶段')

    groups = [] if cancelled else plan_merge(ok_items, merge_by=merge_by, split=split,
                                            per_n=per_n, gap_s=gap_s)

    layouts_done, merged_res = [], []
    if 'files' in layouts and not cancelled:
        _progress('write', '正在写出逐条音频...')
        layouts_done = layout.write_individual(ok_items, out_dir, fmt=fmt, sample_rate='auto',
                                               keep_silk=keep_silk, progress_fn=progress_fn)
    if 'merged' in layouts and groups:
        _progress('merge', '正在合并音频...')
        merged_res = layout.write_merged(groups, out_dir, fmt=fmt, gap_s=gap_s,
                                        max_bytes=max_bytes, progress_fn=progress_fn)
        report['merged'] = [{'file': entry['file'], 'duration_s': entry['duration_s'],
                             'stem': entry['group'].file_stem} for entry in merged_res]
    if 'html-folder' in layouts:
        _progress('html', '正在生成独立 HTML...')
        html.write_html_folder(ok_items, out_dir, '语音导出 %s' % stamp,
                               layouts=layouts_done, merged=merged_res)
    if 'html-inline' in layouts:
        try:
            html.write_html_inline(ok_items, os.path.join(out_dir, 'all-in-one.html'),
                                   '语音导出 %s' % stamp, max_mb=inline_max_mb)
        except Exception as exc:                      # 体积超限/编码失败都不该拖垮整个导出
            report['errors'].append('单文件内嵌 HTML 未生成：%s' % exc)
    layout.write_manifest(ok_items, out_dir, layouts=layouts_done, merged=merged_res)
    if missing:
        layout.write_missing(missing, out_dir)
    layout.write_readme(out_dir, '语音导出 %s' % stamp)

    report['count'] = len(ok_items)
    report['duration_total_s'] = sum(item.duration_s for item in ok_items)
    if zip_output:
        _progress('zip', '正在打包...')
        report['zip'] = layout.make_zip(out_dir, out_dir.rstrip('\\/') + '.zip')
    # 注意：这里**不能**用 'done' 作为 stage —— sse.py 会把 stage='done' 当成完成事件
    # （不带 result），前端会据此提前渲染"导出完成：0 条"并失去下载链接。
    _progress('finish', '完成：%d 条，缺失 %d 条，总时长 %.1f 分钟'
              % (report['count'], report['missing'], report['duration_total_s'] / 60.0))
    return report
