"""
Core analysis generator.
Yields event dicts consumed by both the FastAPI SSE endpoint and the CLI.
"""

import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import httpx
import yaml
import anthropic
from openai import OpenAI

import asyncio

from cf_client import CFClient
import db


def _sync_db(coro):
    """Call an async DB function from a sync generator thread."""
    import app as _app
    future = asyncio.run_coroutine_threadsafe(coro, _app._main_loop)
    return future.result(timeout=30)

Event = dict


class AnalysisLogger:
    """Tracks analysis flow steps and writes to analysis_logs table."""

    def __init__(self, user_id: int | None, github_login: str, log_type: str,
                 contest_id: int | None = None, problem_index: str = ""):
        self._start = time.monotonic()
        self._steps: dict[str, str] = {}
        self._lines: list[str] = []
        self._log_id: int | None = None
        try:
            self._log_id = _sync_db(db.create_analysis_log({
                "user_id": user_id,
                "github_login": github_login,
                "log_type": log_type,
                "contest_id": contest_id,
                "problem_index": problem_index,
                "status": "running",
                "steps_detail": {},
            }))
        except Exception:
            pass

    def step(self, name: str, status: str = "ok", detail: str = ""):
        self._steps[name] = status
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {name}: {status}"
        if detail:
            line += f" — {detail}"
        self._lines.append(line)
        self._flush_steps()

    def _flush_steps(self):
        if not self._log_id:
            return
        try:
            _sync_db(db.update_analysis_log(self._log_id, steps_detail=dict(self._steps)))
        except Exception:
            pass

    def finish(self, status: str = "success", model: str = "", summary: str = "",
               error: str = ""):
        if not self._log_id:
            return
        elapsed = int((time.monotonic() - self._start) * 1000)
        if error:
            self._lines.append(f"[ERROR] {error}")
        try:
            _sync_db(db.update_analysis_log(
                self._log_id,
                status=status,
                finished_at=datetime.now(timezone.utc),
                duration_ms=elapsed,
                model_used=model,
                result_summary=summary,
                ai_debug_log="\n".join(self._lines),
                error_message=error[:500] if error else "",
                steps_detail=dict(self._steps),
            ))
        except Exception:
            pass


class LLMClient:
    """Unified wrapper for Claude and DeepSeek, with identical ask/stream interface."""

    def __init__(self, cfg: dict):
        settings = cfg.get("settings", {})
        self.active = settings.get("active_llm", "claude")

        if self.active == "deepseek":
            ds = cfg.get("deepseek", {})
            self._client = OpenAI(
                api_key=ds["api_key"],
                base_url="https://api.deepseek.com",
            )
            self.model = ds.get("model", "deepseek-chat")
        else:
            cl = cfg.get("claude", {})
            kwargs: dict = {"api_key": cl["api_key"]}
            if cl.get("base_url"):
                kwargs["base_url"] = cl["base_url"]
            self._client = anthropic.Anthropic(**kwargs)
            self.model = cl.get("model", "claude-sonnet-4-6")

    def stream(self, prompt: str, max_tokens: int = 4096):
        """Generator yielding text chunks for streaming output."""
        if self.active == "deepseek":
            stream = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            )
            for chunk in stream:
                text = chunk.choices[0].delta.content
                if text:
                    yield text
        else:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as s:
                for text in s.text_stream:
                    yield text


def _load_config() -> dict:
    with open(Path(__file__).parent / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _meta(info: dict | None) -> str:
    if not info:
        return "未获取到"
    sid = info.get("submission_id", "?")
    url = info.get("url", "")
    handle = info.get("handle", "")
    suffix = f" by {handle}" if handle else ""
    return f"#{sid}{suffix} — {url}"


def _lang(info: dict | None) -> str:
    if not info:
        return "cpp"
    raw = (info.get("language") or "cpp").lower()
    if "python" in raw:
        return "python"
    if "java" in raw:
        return "java"
    return "cpp"


def _src(info: dict | None) -> str:
    return info.get("source") or "（未能获取源码）" if info else "（未获取到）"


_LUOGU_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "x-lentille-request": "content-only",
}


