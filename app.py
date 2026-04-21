"""FastAPI web server for CF Analyzer (Web version). Run: python app.py"""

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
import crypto
import db
from auth import router as auth_router, require_user, require_admin, get_optional_user

_CF_API = "https://codeforces.com/api"
_CF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="CF Analyzer Web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)
app.include_router(auth_router)

STATIC_DIR = Path(__file__).parent / "static"
SHARED_CF_PROFILE = Path(__file__).parent / "data" / "cf_browser_profiles" / "shared"
CF_COOKIES_FILE = Path(__file__).parent / "data" / "cf_cookies.json"
CHROME_SEMAPHORE = asyncio.Semaphore(config.MAX_CHROME_INSTANCES)


_main_loop = None

@app.on_event("startup")
async def startup():
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    await db.init_db()


@app.on_event("shutdown")
async def shutdown():
    await db.dispose_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "•" * 8 + s[-4:]


def _is_masked(s: str) -> bool:
    return "•" in (s or "")


def _sse_thread(gen_fn) -> StreamingResponse:
    """Run a synchronous generator in a thread and stream as SSE."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def worker():
        try:
            for ev in gen_fn():
                loop.call_soon_threadsafe(q.put_nowait, ev)
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, {"type": "fail", "text": str(e)})
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=worker, daemon=True).start()

    async def stream():
        while True:
            ev = await q.get()
            if ev is None:
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sync_db(coro):
    """Call an async DB function from a sync thread."""
    future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
    return future.result(timeout=30)


async def build_user_cfg(user_id: int) -> dict:
    """Build a config dict (same shape as old config.yaml) from user_configs table."""
    uc = await db.get_user_config(user_id)
    claude_key = crypto.decrypt(uc.claude_api_key_enc) if uc.claude_api_key_enc else ""
    ds_key = crypto.decrypt(uc.deepseek_api_key_enc) if uc.deepseek_api_key_enc else ""

    # Fallback to server defaults if user hasn't configured keys
    if not claude_key and config.DEFAULT_LLM == "claude" and config.DEFAULT_LLM_KEY:
        claude_key = config.DEFAULT_LLM_KEY
    if not ds_key and config.DEFAULT_LLM == "deepseek" and config.DEFAULT_LLM_KEY:
        ds_key = config.DEFAULT_LLM_KEY

    return {
        "claude": {
            "api_key": claude_key,
            "base_url": uc.claude_base_url or "",
            "model": uc.claude_model or "claude-sonnet-4-6",
        },
        "deepseek": {
            "api_key": ds_key,
            "model": uc.deepseek_model or "deepseek-chat",
        },
        "settings": {
            "active_llm": uc.active_llm or "claude",
            "same_language_only": uc.same_language_only if uc.same_language_only is not None else True,
            "request_delay": uc.request_delay or 2.0,
            "compare_mode": uc.compare_mode or "auto",
            "compare_target": uc.compare_target or "",
            "compare_targets": uc.compare_targets or [],
        },
        "lgm_handles": uc.lgm_handles or [],
        "codeforces": {"handle": uc.cf_handle or ""},
    }


# ---------------------------------------------------------------------------
# SSE analysis endpoint (analyzer.py path)
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    problem: str
    user_code: str
    user_lang: str


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest, user: db.User = Depends(require_user)):
    cfg = await build_user_cfg(user.id)
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def _push(event):
        loop.call_soon_threadsafe(q.put_nowait, event)

    def worker():
        try:
            from analyzer import run_analysis
            for event in run_analysis(req.problem, req.user_code, req.user_lang,
                                     user_cfg=cfg, user_id=user.id,
                                     live_progress=_push):
                _push(event)
        except Exception as e:
            _push({"type": "error", "message": str(e)})
        finally:
            _push(None)

    threading.Thread(target=worker, daemon=True).start()

    async def event_stream():
        while True:
            event = await q.get()
            if event is None:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# Analysis CRUD
# ---------------------------------------------------------------------------

@app.get("/api/analyses")
async def list_analyses(user: db.User = Depends(require_user)):
    return await db.list_analyses(user.id)


@app.get("/api/analyses/{aid}")
async def get_analysis(aid: str, user: db.User = Depends(require_user)):
    data = await db.get_analysis(aid, user_id=user.id)
    if not data:
        raise HTTPException(404, "Not found")
    return data


class NotesBody(BaseModel):
    notes: str


@app.put("/api/analyses/{aid}/notes")
async def save_notes(aid: str, body: NotesBody, user: db.User = Depends(require_user)):
    if not await db.get_analysis(aid, user_id=user.id):
        raise HTTPException(404, "Not found")
    await db.update_notes(aid, body.notes, user_id=user.id)
    return {"ok": True}


class ImportanceBody(BaseModel):
    importance: str


@app.put("/api/analyses/{aid}/importance")
async def save_importance(aid: str, body: ImportanceBody, user: db.User = Depends(require_user)):
    if body.importance not in ("", "important", "review", "mastered"):
        raise HTTPException(400, "无效的重要度")
    if not await db.get_analysis(aid, user_id=user.id):
        raise HTTPException(404, "Not found")
    await db.update_importance(aid, body.importance, user_id=user.id)
    return {"ok": True}


@app.delete("/api/analyses/{aid}")
async def delete_analysis(aid: str, user: db.User = Depends(require_user)):
    await db.delete_analysis(aid, user_id=user.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config(user: db.User = Depends(require_user)):
    uc = await db.get_user_config(user.id)
    claude_key = crypto.decrypt(uc.claude_api_key_enc) if uc.claude_api_key_enc else ""
    ds_key = crypto.decrypt(uc.deepseek_api_key_enc) if uc.deepseek_api_key_enc else ""
    return {
        "cf_handle":          uc.cf_handle or "",
        "cf_profile_exists":  CF_COOKIES_FILE.exists() and CF_COOKIES_FILE.stat().st_size > 10,
        "active_llm":         uc.active_llm or "claude",
        "claude_api_key":     _mask(claude_key),
        "claude_base_url":    uc.claude_base_url or "",
        "claude_model":       uc.claude_model or "claude-sonnet-4-6",
        "deepseek_api_key":   _mask(ds_key),
        "deepseek_model":     uc.deepseek_model or "deepseek-chat",
        "same_language_only": uc.same_language_only if uc.same_language_only is not None else True,
        "lgm_handles":        uc.lgm_handles or [],
        "lgm_profiles":       uc.lgm_profiles or {},
        "compare_mode":       uc.compare_mode or "auto",
        "compare_target":     uc.compare_target or "",
        "compare_targets":    uc.compare_targets or [],
    }


class LLMConfig(BaseModel):
    api_key:  Optional[str] = None
    base_url: Optional[str] = None
    model:    Optional[str] = None


@app.put("/api/config/llm")
async def save_llm(body: LLMConfig, user: db.User = Depends(require_user)):
    updates = {}
    if body.api_key and not _is_masked(body.api_key):
        updates["claude_api_key_enc"] = crypto.encrypt(body.api_key)
    if body.base_url is not None:
        updates["claude_base_url"] = body.base_url
    if body.model:
        updates["claude_model"] = body.model
    if updates:
        await db.update_user_config(user.id, **updates)
    return {"ok": True}


class DeepSeekConfig(BaseModel):
    api_key: Optional[str] = None
    model:   Optional[str] = None


@app.put("/api/config/deepseek")
async def save_deepseek(body: DeepSeekConfig, user: db.User = Depends(require_user)):
    updates = {}
    if body.api_key and not _is_masked(body.api_key):
        updates["deepseek_api_key_enc"] = crypto.encrypt(body.api_key)
    if body.model:
        updates["deepseek_model"] = body.model
    if updates:
        await db.update_user_config(user.id, **updates)
    return {"ok": True}


class ActiveLLMBody(BaseModel):
    active_llm: str


@app.put("/api/config/active-llm")
async def save_active_llm(body: ActiveLLMBody, user: db.User = Depends(require_user)):
    if body.active_llm not in ("claude", "deepseek"):
        raise HTTPException(400, "active_llm must be 'claude' or 'deepseek'")
    await db.update_user_config(user.id, active_llm=body.active_llm)
    return {"ok": True}


class CFConfig(BaseModel):
    handle: Optional[str] = None


@app.put("/api/config/cf")
async def save_cf(body: CFConfig, user: db.User = Depends(require_user)):
    if body.handle:
        await db.update_user_config(user.id, cf_handle=body.handle.strip())
    return {"ok": True}


class SettingsConfig(BaseModel):
    same_language_only: Optional[bool] = None


@app.put("/api/config/settings")
async def save_settings(body: SettingsConfig, user: db.User = Depends(require_user)):
    updates = {}
    if body.same_language_only is not None:
        updates["same_language_only"] = body.same_language_only
    if updates:
        await db.update_user_config(user.id, **updates)
    return {"ok": True}


class LGMHandlesBody(BaseModel):
    handles: list[str]


@app.put("/api/config/lgm-handles")
async def save_lgm_handles(body: LGMHandlesBody, user: db.User = Depends(require_user)):
    handles = [h.strip() for h in body.handles if h.strip()]
    await db.update_user_config(user.id, lgm_handles=handles)
    return {"ok": True, "count": len(handles)}


@app.post("/api/config/lgm-refresh")
async def refresh_lgm_profiles(user: db.User = Depends(require_user)):
    """Fetch latest CF profiles for all lgm_handles and cache in DB."""
    uc = await db.get_user_config(user.id)
    handles = uc.lgm_handles or []
    if not handles:
        return {"ok": True, "profiles": {}}

    profiles: dict = dict(uc.lgm_profiles or {})
    handles_str = ";".join(handles)

    async with httpx.AsyncClient(timeout=15, headers=_CF_HEADERS) as c:
        try:
            r = await c.get(f"{_CF_API}/user.info", params={"handles": handles_str})
            info = r.json()
        except Exception:
            info = {"status": "FAILED"}

        if info.get("status") == "OK":
            for u in info.get("result", []):
                h = u.get("handle", "")
                av = u.get("titlePhoto", "")
                if av.startswith("/"):
                    av = "https:" + av
                profiles[h.lower()] = {
                    "handle": h,
                    "rating": u.get("rating", 0),
                    "maxRating": u.get("maxRating", 0),
                    "rank": u.get("rank", ""),
                    "avatar": av,
                    "lastOnline": u.get("lastOnlineTimeSeconds", 0),
                }

        for h in handles:
            try:
                r2 = await c.get(f"{_CF_API}/user.rating", params={"handle": h})
                rd = r2.json()
                if rd.get("status") == "OK":
                    key = h.lower()
                    if key in profiles:
                        profiles[key]["contestCount"] = len(rd.get("result", []))
            except Exception:
                pass

    removed = {k for k in profiles if k not in {h.lower() for h in handles}}
    for k in removed:
        del profiles[k]

    await db.update_user_config(user.id, lgm_profiles=profiles)
    return {"ok": True, "profiles": profiles}


class CompareModeBody(BaseModel):
    mode: str
    target: Optional[str] = None
    targets: Optional[list[str]] = None


@app.put("/api/config/compare-mode")
async def save_compare_mode(body: CompareModeBody, user: db.User = Depends(require_user)):
    if body.mode not in ("auto", "target", "comprehensive"):
        raise HTTPException(400, "Invalid mode")
    updates = {"compare_mode": body.mode}
    if body.target is not None:
        updates["compare_target"] = body.target.strip()
    if body.targets is not None:
        updates["compare_targets"] = [h.strip() for h in body.targets if h.strip()][:10]
    await db.update_user_config(user.id, **updates)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Connectivity tests (SSE)
# ---------------------------------------------------------------------------

@app.post("/api/test/llm")
async def test_llm(user: db.User = Depends(require_user)):
    cfg = await build_user_cfg(user.id)

    def gen():
        active = cfg["settings"]["active_llm"]
        yield {"type": "step", "text": f"当前后端：{active}"}
        try:
            if active == "deepseek":
                api_key = cfg["deepseek"]["api_key"]
                model = cfg["deepseek"]["model"]
                if not api_key:
                    yield {"type": "fail", "text": "未配置 DeepSeek API Key"}
                    return
                yield {"type": "step", "text": f"目标模型：{model}"}
                from openai import OpenAI
                client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
                yield {"type": "step", "text": "发送测试消息..."}
                t0 = time.time()
                client.chat.completions.create(
                    model=model, max_tokens=8,
                    messages=[{"role": "user", "content": "Reply: OK"}],
                )
                ms = int((time.time() - t0) * 1000)
                yield {"type": "ok", "text": f"DeepSeek 连接正常 — {model}（{ms}ms）✓"}
            else:
                api_key = cfg["claude"]["api_key"]
                model = cfg["claude"]["model"]
                base_url = cfg["claude"]["base_url"]
                if not api_key:
                    yield {"type": "fail", "text": "未配置 Claude API Key"}
                    return
                yield {"type": "step", "text": f"目标模型：{model}"}
                import anthropic as _anthropic
                kwargs = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = _anthropic.Anthropic(**kwargs)
                yield {"type": "step", "text": "发送测试消息..."}
                t0 = time.time()
                client.messages.create(
                    model=model, max_tokens=8,
                    messages=[{"role": "user", "content": "Reply: OK"}],
                )
                ms = int((time.time() - t0) * 1000)
                yield {"type": "ok", "text": f"Claude 连接正常 — {model}（{ms}ms）✓"}
        except Exception as e:
            yield {"type": "fail", "text": f"连接失败：{e}"}

    return _sse_thread(gen)


@app.post("/api/test/cf")
async def test_cf(user: db.User = Depends(require_user)):
    cfg = await build_user_cfg(user.id)

    def gen():
        handle = cfg["codeforces"]["handle"]
        if not handle:
            yield {"type": "fail", "text": "未配置 CF 账号"}
            return
        yield {"type": "step", "text": "验证 CF session（使用服务器共享 profile）..."}
        from cf_client import CFClient
        client = CFClient(handle=handle, delay=0, profile_dir=SHARED_CF_PROFILE)
        try:
            t0 = time.time()
            client.login()
            ms = int((time.time() - t0) * 1000)
            yield {"type": "ok", "text": f"CF 会话有效（{ms}ms）✓"}
        except Exception as e:
            yield {"type": "fail", "text": f"CF 会话无效：{e}"}
        finally:
            client.close()

    return _sse_thread(gen)


@app.post("/api/test/submission-access")
async def test_submission_access(user: db.User = Depends(require_user)):
    cfg = await build_user_cfg(user.id)

    def gen():
        handle = cfg["codeforces"]["handle"]
        if not handle:
            yield {"type": "fail", "text": "未配置 CF 账号"}
            return
        yield {"type": "step", "text": "验证源码获取（使用服务器共享 profile）..."}
        from cf_client import CFClient
        client = CFClient(handle=handle, delay=0, profile_dir=SHARED_CF_PROFILE)
        try:
            client.login()
            yield {"type": "step", "text": f"已登录：{handle}，获取最近提交..."}
            subs = client._api("user.status", {"handle": handle, "from": 1, "count": 20})
            test_sub = next((s for s in subs if s.get("contestId", 0) < 100000), None)
            if not test_sub:
                yield {"type": "ok", "text": "账号无普通赛提交，跳过源码验证 ✓"}
                return
            test_contest = test_sub["contestId"]
            test_sid = test_sub["id"]
            yield {"type": "step", "text": f"获取源码 #{test_sid}..."}
            t0 = time.time()
            src = client.get_submission_source(test_contest, test_sid)
            ms = int((time.time() - t0) * 1000)
            if src:
                yield {"type": "ok", "text": f"源码获取正常（{ms}ms，{len(src)} 字符）✓"}
            else:
                yield {"type": "fail", "text": "Session 有效但无法获取源码"}
        except Exception as e:
            yield {"type": "fail", "text": f"测试出错：{e}"}
        finally:
            client.close()

    return _sse_thread(gen)


# ---------------------------------------------------------------------------
# Submissions sync endpoints
# ---------------------------------------------------------------------------

@app.get("/api/submissions/status")
async def submissions_status(user: db.User = Depends(require_user)):
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""
    if not handle:
        return {"cold_start_done": False, "last_sync_at": None, "handle": ""}
    state = await db.get_sync_state(handle, user_id=user.id)
    return {
        "cold_start_done": bool(state["cold_start_done"]),
        "last_sync_at": state["last_sync_at"],
        "handle": handle,
    }


class ColdStartBody(BaseModel):
    handle: str
    count: int = 200


@app.post("/api/submissions/cold-start")
async def cold_start(body: ColdStartBody, user: db.User = Depends(require_user)):
    uid = user.id

    def gen():
        yield {"type": "step", "text": f"从 CF API 拉取 {body.handle} 最近 {body.count} 条提交…"}
        try:
            from submissions import fetch_and_normalize
            rows = fetch_and_normalize(body.handle, body.count)
            yield {"type": "step", "text": f"拉取完成，共 {len(rows)} 条，写入数据库…"}
            inserted = _sync_db(db.insert_submissions_batch(rows, user_id=uid))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            max_id = _sync_db(db.get_latest_submission_id(body.handle, user_id=uid))
            _sync_db(db.update_sync_state(
                body.handle, user_id=uid,
                last_submission_id=max_id, last_sync_at=now, cold_start_done=1,
            ))
            yield {"type": "ok", "text": f"冷启动完成：新增 {inserted} 条，总提交 {len(rows)} 条"}
        except Exception as e:
            yield {"type": "fail", "text": str(e)}

    return _sse_thread(gen)


@app.post("/api/submissions/refresh")
async def refresh_submissions(user: db.User = Depends(require_user)):
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""
    uid = user.id

    if not handle:
        return StreamingResponse(
            iter([f"data: {json.dumps({'type': 'fail', 'text': '未配置 CF handle'})}\n\n"]),
            media_type="text/event-stream",
        )

    def gen():
        yield {"type": "step", "text": f"增量刷新 {handle} 的提交…"}
        try:
            from submissions import fetch_and_normalize
            rows = fetch_and_normalize(handle, 50)
            state = _sync_db(db.get_sync_state(handle, user_id=uid))
            last_id = state["last_submission_id"]
            new_rows = [r for r in rows if r["id"] > last_id]

            if not new_rows:
                yield {"type": "ok", "text": "没有新的提交"}
                return

            inserted = 0
            for row in new_rows:
                n = _sync_db(db.insert_submissions_batch([row], user_id=uid))
                inserted += n
                if n > 0:
                    _sync_db(db.update_sync_state(
                        handle, user_id=uid,
                        last_submission_id=row["id"],
                        last_sync_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        cold_start_done=state["cold_start_done"],
                    ))
            yield {"type": "ok", "text": f"刷新完成：新增 {inserted} 条"}
        except Exception as e:
            yield {"type": "fail", "text": str(e)}

    return _sse_thread(gen)


# ---------------------------------------------------------------------------
# Problems list endpoints
# ---------------------------------------------------------------------------

@app.get("/api/problems/pending")
async def get_pending(
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),
    user: db.User = Depends(require_user),
):
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""
    if not handle:
        return {"ac_unreviewed": [], "failed": []}

    data = await db.get_pending_problems(handle, user_id=user.id)

    def _filter(rows):
        result = []
        for r in rows:
            if rating_min is not None and (r.get("rating") or 0) < rating_min:
                continue
            if rating_max is not None and (r.get("rating") or 0) > rating_max:
                continue
            result.append(r)
        return result

    return {
        "ac_unreviewed": _filter(data["ac_unreviewed"]),
        "failed": _filter(data["failed"]),
    }


@app.get("/api/problems/reviewed")
async def get_reviewed(
    search: str = Query(""),
    rating_min: Optional[int] = Query(None),
    rating_max: Optional[int] = Query(None),
    user: db.User = Depends(require_user),
):
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""
    if not handle:
        return []

    rows = await db.get_reviewed_problems(handle, user_id=user.id, search=search)

    if rating_min is not None:
        rows = [r for r in rows if (r.get("rating") or 0) >= rating_min]
    if rating_max is not None:
        rows = [r for r in rows if (r.get("rating") or 0) <= rating_max]
    return rows


# ---------------------------------------------------------------------------
# Card endpoint
# ---------------------------------------------------------------------------

@app.get("/api/problems/{contest_id}/{index}/card")
async def get_card(contest_id: int, index: str, user: db.User = Depends(require_user)):
    index = index.upper()
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""
    if not handle:
        raise HTTPException(400, "未配置 CF handle")

    ac_sub = await db.get_latest_ac_submission(handle, contest_id, index, user_id=user.id)
    ref_sub = ac_sub or await db.get_latest_submission(handle, contest_id, index, user_id=user.id)
    user_submission_id = ref_sub["id"] if ref_sub else None
    user_lang = ref_sub["language"] if ref_sub else ""
    problem_name = ref_sub["problem_name"] if ref_sub else ""
    rating = ref_sub["rating"] if ref_sub else None
    tags = ref_sub["tags"] if ref_sub else "[]"

    analysis = await db.get_or_create_analysis_stub(
        handle=handle,
        contest_id=contest_id,
        problem_index=index,
        user_submission_id=user_submission_id,
        user_lang=user_lang,
        created_at=datetime.now(timezone.utc).isoformat(),
        analysis_id=str(uuid.uuid4()),
        user_id=user.id,
    )

    await db.update_last_reviewed(analysis["id"])

    return {
        "aid": analysis["id"],
        "contest_id": contest_id,
        "problem_index": index,
        "problem_name": problem_name,
        "rating": rating,
        "tags": tags,
        "language": user_lang,
        "user_code": analysis.get("user_code", ""),
        "notes": analysis.get("notes", ""),
        "style_review": analysis.get("style_review", ""),
        "analysis": analysis.get("analysis", ""),
        "problem_statement": analysis.get("problem_statement", ""),
        "user_submission_id": user_submission_id,
        "cf_url": f"https://codeforces.com/contest/{contest_id}/problem/{index}",
        "lgm_source": analysis.get("lgm_source", ""),
        "lgm_handle": analysis.get("lgm_handle", ""),
        "lgm_lang": analysis.get("lgm_lang", ""),
        "lgm_url": analysis.get("lgm_url", ""),
        "importance": analysis.get("importance", ""),
        "last_reviewed_at": analysis.get("last_reviewed_at", ""),
    }


# ---------------------------------------------------------------------------
# PUT: save edits on card fields
# ---------------------------------------------------------------------------

class StyleReviewBody(BaseModel):
    text: str


@app.put("/api/analyses/{aid}/style-review")
async def save_style_review(aid: str, body: StyleReviewBody, user: db.User = Depends(require_user)):
    if not await db.get_analysis(aid, user_id=user.id):
        raise HTTPException(404, "Not found")
    await db.update_style_review(aid, body.text, user_id=user.id)
    return {"ok": True}


class ProblemStatementBody(BaseModel):
    text: str


@app.put("/api/analyses/{aid}/problem-statement")
async def save_problem_statement(aid: str, body: ProblemStatementBody, user: db.User = Depends(require_user)):
    if not await db.get_analysis(aid, user_id=user.id):
        raise HTTPException(404, "Not found")
    await db.update_problem_statement(aid, body.text, user_id=user.id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# SSE: style-review (user code only)
# ---------------------------------------------------------------------------

STYLE_REVIEW_PROMPT = """\
你是一位算法竞赛教练，专注于代码风格和实现质量点评。请用中文输出，格式用 Markdown。

