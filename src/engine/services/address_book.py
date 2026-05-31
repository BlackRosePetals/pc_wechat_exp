"""Address book: read all contacts from contact.db with display names and message stats.

Fast path: reads pre-computed contacts table from data/chats.db (built during backup).
Slow path: falls back to direct contact.db scan if the index is missing or empty.
"""
import os
import re
import sqlite3

from engine.services.name_resolver import pick_display_name, _find_contact_db, _load_chatroom_names, chatroom_fallback_name

# Columns we always want from contact.db (core identity fields)
_CORE_CONTACT_COLS = ['username', 'remark', 'nick_name', 'alias']

# Extra columns that may exist in some WeChat versions — discovered dynamically
_KNOWN_EXTRA_COLS = [
    'description', 'sex', 'country', 'province', 'city',
    'signature', 'small_head_url', 'big_head_url', 'contactType',
]

# wxid patterns that indicate a phone-number-based account
_PHONE_WXID_RE = re.compile(r'^\+(\d{1,3})(\d{7,14})$')


def _phone_from_wxid(wxid: str) -> str:
    """If wxid looks like a phone number, return formatted version."""
    m = _PHONE_WXID_RE.match(wxid or '')
    if m:
        return f'+{m.group(1)} {m.group(2)}'
    return ''


def _discover_contact_columns(decrypted_dir: str) -> list:
    """Return list of column names present in the contact table of contact.db."""
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db or not os.path.isfile(contact_db):
        return list(_CORE_CONTACT_COLS)
    try:
        conn = sqlite3.connect(contact_db)
        rows = conn.execute("PRAGMA table_info(contact)").fetchall()
        conn.close()
        return [r[1] for r in rows]  # r[1] = column name
    except sqlite3.Error:
        return list(_CORE_CONTACT_COLS)


def _build_contact_select(available_cols: list) -> tuple:
    """Build a SELECT clause and return (col_string, col_list)."""
    cols = list(_CORE_CONTACT_COLS)
    for c in _KNOWN_EXTRA_COLS:
        if c in available_cols and c not in cols:
            cols.append(c)
    return ', '.join(cols), cols


def _parse_contact_row(col_names: list, row: tuple) -> dict:
    """Convert a row (matched to col_names) into a contact dict."""
    d = dict(zip(col_names, row))
    wxid = (d.get('username') or '').strip()
    remark = (d.get('remark') or '').strip()
    nick = (d.get('nick_name') or '').strip()
    alias = (d.get('alias') or '').strip()

    display = pick_display_name(wxid, remark, nick, alias, wxid) or wxid
    is_group = wxid.endswith('@chatroom')

    contact = {
        'wxid': wxid,
        'display_name': display,
        'remark': remark,
        'nick_name': nick,
        'alias': alias,
        'avatar_url': f'/api/avatar/{wxid}',
        'msg_count': 0,
        'last_msg_time': None,
        'is_group': is_group,
    }

    # Phone detection from wxid
    phone = _phone_from_wxid(wxid)
    if phone:
        contact['phone'] = phone

    # Extra fields
    for c in _KNOWN_EXTRA_COLS:
        val = d.get(c)
        if val is not None and str(val).strip():
            s = str(val).strip()
            if c == 'sex':
                try:
                    contact['sex'] = int(s)
                except (ValueError, TypeError):
                    pass
            else:
                contact[c] = s

    return contact


def _find_chats_db(decrypted_dir: str) -> str:
    """Find chats.db in decrypted_dir."""
    for p in (os.path.join(decrypted_dir, 'data', 'chats.db'),
              os.path.join(decrypted_dir, 'chats.db')):
        if os.path.isfile(p):
            return p
    return None


def _load_from_contacts_index(decrypted_dir: str) -> list:
    """Fast path: read pre-computed contacts from chats.db contacts table.

    Returns None if the table doesn't exist or is empty, signaling fallback.
    """
    chats_db = _find_chats_db(decrypted_dir)
    if not chats_db:
        return None
    try:
        conn = sqlite3.connect(chats_db)
        # Check if contacts table exists
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
        ).fetchone()
        if not exists:
            conn.close()
            return None
        # Dynamically read contacts table columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()]
        col_str = ', '.join(cols)
        rows = conn.execute(f"SELECT {col_str} FROM contacts").fetchall()
        if not rows:
            conn.close()
            return None
        contacts = {}
        for row in rows:
            d = dict(zip(cols, row))
            wxid = (d.get('wxid') or d.get('username') or '').strip()
            if not wxid:
                continue
            contact = {
                'wxid': wxid,
                'display_name': d.get('display_name') or wxid,
                'remark': d.get('remark') or '',
                'nick_name': d.get('nick_name') or '',
                'alias': d.get('alias') or '',
                'avatar_url': f'/api/avatar/{wxid}',
                'msg_count': 0,
                'last_msg_time': None,
                'is_group': bool(d.get('is_group', 0)),
            }
            # Phone detection
            phone = _phone_from_wxid(wxid)
            if phone:
                contact['phone'] = phone
            # Copy extra fields
            for c in _KNOWN_EXTRA_COLS:
                val = d.get(c)
                if val is not None and str(val).strip():
                    s = str(val).strip()
                    if c == 'sex':
                        try:
                            contact['sex'] = int(s)
                        except (ValueError, TypeError):
                            pass
                    else:
                        contact[c] = s
            contacts[wxid] = contact
        # Enrich with message stats from chats table
        for r in conn.execute(
            "SELECT chat_id, message_count, last_msg_time FROM chats"
        ):
            chat_id, msg_count, last_time = r
            if chat_id in contacts:
                contacts[chat_id]['msg_count'] = msg_count or 0
                contacts[chat_id]['last_msg_time'] = last_time
        conn.close()
        return sorted(contacts.values(), key=lambda c: (
            c['display_name'] or c['wxid']
        ).lower())
    except sqlite3.Error:
        return None


