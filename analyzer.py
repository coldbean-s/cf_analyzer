"""
Core analysis generator.
Yields event dicts consumed by both the FastAPI SSE endpoint and the CLI.
"""

import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Generator

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



ANALYSIS_PROMPT = """\
你是一位算法竞赛教练，对比分析选手与大佬的 Codeforces 代码。请用中文输出，格式严格按照下方模板。

## 题目
Contest {contest_id}, Problem {problem_index}
链接：https://codeforces.com/contest/{contest_id}/problem/{problem_index}

---

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

请严格按照以下模板输出，不要增减标题层级，不要改变格式标记：

## 总结
- 算法差异：[同/异] · [简述，如"都用了 KMP"或"大佬用线段树，你用暴力"]
- 代码量对比：[倍数，如 2.3]x · 你 [行数] 行 / 大佬 [行数] 行
- 关键差距：[1-2 个关键词] · [简短描述]
- 一句话：[一句话总结全局差异，末尾用 **加粗** 标出重点学习方向]

## 发现

针对两份代码的对比，输出 3-5 条发现。每条必须使用如下格式：

### [!severity] 发现标题

> 你的第 X-Y 行 → 大佬第 A-B 行

正文：详细分析此处差异，引用关键代码用 `code`，重点结论用 **加粗**。

---

severity 取值规则：
- `[!critical]`：关键差距——算法选择不同、复杂度差异、严重逻辑问题
- `[!warn]`：可优化——实现不够简洁、多余分支、可改进但不致命
- `[!good]`：值得学习——大佬代码中的精妙技巧，或选手做得好的地方

如果某条发现只涉及一方代码，位置行写 `> 大佬第 X-Y 行` 或 `> 你的第 X-Y 行` 即可。\
"""


COMPREHENSIVE_PROMPT = """\
你是一位算法竞赛教练，综合对比选手与多位大佬的 Codeforces 代码。请用中文输出，格式严格按照下方模板。

## 题目
Contest {contest_id}, Problem {problem_index}
链接：https://codeforces.com/contest/{contest_id}/problem/{problem_index}

---

{boss_sections}

## 选手代码（{user_lang}）
```{user_lang_id}
{user_code}
```

---

请严格按照以下模板输出，不要增减标题层级，不要改变格式标记：

## 总结
- 算法差异：[描述选手和各位大佬分别使用的算法/思路，标注谁用了什么]
- 代码量对比：[列出各人行数，如"你 68 行 / tourist 25 行 / jiangly 30 行"]
- 关键差距：[1-2 个关键词] · [综合多人对比后的核心差距]
- 一句话：[综合总结，末尾用 **加粗** 标出最值得学习的方向]

## 发现

综合所有大佬代码与选手代码的对比，输出 3-8 条发现。每条必须使用如下格式：

### [!severity] 发现标题（来自 {{handle}}）

> 你的第 X-Y 行 → {{handle}} 第 A-B 行

正文：详细分析此处差异，引用关键代码用 `code`，重点结论用 **加粗**。

---

severity 取值规则：
- `[!critical]`：关键差距——算法选择不同、复杂度差异、严重逻辑问题
- `[!warn]`：可优化——实现不够简洁、多余分支、可改进但不致命
- `[!good]`：值得学习——大佬代码中的精妙技巧，或选手做得好的地方

注意：每条发现的标题括号内必须标注来源 handle，便于读者知道这是参考谁的代码。
如果多位大佬在同一处有相同的优秀做法，可以合并为一条发现，标注"来自 {{handle1}} / {{handle2}}"。\
"""


def run_analysis(
    problem: str,
    user_code: str,
    user_lang: str,
    user_cfg: dict | None = None,
    user_id: int | None = None,
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

    try:
        cfg = user_cfg or _load_config()
    except Exception as e:
        yield {"type": "error", "message": f"读取配置失败：{e}"}
        return

    settings = cfg.get("settings", {})
    lgm_handles: list[str] = cfg.get("lgm_handles", [])

    delay = float(settings.get("request_delay", 1.5))
    lang_filter = user_lang if settings.get("same_language_only", True) else None

    if not lgm_handles:
        yield {"type": "error", "message": "未配置大佬 handle，请在配置页添加"}
        return

    # Use user-specific profile if available, otherwise shared profile
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
        yield {"type": "error", "message": f"LLM 初始化失败：{e}"}
        return

    active_llm = cfg.get("settings", {}).get("active_llm", "claude")

    # --- 打开比赛 status 页，通过 CF challenge，browser 就绪后复用 ---
    CF_BASE = "https://codeforces.com"
    yield {"type": "progress", "message": f"打开比赛页面，等待 Cloudflare 验证…"}
    try:
        page = client._get_page()
        page.goto(f"{CF_BASE}/contest/{contest_id}/status", wait_until="load", timeout=60000)
        client._wait_cf_challenge(page, timeout=90.0)
        yield {"type": "progress", "message": "Cloudflare 验证通过 ✓"}
    except Exception as e:
        yield {"type": "error", "message": f"打开比赛页面失败：{e}"}
        client.close()
        return

    # --- Resolve problem name for user.status matching ---
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

    # --- Boss code ---
    boss_info = None
    yield {"type": "progress", "message": f"寻找大佬代码（{', '.join(lgm_handles[:3])}…）"}
    try:
        sub = client.find_handle_submission(contest_id, problem_index, lgm_handles, lang_filter, problem_name=problem_name)
        if not sub:
            yield {"type": "error", "message": "大佬列表中无人解答此题（或语言不符），分析终止"}
            return
        sid = sub["id"]
        boss_contest_id = sub["problem"].get("contestId", contest_id)
        handle = sub["author"]["members"][0]["handle"] if sub["author"]["members"] else "?"
        yield {"type": "progress", "message": f"找到 {handle} #{sid}（contest {boss_contest_id}），获取源码…"}
        src = client.get_submission_source(boss_contest_id, sid, challenge_passed=True)
        boss_info = {
            "submission_id": sid,
            "source": src,
            "url": f"https://codeforces.com/contest/{boss_contest_id}/submission/{sid}",
            "language": sub["programmingLanguage"],
            "handle": handle,
        }
        yield {"type": "progress", "message": f"大佬代码获取完成：{handle} #{sid}"}
        yield {
            "type": "code_data", "role": "lgm",
            "source": src or "", "url": boss_info["url"],
            "lang": sub["programmingLanguage"], "sid": sid, "handle": handle,
        }
    except Exception as e:
        yield {"type": "error", "message": f"获取大佬代码时出错：{e}"}
        return

    # --- Save record ---
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
        "problem_statement": "",
    }
    _sync_db(db.insert_analysis(record, user_id=user_id))

    # --- LLM streaming analysis ---
    yield {"type": "progress", "message": f"正在调用 {active_llm} 进行深度分析..."}
    yield {"type": "analysis_start"}

    boss_handle = gf(boss_info, "handle") or "?"
    user_lang_id = user_lang.lower().split()[0]

    prompt = ANALYSIS_PROMPT.format(
        contest_id=contest_id,
        problem_index=problem_index,
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
    except Exception as e:
        yield {"type": "error", "message": f"LLM API 错误（{active_llm}）：{e}"}
        return

    full_text = "".join(chunks)
    _sync_db(db.update_analysis_text(analysis_id, full_text, user_id=user_id))

    yield {"type": "done", "analysis_id": analysis_id, "problem": f"{contest_id}{problem_index}"}
