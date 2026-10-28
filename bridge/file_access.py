"""用户授权目录的只读检索。

三条硬约束：
1. 默认没有任何权限；只有调试台里手动添加并开启的目录才会被搜。
2. 检索完全在本机做，只有命中的片段才进 prompt——关闭时 0 token，
   开启但没命中也是 0 token，命中才付一次（≤700 字）。
3. 只读、只认文本后缀；密钥/日志/版本库目录永远跳过。
"""

import hashlib
import json
import math
import os
import re
import tempfile
import time

import config

MAX_FILES = 4000                 # 单个目录最多索引这么多文件
MAX_FILE_BYTES = 512 * 1024      # 再大的文件不当文本读
MAX_INDEX_AGE_S = 120            # 索引缓存有效期
MAX_SNIPPETS = 3
SNIPPET_CONTEXT_LINES = 2
MAX_SNIPPET_CHARS = 360          # 单条上限：要能放下整张引脚表
MAX_TOTAL_CHARS = 1000           # 进 prompt 的片段总字数上限

TEXT_EXTS = frozenset({
    ".md", ".txt", ".ino", ".h", ".hpp", ".c", ".cpp", ".py",
    ".json", ".toml", ".cmd", ".bat", ".ps1", ".yml", ".yaml", ".csv",
})
# 目录名小写后精确匹配
SKIP_DIRS = frozenset({
    ".git", ".svn", "node_modules", "__pycache__", "logs", ".venv",
    "venv", "py311", ".idea", ".vscode", "build", "dist",
})
# 文件名小写后包含任一串就跳过：宁可少读，不能把密钥读进 prompt
SKIP_NAME_PARTS = (
    "secret", ".env", "token", "apikey", "api_key", "credential",
    "password", "id_rsa", ".pem", ".key", ".pfx", ".p12", ".kdbx",
)


class PermissionStore:
    """~/.voice-ai/permissions.json 的读写。

    结构：{"schema_version": 1, "folders": [{id,path,label,enabled,added_at}]}
    """

    def __init__(self, path=None):
        self.path = path or config.FILE_PERMS_FILE

    # ---- 读写 --------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"schema_version": 1, "folders": []}
        if not isinstance(data, dict) or not isinstance(data.get("folders"), list):
            return {"schema_version": 1, "folders": []}
        return data

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Windows 上杀毒/搜索索引偶尔短暂占用刚写出的文件；这时重试一次。
        for attempt in (0, 1):
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                       prefix=".perms-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                return
            except PermissionError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                if attempt:
                    raise
                time.sleep(0.05)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # ---- 查询 --------------------------------------------------------
    def items(self):
        out = []
        for f in self._load()["folders"]:
            if not isinstance(f, dict):
                continue
            path = str(f.get("path") or "")
            out.append({
                "id": f.get("id"),
                "path": path,
                "label": f.get("label") or os.path.basename(path.rstrip("\\/")),
                "enabled": bool(f.get("enabled")),
                "exists": os.path.isdir(path),
                "added_at": f.get("added_at"),
            })
        return out

    def enabled_folders(self):
        return [it["path"] for it in self.items() if it["enabled"] and it["exists"]]

    # ---- 修改 --------------------------------------------------------
    def add(self, path, label="", confirm=False):
        """新增一个授权目录；同一个目录重复添加是幂等的。"""
        if not confirm:
            raise ValueError("confirm is required")
        raw = str(path or "").strip().strip('"')
        if not raw:
            raise ValueError("path is required")
        full = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(full):
            raise ValueError("not a folder: %s" % full)
        norm = os.path.normcase(os.path.realpath(full))
        data = self._load()
        for f in data["folders"]:
            if os.path.normcase(os.path.realpath(str(f.get("path") or ""))) == norm:
                return {"ok": True, "item": self._one(f), "existed": True}
        item = {
            "id": hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10],
            "path": full,
            "label": str(label or "").strip() or os.path.basename(full.rstrip("\\/")),
            "enabled": True,
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        data["folders"].append(item)
        self._save(data)
        return {"ok": True, "item": self._one(item), "existed": False}

    def set_enabled(self, folder_id, enabled):
        data = self._load()
        for f in data["folders"]:
            if f.get("id") == folder_id:
                f["enabled"] = bool(enabled)
                self._save(data)
                return {"ok": True, "item": self._one(f)}
        return None

    def remove(self, folder_id):
        data = self._load()
        keep = [f for f in data["folders"] if f.get("id") != folder_id]
        if len(keep) == len(data["folders"]):
            return False
        data["folders"] = keep
        self._save(data)
        return True

    def _one(self, f):
        path = str(f.get("path") or "")
        return {
            "id": f.get("id"),
            "path": path,
            "label": f.get("label") or os.path.basename(path.rstrip("\\/")),
            "enabled": bool(f.get("enabled")),
            "exists": os.path.isdir(path),
            "added_at": f.get("added_at"),
        }


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
_INDEX_CACHE = {}


