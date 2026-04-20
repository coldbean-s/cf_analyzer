"""
本地运行：打开 Chrome 窗口，手动登录 Codeforces，导出 cookies 到 JSON。
Docker 容器通过 volume mount 读取 cookies，跨平台兼容。

用法：python setup_cf_session.py
"""

import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

COOKIES_FILE = Path(__file__).parent / "data" / "cf_cookies.json"


def main():
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    print("正在打开 Chrome，请手动登录 Codeforces...")
    print("登录成功后会自动检测并导出 cookies。\n")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        channel="chrome",
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--window-size=1366,768",
        ],
    )
    ctx = browser.new_context(viewport={"width": 1366, "height": 768})
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )

    if COOKIES_FILE.exists():
        try:
            old = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            ctx.add_cookies(old)
            print(f"已加载旧 cookies（{len(old)} 条），如仍有效可直接关闭窗口。")
        except Exception:
            pass

    page = ctx.new_page()
    page.goto("https://codeforces.com/enter", wait_until="load", timeout=60000)

    print("等待你登录... （检测到登录态后自动保存）")
    saved = False
    try:
        while True:
            if len(ctx.pages) == 0:
                break
            try:
                for p in ctx.pages:
                    if "codeforces.com" in (p.url or "") and p.query_selector('a[href*="logout"]'):
                        cookies = ctx.cookies(["https://codeforces.com"])
                        COOKIES_FILE.write_text(
                            json.dumps(cookies, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        print(f"\n已导出 {len(cookies)} 条 cookies 到 {COOKIES_FILE}")
                        saved = True
                        raise StopIteration
            except StopIteration:
                break
            except Exception:
                pass
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    if not saved and len(ctx.pages) > 0:
        cookies = ctx.cookies(["https://codeforces.com"])
        if cookies:
            COOKIES_FILE.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"\n已导出 {len(cookies)} 条 cookies 到 {COOKIES_FILE}")
            saved = True

    try:
        ctx.close()
        browser.close()
    except Exception:
        pass
    pw.stop()

    if saved:
        print("会话已保存，容器无需重启即可使用。")
    else:
        print("警告：未检测到登录态，cookies 未导出。")


if __name__ == "__main__":
    main()
