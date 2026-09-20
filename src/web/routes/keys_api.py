"""手动输入密钥 API — 让用户自己粘贴密钥，写进统一配置后供其它功能使用。

  GET  /api/keys/status   查询密钥覆盖情况（按数据库逐条校验 HMAC）
  GET  /api/keys/dirs     列出检测到的微信 db_storage 目录
  POST /api/keys/verify   解析并实测匹配（不保存）
  POST /api/keys/save     匹配并保存（可选强制保存未通过校验的）
  POST /api/keys/remove   删除某个数据库已保存的密钥
"""
import os
import sys

from flask import Blueprint, current_app, jsonify, request

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

keys_bp = Blueprint("keys_api", __name__, url_prefix="/api/keys")


def _dir_hint():
    try:
        from engine.utils import data_dir_hint
        return data_dir_hint(short=True)
    except Exception:
        return "未找到微信数据目录。请在微信「设置 → 文件管理」查看数据目录，或手动填写。"

def _detect_dirs(mode="auto"):
    """列出微信数据目录。

    mode: fast = 只做快速探测；auto = 找不到时自动做有限深度搜索（15s）；
          deep = 用户主动点「深度搜索」，给更长时间与层数（45s / 7 层）
    """
    try:
        from engine.utils import find_all_wechat_data_dirs
    except Exception:
        return []
    try:
        if mode == "fast":
            return find_all_wechat_data_dirs(deep=False)
        if mode == "deep":
            return find_all_wechat_data_dirs(deep=True, budget_s=45.0, max_depth=7)
        return find_all_wechat_data_dirs(deep=True)
    except Exception:
        return []


def _resolve_db_dir(param=None):
    """解析要操作的 db_storage 目录：入参 → 配置 → 应用配置 → 自动检测。"""
    if param and os.path.isdir(param):
        return param
    try:
        from engine.config_file import get_db_dir
        d = get_db_dir()
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    d = current_app.config.get("DB_DIR") or ""
    if d and os.path.isdir(d):
        return d
    dirs = _detect_dirs()
    if dirs:
        return dirs[0]["db_path"]
    return param or ""


@keys_bp.route("/dirs", methods=["GET"])
def keys_dirs():
    mode = (request.args.get("mode") or "auto").lower()
    if mode not in ("fast", "auto", "deep"):
        mode = "auto"
    dirs = _detect_dirs(mode)
    return jsonify({"dirs": dirs, "current": _resolve_db_dir(), "mode": mode})


def _resolve_db_storage(raw):
    """把用户填的目录尽量解析成真正的 db_storage（兼容 4 种填法）。"""
    import os as _os
    if not raw:
        return None
    p = _os.path.normpath(raw.strip().strip(chr(34)))

    def _first_account(xwf):
        try:
            cands = []
            for name in _os.listdir(xwf):
                full = _os.path.join(xwf, name, "db_storage")
                if _os.path.isdir(full):
                    try:
                        cands.append((_os.path.getmtime(full), full))
                    except OSError:
                        cands.append((0.0, full))
            cands.sort(reverse=True)
            return cands[0][1] if cands else None
        except OSError:
            return None

    base = _os.path.basename(p).lower()
    if base == "db_storage":
        return p if _os.path.isdir(p) else None
    direct = _os.path.join(p, "db_storage")
    if _os.path.isdir(direct):
        return direct
    if base == "xwechat_files":
        return _first_account(p)
    inner = _os.path.join(p, "xwechat_files")
    if _os.path.isdir(inner):
        return _first_account(inner)
    return None

@keys_bp.route("/dbdir", methods=["POST"])
def keys_set_dbdir():
    """保存用户手动指定的 db_storage 目录，供备份/解密/密钥页复用。"""
    import os as _os
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip().strip(chr(34))
    if not raw:
        return jsonify({"error": "empty_path", "message": "请填写微信数据目录"}), 400
    path = _resolve_db_storage(raw)
    if path is None:
        exists = _os.path.isdir(_os.path.normpath(raw.strip().strip(chr(34))))
        if not exists:
            return jsonify({"error": "not_found",
                            "message": "目录不存在: " + raw}), 400
        return jsonify({"error": "not_db_storage",
                        "message": "该目录下找不到 db_storage（或 db_storage 下没有 message 子目录）: "
                                   + raw}), 400
    if not _os.path.isdir(_os.path.join(path, "message")):
        return jsonify({"error": "not_db_storage",
                        "message": "该目录下没有 message 子目录，看起来不是微信的 db_storage: " + path}), 400
    try:
        from engine.config_file import set_db_dir
        set_db_dir(path)
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500
    return jsonify({"ok": True, "dbDir": path, "status": _status_safe(path)})


def _status_safe(db_dir):
    try:
        from engine.manual_keys import status
        return status(db_dir)
    except Exception:
        return None


@keys_bp.route("/status", methods=["GET"])
def keys_status():
    from engine.manual_keys import status

    db_dir = _resolve_db_dir(request.args.get("db_dir"))
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint(),
                        "dbDir": db_dir or "", "total": 0, "verified": 0,
                        "missing": 0, "invalid": 0, "databases": []}), 200
    try:
        out = status(db_dir)
    except Exception as e:
        return jsonify({"error": "status_failed", "message": str(e)}), 500
    return jsonify(out)


@keys_bp.route("/verify", methods=["POST"])
def keys_verify():
    from engine.manual_keys import match_entries, parse_entries, status

    data = request.get_json(silent=True) or {}
    db_dir = _resolve_db_dir(data.get("db_dir"))
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint()}), 400
    text = data.get("text") or ""
    entries = parse_entries(text)
    if not entries:
        return jsonify({"error": "empty_input", "message": "没有解析到任何密钥行"}), 400
    results = match_entries(db_dir, entries)
    return jsonify({"results": results, "status": status(db_dir), "saved": 0})


@keys_bp.route("/save", methods=["POST"])
def keys_save():
    from engine.manual_keys import apply_entries, parse_entries

    data = request.get_json(silent=True) or {}
    db_dir = _resolve_db_dir(data.get("db_dir"))
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint()}), 400
    entries = parse_entries(data.get("text") or "")
    if not entries:
        return jsonify({"error": "empty_input", "message": "没有解析到任何密钥行"}), 400
    try:
        out = apply_entries(db_dir, entries, force=bool(data.get("force")))
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500
    out["dbDir"] = db_dir
    return jsonify(out)


@keys_bp.route("/remove", methods=["POST"])
def keys_remove():
    from engine.manual_keys import remove_key

    data = request.get_json(silent=True) or {}
    rel = data.get("rel") or ""
    if not rel:
        return jsonify({"error": "missing_rel", "message": "缺少数据库路径"}), 400
    db_dir = _resolve_db_dir(data.get("db_dir"))
    try:
        out = remove_key(db_dir, rel)
    except Exception as e:
        return jsonify({"error": "remove_failed", "message": str(e)}), 500
    return jsonify(out)