def _index_for(root):
    key = os.path.normcase(os.path.abspath(root))
    hit = _INDEX_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < MAX_INDEX_AGE_S:
        return hit[1]
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS)
        for name in filenames:
            if len(entries) >= MAX_FILES:
                break
            low = name.lower()
            if os.path.splitext(low)[1] not in TEXT_EXTS:
                continue
            if any(part in low for part in SKIP_NAME_PARTS):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            entries.append({"path": rel, "full": full, "text": text})
        if len(entries) >= MAX_FILES:
            break
    _INDEX_CACHE[key] = (now, entries)
    return entries


def _terms(query):
    """把一句话拆成检索词：ASCII 按词，中文按二字切分（短词整段保留）。"""
    q = (query or "").lower()
    terms = re.findall(r"[a-z0-9_]{2,}", q)
    for run in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(run) <= 3:
            terms.append(run)
        terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    out = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return out[:24]


def _best_window(text, path, terms, weight):
    """在全文里滑窗打分，返回该文件最相关的一段。

    只看单行会把「文件头恰好提了一次关键词」当成命中；按 5 行窗口打分，
    连续的引脚表/参数表才会浮上来。每个文件只留一段，避免同一文件占满名额。
    """
    lines = text.splitlines()
    if not lines:
        return None
    span = SNIPPET_CONTEXT_LINES * 2 + 1
    name = path.lower()
    best = None
    start = 0
    while True:
        chunk_lines = lines[start:start + span]
        chunk = "\n".join(chunk_lines).lower()
        distinct = [t for t in terms if t in chunk]
        if distinct:
            hits = sum(min(chunk.count(t), 3) for t in distinct)
            score = sum(weight[t] for t in distinct) * 2 + hits * 0.5
            if any(t in name for t in terms):
                score += 2
            if best is None or score > best[0]:
                best = (score, start, chunk_lines)
        if start + span >= len(lines):
            break
        start += 1
    if best is None:
        return None
    score, start, chunk_lines = best
    body = "\n".join(l.rstrip() for l in chunk_lines).strip()[:MAX_SNIPPET_CHARS]
    if not body:
        return None
    return score, start + 1, min(len(lines), start + span), body


def search(query, folders, limit=MAX_SNIPPETS):
    """返回最多 limit 条片段：{"path","line","end_line","text"}。无命中返回 []。"""
    terms = _terms(query)
    if not terms:
        return []
    entries = []
    for folder in folders:
        entries.extend(_index_for(folder))
    if not entries:
        return []
    # 稀有词权重高：到处都出现的「怎么/几个」不该压过「引脚」
    df = {t: sum(1 for e in entries if t in e["text"].lower()) for t in terms}
    weight = {t: math.log(1 + len(entries) / (1 + df[t])) for t in terms}
    scored = []
    for entry in entries:
        found = _best_window(entry["text"], entry["path"], terms, weight)
        if found:
            scored.append((found[0], entry, found))
    scored.sort(key=lambda pair: -pair[0])
    out = []
    for score, entry, (_, first, last, body) in scored[:limit]:
        out.append({
            "path": entry["path"],
            "line": first,
            "end_line": last,
            "text": body,
            "score": round(score, 1),
        })
    return out


def build_block(snippets):
    """拼成一段带来源标注的提示词片段；超长截断。"""
    if not snippets:
        return ""
    parts = ["以下是用户授权目录中检索到的片段（只读）；与问题无关就忽略："]
    used = 0
    for sn in snippets:
        head = "--- %s 第 %d-%d 行\n" % (sn["path"], sn["line"], sn.get("end_line", sn["line"]))
        body = sn["text"]
        room = MAX_TOTAL_CHARS - used - len(head)
        if room <= 40:
            break
        if len(body) > room:
            body = body[:room] + "…"
        parts.append(head + body)
        used += len(head) + len(body)
    parts.append("---")
    return "\n".join(parts)


class FileAccess:
    """Turn 拼 prompt 时调用：授权目录有命中就前置一段，否则原样返回。"""

    def __init__(self, store=None):
        self.store = store or PermissionStore()

    def apply(self, messages, query):
        try:
            folders = self.store.enabled_folders()
        except Exception:
            return messages, None
        if not folders or not query:
            return messages, None
        try:
            snippets = search(query, folders)
        except Exception as exc:
            return messages, {"folders": len(folders), "hits": 0, "chars": 0,
                              "error": "%s: %s" % (type(exc).__name__, exc)}
        if not snippets:
            return messages, {"folders": len(folders), "hits": 0, "chars": 0}
        block = build_block(snippets)
        if not block or not messages or messages[-1].get("role") != "user":
            return messages, {"folders": len(folders), "hits": 0, "chars": 0}
        out = list(messages)
        out[-1] = dict(out[-1],
                       content=block + "\n\n用户问题：" + str(out[-1].get("content") or ""))
        return out, {
            "folders": len(folders),
            "hits": len(snippets),
            "chars": len(block),
            "paths": [sn["path"] for sn in snippets],
        }
