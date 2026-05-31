"""REST API endpoints."""
import os
import sys
from flask import Blueprint, request, jsonify, current_app

# Ensure src/ is on path for engine imports
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from engine.services.chat import get_contacts
from engine.services.message import query_messages, query_message_detail, get_chat_stats, get_chat_dates
from engine.services.media import serve_media, serve_hardlink_media, serve_voice, transcribe_voice, decrypt_emoticon_aes_cbc
from engine.services.address_book import get_all_contacts, get_all_groups
import csv
import io

api_bp = Blueprint('api', __name__)


def _cfg():
    return (current_app.config.get('DECRYPTED_DIR', ''),
            current_app.config.get('WXID'),
            current_app.config.get('DB_DIR'))


@api_bp.route('/contacts')
def contacts():
    decrypted_dir, wxid, _ = _cfg()
    q = request.args.get('q', '').strip().lower()
    all_contacts = get_contacts(decrypted_dir, wxid)
    if q:
        all_contacts = [c for c in all_contacts
                        if q in c['name'].lower() or q in c['id'].lower()]
    return jsonify({'contacts': all_contacts, 'total': len(all_contacts)})


@api_bp.route('/messages')
def messages():
    decrypted_dir, wxid, db_dir = _cfg()
    chat_id = request.args.get('chat_id', '')
    if not chat_id:
        return jsonify({'error': 'chat_id required'}), 400
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    if page < 1:
        return jsonify({'error': 'page must be >= 1'}), 400
    if per_page < 1 or per_page > 200:
        return jsonify({'error': 'per_page must be between 1 and 200'}), 400
    try:
        result = query_messages(
            decrypted_dir, chat_id, wxid=wxid,
            page=page, per_page=per_page,
            start_date=request.args.get('start_date'),
            end_date=request.args.get('end_date'),
            msg_types=request.args.get('type'),
            sender=request.args.get('sender'),
            keyword=request.args.get('keyword'),
        )
    except FileNotFoundError as e:
        return jsonify({'error': str(e), 'messages': [], 'pagination': {'page': 1, 'per_page': 50, 'total': 0, 'total_pages': 1}}), 404
    return jsonify(result)


@api_bp.route('/messages/<int:msg_id>')
def message_detail(msg_id):
    decrypted_dir, _, _ = _cfg()
    chat_id = request.args.get('chat_id', '')
    detail = query_message_detail(decrypted_dir, msg_id, chat_id=chat_id)
    if detail is None:
        return jsonify({'error': 'message not found'}), 404
    return jsonify(detail)


@api_bp.route('/chat/<chat_id>/stats')
def chat_stats(chat_id):
    decrypted_dir, wxid, _ = _cfg()
    try:
        return jsonify(get_chat_stats(decrypted_dir, chat_id, wxid=wxid))
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404


@api_bp.route('/chat/<chat_id>/dates')
def chat_dates(chat_id):
    decrypted_dir, _, _ = _cfg()
    try:
        return jsonify(get_chat_dates(decrypted_dir, chat_id))
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404


@api_bp.route('/chat/<chat_id>/group-info')
def group_info(chat_id):
    decrypted_dir, _, _ = _cfg()
    from engine.services.chat import get_group_info
    try:
        info = get_group_info(decrypted_dir, chat_id)
    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    if info is None:
        return jsonify({'error': 'not a group chat'}), 400
    return jsonify(info)


@api_bp.route('/media')
def media():
    _, _, db_dir = _cfg()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'path required'}), 400
    return serve_media(db_dir, path)


@api_bp.route('/hardlink-media')
def hardlink_media():
    """Serve media file resolved via HardLink DB protobuf data.
    Query params: md5, path (local_path from media_info), type (3=image, 43=video, 6=file).
    """
    decrypted_dir, wxid, _ = _cfg()
    media_info = {
        'md5': request.args.get('md5', ''),
        'local_path': request.args.get('path', ''),
        'media_type': request.args.get('type', 0, type=int),
        'file_name': request.args.get('file_name', ''),
        'local_id': request.args.get('local_id', 0, type=int),
    }
    return serve_hardlink_media(decrypted_dir, media_info, wxid)


@api_bp.route('/voice')
def voice():
    """Serve voice file (SILK format, converted to WAV if possible).
    Query params: path (voice_path), create_time (optional int), local_id (optional int).
    When path alone can't find the file, create_time+local_id are used to
    extract voice data from VoiceInfo table on-the-fly.
    """
    decrypted_dir, _, db_dir = _cfg()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'path required'}), 400
    create_time = request.args.get('create_time', type=int)
    local_id = request.args.get('local_id', type=int)
    return serve_voice(decrypted_dir, path,
                       create_time=create_time, local_id=local_id,
                       db_dir=db_dir)


