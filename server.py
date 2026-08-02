#!/usr/bin/env python3
"""Claude Code 세션 뷰어 — 로컬 웹앱 (표준 라이브러리만).

실행:  python3 server.py
브라우저가 자동으로 열린다. 세션 파일은 읽기 전용으로만 접근한다.
"""
import json
import os
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
SESSIONS_ROOT = (Path.home() / ".claude" / "projects").resolve()
SETTINGS_FILE = (Path.home() / ".claude" / "settings.json").resolve()

# 목록에서 무시할 노이즈 이벤트
NOISE_TYPES = {
    "queue-operation", "attachment", "file-history-snapshot",
    "mode", "ai-title", "last-prompt", "summary",
}

# (path, mtime, size) -> meta dict 캐시
_meta_cache = {}


def _iter_lines(path):
    """파일을 줄 단위로 읽어 (json_obj 또는 None) 를 yield. 깨진 줄은 None."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, json.JSONDecodeError):
                yield None


def scan_meta(path):
    """세션 파일에서 목록용 메타만 가볍게 추출."""
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    cached = _meta_cache.get(key)
    if cached is not None:
        return cached

    meta = {
        "file": str(path),
        "uuid": path.stem,
        "aiTitle": None,
        "lastPrompt": None,
        "cwd": None,
        "gitBranch": None,
        "version": None,
        "firstTs": None,
        "lastTs": None,
        "msgCount": 0,
        "models": [],
        "size": st.st_size,
        "mtime": st.st_mtime,
        "badLines": 0,
        "parentSessionId": None,   # /branch 로 분기된 경우 부모 세션
        "forkedAtMessage": None,
    }
    models = set()
    for obj in _iter_lines(path):
        if obj is None:
            meta["badLines"] += 1
            continue
        if meta["parentSessionId"] is None:
            ff = obj.get("forkedFrom")
            if isinstance(ff, dict) and ff.get("sessionId"):
                meta["parentSessionId"] = ff.get("sessionId")
                meta["forkedAtMessage"] = ff.get("messageUuid")
        t = obj.get("type")
        if t == "ai-title":
            meta["aiTitle"] = obj.get("aiTitle") or meta["aiTitle"]
            continue
        if t == "last-prompt":
            meta["lastPrompt"] = obj.get("lastPrompt") or meta["lastPrompt"]
            continue
        ts = obj.get("timestamp")
        if ts:
            if meta["firstTs"] is None:
                meta["firstTs"] = ts
            meta["lastTs"] = ts
        if t in ("user", "assistant"):
            meta["msgCount"] += 1
            if meta["cwd"] is None and obj.get("cwd"):
                meta["cwd"] = obj.get("cwd")
            if meta["gitBranch"] is None and obj.get("gitBranch"):
                meta["gitBranch"] = obj.get("gitBranch")
            if meta["version"] is None and obj.get("version"):
                meta["version"] = obj.get("version")
            m = (obj.get("message") or {}).get("model")
            if m:
                models.add(m)
    meta["models"] = sorted(models)
    _meta_cache[key] = meta
    return meta


def list_sessions():
    """SESSIONS_ROOT 하위 모든 .jsonl 의 메타 목록."""
    out = []
    if not SESSIONS_ROOT.exists():
        return out
    for sub in sorted(SESSIONS_ROOT.iterdir()):
        if not sub.is_dir():
            continue
        try:
            files = sorted(sub.glob("*.jsonl"))
        except PermissionError:
            continue
        for fp in files:
            try:
                out.append(scan_meta(fp))
            except (PermissionError, OSError):
                continue
    return out


def _normalize_content(content):
    """message.content (문자열 또는 리스트) 를 블록 배열로 정규화."""
    blocks = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"kind": "text", "text": content})
        return blocks
    if not isinstance(content, list):
        return blocks
    for item in content:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                blocks.append({"kind": "text", "text": item})
            continue
        it = item.get("type")
        if it == "text":
            txt = item.get("text", "")
            if txt.strip():
                blocks.append({"kind": "text", "text": txt})
        elif it == "thinking":
            blocks.append({"kind": "thinking",
                           "text": item.get("thinking") or item.get("text") or ""})
        elif it == "tool_use":
            blocks.append({"kind": "tool_use",
                           "name": item.get("name", "?"),
                           "input": item.get("input", {})})
        elif it == "tool_result":
            c = item.get("content")
            if isinstance(c, list):
                parts = []
                for p in c:
                    if isinstance(p, dict):
                        parts.append(p.get("text", "") if p.get("type") == "text"
                                     else f"[{p.get('type')}]")
                    else:
                        parts.append(str(p))
                c = "\n".join(parts)
            blocks.append({"kind": "tool_result",
                           "text": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False),
                           "is_error": bool(item.get("is_error"))})
        elif it == "image":
            blocks.append({"kind": "image"})
    return blocks


def _find_session_file(sid):
    """sessionId 로 .jsonl 파일 경로 찾기 (전 디렉토리 탐색)."""
    if not sid:
        return None
    if not SESSIONS_ROOT.exists():
        return None
    for sub in SESSIONS_ROOT.iterdir():
        if not sub.is_dir():
            continue
        cand = sub / f"{sid}.jsonl"
        if cand.is_file():
            return cand
    return None


def _session_line_uuids(path):
    """파일의 모든 라인 uuid 집합 (상속 메시지 식별용)."""
    out = set()
    for obj in _iter_lines(path):
        if obj and obj.get("uuid"):
            out.add(obj["uuid"])
    return out


def parse_session(path):
    """단일 세션 파일을 타임라인 이벤트 배열로 파싱.

    분기(/branch) 세션이면 부모와 uuid를 공유하는 '상속' 메시지를 잘라내고
    분기점 이후 메시지만 반환한다. 토큰/도구 통계도 함께 계산한다.
    """
    meta = scan_meta(path)
    parent_uuids = set()
    parent_meta = None
    if meta.get("parentSessionId"):
        pf = _find_session_file(meta["parentSessionId"])
        if pf:
            parent_uuids = _session_line_uuids(pf)
            try:
                parent_meta = scan_meta(pf)
            except OSError:
                parent_meta = None

    events = []
    bad = 0
    inherited = 0
    started = False  # 분기점 이후 시작 여부
    stats = {"user": 0, "assistant": 0,
             "tokens": {"input": 0, "output": 0, "cacheRead": 0, "cacheCreate": 0},
             "tools": {}, "firstTs": None, "lastTs": None}
    for obj in _iter_lines(path):
        if obj is None:
            bad += 1
            continue
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        # 분기 세션: 부모와 공유하는(=상속) 메시지는 분기점 전까지만 존재. 건너뛴다.
        if parent_uuids and not started:
            if obj.get("uuid") in parent_uuids:
                inherited += 1
                continue
            started = True
        msg = obj.get("message") or {}
        blocks = _normalize_content(msg.get("content"))
        if not blocks:
            continue
        ts = obj.get("timestamp")
        if ts:
            if stats["firstTs"] is None:
                stats["firstTs"] = ts
            stats["lastTs"] = ts
        stats[t] = stats.get(t, 0) + 1
        if t == "assistant":
            u = msg.get("usage") or {}
            stats["tokens"]["input"] += u.get("input_tokens", 0) or 0
            stats["tokens"]["output"] += u.get("output_tokens", 0) or 0
            stats["tokens"]["cacheRead"] += u.get("cache_read_input_tokens", 0) or 0
            stats["tokens"]["cacheCreate"] += u.get("cache_creation_input_tokens", 0) or 0
        for b in blocks:
            if b["kind"] == "tool_use":
                stats["tools"][b["name"]] = stats["tools"].get(b["name"], 0) + 1
        events.append({
            "role": t,
            "ts": ts,
            "model": msg.get("model"),
            "blocks": blocks,
            "isSidechain": bool(obj.get("isSidechain")),
            "isMeta": bool(obj.get("isMeta")),   # 하니스 주입(스킬 본문/시스템 컨텍스트)
        })

    fork = None
    if meta.get("parentSessionId"):
        fork = {
            "parentSessionId": meta["parentSessionId"],
            "parentTitle": (parent_meta or {}).get("aiTitle") if parent_meta else None,
            "parentFile": str(_find_session_file(meta["parentSessionId"]) or ""),
            "inheritedCount": inherited,
            "found": bool(parent_uuids),
        }
    return {"meta": meta, "events": events, "badLines": bad,
            "stats": stats, "fork": fork}


def _iter_visible_messages(path, meta=None):
    """타임라인에 실제로 표시되는 user/assistant 메시지 obj 만 yield.

    분기(/branch) 세션이면 부모와 uuid 를 공유하는 '상속' 메시지를 건너뛴다.
    parse_session 의 트리밍 규칙과 동일하게 맞춰, 전체 검색 건수가 세션 내
    검색(표시되는 메시지만 대상)과 어긋나지 않게 한다.
    """
    if meta is None:
        meta = scan_meta(path)
    parent_uuids = set()
    if meta.get("parentSessionId"):
        pf = _find_session_file(meta["parentSessionId"])
        if pf:
            parent_uuids = _session_line_uuids(pf)
    started = False
    for obj in _iter_lines(path):
        if obj is None or obj.get("type") not in ("user", "assistant"):
            continue
        if parent_uuids and not started:
            if obj.get("uuid") in parent_uuids:
                continue
            started = True
        yield obj


def _searchable_texts(obj):
    """한 메시지에서 세션 내 검색이 훑는 텍스트 조각들을 반환.

    index.html 의 세션 내 검색은 렌더된 타임라인 본문(본문 텍스트 · thinking ·
    도구 호출 이름/입력 JSON · 도구 결과)을 대상으로 한다. 전체 검색 건수를
    맞추기 위해 같은 대상에서 텍스트를 뽑는다.
    """
    texts = []
    for b in _normalize_content((obj.get("message") or {}).get("content")):
        kind = b["kind"]
        if kind in ("text", "thinking", "tool_result"):
            if b.get("text"):
                texts.append(b["text"])
        elif kind == "tool_use":
            if b.get("name"):
                texts.append(b["name"])
            try:
                texts.append(json.dumps(b.get("input", {}), ensure_ascii=False, indent=2))
            except (TypeError, ValueError):
                texts.append(str(b.get("input")))
    return texts


def search_all(term):
    """전 세션 텍스트를 훑어 매칭 세션 + 스니펫 반환.

    건수(hits)는 세션 내 검색과 동일하게 '출현 횟수'로 센다 (블록당 1회가 아님).
    """
    term_l = term.lower()
    results = []
    for meta in list_sessions():
        hits = 0
        snippet = None
        for obj in _iter_visible_messages(Path(meta["file"]), meta):
            for txt in _searchable_texts(obj):
                low = txt.lower()
                c = low.count(term_l)
                if not c:
                    continue
                hits += c
                if snippet is None:
                    idx = low.find(term_l)
                    start = max(0, idx - 60)
                    snippet = ("…" if start else "") + txt[start:idx + 120].replace("\n", " ")
        if hits:
            r = dict(meta)
            r["hits"] = hits
            r["snippet"] = snippet
            results.append(r)
    results.sort(key=lambda r: r["hits"], reverse=True)
    return results


def _safe_session_path(raw):
    """file 파라미터를 SESSIONS_ROOT 하위로 제한 (경로 탈출 차단)."""
    p = Path(raw).resolve()
    try:
        p.relative_to(SESSIONS_ROOT)
    except ValueError:
        return None
    if not p.is_file() or p.suffix != ".jsonl":
        return None
    return p


def touch_sessions(files):
    """세션 파일들의 mtime 을 현재로 갱신(내용 불변) → Claude Code 자동삭제 시계 리셋."""
    results = []
    for raw in files or []:
        p = _safe_session_path(raw)
        if not p:
            results.append({"file": raw, "ok": False, "error": "invalid file"})
            continue
        try:
            os.utime(p, None)  # atime/mtime 을 현재로. 파일 내용은 건드리지 않는다.
            results.append({"file": raw, "ok": True, "mtime": p.stat().st_mtime})
        except OSError as e:
            results.append({"file": raw, "ok": False, "error": str(e)})
    return {"results": results}


def delete_sessions(files):
    """세션 파일들을 영구 삭제한다. 형제 사이드카(<uuid>/, subagents 기록)도 함께 제거."""
    results = []
    for raw in files or []:
        p = _safe_session_path(raw)
        if not p:
            results.append({"file": raw, "ok": False, "error": "invalid file"})
            continue
        try:
            os.remove(p)
            sidecar = p.with_suffix("")  # .../<uuid>.jsonl → .../<uuid>
            if sidecar.is_dir():
                try:
                    sidecar.relative_to(SESSIONS_ROOT)  # 경로 탈출 방지
                    shutil.rmtree(sidecar)
                except ValueError:
                    pass
            results.append({"file": raw, "ok": True})
        except OSError as e:
            results.append({"file": raw, "ok": False, "error": str(e)})
    return {"results": results}


# ---------- 설정(settings.json) 읽기/쓰기 ----------
# cc-explorer 가 의미 있게 쓰는 설정: 파일/키에 없어도 드로어에 기본값으로 항상 노출하고
# (파일이 없으면 생성해서) 편집을 허용한다.
#   - cleanupPeriodDays: 세션 자동 삭제 기간(일). 삭제 카운트다운 배지의 기준값. 기본 30.
#   - showThinkingSummaries: thinking 기록 여부(뷰어가 thinking 블록을 표시). 켜기 번거로워 기본 노출.
KNOWN_SETTINGS = [
    {"key": "cleanupPeriodDays", "type": "number", "default": 30},
    {"key": "showThinkingSummaries", "type": "bool", "default": False},
]


def _known_default_items(present):
    """파일에 없는 알려진 설정을 기본값 + absent 로 항목화."""
    out = []
    for spec in KNOWN_SETTINGS:
        if (spec["key"],) not in present:
            out.append({"path": [spec["key"]], "label": spec["key"],
                        "value": spec["default"], "type": spec["type"], "absent": True})
    return out


def _coerce_int(value):
    """number 설정 입력을 정수로 강제. 실패 시 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _flatten_settings(obj, path=None):
    """중첩 dict 를 leaf 리스트로 평탄화. 각 항목: {path(list), label, value, type}."""
    path = path or []
    out = []
    for k, v in obj.items():
        p = path + [k]
        if isinstance(v, dict):
            out.extend(_flatten_settings(v, p))
        else:
            out.append({
                "path": p,
                "label": ".".join(p),
                "value": v,
                "type": ("bool" if isinstance(v, bool)
                         else "number" if isinstance(v, (int, float))
                         else "string" if isinstance(v, str)
                         else "other"),
            })
    return out


