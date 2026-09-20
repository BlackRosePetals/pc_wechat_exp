"""手动输入数据库密钥。

使用场景：用户手上已经有密钥（别的工具提取过、旧备份、或从别处获得），
不需要再跑自动提取流程，直接把密钥粘进来，让「解密 / 备份 / 导出 / 查看器」
等功能直接可用。

支持多个数据库、多个密钥。每行一条，# 开头为注释。支持格式：

    <64位hex>                        # 通用密钥：自动匹配到用了它的数据库
    <96位hex>                        # 微信 x'<64位key><32位salt>' 形式，按 salt 精确匹配
    message_0.db = <64位hex>         # 指定数据库（文件名或相对路径均可）
    message/message_0.db: <64位hex>  # 相对路径，/ 与 \\ 都行
    <32位salt> = <64位hex>           # 指定 salt

匹配是否成功由 SQLCipher 4 的 page1 HMAC 实测校验，不会把错密钥写进配置。
"""
import os
import re

from engine.config_file import get_db_keys, set_db_keys

HEX64_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
HEX96_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{96}(?![0-9a-fA-F])")
HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
GROUPED_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2,8}[\s\-]+){3,}[0-9a-fA-F]{2,8}")


def mask_key(key_hex):
    """密钥打码显示，避免界面上/日志里出现完整密钥。"""
    if not key_hex or len(key_hex) < 12:
        return "***"
    return key_hex[:6] + "..." + key_hex[-4:]


def _strip_noise(text):
    t = text.strip()
    for ch in [chr(39), chr(34), "\u2018", "\u2019", "\u201c", "\u201d"]:
        t = t.replace(ch, "")
    t = re.sub(r"\bx'", "", t)
    t = re.sub(r"\b0x", "", t, flags=re.IGNORECASE)
    return t.strip()


def parse_entries(text):
    """解析用户粘贴的密钥文本。

    Returns: [{"raw", "key", "salt_hint", "db_hint", "error"}]
    """
    entries = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        cleaned = _strip_noise(line)
        key_hex = None
        salt_hint = None
        hint = cleaned

        m96 = HEX96_RE.search(cleaned)
        m64 = HEX64_RE.search(cleaned)
        grouped = None
        if not m96 and not m64:
            gm = GROUPED_HEX_RE.search(cleaned)
            if gm:
                joined = re.sub(r"[\s\-]+", "", gm.group(0))
                if len(joined) >= 64 and all(c in "0123456789abcdefABCDEF" for c in joined):
                    grouped = (gm.group(0), joined)

        if m96:
            key_hex = m96.group(0)[:64]
            salt_hint = m96.group(0)[64:].lower()
            hint = cleaned.replace(m96.group(0), " ")
        elif m64:
            key_hex = m64.group(0)
            hint = cleaned.replace(m64.group(0), " ")
        elif grouped:
            token, joined = grouped
            # 形如 x'<key><salt>'：优先当作 key+salt（96 位），否则当作纯 key
            key_hex = joined[:64]
            if len(joined) >= 96:
                salt_hint = joined[64:96].lower()
            hint = cleaned.replace(token, " ")

        db_hint = None
        if hint:
            h = hint.strip().strip("=:;,|\t ").strip()
            h = h.strip("=:;,|\t ").strip()
            if h:
                if not salt_hint:
                    m32 = HEX32_RE.fullmatch(h)
                    if m32:
                        salt_hint = h.lower()
                        h = ""
                if h:
                    db_hint = h

        entry = {"raw": raw_line.strip(), "key": key_hex, "salt_hint": salt_hint,
                 "db_hint": db_hint, "error": None}
        if key_hex is None:
            entry["error"] = "未识别到 64 位十六进制密钥"
        elif len(key_hex) != 64:
            entry["error"] = "密钥长度不是 64 位十六进制"
        elif db_hint and not db_hint.lower().endswith(".db") and "." not in db_hint:
            # 提示串既不像数据库名也不是 salt：忽略它，按通用密钥处理
            entry["db_hint"] = None
        entries.append(entry)
    return entries


