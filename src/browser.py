"""Playwright 브라우저 — 로그인 유지 전용 프로필로 Chrome 실행."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


LogFn = Callable[[str], None]

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
]

def _profile_dir() -> Path:
    try:
        from paths import get_data_dir

        return Path(get_data_dir()) / "chrome_profile"
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "chrome_profile"


PROFILE_DIR = _profile_dir()


@dataclass
class BrowserSession:
    pw: Playwright
    browser: Browser | None
    context: BrowserContext
    page: Page
    mode: str  # "persistent" | "launch"


def open_browser(
    *,
    headless: bool = False,
    on_log: LogFn | None = None,
) -> BrowserSession:
    """전용 프로필로 Chrome을 열어 로그인·쿠키를 유지한다."""
    log = on_log or (lambda _m: None)
    pw = sync_playwright().start()
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)

    launch_opts = {
        "headless": headless,
        "args": list(_LAUNCH_ARGS),
        "viewport": {"width": 1400, "height": 900},
        "locale": "ko-KR",
    }

    last_err: Exception | None = None
    for channel, name in (("chrome", "Chrome"), ("msedge", "Edge")):
        try:
            log(f"  → {name} 실행 (로그인 유지 프로필)")
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=channel,
                **launch_opts,
            )
            page = context.pages[0] if context.pages else context.new_page()
            return BrowserSession(
                pw=pw, browser=None, context=context, page=page, mode="persistent"
            )
        except Exception as exc:
            last_err = exc
            log(f"  → {name} 실패: {type(exc).__name__}: {exc}")

    try:
        log("  → Playwright Chromium 실행 (임시)")
        browser = pw.chromium.launch(headless=headless, args=list(_LAUNCH_ARGS))
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="ko-KR")
        page = context.new_page()
        return BrowserSession(
            pw=pw, browser=browser, context=context, page=page, mode="launch"
        )
    except Exception as exc:
        pw.stop()
        detail = f" / 이전: {last_err}" if last_err else ""
        raise RuntimeError(f"브라우저 실행 실패{detail}") from exc


def close_browser_session(session: BrowserSession | None, *, on_log: LogFn | None = None) -> None:
    if session is None:
        return
    log = on_log or (lambda _m: None)
    try:
        if session.mode == "persistent":
            # 프로필 유지를 위해 컨텍스트 정상 종료 (다음 실행 시 같은 로그인)
            session.context.close()
            log("  → 브라우저 종료 (로그인 세션은 프로필에 저장됨)")
        elif session.browser:
            session.browser.close()
            log("  → 브라우저 종료")
    except Exception:
        pass
    try:
        session.pw.stop()
    except Exception:
        pass


def launch_browser(
    *,
    headless: bool = False,
    on_log: LogFn | None = None,
) -> tuple[Playwright, Browser]:
    """하위 호환."""
    session = open_browser(headless=headless, on_log=on_log)
    if session.browser is not None:
        return session.pw, session.browser
    # persistent 모드는 Browser 핸들이 없음 — 호출측은 open_browser 사용 권장
    raise RuntimeError("launch_browser는 persistent 모드에서 사용할 수 없습니다. open_browser를 쓰세요.")