def read_settings():
    exists = SETTINGS_FILE.is_file()
    if exists:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            return {"path": str(SETTINGS_FILE), "exists": True,
                    "error": f"파싱 실패: {e}", "items": []}
        if not isinstance(data, dict):
            return {"path": str(SETTINGS_FILE), "exists": True,
                    "error": "최상위가 객체가 아님", "items": []}
        items = _flatten_settings(data)
    else:
        # 파일이 없어도 알려진 설정은 기본값으로 노출한다(생성 허용).
        items = []
    present = {tuple(it["path"]) for it in items}
    items.extend(_known_default_items(present))
    return {"path": str(SETTINGS_FILE), "exists": exists, "items": items}


def set_setting(path_list, value):
    """설정 leaf 를 변경한다.

    - 기존 leaf: 같은 타입(bool/str/number)으로만 변경.
    - 없는 leaf: 알려진 최상위 설정(KNOWN_SETTINGS)만 타입에 맞게 생성.
    - 파일이 없어도 알려진 설정이면 새로 만든다. .bak 백업 후 다른 키를 보존하며 기록.
    """
    if not isinstance(path_list, list) or not path_list:
        return {"ok": False, "error": "잘못된 경로"}
    known = None
    if len(path_list) == 1:
        known = next((s for s in KNOWN_SETTINGS if s["key"] == path_list[0]), None)

    file_exists = SETTINGS_FILE.is_file()
    if file_exists:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            return {"ok": False, "error": f"읽기 실패: {e}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "최상위가 객체가 아님"}
    elif known:
        data = {}
    else:
        return {"ok": False, "error": "설정 파일 없음"}

    # 경로를 따라가며 leaf 컨테이너 확인
    node = data
    for k in path_list[:-1]:
        if not isinstance(node, dict) or k not in node:
            return {"ok": False, "error": "존재하지 않는 키"}
        node = node[k]
    leaf = path_list[-1]
    if not isinstance(node, dict):
        return {"ok": False, "error": "존재하지 않는 키"}

    if leaf in node:
        existing = node[leaf]
        if isinstance(existing, bool):
            if not isinstance(value, bool):
                return {"ok": False, "error": "boolean 값이어야 함"}
        elif isinstance(existing, str):
            if not isinstance(value, str):
                return {"ok": False, "error": "문자열 값이어야 함"}
        elif isinstance(existing, (int, float)):
            v = _coerce_int(value)
            if v is None:
                return {"ok": False, "error": "정수 값이어야 함"}
            value = v
        else:
            return {"ok": False, "error": "편집 불가 타입"}
    else:
        # 없는 키: 알려진 설정만 타입에 맞게 생성 허용
        if not known:
            return {"ok": False, "error": "존재하지 않는 키"}
        if known["type"] == "bool":
            if not isinstance(value, bool):
                return {"ok": False, "error": "boolean 값이어야 함"}
        elif known["type"] == "number":
            v = _coerce_int(value)
            if v is None:
                return {"ok": False, "error": "정수 값이어야 함"}
            value = v

    # cleanupPeriodDays 범위 검증(1 이상)
    if path_list == ["cleanupPeriodDays"] and (not isinstance(value, int) or value < 1):
        return {"ok": False, "error": "1 이상의 정수여야 함"}

    node[leaf] = value
    try:
        if file_exists:
            # 쓰기 직전 백업 1부
            SETTINGS_FILE.with_suffix(".json.bak").write_text(
                SETTINGS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"쓰기 실패: {e}"}
    return {"ok": True, "path": path_list, "value": value}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self._send_file(HERE / "index.html", "text/html; charset=utf-8")
            if u.path == "/api/projects":
                return self._send_json({
                    "root": str(SESSIONS_ROOT),
                    "exists": SESSIONS_ROOT.exists(),
                    "sessions": list_sessions(),
                })
            if u.path == "/api/session":
                raw = (q.get("file") or [""])[0]
                p = _safe_session_path(raw)
                if not p:
                    return self._send_json({"error": "invalid file"}, 400)
                return self._send_json(parse_session(p))
            if u.path == "/api/search":
                term = (q.get("q") or [""])[0].strip()
                if len(term) < 2:
                    return self._send_json({"results": [], "note": "검색어 2자 이상"})
                return self._send_json({"results": search_all(term)})
            if u.path == "/api/settings":
                return self._send_json(read_settings())
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_POST(self):
        u = urlparse(self.path)
        try:
            try:
                body = self._read_json_body()
            except ValueError:
                return self._send_json({"ok": False, "error": "잘못된 JSON"}, 400)
            if u.path == "/api/settings/set":
                res = set_setting(body.get("path"), body.get("value"))
                return self._send_json(res, 200 if res.get("ok") else 400)
            if u.path == "/api/touch":
                return self._send_json(touch_sessions(body.get("files")))
            if u.path == "/api/delete":
                return self._send_json(delete_sessions(body.get("files")))
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(e)}, 500)


def main():
    if not SESSIONS_ROOT.exists():
        print(f"[!] 세션 폴더가 없습니다: {SESSIONS_ROOT}", file=sys.stderr)
    port = 8765
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("[!] 사용 가능한 포트를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print(f"Claude Code 세션 뷰어 → {url}")
    print(f"  세션 루트: {SESSIONS_ROOT}")
    print("  종료: Ctrl+C")
    if "--no-open" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
