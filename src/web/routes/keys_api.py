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


def _detect_dirs():
    try:
        from engine.utils import find_all_wechat_data_dirs
        return find_all_wechat_data_dirs()
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
    dirs = _detect_dirs()
    return jsonify({"dirs": dirs, "current": _resolve_db_dir()})


@keys_bp.route("/status", methods=["GET"])
def keys_status():
    from engine.manual_keys import status

    db_dir = _resolve_db_dir(request.args.get("db_dir"))
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": "未找到微信数据目录（db_storage），请手动选择",
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
                        "message": "未找到微信数据目录（db_storage）"}), 400
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
                        "message": "未找到微信数据目录（db_storage）"}), 400
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
