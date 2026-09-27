"""네이버 서치어드바이저 — 보드의「소유확인 진행」만 일괄 처리.

흐름:
1) 네이버 로그인 → 서치어드바이저 보드
2) 「소유확인 진행」링크가 있는 행을 모아 처리
3) 검증 화면에서 HTML 파일/태그 선택 없이 「소유확인」버튼만 클릭
4) 보안문자(캡챠) 모달만 OpenAI로 해결
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from src.browser import BrowserSession, close_browser_session, open_browser
from src.naver_captcha import NaverCaptchaSolver

LogFn = Callable[[str], None]
CheckpointFn = Callable[[], bool]

NAVER_LOGIN = "https://nid.naver.com/nidlogin.login"
ADVISOR_BOARD = "https://searchadvisor.naver.com/console/board"
ADVISOR_HOME = "https://searchadvisor.naver.com/"


@dataclass
class VerifyResult:
    site_url: str
    ok: bool
    message: str = ""


def normalize_site(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if not text.startswith("http"):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return text.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def load_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class OwnershipBatchClient:
    def __init__(
        self,
        *,
        headless: bool = False,
        openai_api_key: str = "",
        on_log: LogFn | None = None,
        checkpoint: CheckpointFn | None = None,
    ) -> None:
        self.headless = headless
        self.openai_api_key = (openai_api_key or "").strip()
        self.on_log = on_log or (lambda _m: None)
        self.checkpoint = checkpoint
        self._session: BrowserSession | None = None
        self.page = None
        self._last_dialog_msg = ""
        self._dialog_gen = 0  # 소유확인 클릭마다 증가 — 이전 팝업 무시
        self._complete_dialog_gen = 0  # 완료 문구가 잡힌 세대
        self._cdp_session = None

    def _log(self, msg: str) -> None:
        self.on_log(msg)

    def _ok(self) -> bool:
        if self.checkpoint and not self.checkpoint():
            return False
        return True

    def __enter__(self) -> "OwnershipBatchClient":
        self._session = open_browser(headless=self.headless, on_log=self._log)
        context = self._session.context
        try:
            context.add_init_script(
                """
                (() => {
                  window.__th_last_dialog = '';
                  window.__th_dialog_log = [];
                  window.__th_dialog_gen = 0;
                  window.__th_complete_gen = 0;
                  const NEEDLE = '사이트 소유 확인이 완료되었습니다';
                  const push = (type, msg) => {
                    const m = String(msg || '');
                    window.__th_last_dialog = m;
                    window.__th_dialog_log.push({ type, msg: m, t: Date.now(), gen: window.__th_dialog_gen });
                    if (m.includes(NEEDLE)) {
                      window.__th_complete_gen = window.__th_dialog_gen;
                    }
                  };
                  window.alert = (msg) => { push('alert', msg); };
                  window.confirm = (msg) => { push('confirm', msg); return true; };
                  window.prompt = (msg, def) => { push('prompt', msg); return def == null ? '' : String(def); };
                })();
                """
            )
        except Exception:
            pass
        self.page = self._session.page
        self.page.on("dialog", self._on_dialog)
        self._install_cdp_dialog_guard(self.page)
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        close_browser_session(self._session, on_log=self._log)
        self._session = None
        self.page = None
        self._cdp_session = None

    def _install_cdp_dialog_guard(self, page) -> None:
        try:
            session = page.context.new_cdp_session(page)
            session.send("Page.enable")

            def _on_js_dialog(params: dict) -> None:
                msg = (params or {}).get("message") or ""
                self._note_dialog(msg, source="CDP")
                try:
                    session.send("Page.handleJavaScriptDialog", {"accept": True})
                except Exception:
                    pass

            session.on("Page.javascriptDialogOpening", _on_js_dialog)
            self._cdp_session = session
        except Exception as exc:
            self._log(f"  CDP 다이얼로그 가드 실패(무시): {exc}")

    def _note_dialog(self, msg: str, *, source: str = "") -> None:
        text = str(msg or "")
        self._last_dialog_msg = text
        if "사이트 소유 확인이 완료되었습니다" in text:
            self._complete_dialog_gen = self._dialog_gen
        tag = f"팝업({source})" if source else "팝업"
        if text:
            self._log(f"  {tag}: {text[:120]}")

    def _on_dialog(self, dialog) -> None:
        try:
            raw = getattr(dialog, "message", "")
            msg = raw() if callable(raw) else raw
            self._note_dialog(str(msg or ""))
            dialog.accept()
        except Exception:
            try:
                dialog.accept()
            except Exception:
                pass

    # ── login ─────────────────────────────────────────────
    def login(self, user_id: str, password: str, *, wait_manual_sec: int = 300) -> None:
        page = self.page
        assert page is not None

        # 기존 Chrome에 이미 로그인돼 있으면 바로 진행
        if self._is_logged_in() and not self._has_account_protection():
            self._log("  기존 Chrome 세션 — 이미 로그인됨, 로그인 생략")
            self._ensure_advisor()
            if self._is_logged_in() and not self._has_account_protection():
                return

        self._log("네이버 로그인...")
        page.goto(NAVER_LOGIN, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(1.2)
        if user_id and password:
            try:
                self._fill_credentials(user_id, password)
                self._submit_login()
                self._log(f"  자동 로그인 시도: {user_id}")
            except Exception as exc:
                self._log(f"  자동 입력 실패 — 수동 로그인: {exc}")
        else:
            self._log("  계정 없음 — 브라우저에서 수동 로그인")

        solver = NaverCaptchaSolver(
            page,
            api_key=self.openai_api_key,
            on_log=self._log,
            should_stop=lambda: not self._ok(),
        )
        captcha_rounds = 0
        protection_notified = False
        last_protect_log = 0.0
        # 보호조치 본인인증은 시간이 더 걸리므로 별도 여유
        deadline = time.time() + wait_manual_sec
        while time.time() < deadline:
            if not self._ok():
                raise RuntimeError("중단됨")

            # 보호조치: 작업 진행 금지 — 해제될 때까지 대기
            if self._has_account_protection():
                if not protection_notified:
                    self._log("=" * 48)
                    self._log("⚠ 네이버 계정 보호조치 감지")
                    self._log("  브라우저에서 본인인증/해제를 완료해 주세요.")
                    self._log("  해제되기 전에는 소유확인 작업을 시작하지 않습니다.")
                    self._log("=" * 48)
                    protection_notified = True
                    last_protect_log = time.time()
                    # 보호조치 대기 시간 연장 (최대 +10분)
                    deadline = max(deadline, time.time() + 600)
                elif time.time() - last_protect_log >= 15:
                    self._log("  …보호조치 해제 대기 중 (브라우저에서 완료해 주세요)")
                    last_protect_log = time.time()
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                time.sleep(2.5)
                continue

            if self._is_logged_in():
                # 로그인으로 보이더라도 보호조치 잔여/리다이렉트 재확인
                if self._has_account_protection():
                    continue
                self._log("  네이버 로그인 완료")
                self._ensure_advisor()
                if self._has_account_protection():
                    self._log("  서치어드바이저 이동 후에도 보호조치 화면 — 대기")
                    protection_notified = False
                    continue
                return

            # 일반 로그인 캡챠만 자동 해결 (보호조치와 분리)
            if solver.looks_like_captcha() and not self._has_account_protection():
                if solver.enabled() and captcha_rounds < 8:
                    captcha_rounds += 1
                    self._log("  로그인 캡챠 — OpenAI 해결 시도")
                    if solver.try_solve(max_attempts=5):
                        time.sleep(1.2)
                        continue
                else:
                    self._log("  캡챠를 브라우저에서 직접 완료해 주세요.")
            time.sleep(1.0)

        if self._has_account_protection():
            raise RuntimeError(
                "네이버 계정 보호조치가 해제되지 않아 작업을 시작할 수 없습니다. "
                "브라우저에서 보호조치를 해제한 뒤 다시 실행해 주세요."
            )
        raise RuntimeError("네이버 로그인 시간 초과")

    def _fill_credentials(self, user_id: str, password: str) -> None:
        page = self.page
        assert page is not None
        id_box = page.locator('#id, input[name="id"]').first
        pw_box = page.locator('#pw, input[name="pw"], input[type="password"]').first
        id_box.wait_for(state="visible", timeout=15_000)
        pw_box.wait_for(state="visible", timeout=15_000)
        id_box.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(user_id, delay=35)
        pw_box.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(password, delay=35)

    def _submit_login(self) -> None:
        page = self.page
        assert page is not None
        btn = page.locator('#log\\.login, button[type="submit"], .btn_login').first
        try:
            btn.click(timeout=5_000)
        except Exception:
            page.keyboard.press("Enter")

    def _has_account_protection(self) -> bool:
        """계정 보호조치(본인인증 필요) 화면인지 — 캡챠와 별개."""
        page = self.page
        assert page is not None
        url = (page.url or "").lower()
        url_hits = (
            "idsafety",
            "idsafetyrelease",
            "releasestatus",
            "loginprotect",
            "user2/help",
            "user/help",
            "auth/release",
            "releaseid",
        )
        if any(k in url for k in url_hits):
            return True

        try:
            text = page.evaluate(
                """() => {
                  const body = document.body ? (document.body.innerText || '') : '';
                  const title = document.title || '';
                  return (title + '\\n' + body).slice(0, 8000);
                }"""
            ) or ""
        except Exception:
            return False

        # 캡챠 문구만 있는 경우는 제외 — 보호조치 전용 키워드
        protect_keys = (
            "보호조치",
            "보호 조치",
            "보호조치가 적용",
            "보호조치 해제",
            "보호조치를 해제",
            "계정이 보호",
            "계정 보호",
            "로그인 제한",
            "로그인이 제한",
            "비정상적인 접근",
            "본인 확인이 필요",
            "본인확인이 필요",
            "본인 인증이 필요",
            "본인인증이 필요",
            "안전한 로그인을 위해",
        )
        if any(k in text for k in protect_keys):
            # 영수증/보안문자 캡챠 화면과 구분: 보호조치 전용 표현이거나 URL이 nid 도움말
            captcha_only = (
                ("보안문자" in text or "자동입력 방지" in text or "영수증" in text)
                and "보호조치" not in text
                and "보호 조치" not in text
                and "로그인 제한" not in text
            )
            if captcha_only:
                return False
            return True
        return False

    def _is_logged_in(self) -> bool:
        page = self.page
        assert page is not None
        # 보호조치 중이면 절대 로그인 완료로 보지 않음
        if self._has_account_protection():
            return False
        url = (page.url or "").lower()
        if "nid.naver.com" in url:
            # 로그인·보호·본인인증 관련 nid 페이지는 미완료
            if any(
                k in url
                for k in (
                    "login",
                    "nidlogin",
                    "captcha",
                    "rcaptcha",
                    "idsafety",
                    "help",
                    "release",
                    "protect",
                    "auth",
                )
            ):
                return False
        try:
            if page.locator('#id, input[name="id"]').first.is_visible():
                return False
        except Exception:
            pass
        if "searchadvisor.naver.com" in url:
            return True
        # 쿠키로 세션 확인
        try:
            names = {c.get("name", "") for c in page.context.cookies()}
            if "NID_AUT" in names or "NID_SES" in names:
                if "nid.naver.com" not in url or "login" not in url:
                    return "naver.com" in url
        except Exception:
            pass
        if "naver.com" in url and "nidlogin" not in url and "nid.naver.com" not in url:
            return True
        return False

    def _ensure_advisor(self) -> None:
        page = self.page
        assert page is not None
        if "searchadvisor.naver.com" not in (page.url or ""):
            page.goto(ADVISOR_HOME, wait_until="domcontentloaded", timeout=25_000)
            time.sleep(1.2)
        # 이동 직후 보호조치 리다이렉트 가능
        time.sleep(0.5)

    # ── board / ownership ─────────────────────────────────
    def open_board(self) -> None:
        page = self.page
        assert page is not None
        self._log("서치어드바이저 보드 열기...")
        page.goto(ADVISOR_BOARD, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_selector("table tbody tr, a.api_link", timeout=15_000)
        except Exception:
            self._log("  ⚠ 사이트 목록이 아직 안 보임 — 잠시 대기")
        time.sleep(1.5)

    def _scroll_board(self, *, to_bottom: bool = False, step: int = 600) -> None:
        page = self.page
        assert page is not None
        page.evaluate(
            """({ toBottom, step }) => {
              const candidates = [
                document.querySelector('.v-data-table__wrapper'),
                document.querySelector('.v-main'),
                document.scrollingElement,
                document.documentElement,
                document.body,
              ].filter(Boolean);
              for (const el of candidates) {
                try {
                  if (toBottom) {
                    el.scrollTop = el.scrollHeight;
                  } else {
                    el.scrollTop = (el.scrollTop || 0) + step;
                  }
                } catch (e) {}
              }
              if (toBottom) {
                window.scrollTo(0, document.body.scrollHeight);
              } else {
                window.scrollBy(0, step);
              }
            }""",
            {"toBottom": to_bottom, "step": step},
        )

    def _collect_pending_on_screen(self) -> list[str]:
        page = self.page
        assert page is not None
        batch = page.evaluate(
            """
            () => {
              const out = [];
              const rows = Array.from(document.querySelectorAll('table tbody tr'));
              for (const tr of rows) {
                const link = Array.from(tr.querySelectorAll('a')).find(a => {
                  const t = (a.textContent || '').replace(/\\s+/g, ' ').trim();
                  return t.includes('소유확인') && t.includes('진행');
                });
                if (!link) continue;
                let site = '';
                for (const td of tr.querySelectorAll('td')) {
                  const txt = (td.innerText || '').trim();
                  const m = txt.match(/https?:\\/\\/[^\\s]+/);
                  if (m) { site = m[0]; break; }
                }
                if (!site) {
                  const box = tr.querySelector('td .body-2, td a.api_link, td a');
                  const t = (box && (box.innerText || box.textContent) || '').trim();
                  const m = t.match(/https?:\\/\\/[^\\s]+/);
                  if (m) site = m[0];
                }
                if (site) out.push(site);
              }
              return out;
            }
            """
        )
        result: list[str] = []
        seen: set[str] = set()
        for raw in batch or []:
            site = normalize_site(str(raw))
            if site and site not in seen:
                seen.add(site)
                result.append(site)
        return result

    def list_pending_sites(self) -> list[str]:
        """보드를 스크롤하며「소유확인 진행」사이트를 수집."""
        page = self.page
        assert page is not None
        found: list[str] = []
        seen: set[str] = set()
        stagnant = 0
        self._log("  「소유확인 진행」목록 수집 중...")
        for i in range(25):
            if not self._ok():
                raise RuntimeError("중단됨")
            before = len(found)
            for site in self._collect_pending_on_screen():
                if site not in seen:
                    seen.add(site)
                    found.append(site)
            added = len(found) - before
            if added:
                self._log(f"  …{len(found)}개 발견 (이번 +{added})")
                stagnant = 0
            else:
                stagnant += 1
            if stagnant >= 3 and found:
                break
            if stagnant >= 5:
                break
            self._scroll_board(to_bottom=(i % 3 == 2), step=700)
            time.sleep(0.45)
        # 맨 위로 올려 처리 시작 위치 확보
        try:
            page.evaluate(
                "() => { window.scrollTo(0,0); const w=document.querySelector('.v-data-table__wrapper'); if(w) w.scrollTop=0; }"
            )
        except Exception:
            pass
        self._log(f"  「소유확인 진행」대상 {len(found)}개 수집 완료")
        return found

    def verify_site(self, site_url: str) -> VerifyResult:
        """한 사이트: 보드에서 링크 클릭(또는 verify URL) → 소유확인만 클릭 → 캡챠."""
        site = normalize_site(site_url)
        if not site:
            return VerifyResult(site_url=site_url, ok=False, message="URL 없음")
        self._log(f"======== 소유확인: {site} ========")
        try:
            if not self._ok():
                raise RuntimeError("중단됨")
            self._clear_completion_state()
            cur = (self.page.url or "") if self.page else ""
            if ADVISOR_BOARD not in cur and "/console/board" not in cur:
                self.open_board()
            clicked = self._click_progress_for_site(site)
            if not clicked:
                self._log("  보드에서 못 찾음 — verify 페이지 직접 이동")
                encoded = quote(site, safe="")
                self.page.goto(
                    f"https://searchadvisor.naver.com/console/verify?site={encoded}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                time.sleep(2.0)
            else:
                time.sleep(1.5)

            if not self._has_verify_ui():
                return VerifyResult(site_url=site, ok=False, message="소유확인 화면 없음")

            ok = self._click_ownership_and_solve_captcha()
            if ok:
                return VerifyResult(site_url=site, ok=True, message="소유확인 완료")
            return VerifyResult(site_url=site, ok=False, message="소유확인 실패(완료 팝업 없음)")
        except Exception as exc:
            return VerifyResult(site_url=site, ok=False, message=str(exc))

    def run_batch(
        self,
        sites: list[str] | None = None,
        *,
        only_pending: bool = True,
    ) -> list[VerifyResult]:
        """sites가 비면 보드의「소유확인 진행」전체를 대상으로 함."""
        if self._has_account_protection():
            raise RuntimeError(
                "네이버 계정 보호조치 상태입니다. 해제 후 다시 실행해 주세요."
            )
        self.open_board()
        if self._has_account_protection():
            raise RuntimeError(
                "보드 이동 중 보호조치 화면이 나타났습니다. 해제 후 다시 실행해 주세요."
            )

        want: set[str] | None = None
        if sites:
            want = {normalize_site(s) for s in sites if normalize_site(s)}
            self._log(f"  지정 사이트 {len(want)}개 필터")

        # 한 건씩: 찾기 → 소유확인 → 보드 복귀 (긴 수집 루프에 안 멈춤)
        results: list[VerifyResult] = []
        done: set[str] = set()
        empty_rounds = 0
        max_items = 200

        while len(results) < max_items:
            if not self._ok():
                self._log("중단됨")
                break
            if self._has_account_protection():
                self._log("⚠ 작업 중 보호조치 감지 — 중단")
                break

            cur = self.page.url or ""
            if "/console/board" not in cur:
                self.open_board()

            pending = self._collect_pending_on_screen()
            if want is not None:
                pending = [s for s in pending if s in want]

            nxt = next((s for s in pending if s not in done), None)
            if not nxt:
                # 화면에 없으면 스크롤해서 더 찾기
                empty_rounds += 1
                self._log(f"  다음 대상 탐색 중... ({empty_rounds})")
                self._scroll_board(to_bottom=(empty_rounds % 2 == 0), step=800)
                time.sleep(0.5)
                pending2 = self._collect_pending_on_screen()
                if want is not None:
                    pending2 = [s for s in pending2 if s in want]
                nxt = next((s for s in pending2 if s not in done), None)
                if not nxt:
                    if empty_rounds >= 6:
                        # 지정 URL 중 아직 안 한 것은 verify 직접 시도
                        if want is not None:
                            leftover = [s for s in want if s not in done]
                            if leftover:
                                self._log(f"  보드에 안 보이는 지정 URL {len(leftover)}개 — 직접 이동")
                                for s in leftover:
                                    if not self._ok():
                                        break
                                    done.add(s)
                                    self._log(f"▶ [{len(results)+1}] {s}")
                                    results.append(self.verify_site(s))
                                    try:
                                        self.open_board()
                                    except Exception:
                                        pass
                        break
                    continue
                empty_rounds = 0
            else:
                empty_rounds = 0

            done.add(nxt)
            self._log(f"▶ [{len(results)+1}] {nxt}")
            results.append(self.verify_site(nxt))
            # 다음 건을 위해 보드로
            try:
                if "/console/board" not in (self.page.url or ""):
                    self.open_board()
                else:
                    time.sleep(0.8)
            except Exception as exc:
                self._log(f"  보드 복귀 실패: {exc}")
                try:
                    self.open_board()
                except Exception:
                    break

        if not results:
            self._log("처리할「소유확인 진행」대상이 없습니다.")
        else:
            self._log(f"배치 종료 — 처리 {len(results)}건")
        return results

    def _click_progress_for_site(self, site: str) -> bool:
        page = self.page
        assert page is not None
        host = site.lower().replace("https://", "").replace("http://", "").rstrip("/")
        # 위에서부터 스크롤하며 해당 행의「소유확인 진행」클릭
        try:
            page.evaluate(
                "() => { window.scrollTo(0,0); const w=document.querySelector('.v-data-table__wrapper'); if(w) w.scrollTop=0; }"
            )
        except Exception:
            pass
        time.sleep(0.3)
        for i in range(40):
            hit = page.evaluate(
                """
                (host) => {
                  const rows = Array.from(document.querySelectorAll('table tbody tr'));
                  for (const tr of rows) {
                    const text = (tr.innerText || '').toLowerCase();
                    if (!text.includes(host) && !text.includes(host.split('/')[0])) continue;
                    const link = Array.from(tr.querySelectorAll('a')).find(a => {
                      const t = (a.textContent || '').replace(/\\s+/g, ' ').trim();
                      return t.includes('소유확인') && t.includes('진행');
                    });
                    if (!link) return 'no-link';
                    link.scrollIntoView({ block: 'center' });
                    link.click();
                    return 'clicked';
                  }
                  return '';
                }
                """,
                host,
            )
            if hit == "clicked":
                self._log("  「소유확인 진행」클릭")
                return True
            if hit == "no-link":
                return False
            self._scroll_board(step=500)
            time.sleep(0.3)
        return False

    def _has_verify_ui(self) -> bool:
        page = self.page
        assert page is not None
        url = page.url or ""
        if "/console/verify" in url:
            return True
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                      const t = document.body ? (document.body.innerText || '') : '';
                      return t.includes('사이트 소유확인') || t.includes('소유확인');
                    }
                    """
                )
            )
        except Exception:
            return False

    def _click_ownership_button(self) -> bool:
        """HTML 파일/태그 선택 없이 하단「소유확인」만 클릭."""
        page = self.page
        assert page is not None
        clicked = page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll(
                'button, a, div[role="button"], .v-btn'
              ));
              const scored = [];
              for (const el of buttons) {
                const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!/^소유\\s*확인$/.test(t) && t !== '소유확인') continue;
                if (/진행|취소/.test(t)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) continue;
                scored.push({ el, t, y: r.top });
              }
              scored.sort((a, b) => b.y - a.y);
              if (!scored.length) return '';
              scored[0].el.scrollIntoView({ block: 'center' });
              scored[0].el.click();
              return scored[0].t;
            }
            """
        )
        if clicked:
            self._log(f"  「소유확인」클릭 ({clicked}) — 방식 선택 생략")
            return True
        self._log("  ⚠ 「소유확인」버튼 없음")
        return False

    def _clear_completion_state(self) -> None:
        """이전 사이트의 완료 팝업/다이얼로그 잔여 상태 초기화 (세대 증가)."""
        self._dialog_gen += 1
        self._complete_dialog_gen = 0
        self._last_dialog_msg = ""
        page = self.page
        if page is None:
            return
        try:
            page.evaluate(
                """(gen) => {
                  window.__th_dialog_gen = gen;
                  window.__th_complete_gen = 0;
                  window.__th_last_dialog = '';
                  window.__th_dialog_log = [];
                  // 이전 완료 토스트/다이얼로그 닫기
                  const needle = '사이트 소유 확인이 완료되었습니다';
                  const nodes = Array.from(document.querySelectorAll(
                    '.v-dialog, .v-overlay, .v-snackbar, [role="dialog"], [role="alertdialog"], .v-alert'
                  ));
                  for (const el of nodes) {
                    const t = (el.innerText || el.textContent || '');
                    if (!t.includes(needle)) continue;
                    const btn = el.querySelector('button, .v-btn, [role="button"]');
                    if (btn) {
                      try { btn.click(); } catch (e) {}
                    }
                    try { el.remove(); } catch (e) {}
                  }
                }""",
                self._dialog_gen,
            )
        except Exception:
            pass

    def _ownership_complete_popup_seen(self) -> bool:
        """이번 소유확인 클릭(현재 세대) 이후에 뜬 완료 팝업만 인정."""
        if self._dialog_gen <= 0:
            return False
        if self._complete_dialog_gen == self._dialog_gen:
            return True
        page = self.page
        if page is None:
            return False
        try:
            found = bool(
                page.evaluate(
                    """(gen) => {
                      return !!(gen && window.__th_complete_gen === gen);
                    }""",
                    self._dialog_gen,
                )
            )
            if found:
                self._complete_dialog_gen = self._dialog_gen
            return found
        except Exception:
            return False

    def _wait_complete(self, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ownership_complete_popup_seen():
                return True
            # 방금 액션 이후에만 HTML 완료 오버레이 허용
            if self._visible_complete_overlay():
                self._complete_dialog_gen = self._dialog_gen
                return True
            time.sleep(0.35)
        return False

    def _visible_complete_overlay(self) -> bool:
        """지금 보이는 활성 다이얼로그/토스트에 완료 문구가 있는지."""
        page = self.page
        if page is None:
            return False
        needle = "사이트 소유 확인이 완료되었습니다"
        try:
            return bool(
                page.evaluate(
                    """(needle) => {
                      const nodes = document.querySelectorAll(
                        '.v-dialog--active, .v-overlay--active [role="dialog"],'
                        + ' .v-snackbar--active, [role="alertdialog"]'
                      );
                      for (const el of nodes) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const t = (el.innerText || el.textContent || '');
                        if (t.includes(needle)) return true;
                      }
                      return false;
                    }""",
                    needle,
                )
            )
        except Exception:
            return False

    def _click_ownership_and_solve_captcha(self) -> bool:
        self._clear_completion_state()
        if not self._click_ownership_button():
            return False
        time.sleep(1.2)
        page = self.page
        assert page is not None
        solver = NaverCaptchaSolver(
            page,
            api_key=self.openai_api_key,
            on_log=self._log,
            should_stop=lambda: not self._ok(),
        )
        for attempt in range(1, 9):
            if not self._ok():
                raise RuntimeError("중단됨")
            # alert/confirm 가로채기 결과만 — 이전 세대·잔여 토스트는 무시
            if self._ownership_complete_popup_seen():
                self._log("  소유확인 완료 팝업 확인")
                self._clear_completion_state()
                return True
            if solver.has_advisor_modal():
                self._log(f"  보안문자 모달 — OpenAI 해결 ({attempt}/8)")
                solved = solver.try_solve(max_attempts=4)
                if not solved:
                    self._log("  캡챠 실패 — 재시도")
                    self._clear_completion_state()
                    self._click_ownership_button()
                    time.sleep(1.0)
                    continue
                if self._wait_complete(8.0):
                    self._log("  소유확인 완료")
                    self._clear_completion_state()
                    return True
                self._log("  캡챠 제출 후 완료 팝업 없음 — 재시도")
                self._clear_completion_state()
                self._click_ownership_button()
                time.sleep(1.0)
                continue
            time.sleep(1.0)
            if self._ownership_complete_popup_seen():
                self._log("  소유확인 완료")
                self._clear_completion_state()
                return True
            # 캡챠 없이 바로 완료 HTML 팝업만 뜬 경우
            if self._visible_complete_overlay():
                self._complete_dialog_gen = self._dialog_gen
                self._log("  소유확인 완료 (화면 팝업)")
                self._clear_completion_state()
                return True
            if not solver.has_advisor_modal():
                self._log("  모달 없음 — 소유확인 재클릭")
                self._clear_completion_state()
                self._click_ownership_button()
                time.sleep(1.0)
        return False