@api_bp.route('/voice/transcribe')
def voice_transcribe():
    """Transcribe voice to text.
    Query params: path (voice_path from media_info).
    """
    decrypted_dir, _, _ = _cfg()
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'path required'}), 400
    try:
        text = transcribe_voice(decrypted_dir, path)
        return jsonify({'text': text})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/emoji')
def emoji():
    """Serve emoji/sticker image by MD5, with remote CDN fallback.

    Query params: account (wxid), md5, emoji_url (optional remote CDN URL).
    """
    from flask import redirect, abort

    md5_val = request.args.get('md5', '').strip().lower()
    emoji_url = request.args.get('emoji_url', '').strip()
    account = request.args.get('account', '').strip()

    if not md5_val or len(md5_val) != 32:
        abort(404)

    decrypted_dir, _, _ = _cfg()
    if not os.path.isdir(decrypted_dir):
        if emoji_url:
            return redirect(emoji_url)
        abort(404)

    # 1. Search local filesystem for emoji by MD5
    search_dirs = [
        os.path.join(decrypted_dir, 'emoticon'),
        os.path.join(decrypted_dir, 'Emoticon'),
        os.path.join(decrypted_dir, 'sticker'),
        os.path.join(decrypted_dir, 'Sticker'),
        os.path.join(decrypted_dir, 'msg', 'attach'),
    ]
    if account:
        search_dirs.insert(0, os.path.join(os.path.dirname(decrypted_dir), account, 'emoticon'))

    variants = [md5_val, f'{md5_val}_t', f'{md5_val}_h',
                f'{md5_val}.jpg', f'{md5_val}.png', f'{md5_val}.gif', f'{md5_val}.webp',
                f'{md5_val}_t.jpg', f'{md5_val}_t.png',
                f'{md5_val}.dat', f'{md5_val}_h.dat', f'{md5_val}_t.dat']

    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for v in variants:
            path = os.path.join(d, v)
            if os.path.isfile(path):
                resolved = path
                # Handle .dat encrypted files
                if resolved.lower().endswith('.dat'):
                    try:
                        with open(resolved, 'rb') as f:
                            raw = f.read()
                        dec = decrypt_emoticon_aes_cbc(raw, md5_val)
                        if dec:
                            from flask import send_file
                            from io import BytesIO
                            return send_file(BytesIO(dec), mimetype='image/png')
                    except Exception:
                        pass
                    continue
                from flask import send_file
                mime, _ = __import__('mimetypes').guess_type(resolved)
                return send_file(resolved, mimetype=mime or 'image/png')

    # 2. Fallback: proxy from remote CDN URL
    if emoji_url:
        return redirect(emoji_url)

    abort(404)


