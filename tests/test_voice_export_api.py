# -*- coding: utf-8 -*-
"""语音导出 API 测试：能力探测、参数校验、SSE 完成事件、下载防穿越。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web.app as webapp  # noqa: E402
import web.routes.voice_export_api as api  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    flask_app = webapp.create_app(str(tmp_path), wxid='wxid_owner_1a2b')
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as test_client:
        yield test_client


def test_status_reports_capabilities(client):
    resp = client.get('/api/export/voice/status')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['wav'] is True
    assert set(['mp3', 'm4a', 'ffmpeg']).issubset(data)


def test_post_without_chat_or_sender_is_400(client):
    resp = client.post('/api/export/voice', json={})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'bad_request'


def test_senders_returns_list(client, monkeypatch):
    monkeypatch.setattr(api, '_decrypted_dir', lambda: '')
    resp = client.get('/api/export/voice/senders?chat=wxid_demo_1a2b')
    assert resp.status_code == 200
    assert resp.get_json()['senders'] == []


def test_sse_done_event_carries_zip_url(client, monkeypatch):
    def fake_export(opts, push, cancel):
        assert opts['chats'] == ['wxid_demo_1a2b']
        assert opts['fmt'] == 'wav'
        return {'count': 3, 'missing': 1, 'duration_total_s': 12.5,
                'out_dir': os.path.join(str(client.application.config['DECRYPTED_DIR']), 'x'),
                'zip': os.path.join(str(client.application.config['DECRYPTED_DIR']), 'x.zip'),
                'merged': [], 'errors': []}

    monkeypatch.setattr(api, '_do_export', fake_export)
    resp = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b', 'format': 'wav'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '"zip_url"' in body and 'x.zip' in body and '"count": 3' in body


def test_sse_error_event_on_failure(client, monkeypatch):
    def boom(opts, push, cancel):
        raise RuntimeError('未检测到 ffmpeg')

    monkeypatch.setattr(api, '_do_export', boom)
    resp = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b', 'format': 'm4a'})
    assert resp.status_code == 200
    assert '未检测到 ffmpeg' in resp.get_data(as_text=True)


def test_download_strips_path_traversal(client, tmp_path, monkeypatch):
    out_root = tmp_path / 'export' / 'voice'
    out_root.mkdir(parents=True)
    (out_root / 'pack.zip').write_bytes(b'PK\x03\x04demo')
    monkeypatch.setattr(api, '_OUT_ROOT', str(out_root))
    resp = client.get('/api/export/voice/download/pack.zip')
    assert resp.status_code == 200 and resp.data.startswith(b'PK')