def _load_db_keys():
    try:
        keys = get_db_keys() or {}
    except Exception:
        return {}
    out = {}
    for k, v in keys.items():
        if isinstance(v, str) and len(v) == 64:
            out[str(k)] = v
    return out


def scan_databases(db_dir, with_pages=True):
    """列出 db_dir 下所有数据库及其密钥状态。

    Returns: [{"rel", "rel_norm", "name", "path", "size_mb", "salt", "page1",
               "key", "verified"}]
    """
    from engine.services.wechat_key_extract import collect_db_files, verify_enc_key

    if not db_dir or not os.path.isdir(db_dir):
        return []
    db_files, _salt_to_dbs = collect_db_files(db_dir)
    configured = _load_db_keys()
    norm_configured = {}
    for k, v in configured.items():
        norm_configured[k.replace("\\", "/").lower()] = v
        norm_configured[os.path.basename(k).lower()] = v

    out = []
    for rel, path, size, salt_hex, page1 in db_files:
        rel_norm = rel.replace("\\", "/")
        key = (configured.get(rel) or configured.get(rel_norm)
               or norm_configured.get(rel_norm.lower())
               or norm_configured.get(os.path.basename(rel).lower()))
        # 少数数据库本身就是明文 SQLite（未加密），不需要密钥
        plain = page1[:16] == b"SQLite format 3" + bytes(1)
        verified = plain
        if key and not plain:
            try:
                verified = bool(verify_enc_key(bytes.fromhex(key), page1))
            except ValueError:
                verified = False
        out.append({
            "rel": rel,
            "rel_norm": rel_norm,
            "name": os.path.basename(rel),
            "path": path,
            "size_mb": round(size / 1024 / 1024, 1),
            "salt": salt_hex,
            "page1": page1 if with_pages else None,
            "key": key or "",
            "key_masked": mask_key(key) if key else "",
            "verified": verified,
            "plain": plain,
        })
    out.sort(key=lambda d: d["rel_norm"].lower())
    return out


def status(db_dir):
    """密钥覆盖情况汇总（供界面展示）。"""
    dbs = scan_databases(db_dir, with_pages=False)
    total = len(dbs)
    plain = sum(1 for d in dbs if d.get("plain"))
    verified = sum(1 for d in dbs if d["verified"] and not d.get("plain"))
    wrong = sum(1 for d in dbs if d["key"] and not d["verified"] and not d.get("plain"))
    missing = total - verified - wrong - plain
    return {
        "dbDir": db_dir or "",
        "total": total,
        "verified": verified,
        "missing": missing,
        "invalid": wrong,
        "plain": plain,
        "databases": [{"rel": d["rel"], "name": d["name"], "sizeMb": d["size_mb"],
                       "salt": d["salt"][:16], "hasKey": bool(d["key"]),
                       "verified": d["verified"], "keyMasked": d["key_masked"],
                       "plain": bool(d.get("plain"))}
                      for d in dbs],
    }

def _resolve_rel(dbs, hint):
    """把用户写的数据库提示（文件名或相对路径）解析成真实相对路径。"""
    if not hint:
        return None
    h = hint.replace("\\", "/").strip().strip("./").lower()
    for d in dbs:
        if d["rel_norm"].lower() == h:
            return d["rel"]
    base = os.path.basename(h)
    for d in dbs:
        if d["name"].lower() == base:
            return d["rel"]
    return None