def fetch_luogu_statement(contest_id: int, problem_index: str) -> dict | None:
    """Fetch problem statement from Luogu. Returns {text, title} or None."""
    pid = f"CF{contest_id}{problem_index}"
    url = f"https://www.luogu.com.cn/problem/{pid}"
    try:
        r = httpx.get(url, headers=_LUOGU_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code == 404:
            return None
        d = r.json()
        p = d["data"]["problem"]
    except Exception:
        return None

    translation = (p.get("translation") or "").strip()
    content = p.get("content") or {}

    if translation:
        text = translation
    else:
        parts = []
        if content.get("description"):
            parts.append("## Description\n" + content["description"])
        if content.get("formatI"):
            parts.append("## Input\n" + content["formatI"])
        if content.get("formatO"):
            parts.append("## Output\n" + content["formatO"])
        for i, (inp, out) in enumerate(p.get("samples") or [], 1):
            parts.append(f"## Sample {i}\nInput:\n```\n{inp}\n```\nOutput:\n```\n{out}\n```")
        if content.get("hint"):
            parts.append("## Note\n" + content["hint"])
        text = "\n\n".join(parts)

    if not text:
        return None
    title = p.get("title", "") or content.get("name", "")
    return {"text": text, "title": title}


ANALYSIS_PROMPT = """\
你是一位算法竞赛教练，帮选手做赛后 upsolving 复盘。通过对比选手和大佬的代码，提炼出选手需要掌握的**算法思路和关键观察**。请用中文输出。

## 绝对禁止分析的内容（违反即视为失败）

以下全部属于竞赛选手的个人模板习惯，与解题能力完全无关，**一个字都不要提**：
- 头文件、`#pragma`、`bits/stdc++.h`
- 宏定义（`#define int long long`、`#define pb` 等）
- `typedef`、`using namespace std`
- I/O 优化（`ios::sync_with_stdio`、`cin.tie`、`scanf/printf`）
- 模板预声明的变量（即使未使用）、调试宏、快读模板
- 变量命名风格、代码缩进风格
- 多组数据循环写法（`while(t--)` vs `for`）
- 函数命名或封装风格（把代码写在 `solve()` 还是 `main()` 里）

如果你发现自己在写"模板过于复杂""有未使用的变量""大佬的代码更简洁"之类的话，**立刻停下来，删掉这段，换成算法分析**。

---

## 题目
Contest {contest_id}, Problem {problem_index}
链接：https://codeforces.com/contest/{contest_id}/problem/{problem_index}
{problem_statement_section}
## 参考代码：大佬代码（{boss_handle}）
{boss_meta}
```{boss_lang}
{boss_src}
```

## 选手代码（{user_lang}）
```{user_lang_id}
{user_code}
```

---

请严格按照以下模板输出：

## 题意提炼
用 1-2 句话概括题目核心问题，不要复述原题，提炼出数学/算法本质。

## 关键观察
列出解这道题需要发现的 1-3 个关键性质或观察（这是整个分析最重要的部分）。用 `>` 引用块格式，每条观察后附 1 句解释为什么这个观察成立。

## 总结
- 思路对比：[大佬的解题思路] vs [选手的解题思路]，指出**思路分叉点**在哪里
- 复杂度：大佬 O(?) / 选手 O(?)
- 一句话：[末尾用 **加粗** 标出选手最需要掌握的一个知识点或思维方式]

## 发现

输出 3-5 条发现，每条**必须关于算法逻辑或核心实现**。格式：

### [!severity] 发现标题

> 你的第 X-Y 行 → 大佬第 A-B 行

正文：分析此处的**算法思路差异**，引用关键代码用 `code`，重点结论用 **加粗**。
每条发现结尾附一句"**你应该学会：**[具体可操作的建议]"。

---

severity 取值规则：
- `[!critical]`：算法选择不同、复杂度差异、逻辑错误、遗漏关键性质
- `[!warn]`：核心逻辑中可优化的实现（边界处理、状态转移可简化等）
- `[!good]`：大佬代码中值得学习的算法技巧或巧妙实现\
"""


COMPREHENSIVE_PROMPT = """\
你是一位算法竞赛教练，帮选手做赛后 upsolving 复盘。通过对比选手和多位大佬的代码，提炼出选手需要掌握的**算法思路和关键观察**。请用中文输出。

## 绝对禁止分析的内容（违反即视为失败）

以下全部属于竞赛选手的个人模板习惯，与解题能力完全无关，**一个字都不要提**：
- 头文件、`#pragma`、`bits/stdc++.h`
- 宏定义（`#define int long long`、`#define pb` 等）
- `typedef`、`using namespace std`
- I/O 优化（`ios::sync_with_stdio`、`cin.tie`、`scanf/printf`）
- 模板预声明的变量（即使未使用）、调试宏、快读模板
- 变量命名风格、代码缩进风格
- 多组数据循环写法（`while(t--)` vs `for`）
- 函数命名或封装风格（把代码写在 `solve()` 还是 `main()` 里）

如果你发现自己在写"模板过于复杂""有未使用的变量""大佬的代码更简洁"之类的话，**立刻停下来，删掉这段，换成算法分析**。

---

## 题目
Contest {contest_id}, Problem {problem_index}
链接：https://codeforces.com/contest/{contest_id}/problem/{problem_index}
{problem_statement_section}
{boss_sections}

## 选手代码（{user_lang}）
```{user_lang_id}
{user_code}
```

---

请严格按照以下模板输出：

## 题意提炼
用 1-2 句话概括题目核心问题，不要复述原题，提炼出数学/算法本质。

## 关键观察
列出解这道题需要发现的 1-3 个关键性质或观察（这是整个分析最重要的部分）。用 `>` 引用块格式，每条观察后附 1 句解释为什么这个观察成立。

## 总结
- 思路对比：[各位大佬的解题思路] vs [选手的解题思路]，指出**思路分叉点**在哪里
- 复杂度：各人的时间复杂度对比
- 一句话：[末尾用 **加粗** 标出选手最需要掌握的一个知识点或思维方式]

## 发现

综合所有大佬代码与选手代码的对比，输出 3-8 条发现，每条**必须关于算法逻辑或核心实现**。格式：

### [!severity] 发现标题（来自 {{handle}}）

> 你的第 X-Y 行 → {{handle}} 第 A-B 行

正文：分析此处的**算法思路差异**，引用关键代码用 `code`，重点结论用 **加粗**。
每条发现结尾附一句"**你应该学会：**[具体可操作的建议]"。

---

severity 取值规则：
- `[!critical]`：算法选择不同、复杂度差异、逻辑错误、遗漏关键性质
- `[!warn]`：核心逻辑中可优化的实现（边界处理、状态转移可简化等）
- `[!good]`：大佬代码中值得学习的算法技巧或巧妙实现

注意：每条发现的标题括号内必须标注来源 handle。
如果多位大佬在同一处有相同的优秀做法，可以合并为一条发现，标注"来自 {{handle1}} / {{handle2}}"。\
"""


def run_analysis(
    problem: str,
    user_code: str,
    user_lang: str,
    user_cfg: dict | None = None,
    user_id: int | None = None,
    live_progress=None,
) -> Generator[Event, None, None]:
    """
    Synchronous generator. Yields event dicts:
      progress      {"type": "progress", "message": str}
      code_data     {"type": "code_data", "role": str, "source": str, "url": str,
                     "lang": str, "sid": int, "handle": str}
      analysis_start{}
      analysis_chunk{"type": "analysis_chunk", "text": str}
      done          {"type": "done", "analysis_id": str, "problem": str}
      error         {"type": "error", "message": str}
    """
    m = re.fullmatch(r"(\d+)\s*([A-Za-z]\d*)", problem.strip())
    if not m:
        yield {"type": "error", "message": f"无法解析题目编号：{problem!r}。示例：2094A"}
        return

    contest_id = int(m.group(1))
    problem_index = m.group(2).upper()

    if contest_id >= 100000:
        yield {"type": "error", "message": "Gym 题目不支持大佬代码对比分析"}
        return

    # Resolve github_login for logging
    _github_login = ""
    if user_id:
        try:
            u = _sync_db(db.get_user_by_id(user_id))
            if u:
                _github_login = u.github_login
        except Exception:
            pass

    alog = AnalysisLogger(user_id, _github_login, "analyze",
                          contest_id=contest_id, problem_index=problem_index)
    alog.step("parse_problem", "ok", f"{contest_id}{problem_index}")

    yield {"type": "progress", "message": "从洛谷获取题面…"}
    luogu = fetch_luogu_statement(contest_id, problem_index)
    if luogu:
        alog.step("fetch_luogu", "ok", f"{len(luogu['text'])} chars")
        yield {"type": "progress", "message": f"题面获取成功：{luogu['title']}"}
        yield {"type": "problem_statement", "text": luogu["text"]}
    else:
        alog.step("fetch_luogu", "skip", "not found")
        yield {"type": "progress", "message": "洛谷暂未收录此题（可能题目过新），跳过题面获取"}

    try:
        cfg = user_cfg or _load_config()
    except Exception as e:
        alog.step("load_config", "error", str(e))
        alog.finish("error", error=str(e))
        yield {"type": "error", "message": f"读取配置失败：{e}"}
        return
    alog.step("load_config", "ok")

    settings = cfg.get("settings", {})
    lgm_handles: list[str] = cfg.get("lgm_handles", [])

    delay = float(settings.get("request_delay", 1.5))
    lang_filter = user_lang if settings.get("same_language_only", True) else None

    if not lgm_handles:
        alog.step("check_handles", "error", "empty lgm_handles")
        alog.finish("error", error="未配置大佬 handle")
        yield {"type": "error", "message": "未配置大佬 handle，请在配置页添加"}
        return

    if user_id:
        profile = Path(__file__).parent / "data" / "cf_browser_profiles" / str(user_id)
        if not profile.exists():
            profile = Path(__file__).parent / "data" / "cf_browser_profiles" / "shared"
    else:
        profile = Path(__file__).parent / "data" / "cf_browser_profiles" / "shared"
    client = CFClient("", delay=delay, profile_dir=profile)

    try:
        llm = LLMClient(cfg)
    except Exception as e:
        alog.step("llm_init", "error", str(e))
        alog.finish("error", error=str(e))
        yield {"type": "error", "message": f"LLM 初始化失败：{e}"}
        return
    alog.step("llm_init", "ok")

    active_llm = cfg.get("settings", {}).get("active_llm", "claude")

    CF_BASE = "https://codeforces.com"

    def _live(msg):
        if live_progress:
            live_progress({"type": "progress", "message": msg})

    yield {"type": "progress", "message": f"打开比赛页面，等待 Cloudflare 验证…"}
    try:
        page = client._get_page()
        _live("浏览器已启动，正在加载页面…")
        page.goto(f"{CF_BASE}/contest/{contest_id}/status", wait_until="load", timeout=60000)
        _live("页面已加载，等待 Cloudflare 放行…")
        client._wait_cf_challenge(page, timeout=90.0, on_progress=_live)
        yield {"type": "progress", "message": "Cloudflare 验证通过 ✓"}
        alog.step("cf_challenge", "ok")
    except Exception as e:
        alog.step("cf_challenge", "error", str(e))
        alog.finish("error", error=f"CF challenge failed: {e}")
        yield {"type": "error", "message": f"打开比赛页面失败：{e}"}
        client.close()
        return

    problem_name = ""
    try:
        standings = client._api("contest.standings", {
            "contestId": contest_id, "from": 1, "count": 1,
        })
        for p in standings.get("problems", []):
            if p.get("index") == problem_index:
                problem_name = p.get("name", "")
                break
    except Exception:
        pass

    boss_info = None
    yield {"type": "progress", "message": f"寻找大佬代码（{', '.join(lgm_handles[:3])}…）"}
    try:
        sub = client.find_handle_submission(contest_id, problem_index, lgm_handles, lang_filter, problem_name=problem_name, on_progress=_live)
        if not sub:
            alog.step("find_lgm", "error", "no AC submission found")
            alog.finish("error", error="大佬列表中无人解答此题")
            yield {"type": "error", "message": "大佬列表中无人解答此题（或语言不符），分析终止"}
            return
        sid = sub["id"]
        boss_contest_id = sub["problem"].get("contestId", contest_id)
        handle = sub["author"]["members"][0]["handle"] if sub["author"]["members"] else "?"
        alog.step("find_lgm", "ok", f"{handle} #{sid}")
        yield {"type": "progress", "message": f"找到 {handle} #{sid}（contest {boss_contest_id}），获取源码…"}
        src = client.get_submission_source(boss_contest_id, sid, challenge_passed=True)
        boss_info = {
            "submission_id": sid,
            "source": src,
            "url": f"https://codeforces.com/contest/{boss_contest_id}/submission/{sid}",
            "language": sub["programmingLanguage"],
            "handle": handle,
        }
        alog.step("fetch_source", "ok", f"{len(src or '')} chars")
        yield {"type": "progress", "message": f"大佬代码获取完成：{handle} #{sid}"}
        yield {
            "type": "code_data", "role": "lgm",
            "source": src or "", "url": boss_info["url"],
            "lang": sub["programmingLanguage"], "sid": sid, "handle": handle,
        }
    except Exception as e:
        alog.step("find_lgm", "error", str(e))
        alog.finish("error", error=str(e))
        yield {"type": "error", "message": f"获取大佬代码时出错：{e}"}
        return

    analysis_id = str(uuid.uuid4())

    def gf(info, key):
        return info.get(key) if info else None

    record = {
        "id": analysis_id,
        "contest_id": contest_id,
        "problem_index": problem_index,
        "user_lang": user_lang,
        "user_code": user_code,
        "lgm_source": gf(boss_info, "source"),
        "lgm_url": gf(boss_info, "url"),
        "lgm_lang": gf(boss_info, "language"),
        "lgm_sid": gf(boss_info, "submission_id"),
        "lgm_handle": gf(boss_info, "handle"),
        "analysis": "",
        "notes": "",
        "created_at": datetime.now().isoformat(),
        "handle": cfg.get("codeforces", {}).get("handle", ""),
        "user_submission_id": None,
        "style_review": "",
        "problem_statement": luogu["text"] if luogu else "",
    }
    _sync_db(db.insert_analysis(record, user_id=user_id))
    alog.step("save_record", "ok")

    yield {"type": "progress", "message": f"正在调用 {active_llm} 进行深度分析..."}
    yield {"type": "analysis_start"}

    boss_handle = gf(boss_info, "handle") or "?"
    user_lang_id = user_lang.lower().split()[0]

    stmt_section = ""
    if luogu:
        stmt_section = f"\n## 题面\n{luogu['text']}\n"

    prompt = ANALYSIS_PROMPT.format(
        contest_id=contest_id,
        problem_index=problem_index,
        problem_statement_section=stmt_section,
        boss_handle=boss_handle,
        boss_meta=_meta(boss_info),
        boss_lang=_lang(boss_info),
        boss_src=_src(boss_info),
        user_lang=user_lang,
        user_lang_id=user_lang_id,
        user_code=user_code,
    )

    chunks: list[str] = []
    try:
        for text in llm.stream(prompt, max_tokens=4096):
            chunks.append(text)
            yield {"type": "analysis_chunk", "text": text}
        alog.step("llm_call", "ok", f"{len(chunks)} chunks")
    except Exception as e:
        alog.step("llm_call", "error", str(e))
        alog.finish("error", model=active_llm, error=f"LLM API error: {e}")
        yield {"type": "error", "message": f"LLM API 错误（{active_llm}）：{e}"}
        return

    full_text = "".join(chunks)
    _sync_db(db.update_analysis_text(analysis_id, full_text, user_id=user_id))

    alog.finish("success", model=active_llm,
                summary=f"{contest_id}{problem_index} vs {boss_handle}, {len(full_text)} chars")

    yield {"type": "done", "analysis_id": analysis_id, "problem": f"{contest_id}{problem_index}"}