def _load_from_contact_db(decrypted_dir: str) -> list:
    """Slow path: scan contact.db directly when chats.db index is unavailable."""
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db or not os.path.isfile(contact_db):
        return []

    available_cols = _discover_contact_columns(decrypted_dir)
    select_str, select_cols = _build_contact_select(available_cols)

    contacts = {}
    try:
        conn = sqlite3.connect(contact_db)
        for row in conn.execute(f"SELECT {select_str} FROM contact"):
            contact = _parse_contact_row(select_cols, row)
            wxid = contact['wxid']
            if not wxid:
                continue
            contacts[wxid] = contact
        conn.close()
    except sqlite3.Error:
        return []

    _attach_chat_stats(decrypted_dir, contacts)
    return sorted(contacts.values(), key=lambda c: (
        c['display_name'] or c['wxid']
    ).lower())


def _enrich_with_chatroom_names(decrypted_dir: str, contacts: list) -> None:
    """Replace raw @chatroom IDs with display names from chat_room table."""
    chatroom_names = _load_chatroom_names(decrypted_dir)
    for c in contacts:
        wxid = c.get('wxid', '')
        if wxid.endswith('@chatroom') and c.get('display_name') == wxid:
            name = chatroom_names.get(wxid) if chatroom_names else None
            if name:
                c['display_name'] = name
            else:
                c['display_name'] = chatroom_fallback_name(wxid)


def get_all_contacts(decrypted_dir: str) -> list:
    """Return all contacts with display names and optional chat stats.

    Fast path: reads pre-computed contacts table from chats.db (instant).
    Slow path: scans contact.db directly when index is missing.

    Result is cached in memory — contact data is static during a session.
    """
    cache_key = f'_contacts_cache_{decrypted_dir}'
    if cache_key in _ALL_CONTACTS_CACHE:
        return _ALL_CONTACTS_CACHE[cache_key]
    contacts = _load_from_contacts_index(decrypted_dir)
    if contacts is None:
        contacts = _load_from_contact_db(decrypted_dir)
    if contacts:
        _enrich_with_chatroom_names(decrypted_dir, contacts)
    _ALL_CONTACTS_CACHE[cache_key] = contacts
    return contacts


_ALL_CONTACTS_CACHE = {}


def _attach_chat_stats(decrypted_dir: str, contacts: dict) -> None:
    """Enrich contacts dict with msg_count and last_msg_time from chats.db."""
    chats_db = _find_chats_db(decrypted_dir)
    if not chats_db:
        return
    try:
        conn = sqlite3.connect(chats_db)
        rows = conn.execute(
            "SELECT chat_id, message_count, last_msg_time FROM chats"
        ).fetchall()
        conn.close()
        for chat_id, msg_count, last_time in rows:
            if chat_id in contacts:
                contacts[chat_id]['msg_count'] = msg_count or 0
                contacts[chat_id]['last_msg_time'] = last_time
    except sqlite3.Error:
        pass


def get_all_groups(decrypted_dir: str) -> list:
    """Return all group chats with pre-computed display names.

    Fast path: reads from chats.db contacts table (is_group=1), pre-computed
              during backup indexing.
    Slow path: reads chat_room from contact.db directly.
    """
    # Fast path: use pre-computed contacts index
    all_contacts = _load_from_contacts_index(decrypted_dir)
    if all_contacts is not None:
        return [c for c in all_contacts if c['is_group']]

    # Slow path: direct contact.db scan
    contact_db = _find_contact_db(decrypted_dir)
    if not contact_db or not os.path.isfile(contact_db):
        return []

    groups = []
    try:
        conn = sqlite3.connect(contact_db)
        for r in conn.execute("SELECT username, owner FROM chat_room"):
            uname, owner = r
            uname = (uname or '').strip()
            if not uname:
                continue
            groups.append({
                'wxid': uname,
                'display_name': uname,
                'owner': (owner or '').strip(),
                'avatar_url': f'/api/avatar/{uname}',
            })
        conn.close()
    except sqlite3.Error:
        pass
    return groups