## 题目
Contest {contest_id}, Problem {problem_index}
链接：https://codeforces.com/contest/{contest_id}/problem/{problem_index}
{problem_statement_section}
## 选手代码（{user_lang}）
```{lang_id}
{user_code}
```

---

请按以下结构输出点评报告：

## 代码风格与可读性
变量命名、代码结构、注释等方面的评价。

## 算法思路
简述选手的解题思路，是否简洁有效。

## 潜在问题
列出可能存在的边界问题、逻辑漏洞或低效点（若 AC 则从鲁棒性角度分析）。

## 改进建议
3-5 条具体可执行的改进方向，面向下次遇到类似题型。\
"""


@app.post("/api/problems/{contest_id}/{index}/style-review")
async def style_review_sse(contest_id: int, index: str, user: db.User = Depends(require_user)):
    index = index.upper()
    cfg = await build_user_cfg(user.id)
    uid = user.id
    github_login = user.github_login
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""

    def gen():
        from analyzer import AnalysisLogger, LLMClient
        alog = AnalysisLogger(uid, github_login, "style_review",
                              contest_id=contest_id, problem_index=index)
        active_llm = cfg.get("settings", {}).get("active_llm", "claude")
        try:
            analysis = _sync_db(db.get_analysis_by_problem(handle, contest_id, index, user_id=uid))
            if not analysis:
                alog.step("get_analysis", "error", "no card")
                alog.finish("error", error="请先打开卡片")
                yield {"type": "fail", "text": "请先打开卡片"}
                return
            alog.step("get_analysis", "ok")

            aid = analysis["id"]
            user_code = analysis.get("user_code", "")

            if not user_code and analysis.get("user_submission_id"):
                sid = analysis["user_submission_id"]
                yield {"type": "step", "text": f"正在获取提交源码 #{sid}（需启动浏览器）…"}
                from cf_client import CFClient
                client = CFClient(handle=handle, delay=0, profile_dir=SHARED_CF_PROFILE)
                try:
                    page = client._get_page()
                    page.goto(
                        f"https://codeforces.com/contest/{contest_id}/status",
                        wait_until="load", timeout=60000,
                    )
                    client._wait_cf_challenge(page, timeout=90.0)
                    user_code = client.get_submission_source(contest_id, sid, challenge_passed=True) or ""
                finally:
                    client.close()

                if user_code:
                    _sync_db(db.update_user_code(aid, user_code, user_id=uid))
                    yield {"type": "code_fetched", "user_code": user_code}
                    alog.step("fetch_code", "ok")
                else:
                    alog.step("fetch_code", "error", "empty source")
                    alog.finish("error", error="无法获取提交源码")
                    yield {"type": "fail", "text": "无法获取提交源码，请检查 CF 会话"}
                    return

            if not user_code:
                alog.step("fetch_code", "error", "no user code")
                alog.finish("error", error="未能获取用户代码")
                yield {"type": "fail", "text": "未能获取用户代码"}
                return

            ps = analysis.get("problem_statement", "") or ""
            ps_section = f"\n## 题目描述\n{ps}\n\n" if ps.strip() else ""
            lang_id = (analysis.get("user_lang") or "cpp").lower().split()[0]

            prompt = STYLE_REVIEW_PROMPT.format(
                contest_id=contest_id,
                problem_index=index,
                problem_statement_section=ps_section,
                user_lang=analysis.get("user_lang") or "C++",
                lang_id=lang_id,
                user_code=user_code,
            )
            alog.step("build_prompt", "ok")

            yield {"type": "step", "text": "AI 正在点评代码…"}
            yield {"type": "analysis_start"}

            llm = LLMClient(cfg)
            chunks: list[str] = []
            for text in llm.stream(prompt, max_tokens=3000):
                chunks.append(text)
                yield {"type": "analysis_chunk", "text": text}

            full = "".join(chunks)
            _sync_db(db.update_style_review(aid, full, user_id=uid))
            alog.step("llm_call", "ok", f"{len(chunks)} chunks")
            alog.finish("success", model=active_llm,
                        summary=f"style review {contest_id}{index}, {len(full)} chars")
            yield {"type": "done", "aid": aid}

        except Exception as e:
            import traceback
            alog.step("exception", "error", str(e))
            alog.finish("error", model=active_llm, error=str(e))
            tb = traceback.format_exc()
            is_browser_err = any(k in tb for k in ("page.goto", "playwright", "Cloudflare", "TimeoutError", "net::ERR_"))
            if is_browser_err:
                yield {"type": "fail", "text": "浏览器访问 CF 超时（偶发），如果之前生成过报告，可到「已完成」查看完整报告"}
            else:
                yield {"type": "fail", "text": str(e)}
            yield {"type": "step", "text": tb[:400]}

    return _sse_thread(gen)


# ---------------------------------------------------------------------------
# SSE: full-review (boss comparison)
# ---------------------------------------------------------------------------

@app.post("/api/problems/{contest_id}/{index}/full-review")
async def full_review_sse(contest_id: int, index: str, user: db.User = Depends(require_user)):
    index = index.upper()

    if contest_id >= 100000:
        return StreamingResponse(
            iter([f"data: {json.dumps({'type': 'fail', 'text': 'Gym 题目不支持大佬代码对比分析'})}\n\n"]),
            media_type="text/event-stream",
        )

    cfg = await build_user_cfg(user.id)
    uid = user.id
    github_login = user.github_login
    uc = await db.get_user_config(user.id)
    handle = uc.cf_handle or ""
    settings = cfg["settings"]
    lgm_handles: list[str] = cfg["lgm_handles"]

    def gen():
        from analyzer import ANALYSIS_PROMPT, COMPREHENSIVE_PROMPT, _lang, _meta, _src, LLMClient, AnalysisLogger
        alog = AnalysisLogger(uid, github_login, "full_review",
                              contest_id=contest_id, problem_index=index)
        active_llm = settings.get("active_llm", "claude")
        try:
            if not lgm_handles:
                alog.step("check_handles", "error", "empty")
                alog.finish("error", error="未配置大佬 handle")
                yield {"type": "fail", "text": "未配置大佬 handle，请在设置页添加"}
                return

            analysis = _sync_db(db.get_analysis_by_problem(handle, contest_id, index, user_id=uid))
            if not analysis:
                alog.step("get_analysis", "error", "no card")
                alog.finish("error", error="请先打开卡片")
                yield {"type": "fail", "text": "请先打开卡片"}
                return
            alog.step("get_analysis", "ok")

            aid = analysis["id"]
            user_code = analysis.get("user_code", "")

            ps = (analysis.get("problem_statement") or "").strip()
            if not ps:
                from analyzer import fetch_luogu_statement
                yield {"type": "step", "text": "从洛谷获取题面…"}
                luogu = fetch_luogu_statement(contest_id, index)
                if luogu:
                    ps = luogu["text"]
                    _sync_db(db.update_problem_statement(aid, ps, user_id=uid))
                    yield {"type": "problem_statement", "text": ps}
                    yield {"type": "step", "text": f"题面获取成功：{luogu['title']}"}
                    alog.step("fetch_luogu", "ok", f"{len(ps)} chars")
                else:
                    yield {"type": "step", "text": "洛谷暂未收录此题（可能题目过新），跳过题面获取"}
                    alog.step("fetch_luogu", "skip", "not found")

            from cf_client import CFClient
            delay = float(settings.get("request_delay", 1.5))
            lang_filter = analysis.get("user_lang") if settings.get("same_language_only", True) else None
            client = CFClient(handle=handle, delay=delay, profile_dir=SHARED_CF_PROFILE)

            try:
                yield {"type": "step", "text": "打开比赛页面，等待 Cloudflare 验证…"}
                page = client._get_page()
                page.goto(
                    f"https://codeforces.com/contest/{contest_id}/status",
                    wait_until="load", timeout=60000,
                )
                client._wait_cf_challenge(page, timeout=90.0)
                yield {"type": "step", "text": "Cloudflare 验证通过 ✓"}
                alog.step("cf_challenge", "ok")

                if not user_code and analysis.get("user_submission_id"):
                    sid = analysis["user_submission_id"]
                    yield {"type": "step", "text": f"获取用户提交源码 #{sid}…"}
                    user_code = client.get_submission_source(contest_id, sid, challenge_passed=True) or ""
                    if user_code:
                        _sync_db(db.update_user_code(aid, user_code, user_id=uid))
                        yield {"type": "code_fetched", "user_code": user_code}
                        alog.step("fetch_user_code", "ok")

                if not user_code:
                    alog.step("fetch_user_code", "error", "empty")
                    alog.finish("error", error="未能获取用户代码")
                    yield {"type": "fail", "text": "未能获取用户代码"}
                    return

                compare_mode = settings.get("compare_mode", "auto")
                prob_name = analysis.get("problem_name") or ""
                if not prob_name:
                    ref_sub = _sync_db(db.get_latest_ac_submission(handle, contest_id, index, user_id=uid))
                    prob_name = ref_sub["problem_name"] if ref_sub else ""

                if compare_mode == "target":
                    target_handle = settings.get("compare_target", "")
                    if not target_handle:
                        alog.step("find_lgm", "error", "no target handle configured")
                        alog.finish("error", error="未配置指定对手")
                        yield {"type": "fail", "text": "未配置指定对手"}
                        return
                    search_handles = [target_handle]
                elif compare_mode == "comprehensive":
                    search_handles = settings.get("compare_targets", [])
                    if len(search_handles) < 2:
                        alog.step("find_lgm", "error", "need >= 2 targets")
                        alog.finish("error", error="综合对比至少需要选择 2 人")
                        yield {"type": "fail", "text": "综合对比至少需要选择 2 人"}
                        return
                else:
                    search_handles = lgm_handles

                boss_list = []

                if compare_mode == "comprehensive":
                    for bh in search_handles[:10]:
                        yield {"type": "step", "text": f"查找 {bh} 的 AC 提交…"}
                        sub = client.find_handle_submission(contest_id, index, [bh], lang_filter, problem_name=prob_name)
                        if not sub:
                            yield {"type": "step", "text": f"{bh} 未解答此题，跳过"}
                            continue
                        bsid = sub["id"]
                        bcid = sub["problem"].get("contestId", contest_id)
                        bhandle = sub["author"]["members"][0]["handle"] if sub["author"]["members"] else bh
                        yield {"type": "step", "text": f"找到 {bhandle} #{bsid}，获取源码…"}
                        bsrc = client.get_submission_source(bcid, bsid, challenge_passed=True) or ""
                        blang = sub["programmingLanguage"]
                        burl = f"https://codeforces.com/contest/{bcid}/submission/{bsid}"
                        boss_list.append({"handle": bhandle, "src": bsrc, "lang": blang, "url": burl, "sid": bsid, "contest_id": bcid})
                        yield {"type": "code_data", "role": "lgm", "source": bsrc, "url": burl, "lang": blang, "sid": bsid, "handle": bhandle}
                    if not boss_list:
                        alog.step("find_lgm", "error", "no boss found (comprehensive)")
                        alog.finish("error", error="所选大佬中无人解答此题")
                        yield {"type": "fail", "text": "所选大佬中无人解答此题（或语言不符）"}
                        return
                    alog.step("find_lgm", "ok", f"{len(boss_list)} bosses found")
                else:
                    yield {"type": "step", "text": f"寻找大佬代码（{', '.join(search_handles[:3])}…）"}
                    sub = client.find_handle_submission(contest_id, index, search_handles, lang_filter, problem_name=prob_name)
                    if not sub:
                        alog.step("find_lgm", "error", "no AC submission found")
                        alog.finish("error", error="大佬列表中无人解答此题")
                        yield {"type": "fail", "text": "大佬列表中无人解答此题（或语言不符）"}
                        return
                    bsid = sub["id"]
                    bcid = sub["problem"].get("contestId", contest_id)
                    bhandle = sub["author"]["members"][0]["handle"] if sub["author"]["members"] else "?"
                    yield {"type": "step", "text": f"找到 {bhandle} #{bsid}（contest {bcid}），获取源码…"}
                    bsrc = client.get_submission_source(bcid, bsid, challenge_passed=True) or ""
                    blang = sub["programmingLanguage"]
                    burl = f"https://codeforces.com/contest/{bcid}/submission/{bsid}"
                    boss_list.append({"handle": bhandle, "src": bsrc, "lang": blang, "url": burl, "sid": bsid, "contest_id": bcid})
                    yield {"type": "code_data", "role": "lgm", "source": bsrc, "url": burl, "lang": blang, "sid": bsid, "handle": bhandle}
                    alog.step("find_lgm", "ok", f"{bhandle} #{bsid}")

                alog.step("fetch_lgm_source", "ok", f"{len(boss_list)} sources")

            finally:
                client.close()

            b0 = boss_list[0]
            _sync_db(db.update_lgm_info(aid, b0["src"], b0["url"], b0["lang"], b0["sid"], b0["handle"], user_id=uid))

            ps_section = f"\n## 题面\n{ps}\n" if ps else ""

            user_lang = analysis.get("user_lang") or "C++"
            lang_id = user_lang.lower().split()[0]

            if compare_mode == "comprehensive" and len(boss_list) > 1:
                boss_sections = ""
                for i, b in enumerate(boss_list, 1):
                    bl = _lang(b)
                    boss_sections += f"## 参考代码 {i}：{b['handle']}\n"
                    boss_sections += f"#{b['sid']} — {b['url']}\n"
                    boss_sections += f"```{bl}\n{b['src']}\n```\n\n"
                prompt = COMPREHENSIVE_PROMPT.format(
                    contest_id=contest_id, problem_index=index,
                    problem_statement_section=ps_section,
                    boss_sections=boss_sections, user_lang=user_lang,
                    user_lang_id=lang_id, user_code=user_code,
                )
                max_tok = 6000
            else:
                boss_info = {
                    "submission_id": b0["sid"], "source": b0["src"],
                    "url": b0["url"], "language": b0["lang"], "handle": b0["handle"],
                }
                prompt = ANALYSIS_PROMPT.format(
                    contest_id=contest_id, problem_index=index,
                    problem_statement_section=ps_section,
                    boss_handle=b0["handle"], boss_meta=_meta(boss_info),
                    boss_lang=_lang(boss_info), boss_src=_src(boss_info),
                    user_lang=user_lang, user_lang_id=lang_id, user_code=user_code,
                )
                max_tok = 4096

            yield {"type": "step", "text": f"正在调用 {active_llm} 进行深度分析…"}
            yield {"type": "analysis_start"}

            llm = LLMClient(cfg)
            chunks: list[str] = []
            for text in llm.stream(prompt, max_tokens=max_tok):
                chunks.append(text)
                yield {"type": "analysis_chunk", "text": text}

            full = "".join(chunks)
            _sync_db(db.update_analysis_text(aid, full, user_id=uid))
            alog.step("llm_call", "ok", f"{len(chunks)} chunks")
            handles_str = ", ".join(b["handle"] for b in boss_list)
            alog.finish("success", model=active_llm,
                        summary=f"full review {contest_id}{index} vs [{handles_str}], {len(full)} chars")
            yield {"type": "done", "aid": aid, "problem": f"{contest_id}{index}"}

        except Exception as e:
            import traceback
            alog.step("exception", "error", str(e))
            alog.finish("error", model=active_llm, error=str(e))
            tb = traceback.format_exc()
            is_browser_err = any(k in tb for k in ("page.goto", "playwright", "Cloudflare", "TimeoutError", "net::ERR_"))
            if is_browser_err:
                yield {"type": "fail", "text": "浏览器访问 CF 超时（偶发），如果之前生成过报告，可到「已完成」查看完整报告"}
            else:
                yield {"type": "fail", "text": str(e)}
            yield {"type": "step", "text": tb[:400]}

    return _sse_thread(gen)


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

@app.get("/api/admin/stats")
async def admin_stats(admin: db.User = Depends(require_admin)):
    return {
        "total_users": await db.count_users(),
        "dau": await db.count_dau(),
        "total_analyses": await db.count_analyses(),
        "analyses_today": await db.count_analyses_today(),
        "recent_users": await db.get_recent_users(limit=20),
    }


@app.get("/api/admin/analysis-logs")
async def admin_analysis_logs(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    admin: db.User = Depends(require_admin),
):
    return await db.list_analysis_logs(limit=limit, offset=offset)


@app.get("/api/admin/active-users")
async def admin_active_users(
    period: str = Query("week"),
    limit: int = Query(50, le=200),
    admin: db.User = Depends(require_admin),
):
    return await db.get_active_users_ranking(period=period, limit=limit)


# ---------------------------------------------------------------------------
# CF API proxy (bypass Cloudflare on server)
# ---------------------------------------------------------------------------

@app.get("/api/cf-proxy/user.info")
async def cf_proxy_user_info(handles: str, user: db.User = Depends(require_user)):
    async with httpx.AsyncClient(timeout=15, headers=_CF_HEADERS) as c:
        r = await c.get(f"{_CF_API}/user.info", params={"handles": handles})
        if r.headers.get("content-type", "").startswith("text/html"):
            raise HTTPException(502, "CF API blocked by Cloudflare")
        return r.json()


@app.get("/api/cf-proxy/user.rating")
async def cf_proxy_user_rating(handle: str, user: db.User = Depends(require_user)):
    async with httpx.AsyncClient(timeout=15, headers=_CF_HEADERS) as c:
        r = await c.get(f"{_CF_API}/user.rating", params={"handle": handle})
        if r.headers.get("content-type", "").startswith("text/html"):
            raise HTTPException(502, "CF API blocked by Cloudflare")
        return r.json()


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=6010, reload=False)
