"""
Codeforces client using Playwright with cookie injection.

CF's login page uses Cloudflare Turnstile — automated form submission is
blocked regardless of stealth techniques. The correct approach:

  1. Run setup_cf_session.py locally to log in and export cookies to JSON.
  2. login(): inject cookies and verify the session is still valid.
  3. API calls and source fetching: use the browser's session cookies via
     page.evaluate(fetch(...)), which carries them automatically.

Do NOT attempt to auto-fill or auto-submit the login form.
"""

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

CF_BASE = "https://codeforces.com"
CF_COOKIES_FILE = Path(__file__).parent / "data" / "cf_cookies.json"


class CFClient:
    def __init__(self, handle: str, delay: float = 1.5, profile_dir: Path | None = None):
        self.handle = handle
        self.delay = delay
        self.profile_dir = profile_dir
        self._last_req = 0.0
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None

    def _wait(self, extra: float = 0.0):
        elapsed = time.time() - self._last_req
        wait = max(0.0, self.delay + extra - elapsed)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()

    def _get_page(self) -> Page:
        if self._page is not None:
            return self._page

        import os
        headless = os.environ.get("CF_HEADLESS", "false").lower() == "true"
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--window-size=1366,768",
            ],
        )
        self._ctx = browser.new_context(viewport={"width": 1366, "height": 768})
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._load_cookies()
        self._page = self._ctx.new_page()
        return self._page

    def _load_cookies(self):
        cf = self.profile_dir / "cf_cookies.json" if self.profile_dir else None
        for path in [cf, CF_COOKIES_FILE]:
            if path and path.exists():
                try:
                    cookies = json.loads(path.read_text(encoding="utf-8"))
                    if cookies:
                        self._ctx.add_cookies(cookies)
                        return
                except Exception:
                    continue

    # ------------------------------------------------------------------ #
    # CF challenge helpers                                                 #
    # ------------------------------------------------------------------ #

    def _wait_cf_challenge(self, page: Page, timeout: float = 60.0, on_progress=None) -> None:
        """Wait for Cloudflare interstitial pages to fully resolve.

        Handles:
          - "Just a moment..." / "请稍候" — JS PoW challenge
          - "Please Wait" — CF interstitial before showing page
          - "Verification" / "正在验证" / "安全验证" — Turnstile
          - Empty/blank page — still loading or redirecting
        """
        cf_keywords = [
            "just a moment", "please wait", "please turn",
            "browser is being checked", "attention required",
            "verification", "checking your browser",
            "请稍候", "正在验证", "安全验证", "正在进行", "请等待",
        ]
        start = time.time()
        deadline = start + timeout
        last_report = 0

        def _report(phase):
            nonlocal last_report
            elapsed = int(time.time() - start)
            if on_progress and elapsed >= last_report + 5:
                last_report = elapsed
                on_progress(f"Cloudflare 验证中… ({elapsed}s，{phase})")

        # Phase 1: wait until page has a title AND no challenge keywords
        # Check both HTML source and visible text (CF challenge text is JS-rendered)
        while time.time() < deadline:
            try:
                title = page.title().lower().strip()
                html_snippet = page.content()[:1000].lower()
                visible_text = (page.inner_text("body") or "")[:500].lower()
            except Exception:
                time.sleep(1)
                continue
            # Empty title = page still loading/redirecting, keep waiting
            if not title:
                time.sleep(0.5)
                continue
            combined = title + " " + html_snippet + " " + visible_text
            if not any(kw in combined for kw in cf_keywords):
                break
            _report("等待页面响应")
            time.sleep(0.5)

        # Phase 2: wait for load + confirm it's a real CF page (no challenge text anywhere)
        while time.time() < deadline:
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:
                time.sleep(1)
                continue
            try:
                url = page.url or ""
                title = page.title().lower().strip()
                visible_text = (page.inner_text("body") or "")[:500].lower()
            except Exception:
                time.sleep(1)
                continue
            if ("codeforces.com" in url
                    and title
                    and not any(kw in title or kw in visible_text for kw in cf_keywords)):
                break
            _report("验证页面内容")
            time.sleep(1)

        # Phase 3: settle time for JS rendering
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # Login / verify                                                       #
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        """Verify that the saved Chrome profile still has a valid CF session.

        Does NOT attempt to auto-fill or submit the login form — Turnstile
        blocks that. If the session is expired, the user must re-run setup_cf.
        """
        print("[*] 检查 Codeforces 登录状态...")
        page = self._get_page()
        self._wait()
        page.goto(f"{CF_BASE}/", wait_until="load", timeout=60000)
        self._wait_cf_challenge(page)

        if page.query_selector('a[href*="logout"]'):
            print("[OK] 已登录")
            return True

        raise RuntimeError(
            "CF 会话已失效，请在配置页点击「建立 CF 会话」，"
            "在弹出的 Chrome 窗口中手动登录后重试。"
        )

    # ------------------------------------------------------------------ #
    # API calls via browser fetch (carries session cookies automatically)  #
    # ------------------------------------------------------------------ #

    def _api(self, method: str, params: dict | None = None) -> object:
        self._wait()
        page = self._get_page()

        qs = ""
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())

        url = f"/api/{method}{'?' + qs if qs else ''}"

        result = page.evaluate(f"""
            async () => {{
                const resp = await fetch({json.dumps(url)});
                const text = await resp.text();
                return text;
            }}
        """)

        try:
            data = json.loads(result)
        except Exception:
            raise RuntimeError(f"CF API [{method}] 返回非 JSON：{result[:200]}")

        if data.get("status") != "OK":
            raise RuntimeError(f"CF API [{method}] error: {data.get('comment', '?')}")
        return data["result"]

    # ------------------------------------------------------------------ #
    # Submission source                                                    #
    # ------------------------------------------------------------------ #

    def get_submission_source(self, contest_id: int, submission_id: int,
                              challenge_passed: bool = False) -> str | None:
        """Navigate to the submission page and scrape source from the DOM.

        Args:
            challenge_passed: If True, skip the status-page warm-up step
                (caller already navigated to a CF page and passed the challenge).
        """
        page = self._get_page()
        target = f"{CF_BASE}/contest/{contest_id}/submission/{submission_id}"

        # Step 1: 打开比赛 status 页，让 CF challenge 在这里通过。
        if not challenge_passed:
            status_url = f"{CF_BASE}/contest/{contest_id}/status"
            self._wait()
            page.goto(status_url, wait_until="load", timeout=60000)
            self._wait_cf_challenge(page, timeout=90.0)

        # Step 2: 导航到提交页。
        # CF 会在提交页触发 challenge（"Please wait. Your browser is being checked."），
        # challenge 通过后往往重定向回 status 页而非提交页。
        # 此时 challenge cookie 已生效，等几秒后再导航一次即可。
        for attempt in range(3):
            if attempt > 0:
                # 重定向回来后必须等待足够久，否则 CF 返回 "not allowed"
                wait_secs = 15
                print(f"[*] 等待 {wait_secs} 秒后重试导航到提交页 (attempt {attempt+1})...")
                # 分段 sleep，中间 ping 一下页面防止浏览器被回收
                for _ in range(wait_secs // 3):
                    time.sleep(3)
                    try:
                        page.title()  # keep-alive
                    except Exception:
                        pass
            else:
                self._wait()

            page.goto(target, wait_until="load", timeout=60000)
            self._wait_cf_challenge(page, timeout=90.0)

            current = (page.url or "").split("?")[0].rstrip("/")
            if current == target.rstrip("/"):
                break
            print(f"[*] 被重定向到 {page.url}")

        try:
            page.wait_for_selector(
                "pre#program-source-text, pre.source-code",
                timeout=30000,
            )
        except Exception:
            print(f"[!] 提交页源码元素未找到，当前 URL: {page.url}")
            return None

        pre = (
            page.query_selector("pre#program-source-text")
            or page.query_selector("pre.source-code")
        )
        return pre.inner_text() if pre else None

    # ------------------------------------------------------------------ #
    # Submission finders                                                   #
    # ------------------------------------------------------------------ #

    def find_handle_submission(
        self,
        contest_id: int,
        problem_index: str,
        handles: list[str],
        language_filter: str | None = None,
        problem_name: str | None = None,
        on_progress=None,
    ) -> dict | None:
        """Find an AC submission from one of the handles.

        Uses user.status + problem name match (handles Div.1/Div.2 splits).
        """
        if not problem_name:
            return None

        def _norm_lang(lang: str) -> str:
            l = lang.lower()
            if "c++" in l or "gcc" in l or "g++" in l or "clang++" in l:
                return "cpp"
            if "python" in l or "pypy" in l:
                return "python"
            if "java" in l and "javascript" not in l:
                return "java"
            if "kotlin" in l:
                return "kotlin"
            if "rust" in l:
                return "rust"
            if "c#" in l or "csharp" in l or "mono c" in l:
                return "csharp"
            if "javascript" in l or "node" in l:
                return "js"
            if "go " in l or l.startswith("go"):
                return "go"
            return l

        def _match_lang(s):
            return language_filter is None or _norm_lang(language_filter) == _norm_lang(s["programmingLanguage"])

        pname_lower = problem_name.strip().lower()
        for i, handle in enumerate(handles):
            if on_progress:
                on_progress(f"查找 {handle} 的 AC（{i+1}/{len(handles)}）…")
            print(f"[*] 查找 {handle} 的 AC...")
            try:
                subs = self._api("user.status", {
                    "handle": handle,
                    "from": 1,
                    "count": 500,
                })
            except Exception:
                continue
            for s in subs:
                if (
                    s.get("verdict") == "OK"
                    and s["problem"].get("name", "").strip().lower() == pname_lower
                    and _match_lang(s)
                ):
                    print(f"[OK] 找到 {handle} 的 AC：#{s['id']} (contest {s['problem'].get('contestId')})")
                    return s
        return None

    # ------------------------------------------------------------------ #
    # Cleanup                                                              #
    # ------------------------------------------------------------------ #

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._ctx = None
        self._pw = None