def match_entries(db_dir, entries):
    """用 HMAC 实测把用户输入的密钥与数据库配对（不写配置）。"""
    from engine.services.wechat_key_extract import verify_enc_key

    dbs = scan_databases(db_dir, with_pages=True)
    by_norm = {d["rel_norm"].lower(): d for d in dbs}
    by_base = {}
    by_salt = {}
    for d in dbs:
        by_base.setdefault(d["name"].lower(), []).append(d)
        by_salt.setdefault(d["salt"].lower(), []).append(d)

    results = []
    for lineno, e in enumerate(entries, 1):
        # 不回传原始输入（可能整行就是密钥），只给行号供界面定位
        item = {"line": lineno,
                "keyMasked": mask_key(e["key"]) if e["key"] else "",
                "dbHint": e["db_hint"] or "",
                "saltHint": (e["salt_hint"] or "")[:16],
                "status": "invalid", "message": "", "matched": []}
        if e["error"] or not e["key"]:
            item["message"] = e["error"] or "缺少密钥"
            results.append(item)
            continue
        try:
            key_bytes = bytes.fromhex(e["key"])
        except ValueError:
            item["message"] = "密钥不是合法的十六进制字符串"
            results.append(item)
            continue

        if e["db_hint"]:
            rel = _resolve_rel(dbs, e["db_hint"])
            if not rel:
                item["status"] = "db_not_found"
                item["message"] = "没找到数据库: " + e["db_hint"]
                results.append(item)
                continue
            candidates = [d for d in dbs if d["rel"] == rel]
        elif e["salt_hint"]:
            candidates = by_salt.get(e["salt_hint"].lower()) or []
            if not candidates:
                item["status"] = "db_not_found"
                item["message"] = "没有 salt 以 " + e["salt_hint"][:16] + " 开头的数据库"
                results.append(item)
                continue
        else:
            candidates = dbs

        matched = []
        for d in candidates:
            if not d["page1"]:
                continue
            try:
                ok = verify_enc_key(key_bytes, d["page1"])
            except Exception:
                ok = False
            if ok:
                matched.append({"rel": d["rel"], "name": d["name"],
                                "salt": d["salt"][:16], "sizeMb": d["size_mb"]})
        if matched:
            item["status"] = "matched"
            item["matched"] = matched
            item["message"] = "HMAC 校验通过，匹配 %d 个数据库" % len(matched)
        else:
            item["status"] = "no_match"
            scope = "所选数据库" if (e["db_hint"] or e["salt_hint"]) else "本机任何数据库"
            item["message"] = "HMAC 校验未通过：该密钥与" + scope + "不匹配"
        results.append(item)
    return results


def apply_entries(db_dir, entries, force=False):
    """匹配 + 保存密钥。

    force=True 时，明确指定了数据库但校验未通过的密钥也会被保存（用户自担）。
    Returns: {"results", "saved", "savedDbs", "status"}
    """
    results = match_entries(db_dir, entries)
    dbs = scan_databases(db_dir, with_pages=False)
    pairs = {}
    for e, r in zip(entries, results):
        if r["status"] == "matched":
            for m in r["matched"]:
                pairs[m["rel"]] = e["key"]
        elif force and e["db_hint"] and r["status"] in ("no_match",):
            # 数据库文件不在本机时也允许按用户写的名字强制保存
            rel = _resolve_rel(dbs, e["db_hint"]) or e["db_hint"].replace("/", "\\")
            if rel:
                pairs[rel] = e["key"]
                r["status"] = "forced"
                r["message"] = "未通过校验，已按指定数据库强制保存：" + rel
    saved = 0
    if pairs:
        set_db_keys(pairs, db_dir=db_dir)
        saved = len(pairs)
    return {"results": results, "saved": saved,
            "savedDbs": sorted(pairs.keys()), "status": status(db_dir)}


def remove_key(db_dir, rel):
    """删除某个数据库已保存的密钥。"""
    from engine.config_file import remove_db_keys
    n = remove_db_keys([rel])
    return {"removed": n, "status": status(db_dir)}


def format_results(results):
    """把匹配结果格式化成终端文本。"""
    lines = []
    for i, r in enumerate(results, 1):
        if r["status"] == "matched":
            names = ", ".join(m["rel"] for m in r["matched"])
            lines.append("  [%d] %s  ->  %s" % (i, r["keyMasked"], names))
        else:
            lines.append("  [%d] %s  ->  %s (%s)" % (i, r["keyMasked"],
                                                     r["status"], r["message"]))
    return "\n".join(lines)
