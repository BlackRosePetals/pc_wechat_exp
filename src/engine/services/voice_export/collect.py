# -*- coding: utf-8 -*-
"""语音采集：枚举 ``Msg_`` 表里的 type=34 消息，按分片 ``Name2Id`` 判归属。

关键点（见设计文档 §2）：

* 发送者归属用**分片级 ``Name2Id``**（``real_sender_id`` 就是它的 ``rowid``），与
  聊天气泡/统计/导出用的是同一套权威判据；
* 会话反查用 ``md5(chat_id) == Msg_ 表名后缀``，一次性建映射，避免逐表遍历；
* 取不到的语音**不静默**：进 ``missing`` 列表（由上层写 ``missing.csv``）。
"""

import hashlib
import os
import re
import sqlite3

from engine.services.media import _extract_voice_from_db, silk_to_pcm
from engine.services.sender_model import load_name2id, own_id_forms
from .model import VoiceItem, pick_sample_rate

_VOICELENGTH_RE = re.compile(rb'voicelength="(\d+)"')
ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def collect_voice_items(decrypted_dir, *, chats=None, senders=None, start_ts=None, end_ts=None,
                        include_other_chats=False, own_wxid='', own_names=None,
                        name_lookup=None, progress_fn=None, extract=True, workers=1):
    """采集语音消息。

    Args:
        extract: 是否抽取 SILK 并解码出真实时长/采样率（False 时只用 XML 时长兜底）。
        workers: 解码阶段的并行线程数（解码是子进程 + 只读 DB，可并行）。

    Returns:
        ``(items, missing)``：``items`` 为 :class:`VoiceItem` 列表（按时间排序，解码失败者带 ``error``）；
        ``missing`` 为 ``[{'sender_name','chat_name','datetime','reason'}]``。
    """
    own = own_id_forms(own_wxid or '')
    chat_filter = set(chats or [])
    sender_filter = set(senders or [])
    items, missing = [], []
    msg_dir = os.path.join(decrypted_dir, 'message')
    if not os.path.isdir(msg_dir):
        return items, missing
    shards = [os.path.join(msg_dir, name) for name in sorted(os.listdir(msg_dir))
              if name.startswith('message_') and name.endswith('.db')]

    for shard in shards:
        conn = None
        try:
            conn = sqlite3.connect('file:%s?mode=ro' % shard, uri=True)
            name2id = load_name2id(conn)
            reverse = {}
            for user_name in name2id.values():
                reverse.setdefault(hashlib.md5(str(user_name).encode('utf-8')).hexdigest(),
                                   user_name)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            if progress_fn:
                progress_fn('collect', '扫描 %s（%d 张消息表）'
                            % (os.path.basename(shard), len(tables)))
            for table in tables:
                chat_id = reverse.get(table[4:] if table.startswith('Msg_') else table)
                if not chat_id:
                    # 反查不到会话时：只有"明确只导一个会话"才敢当成它，否则不猜
                    if len(chat_filter) == 1:
                        chat_id = next(iter(chat_filter))
                    else:
                        continue
                if chat_filter and chat_id not in chat_filter and not include_other_chats:
                    continue
                for local_id, create_time, rsid, xml_ms in _iter_voice_rows(conn, table):
                    if start_ts and create_time < start_ts:
                        continue
                    if end_ts and create_time > end_ts:
                        continue
                    sender_id = name2id.get(int(rsid or 0)) or ''
                    is_own = bool(own) and sender_id in own
                    sender_name = _sender_name(sender_id, is_own, own_wxid, own_names, name_lookup)
                    if sender_filter and sender_name not in sender_filter \
                            and sender_id not in sender_filter:
                        continue
                    item = VoiceItem(chat_id=chat_id, chat_name=chat_id,
                                     local_id=int(local_id), create_time=int(create_time),
                                     duration_ms=int(xml_ms or 0),
                                     sender_id=sender_id, sender_name=sender_name)
                    if not extract:
                        item.duration_s = item.duration_ms / 1000.0
                        items.append(item)
                        continue
                    item.silk_path = _extract_voice_from_db(
                        decrypted_dir, item.create_time, item.local_id, chat=chat_id) or ''
                    if not item.silk_path:
                        missing.append(_missing(item, '备份中找不到这条语音数据（可重跑一次「一键备份」）'))
                        continue
                    items.append(item)
        except sqlite3.Error as e:
            if progress_fn:
                progress_fn('collect', '跳过 %s：%s' % (os.path.basename(shard), e))
        finally:
            if conn:
                conn.close()

    if extract and items:
        if progress_fn:
            progress_fn('extract', '正在解码 %d 条语音（%d 线程）...' % (len(items), workers))
        if workers and workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=int(workers)) as pool:
                list(pool.map(_decode_item, items))
        else:
            for item in items:
                _decode_item(item)

    items.sort(key=lambda i: (i.create_time, i.local_id))
    return items, missing


def _decode_item(item):
    """解码一条语音，回填真实采样率与时长（**以 PCM 长度为准**）。"""
    pcm = silk_to_pcm(item.silk_path)
    if not pcm:
        item.error = 'SILK 解码失败'
        return item
    item.sample_rate = pick_sample_rate(len(pcm), item.duration_ms)
    item.duration_s = len(pcm) / 2.0 / item.sample_rate
    return item


def _missing(item, reason):
    return {'sender_name': item.sender_name, 'chat_name': item.chat_name,
            'datetime': item.datetime_text, 'reason': reason}


def _sender_name(sender_id, is_own, own_wxid, own_names, name_lookup):
    if is_own or (own_wxid and sender_id == own_wxid) or (sender_id and sender_id in (own_names or ())):
        return '我'
    if not sender_id:
        return '未知'
    if name_lookup:
        got = name_lookup(sender_id)
        if got:
            return got
    return sender_id


def _iter_voice_rows(conn, table):
    """产出 ``(local_id, create_time, real_sender_id, xml_ms)``。"""
    try:
        rows = conn.execute(
            'SELECT local_id, create_time, real_sender_id, message_content FROM [%s] '
            'WHERE (local_type & 65535) = 34' % table).fetchall()
    except sqlite3.Error:
        return
    for local_id, create_time, rsid, content in rows:
        yield local_id, create_time, rsid, _voicelength_ms(content)


def _voicelength_ms(content):
    """从消息内容里取 XML 的 ``voicelength``（毫秒）；取不到返回 0。"""
    if content is None:
        return 0
    raw = content
    if isinstance(content, (bytes, bytearray)):
        raw = bytes(content)
        if raw[:4] == ZSTD_MAGIC:
            from engine.services.message import _zstd_decompress_raw
            raw = _zstd_decompress_raw(raw) or b''
    elif isinstance(content, str):
        raw = content.encode('utf-8', 'replace')
    match = _VOICELENGTH_RE.search(raw)
    return int(match.group(1)) if match else 0