@api_bp.route('/harvest-keys/status')
def harvest_keys_status():
    """Get V2 key cache status: how many keys cached vs total V2 files."""
    import json as _json
    decrypted_dir, wxid, _ = _cfg()
    if not os.path.isdir(decrypted_dir):
        return jsonify({'error': 'decrypted_dir not configured'}), 400

    keys_file = os.path.join(decrypted_dir, '_media_keys.json')
    cached = 0
    try:
        if os.path.isfile(keys_file) and os.path.getsize(keys_file) > 0:
            with open(keys_file, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            cached = len(data.get('md5_keys', {}))
    except Exception:
        pass

    # Count V2 files in media/images/
    v2_total = 0
    img_dir = os.path.join(decrypted_dir, 'media', 'images')
    if os.path.isdir(img_dir):
        for fname in os.listdir(img_dir):
            if fname.lower().endswith('.dat'):
                fpath = os.path.join(img_dir, fname)
                try:
                    with open(fpath, 'rb') as f:
                        header = f.read(6)
                    if header == b'\x07\x08V2\x08\x07':
                        v2_total += 1
                except OSError:
                    pass

    from engine.services.v2_key_extract import is_wechat_running as _wx_running
    return jsonify({
        'cached': cached,
        'v2_total': v2_total,
        'pending': max(0, v2_total - cached),
        'wechat_running': _wx_running(),
    })


@api_bp.route('/harvest-keys/run', methods=['POST'])
def harvest_keys_run():
    """Run one round of V2 key harvesting from WeChat memory."""
    import json as _json
    from engine.services.v2_key_extract import harvest_v2_keys, is_wechat_running as _wx_running

    decrypted_dir, wxid, _ = _cfg()
    if not os.path.isdir(decrypted_dir):
        return jsonify({'error': 'decrypted_dir not configured'}), 400

    if not _wx_running():
        return jsonify({'error': '微信未运行，请先启动微信并浏览包含图片的聊天记录'}), 400

    # Run a single scan round
    found = harvest_v2_keys(
        decrypted_dir, wxid=wxid,
        interval=0, max_rounds=1,
        print_fn=lambda *a, **kw: None
    )

    return jsonify({
        'found': len(found),
        'keys': {md5: key.hex() for md5, key in found.items()},
    })


@api_bp.route('/address-book')
def address_book():
    """Return contacts from contact.db with message stats, paginated.

    Query params:
        q        — search keyword (matches display_name, remark, nick_name, alias, wxid, phone, description)
        sort     — 'name' (default), 'msg_count', 'last_time'
        has_chat — '1' (only with chats), '0' (only without)
        letter   — filter by first letter of display_name
        page     — page number (default 1)
        per_page — items per page (default 100, max 500)
    """
    decrypted_dir, wxid, db_dir = _cfg()
    contacts = get_all_contacts(decrypted_dir)

    q = request.args.get('q', '').strip().lower()
    sort = request.args.get('sort', 'name')
    has_chat = request.args.get('has_chat')
    letter = request.args.get('letter', '').strip().upper()

    if q:
        def _match(c):
            if q in c['display_name'].lower():
                return True
            if q in c['remark'].lower():
                return True
            if q in c['nick_name'].lower():
                return True
            if q in c['alias'].lower():
                return True
            if q in c['wxid'].lower():
                return True
            if c.get('phone') and q in c['phone']:
                return True
            if c.get('description') and q in c['description'].lower():
                return True
            return False
        contacts = [c for c in contacts if _match(c)]
    if has_chat == '1':
        contacts = [c for c in contacts if c['msg_count'] > 0]
    elif has_chat == '0':
        contacts = [c for c in contacts if c['msg_count'] == 0]
    if letter:
        contacts = [c for c in contacts
                    if (c['display_name'] or c['wxid'])[:1].upper() == letter]

    if sort == 'msg_count':
        contacts.sort(key=lambda c: c['msg_count'], reverse=True)
    elif sort == 'last_time':
        contacts.sort(key=lambda c: c['last_msg_time'] or 0, reverse=True)

    # Pagination
    total = len(contacts)
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = max(1, min(500, int(request.args.get('per_page', 100))))
    except (ValueError, TypeError):
        per_page = 100

    start = (page - 1) * per_page
    end = start + per_page
    page_contacts = contacts[start:end]
    total_pages = max(1, (total + per_page - 1) // per_page)

    return jsonify({
        'contacts': page_contacts,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': total_pages,
    })


@api_bp.route('/address-book/<wxid>')
def address_book_detail(wxid):
    """Return single contact detail."""
    decrypted_dir, _, _ = _cfg()
    contacts = get_all_contacts(decrypted_dir)
    for c in contacts:
        if c['wxid'] == wxid:
            return jsonify(c)
    return jsonify({'error': 'contact not found'}), 404


@api_bp.route('/address-book/groups')
def address_book_groups():
    """Return group chat list with resolved display names.

    Fast path: groups from chats.db index already have pre-computed display_name.
    Slow path: groups from contact.db need _resolve_display lookup.
    """
    decrypted_dir, wxid, db_dir = _cfg()
    groups = get_all_groups(decrypted_dir)

    # Check if groups already have meaningful display names (fast path)
    needs_resolve = any(
        (g.get('display_name') or '') == (g.get('wxid') or '')
        for g in groups[:10]
    )
    if needs_resolve:
        from engine.services.chat import _load_contacts, _load_sessions, _load_room_owners, _find_file, _resolve_display
        contact_db = _find_file(decrypted_dir, "contact/contact.db", "contact.db")
        session_db = _find_file(decrypted_dir, "session/session.db", "session.db")
        id_to_name, name_to_id, _ = _load_contacts(contact_db)
        session_summaries = _load_sessions(session_db)
        room_owners = _load_room_owners(contact_db)
        for g in groups:
            uname = g['wxid']
            g['display_name'] = _resolve_display(
                uname, is_group=True, decrypted_dir=decrypted_dir,
                id_to_name=id_to_name, name_to_id=name_to_id,
                session_summaries=session_summaries, room_owners=room_owners,
            )

    return jsonify({'groups': groups, 'total': len(groups)})


@api_bp.route('/address-book/export')
def address_book_export():
    """Export contacts as CSV."""
    decrypted_dir, _, _ = _cfg()
    contacts = get_all_contacts(decrypted_dir)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['wxid', 'display_name', 'remark', 'nick_name', 'alias',
                     'phone', 'sex', 'region', 'signature', 'description',
                     'msg_count', 'last_msg_time', 'is_group'])
    SEX_MAP = {0: '', 1: '男', 2: '女'}
    for c in contacts:
        sex_label = SEX_MAP.get(c.get('sex'), '')
        region = ' '.join(filter(None, [c.get('country', ''), c.get('province', ''), c.get('city', '')]))
        writer.writerow([
            c['wxid'], c['display_name'], c['remark'], c['nick_name'],
            c['alias'],
            c.get('phone', ''),
            sex_label,
            region,
            c.get('signature', ''),
            c.get('description', ''),
            c['msg_count'],
            c['last_msg_time'] or '',
            'Y' if c['is_group'] else 'N',
        ])

    from flask import Response
    csv_str = output.getvalue()
    output.close()
    return Response(
        csv_str,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=address_book.csv'}
    )
