"""네이버 로그인 캡챠 자동 해결 (네이버 신고 프로그램 로직 → Playwright).

- 문자 캡챠(#captchaimg, 입력란 #chptcha)
- 영수증/질문형 보안 화면
OpenAI Vision(gpt-4o) 사용.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Callable

LogFn = Callable[[str], None]
StopFn = Callable[[], bool]


def _vision_answer(
    api_key: str,
    prompt: str,
    image_b64: str,
    *,
    model: str = "gpt-4o",
    mime: str = "image/png",
) -> str:
    from openai import OpenAI

    key = "".join(c for c in (api_key or "") if ord(c) < 128).strip()
    if not key or not key.startswith("sk-"):
        raise RuntimeError(
            "OpenAI API 키가 올바르지 않습니다. 설정에 sk- 로 시작하는 키를 다시 입력해 주세요."
        )
    client = OpenAI(api_key=key)
    mime = mime or "image/png"
    resp = client.chat.completions.create(
        model=model,
        max_tokens=100,
        temperature=0,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ],
    )
    return (resp.choices[0].message.content or "").strip()


_REFUSAL_MARKERS = (
    "sorry",
    "cannot",
    "can't",
    "unable",
    "죄송",
    "읽을 수 없",
    "보이지 않",
    "확인할 수 없",
    "help with",
    "as an ai",
)


def _normalize_char_text(raw: str) -> str:
    """Vision 응답에서 캡챠 문자열만 추출 (신고 프로그램 + 여러 줄 대응)."""
    text = (raw or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if any(m in lower for m in _REFUSAL_MARKERS):
        return ""
    text = text.replace("```", "").strip()

    # 1) 신고 프로그램과 동일: 첫 줄만 영숫자
    first = re.sub(r"[^A-Za-z0-9]", "", text.split("\n", 1)[0].strip("`\"'“”‘’"))
    if 4 <= len(first) <= 8:
        return first

    # 2) 설명문이 섞인 경우 — 줄별·토큰별 후보
    candidates: list[str] = []
    for line in text.splitlines():
        alnum = re.sub(r"[^A-Za-z0-9]", "", line.strip().strip("`\"'“”‘’"))
        if alnum:
            candidates.append(alnum)
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    if compact:
        candidates.append(compact)
    for m in re.finditer(r"[A-Za-z0-9]{4,8}", compact):
        candidates.append(m.group(0))

    for c in candidates:
        if 4 <= len(c) <= 8:
            return c
    for c in candidates:
        if len(c) == 3:
            return c
    return ""


class NaverCaptchaSolver:
    def __init__(
        self,
        page,
        *,
        api_key: str = "",
        on_log: LogFn | None = None,
        should_stop: StopFn | None = None,
    ):
        self.page = page
        self.api_key = (api_key or "").strip()
        self.on_log = on_log or (lambda _m: None)
        self.should_stop = should_stop or (lambda: False)
        self._last_image_mime = "image/png"

    def enabled(self) -> bool:
        return bool(self.api_key)

    def _log(self, msg: str) -> None:
        self.on_log(msg)

    def _stopped(self) -> bool:
        try:
            return bool(self.should_stop())
        except Exception:
            return False

    def looks_like_captcha(self) -> bool:
        return (
            self.has_advisor_modal()
            or self._has_receipt_captcha()
            or self._has_char_captcha()
            or self._page_mentions_captcha()
        )

    def _page_mentions_captcha(self) -> bool:
        try:
            text = self.page.inner_text("body", timeout=1500)
        except Exception:
            return False
        return any(
            k in text
            for k in (
                "보호조치",
                "자동입력 방지",
                "보안문자",
                "캡차",
                "보안 확인",
                "자동 입력 방지",
                "정답을 입력",
                "영수증",
            )
        )

    def _is_receipt_question(self, text: str) -> bool:
        if not text or self._is_char_instruction(text):
            return False
        return any(
            k in text
            for k in (
                "입니까",
                "얼마",
                "무엇",
                "몇",
                "합계",
                "가격",
                "개수",
                "빈 칸",
                "전화번호",
                "영수증",
                "가게",
                "제품",
                "번째 숫자",
                "번째숫자",
                "구매한",
                "총 몇",
            )
        )

    def _is_char_instruction(self, text: str) -> bool:
        compact = (text or "").replace(" ", "").lower()
        return any(
            k in compact
            for k in ("자동입력방지", "자동입력", "문자를입력", "보안문자", "캡차", "captcha")
        )

    def _has_char_captcha(self) -> bool:
        if self._has_receipt_captcha():
            return False
        # 영수증 질문이 보이면 절대 문자 캡챠로 취급하지 않음
        if self._receipt_question():
            return False
        url = (self.page.url or "").lower()
        if "rcaptcha" in url or "nidlogin.captcha" in url:
            # URL만으로는 영수증일 수 있음 — 큰 이미지면 영수증
            img = self._any_captcha_image()
            if img and self._image_is_wide(img):
                return False
            return True
        return self._char_image() is not None

    def _has_receipt_captcha(self) -> bool:
        q = self._receipt_question()
        if not q:
            return False
        # 질문이 영수증형이면 이미지는 #captchaimg 포함 아무거나
        return self._receipt_image() is not None or self._any_captcha_image() is not None

    def _find_visible(self, *selectors: str):
        for sel in selectors:
            loc = self.page.locator(sel)
            try:
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 6)):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        return el
                except Exception:
                    continue
        return None

    def _image_is_wide(self, loc) -> bool:
        try:
            box = loc.bounding_box() or {}
            w = float(box.get("width") or 0)
            h = float(box.get("height") or 0)
            # 영수증 콜라주는 가로로 김 (문자 캡챠는 보통 ≤220x80)
            return w >= 250 or (w >= 180 and h >= 90) or (w * h >= 20000)
        except Exception:
            return False

    def _any_captcha_image(self):
        return self._find_visible(
            "#captchaimg",
            "img.captcha_img",
            "div.captcha img",
            ".captcha_box img",
            ".captcha_inner img",
            "img[src*='captcha']",
            "img[src*='ncaptcha']",
        )

    def has_advisor_modal(self) -> bool:
        """서치어드바이저 소유확인 보안문자 모달."""
        try:
            text = self.page.inner_text("body", timeout=1500)
        except Exception:
            return False
        markers = ("자동등록을 방지", "보안절차", "글자 및 숫자", "이미지에 보이는 글자")
        if not any(k in text for k in markers):
            return False
        # 다이얼로그 클래스 없어도 안내문+새로고침+입력칸이면 모달로 본다
        if "새로고침" in text:
            return True
        for sel in (".v-dialog--active", '[role="dialog"]', ".v-dialog", ".modal", ".ly_pop"):
            loc = self.page.locator(sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _advisor_scope_selectors(self) -> tuple[str, ...]:
        return (
            ".v-dialog--active",
            '[role="dialog"]',
            ".v-dialog",
            ".modal",
            ".ly_pop",
            ".captcha_layer",
            ".captcha_popup",
            ".v-card",
        )

    def _mark_advisor_captcha_target(self) -> bool:
        """모달 내 캡챠 이미지/배경을 data-th-captcha-img 로 마킹."""
        return bool(
            self.page.evaluate(
                """
                () => {
                  document.querySelectorAll('[data-th-captcha-img]').forEach(el => {
                    el.removeAttribute('data-th-captcha-img');
                  });
                  const roots = [
                    ...document.querySelectorAll(
                      '.v-dialog--active, [role="dialog"], .v-dialog, .modal, .ly_pop, .v-card'
                    ),
                    document.body,
                  ];
                  const pickVisible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width >= 60 && r.height >= 20 && r.bottom > 0 && r.top < innerHeight;
                  };

                  for (const root of roots) {
                    if (!root) continue;
                    // 1) img
                    for (const img of root.querySelectorAll('img')) {
                      if (!pickVisible(img)) continue;
                      const src = (img.currentSrc || img.src || '').toLowerCase();
                      const alt = (img.alt || '').toLowerCase();
                      if (src.includes('captcha') || alt.includes('보안') || alt.includes('captcha')
                        || pickVisible(img)) {
                        // 새로고침 근처 이미지 우선: 형제/부모에 새로고침
                        const near = (img.closest('div')?.innerText || '').includes('새로고침');
                        if (near || src.includes('captcha') || roots[0] !== document.body) {
                          img.setAttribute('data-th-captcha-img', '1');
                          return true;
                        }
                      }
                    }
                    // 2) background-image
                    for (const el of root.querySelectorAll('div, span, p, i')) {
                      if (!pickVisible(el)) continue;
                      const bs = window.getComputedStyle(el).backgroundImage || '';
                      if (!bs || bs === 'none') continue;
                      if (/captcha|nhncaptcha|data:image/i.test(bs)) {
                        el.setAttribute('data-th-captcha-img', '1');
                        return true;
                      }
                      // 노이즈 패턴 박스: 가로로 길고 옆에 새로고침
                      const parentText = (el.parentElement?.innerText || '').slice(0, 80);
                      const r = el.getBoundingClientRect();
                      if (r.width >= 100 && r.width <= 320 && r.height >= 30 && r.height <= 100
                        && /새로고침/.test(parentText)) {
                        el.setAttribute('data-th-captcha-img', '1');
                        return true;
                      }
                    }
                  }

                  // 3) 「새로고침」왼쪽 형제/앞 요소
                  for (const node of document.querySelectorAll('a, button, span, div')) {
                    const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t !== '새로고침' && !/^새로\\s*고침$/.test(t)) continue;
                    let prev = node.previousElementSibling;
                    for (let i = 0; i < 4 && prev; i++, prev = prev.previousElementSibling) {
                      if (pickVisible(prev)) {
                        prev.setAttribute('data-th-captcha-img', '1');
                        return true;
                      }
                    }
                    const parent = node.parentElement;
                    if (parent) {
                      for (const child of parent.children) {
                        if (child === node) continue;
                        if (pickVisible(child) && child.querySelector('img, canvas')) {
                          const img = child.querySelector('img, canvas') || child;
                          img.setAttribute('data-th-captcha-img', '1');
                          return true;
                        }
                        if (pickVisible(child)) {
                          const r = child.getBoundingClientRect();
                          if (r.width >= 80 && r.height >= 25) {
                            child.setAttribute('data-th-captcha-img', '1');
                            return true;
                          }
                        }
                      }
                    }
                  }
                  return false;
                }
                """
            )
        )

    def _char_image(self):
        """문자 캡챠 이미지. 소유확인 모달 안 이미지/배경 우선."""
        advisor = False
        try:
            advisor = self.has_advisor_modal()
        except Exception:
            pass

        if self._receipt_question() and not advisor:
            return None

        if advisor:
            self._mark_advisor_captcha_target()
            marked = self._find_visible("[data-th-captcha-img='1']")
            if marked:
                return marked
            # 모달 안 가장 그럴듯한 img
            for scope in self._advisor_scope_selectors():
                root = self.page.locator(scope)
                try:
                    if root.count() == 0 or not root.first.is_visible():
                        continue
                except Exception:
                    continue
                for sel in ("img", "canvas", "div"):
                    loc = root.locator(sel)
                    try:
                        n = min(loc.count(), 10)
                    except Exception:
                        n = 0
                    best = None
                    best_area = 0
                    for i in range(n):
                        el = loc.nth(i)
                        try:
                            if not el.is_visible():
                                continue
                            box = el.bounding_box() or {}
                            w = float(box.get("width") or 0)
                            h = float(box.get("height") or 0)
                            if w < 60 or h < 20 or w > 400 or h > 150:
                                continue
                            area = w * h
                            if area > best_area:
                                best = el
                                best_area = area
                        except Exception:
                            continue
                    if best:
                        return best

        if self._receipt_question():
            return None
        el = self._find_visible("#captchaimg")
        if el and not self._image_is_wide(el):
            return el
        loc = self.page.locator("img.captcha_img")
        try:
            n = loc.count()
        except Exception:
            n = 0
        for i in range(min(n, 6)):
            img = loc.nth(i)
            try:
                if not img.is_visible():
                    continue
                box = img.bounding_box() or {}
                w = float(box.get("width") or 0)
                h = float(box.get("height") or 0)
                if w <= 220 and h <= 80 and w >= 40:
                    return img
            except Exception:
                continue
        return None

    def _receipt_image(self):
        """영수증 이미지 — #captchaimg 포함, 가장 큰 것."""
        candidates = []
        for sel in (
            "#captchaimg",
            "div.captcha img",
            ".captcha_box img",
            ".captcha_inner img",
            "img.captcha_img",
            "img[src*='captcha']",
            "img[src*='ncaptcha']",
        ):
            loc = self.page.locator(sel)
            try:
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 8)):
                img = loc.nth(i)
                try:
                    if not img.is_visible():
                        continue
                    box = img.bounding_box() or {}
                    w = float(box.get("width") or 0)
                    h = float(box.get("height") or 0)
                    area = w * h
                    if area < 3000:
                        continue
                    candidates.append((area, w, h, img))
                except Exception:
                    continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][3]
        # 영수증 질문이 있으면 작은 captchaimg도 허용
        if self._receipt_question():
            return best
        # 질문 없이 이미지 크기만으로 판단할 때는 넓은 것만
        if self._image_is_wide(best):
            return best
        return None

    def _receipt_question(self) -> str:
        selectors = [
            "div.captcha_message",
            "div#captcha_info",
            "p.captcha_message",
            "span.captcha_message",
            "div.captcha_box",
            "div#captcha_inner",
            ".captcha_question",
            "[class*='captcha']",
        ]
        for sel in selectors:
            loc = self.page.locator(sel)
            try:
                n = min(loc.count(), 10)
            except Exception:
                continue
            for i in range(n):
                try:
                    text = (loc.nth(i).inner_text(timeout=800) or "").strip()
                except Exception:
                    continue
                # 여러 줄이면 질문 줄만 추출
                for line in text.splitlines():
                    t = line.strip()
                    if 8 < len(t) < 180 and self._is_receipt_question(t):
                        return t
                if 8 < len(text) < 180 and self._is_receipt_question(text):
                    return text

        # XPath 스타일: 입니까/몇/개수 포함 텍스트
        try:
            for phrase in ("입니까", "몇 개", "개수", "얼마", "합계", "빈 칸", "전화번호"):
                loc = self.page.get_by_text(phrase, exact=False)
                n = min(loc.count(), 8)
                for i in range(n):
                    try:
                        t = (loc.nth(i).inner_text(timeout=500) or "").strip()
                    except Exception:
                        continue
                    if 8 < len(t) < 180 and self._is_receipt_question(t):
                        return t
        except Exception:
            pass

        try:
            body = self.page.inner_text("body", timeout=1500)
        except Exception:
            return ""
        for line in body.splitlines():
            t = line.strip()
            if 8 < len(t) < 180 and self._is_receipt_question(t) and "로그인" not in t[:4]:
                if "가상으로 제작" in t:  # 안내문구 제외
                    continue
                return t
        return ""

    def _answer_input(self, *, receipt: bool = False, advisor_modal: bool = False):
        """문자: #chptcha / 영수증: 정답 placeholder / 소유확인모달: dialog 안 input."""
        if advisor_modal:
            for scope in self._advisor_scope_selectors():
                root = self.page.locator(scope)
                try:
                    if root.count() == 0 or not root.first.is_visible():
                        continue
                except Exception:
                    continue
                inp = root.locator('input[type="text"], input:not([type]), textarea').first
                try:
                    if inp.count() > 0 and inp.is_visible():
                        return inp
                except Exception:
                    continue
            # fallback
            return self._find_visible(
                'input[type="text"]',
                "input:not([type])",
                "input[placeholder*='정답']",
                "input[placeholder*='보안']",
            )

        if receipt:
            for sel in (
                "input[placeholder*='정답']",
                "input[title*='정답']",
                "input[aria-label*='정답']",
                "#captcha",
                "input[name='captcha']",
                "input.captcha_input",
                "div.captcha input[type='text']",
            ):
                el = self._find_visible(sel)
                if not el:
                    continue
                try:
                    eid = (el.get_attribute("id") or "").lower()
                except Exception:
                    eid = ""
                if eid in ("id", "pw", "chptcha"):
                    continue
                return el
            loc = self.page.locator("input[type='text']")
            try:
                n = min(loc.count(), 10)
            except Exception:
                n = 0
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    eid = (el.get_attribute("id") or "").lower()
                    if eid in ("id", "pw", "chptcha"):
                        continue
                    ph = el.get_attribute("placeholder") or ""
                    if "비밀번호" in ph:
                        continue
                    return el
                except Exception:
                    continue
            return None

        return self._find_visible(
            "#chptcha",
            "#captcha",
            "input[name='captcha']",
            "input[name='chptcha']",
            "input.captcha_input",
            "input[placeholder*='정답']",
            "input[placeholder*='보안']",
            "input[placeholder*='자동입력']",
            "input[placeholder*='문자']",
            "div.captcha input[type='text']",
        )

    def _wait_image_ready(self, loc, *, timeout_ms: int = 8000) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self._stopped():
                return False
            try:
                ready = loc.evaluate(
                    """(el) => {
                      if (!el) return false;
                      // 실제 픽셀이 로드된 경우만 ready (빈 placeholder 제외)
                      if (el.complete && (el.naturalWidth||0) >= 40 && (el.naturalHeight||0) >= 15) return true;
                      return false;
                    }"""
                )
                if ready:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def _img_meta(self, loc) -> dict:
        try:
            return loc.evaluate(
                """(el) => ({
                  nw: el.naturalWidth||0,
                  nh: el.naturalHeight||0,
                  cw: Math.floor(el.getBoundingClientRect().width)||0,
                  ch: Math.floor(el.getBoundingClientRect().height)||0,
                  complete: !!el.complete,
                  src: (el.currentSrc||el.src||'').slice(0,160)
                })"""
            ) or {}
        except Exception:
            return {}

    def _save_debug_png(self, raw: bytes, tag: str) -> None:
        try:
            from paths import data_path

            folder = data_path("captcha_debug")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"last_{tag}.png"
            path.write_bytes(raw)
            self._log(f"  캡챠 디버그 저장: {path.name} ({len(raw)} bytes)")
        except Exception:
            pass

    def _fetch_src_b64(self, loc) -> str:
        """img src를 브라우저 쿠키로 직접 다운로드 (스크린샷보다 선명)."""
        try:
            src = loc.evaluate("(el) => el.currentSrc || el.src || ''") or ""
        except Exception:
            src = ""
        if not src:
            return ""
        if src.startswith("data:image") and "," in src:
            return src.split(",", 1)[1]
        if src.startswith("//"):
            src = "https:" + src
        if not (src.startswith("http://") or src.startswith("https://")):
            try:
                from urllib.parse import urljoin

                src = urljoin(self.page.url, src)
            except Exception:
                return ""
        try:
            resp = self.page.request.get(src, timeout=10_000)
            if resp.ok:
                body = resp.body()
                if body and len(body) > 200:
                    self._save_debug_png(body, "src")
                    # jpeg/gif/png 모두 data URL로 전달 가능 — Vision은 mime 힌트보다 바이트 내용 사용
                    mime = "image/png"
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "jpeg" in ct or "jpg" in ct:
                        mime = "image/jpeg"
                    elif "gif" in ct:
                        mime = "image/gif"
                    elif "webp" in ct:
                        mime = "image/webp"
                    b64 = base64.b64encode(body).decode("ascii")
                    # mime을 붙인 data URL에서 b64만 쓰므로, jpeg이면 Vision URL에 jpeg 지정 필요
                    self._last_image_mime = mime
                    return b64
            self._log(f"  캡챠 src 다운로드 실패: HTTP {getattr(resp, 'status', '?')}")
        except Exception as exc:
            self._log(f"  캡챠 src 다운로드 오류: {exc}")
        return ""

    def _element_b64(self, loc) -> str:
        """1) src 원본 다운로드 → 2) screenshot → 3) clip → 4) canvas."""
        self._last_image_mime = "image/png"
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        ready = self._wait_image_ready(loc)
        meta = self._img_meta(loc)
        self._log(
            f"  캡챠 img meta: ready={ready} "
            f"{meta.get('nw')}x{meta.get('nh')} css={meta.get('cw')}x{meta.get('ch')} "
            f"src={(meta.get('src') or '')[:60]}"
        )

        # 1) 원본 URL 바이트 (Selenium이 잘 되는 이유와 가장 가까운 품질)
        b64 = self._fetch_src_b64(loc)
        if b64 and len(b64) > 200:
            self._log(f"  캡챠 캡처: src 다운로드 OK ({len(b64)} b64, {self._last_image_mime})")
            return b64

        # 2) Playwright element screenshot
        try:
            raw = loc.screenshot(type="png", animations="disabled")
            if raw and len(raw) > 200:
                self._save_debug_png(raw, "shot")
                self._last_image_mime = "image/png"
                self._log(f"  캡챠 캡처: screenshot OK ({len(raw)} bytes)")
                return base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            self._log(f"  캡챠 screenshot 실패: {exc}")

        # 3) 페이지 clip screenshot
        try:
            box = loc.bounding_box()
            if box and box.get("width", 0) >= 40 and box.get("height", 0) >= 15:
                raw = self.page.screenshot(
                    type="png",
                    clip={
                        "x": max(0, box["x"]),
                        "y": max(0, box["y"]),
                        "width": box["width"],
                        "height": box["height"],
                    },
                    animations="disabled",
                )
                if raw and len(raw) > 200:
                    self._save_debug_png(raw, "clip")
                    self._last_image_mime = "image/png"
                    self._log(f"  캡챠 캡처: clip OK ({len(raw)} bytes)")
                    return base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            self._log(f"  캡챠 clip 실패: {exc}")

        # 4) canvas 3x
        try:
            data_url = loc.evaluate(
                """(el) => {
                  try {
                    const scale = 3;
                    const w = el.naturalWidth || el.width || Math.floor(el.getBoundingClientRect().width) || 120;
                    const h = el.naturalHeight || el.height || Math.floor(el.getBoundingClientRect().height) || 40;
                    if (w < 8 || h < 8) return '';
                    const c = document.createElement('canvas');
                    c.width = w * scale;
                    c.height = h * scale;
                    const ctx = c.getContext('2d');
                    ctx.imageSmoothingEnabled = false;
                    ctx.fillStyle = '#ffffff';
                    ctx.fillRect(0, 0, c.width, c.height);
                    ctx.drawImage(el, 0, 0, c.width, c.height);
                    return c.toDataURL('image/png');
                  } catch (e) {
                    return '';
                  }
                }"""
            )
            if isinstance(data_url, str) and data_url.startswith("data:image") and "," in data_url:
                b64 = data_url.split(",", 1)[1]
                if len(b64) > 200:
                    self._last_image_mime = "image/png"
                    self._log(f"  캡챠 캡처: canvas OK ({len(b64)} b64)")
                    return b64
        except Exception as exc:
            self._log(f"  캡챠 canvas 캡처 실패: {exc}")
        return ""
    def _click_refresh(self) -> bool:
        # 서치어드바이저 모달: 「새로고침」 텍스트 링크
        try:
            loc = self.page.get_by_text("새로고침", exact=False)
            n = min(loc.count(), 6)
            for i in range(n):
                el = loc.nth(i)
                try:
                    if el.is_visible():
                        el.click(timeout=3000)
                        self._log("  캡챠 이미지 새로고침")
                        time.sleep(1.1)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        for sel in (
            "#captcha_reload",
            "#capcha_reload",
            "#btnCaptchaReload",
            "button.btn_reload",
            "a.btn_refresh",
            "button[class*='refresh']",
            "button[class*='reload']",
            "a[class*='refresh']",
            "a#captcha_reload",
        ):
            loc = self.page.locator(sel)
            try:
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 4)):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    el_id = (btn.get_attribute("id") or "").lower()
                    if el_id in ("log.login", "loginbtn_row", "loginbtn_column"):
                        continue
                    classes = (btn.get_attribute("class") or "").lower()
                    if "btn_done" in classes:
                        continue
                    btn.click(timeout=3000)
                    self._log("  캡챠 이미지 새로고침")
                    time.sleep(1.1)
                    return True
                except Exception:
                    continue
        # JS fallback
        try:
            clicked = self.page.evaluate(
                """() => {
                  const btns = document.querySelectorAll('button, a, span, div[role="button"]');
                  for (const b of btns) {
                    const t = (b.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t === '새로고침' || /^새로\\s*고침$/.test(t)) {
                      if (b.offsetParent !== null) { b.click(); return true; }
                    }
                    const cls = (b.className || '').toLowerCase();
                    const id = (b.id || '').toLowerCase();
                    if (id === 'log.login' || id.indexOf('loginbtn') >= 0) continue;
                    if (cls.indexOf('btn_done') >= 0) continue;
                    if ((cls.indexOf('refresh') >= 0 || cls.indexOf('reload') >= 0)
                        && b.offsetParent !== null) {
                      b.click();
                      return true;
                    }
                  }
                  return false;
                }"""
            )
            if clicked:
                self._log("  캡챠 이미지 새로고침 (JS)")
                time.sleep(1.1)
                return True
        except Exception:
            pass
        return False

    def _click_confirm(self, *, advisor_modal: bool = False) -> bool:
        if advisor_modal:
            return self._click_advisor_confirm_button()
        candidates = [
            self.page.locator('[id="log.login"]'),
            self.page.locator("button.btn_login"),
            self.page.locator("button:has-text('확인')"),
            self.page.locator("button:has-text('로그인')"),
            self.page.get_by_role("button", name="확인"),
            self.page.get_by_role("button", name="로그인"),
        ]
        for loc in candidates:
            try:
                if loc.count() == 0:
                    continue
                target = loc.first
                if target.is_visible():
                    txt = ""
                    try:
                        txt = (target.inner_text(timeout=500) or "").strip()
                    except Exception:
                        pass
                    if "취소" in txt:
                        continue
                    target.click(timeout=4000)
                    return True
            except Exception:
                continue
        try:
            self.page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    def _click_advisor_confirm_button(self) -> bool:
        """소유확인 보안문자 모달 안의 「확인」만 클릭 (페이지 다른 확인 버튼 제외)."""
        page = self.page
        # 1) JS로 모달 스코프 내 확인 버튼 좌표 확보 후 실제 클릭
        box = page.evaluate(
            """
            () => {
              const roots = Array.from(document.querySelectorAll(
                '.v-dialog--active, [role="dialog"], .v-dialog, .modal, .ly_pop, .v-overlay--active .v-card'
              ));
              if (!roots.length) {
                // 안내문 기준으로 가까운 카드
                for (const el of document.querySelectorAll('div, section, form')) {
                  const t = el.innerText || '';
                  if (t.includes('자동등록을 방지') || t.includes('보안절차') || t.includes('이미지에 보이는')) {
                    roots.push(el);
                    break;
                  }
                }
              }
              for (const root of roots) {
                if (!root) continue;
                const buttons = Array.from(root.querySelectorAll('button, .v-btn, a[role="button"], [role="button"]'));
                for (const b of buttons) {
                  const t = (b.textContent || '').replace(/\\s+/g, ' ').trim();
                  if (t !== '확인') continue;
                  if (/취소|닫기/.test(t)) continue;
                  const r = b.getBoundingClientRect();
                  if (r.width < 20 || r.height < 12) continue;
                  if (r.bottom < 0 || r.top > (window.innerHeight || 0)) continue;
                  b.scrollIntoView({ block: 'center', inline: 'center' });
                  const r2 = b.getBoundingClientRect();
                  return {
                    x: r2.left + r2.width / 2,
                    y: r2.top + r2.height / 2,
                    w: r2.width,
                    h: r2.height,
                    text: t,
                  };
                }
              }
              return null;
            }
            """
        )
        if box:
            try:
                self._log(
                    f"  소유확인 캡챠 「확인」클릭 @{int(box['x'])},{int(box['y'])} "
                    f"size={int(box['w'])}x{int(box['h'])}"
                )
                page.mouse.click(box["x"], box["y"], delay=40)
                return True
            except Exception as exc:
                self._log(f"  확인 좌표 클릭 실패: {exc}")

        # 2) Playwright locator (모달 스코프)
        for scope in self._advisor_scope_selectors():
            try:
                root = page.locator(scope).first
                if root.count() == 0:
                    continue
                btn = root.get_by_role("button", name="확인")
                if btn.count() == 0:
                    btn = root.locator("button, .v-btn").filter(has_text=re.compile(r"^확인$"))
                if btn.count() == 0:
                    continue
                target = btn.first
                if target.is_visible():
                    self._log("  소유확인 캡챠 「확인」locator 클릭")
                    target.click(timeout=4000, no_wait_after=True)
                    return True
            except Exception:
                continue

        # 3) 입력란 포커스 후 Enter (제출)
        try:
            inp = self._answer_input(advisor_modal=True)
            if inp:
                inp.click(timeout=2000)
                time.sleep(0.1)
                self._log("  소유확인 캡챠 입력란 Enter")
                page.keyboard.press("Enter")
                return True
        except Exception:
            pass
        self._log("  소유확인 캡챠 「확인」버튼을 찾지 못함")
        return False

    def _advisor_captcha_still_open(self) -> bool:
        """캡챠 입력란이 아직 보이면 모달 미통과로 본다."""
        try:
            inp = self._answer_input(advisor_modal=True)
            if inp and inp.is_visible():
                return True
        except Exception:
            pass
        return self.has_advisor_modal()

    def _submit_advisor_answer(self, text: str) -> bool:
        """입력 → Enter → 모달 「확인」클릭 → 모달 닫힘 확인 후에만 성공."""
        self._log(f"  소유확인 캡챠 결과: {text}")
        if not self._type_answer(text, advisor_modal=True):
            return False
        time.sleep(0.25)

        # 입력란에서 Enter 먼저
        try:
            inp = self._answer_input(advisor_modal=True)
            if inp:
                inp.click(timeout=2000)
                time.sleep(0.08)
                self.page.keyboard.press("Enter")
                self._log("  캡챠 입력 후 Enter")
                time.sleep(0.45)
        except Exception as exc:
            self._log(f"  캡챠 Enter 실패: {exc}")

        # 모달이 아직 열려 있으면 「확인」버튼 필수 클릭
        if self._advisor_captcha_still_open():
            if not self._click_advisor_confirm_button():
                self._log("  소유확인 캡챠 확인 버튼 실패")
                return False
            self._log("  캡챠 「확인」클릭 완료 — 모달 닫힘 대기")
            time.sleep(1.2)
        else:
            self._log("  Enter 후 모달 닫힘 감지")

        # 최대 ~4초 모달 종료 대기
        closed = False
        for _ in range(10):
            if not self._advisor_captcha_still_open():
                closed = True
                break
            time.sleep(0.4)

        if not closed:
            self._log("  소유확인 캡챠 오답/미제출 — 모달 유지 · 새로고침")
            self._clear_answer(advisor_modal=True)
            self._click_refresh()
            return False

        self._log("  소유확인 캡챠 확인 완료 (모달 종료)")
        return True

    def _clear_answer(self, *, receipt: bool = False, advisor_modal: bool = False) -> None:
        inp = self._answer_input(receipt=receipt, advisor_modal=advisor_modal)
        if not inp:
            return
        try:
            inp.click(timeout=2000)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
            inp.evaluate("(el) => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }")
        except Exception:
            pass

    def _type_answer(self, text: str, *, receipt: bool = False, advisor_modal: bool = False) -> bool:
        inp = self._answer_input(receipt=receipt, advisor_modal=advisor_modal)
        if not inp:
            kind = "소유확인 모달" if advisor_modal else ("영수증 정답란" if receipt else "#chptcha/#captcha")
            self._log(f"  캡챠 입력란 없음 ({kind})")
            return False
        try:
            aid = inp.get_attribute("id") or ""
            ph = inp.get_attribute("placeholder") or ""
            self._log(f"  캡챠 입력란: id={aid}, placeholder={ph}")
            self._clear_answer(receipt=receipt, advisor_modal=advisor_modal)
            inp.click(timeout=3000)
            time.sleep(0.12)
            try:
                inp.fill(text, timeout=3000)
            except Exception:
                self.page.keyboard.type(text, delay=45)
            try:
                cur = inp.input_value(timeout=1000)
            except Exception:
                cur = ""
            if (cur or "") != text:
                inp.evaluate(
                    """(el, val) => {
                      el.focus();
                      el.value = val;
                      el.dispatchEvent(new Event('input', {bubbles:true}));
                      el.dispatchEvent(new Event('change', {bubbles:true}));
                    }""",
                    text,
                )
            return True
        except Exception as exc:
            self._log(f"  캡챠 입력 실패: {exc}")
            return False

    def _recognize_char(self, loc) -> str:
        b64 = self._element_b64(loc)
        if not b64:
            self._log("  캡챠 이미지 캡처 실패(빈 이미지)")
            return ""
        mime = getattr(self, "_last_image_mime", "image/png") or "image/png"
        self._log(f"  캡챠 이미지 캡처 OK ({len(b64)} bytes b64, {mime})")
        # OCR 프롬프트 — 영문 우선(인코딩/거부 이슈 완화), 소유확인은 대문자+숫자
        prompts = [
            (
                "Read the distorted uppercase letters and digits left to right. "
                "Output ONLY the characters (about 4-6), nothing else. Example: 81PT5G"
            ),
            (
                "이미지에 보이는 왜곡된 영문 대문자와 숫자를 왼쪽부터 순서대로 읽으세요. "
                "보통 4~6글자입니다. 공백·설명 없이 글자만 한 줄로 출력하세요. 예: 81PT5G"
            ),
            (
                "보안문자 이미지입니다. 보이는 영문/숫자만 공백 없이 한 줄로 출력하세요."
            ),
        ]
        for idx, prompt in enumerate(prompts, start=1):
            if self._stopped():
                return ""
            try:
                raw = _vision_answer(self.api_key, prompt, b64, mime=mime)
            except Exception as exc:
                self._log(f"  OpenAI Vision 오류: {exc}")
                continue  # 다음 프롬프트 시도
            text = _normalize_char_text(raw)
            preview = (raw or "").replace("\n", " ")[:100]
            self._log(f"  Vision 원문({idx}): {preview}")
            if text:
                if idx > 1:
                    self._log(f"  캡챠 재인식 성공 (프롬프트 {idx})")
                return text
        return ""

    def _solve_receipt_answer(self, question: str, loc) -> str:
        b64 = self._element_b64(loc)
        if not b64:
            return ""
        mime = getattr(self, "_last_image_mime", "image/png") or "image/png"
        q = question or ""
        try:
            if re.search(r"(\d+)번째.*숫자", q):
                phone_raw = _vision_answer(
                    self.api_key,
                    "영수증 이미지에서 가게 전화번호(☎ 표시 옆)를 찾아 숫자만 출력하세요. 기호 없이 숫자만.",
                    b64,
                    mime=mime,
                )
                digits = re.sub(r"\D", "", (phone_raw or "").split("\n")[0])
                pos_m = re.search(r"(\d+)번째", q)
                if pos_m and digits:
                    pos = int(pos_m.group(1)) - 1
                    if 0 <= pos < len(digits):
                        return digits[pos]

            # 총 개수 / 개수 합계 — 가장 흔함
            if any(k in q for k in ("몇 개", "몇개", "개수", "총 몇", "구매한 물건")):
                prompt = (
                    "네이버 로그인 영수증 보안 질문입니다.\n"
                    f"질문: {q}\n"
                    "영수증 오른쪽 '개수' 열의 숫자를 모두 더한 합을 구하세요.\n"
                    "가격·총합 열이 아니라 '개수' 열만 사용하세요.\n"
                    "설명 없이 숫자 하나만 출력하세요."
                )
                ans = re.sub(r"\D", "", _vision_answer(self.api_key, prompt, b64, mime=mime).split("\n")[0])
                if ans:
                    return ans

            if any(k in q for k in ("가격", "얼마", "합계", "한 개")):
                prompt = (
                    f"네이버 로그인 영수증 보안 질문입니다.\n질문: {q}\n"
                    "영수증 표의 가격·개수·합계를 읽고 질문에 맞는 숫자 하나만 출력하세요."
                )
                ans = re.sub(r"\D", "", _vision_answer(self.api_key, prompt, b64, mime=mime).split("\n")[0])
                if ans:
                    return ans

            if "빈 칸" in q:
                prompt = (
                    f"네이버 로그인 영수증 보안 질문입니다.\n질문: {q}\n"
                    "영수증의 주소·지명 등에서 빈 칸에 들어갈 단어만 출력하세요."
                )
                ans = re.sub(r"[^\w가-힣]", "", _vision_answer(self.api_key, prompt, b64, mime=mime).split("\n")[0])
                if ans:
                    return ans

            if any(k in q for k in ("무엇", "이름", "제품", "물건")) and "몇" not in q:
                prompt = (
                    f"네이버 로그인 영수증 보안 질문입니다.\n질문: {q}\n"
                    "영수증 내용을 읽고 질문에 맞는 단어(제품명 등)만 출력하세요."
                )
                ans = re.sub(r"[^\w가-힣]", "", _vision_answer(self.api_key, prompt, b64, mime=mime).split("\n")[0])
                if ans:
                    return ans

            prompt = (
                "네이버 로그인 영수증 보안 질문입니다. 이미지의 영수증 내용을 읽고 질문에 답하세요.\n"
                f"질문: {q or '이미지 내용을 바탕으로 요구된 정답을 찾으세요.'}\n"
                "설명 없이 정답만 출력하세요. 숫자면 숫자만, 문자면 해당 문자만."
            )
            answer = _vision_answer(self.api_key, prompt, b64, mime=mime).split("\n")[0].strip()
            if any(k in q for k in ("가격", "얼마", "합계", "개수", "몇")):
                digits = re.sub(r"\D", "", answer)
                if digits:
                    return digits
            return re.sub(r"[^\w가-힣0-9]", "", answer)
        except Exception as exc:
            self._log(f"  영수증 캡챠 Vision 오류: {exc}")
            return ""

    def solve_advisor_modal_once(self) -> bool:
        """서치어드바이저 소유확인 모달 — 영문+숫자(예: 81PT5G) OCR."""
        if self._stopped():
            return False
        if not self.has_advisor_modal():
            return False
        img = self._char_image()
        if not img:
            self._log("  소유확인 캡챠 이미지 없음 — 영역 스크린샷 재시도")
            # 페이지 클립: 새로고침 왼쪽 대략 영역
            try:
                box = self.page.evaluate(
                    """
                    () => {
                      for (const node of document.querySelectorAll('a, button, span, div')) {
                        const t = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t !== '새로고침' && !/^새로\\s*고침$/.test(t)) continue;
                        const r = node.getBoundingClientRect();
                        return { x: Math.max(0, r.left - 220), y: Math.max(0, r.top - 10),
                                 width: 210, height: Math.max(40, r.height + 20) };
                      }
                      return null;
                    }
                    """
                )
                if box:
                    raw = self.page.screenshot(type="png", clip=box, animations="disabled")
                    if raw and len(raw) > 200:
                        self._save_debug_png(raw, "advisor_clip")
                        b64 = base64.b64encode(raw).decode("ascii")
                        self._last_image_mime = "image/png"
                        text = self._recognize_char_b64(b64)
                        if text:
                            return self._submit_advisor_answer(text)
            except Exception as exc:
                self._log(f"  캡챠 영역 캡처 실패: {exc}")
            self._click_refresh()
            return False

        self._log("  소유확인 보안문자 인식 중 (영문+숫자)...")
        text = self._recognize_char(img)
        if not text:
            self._log("  소유확인 캡챠 인식 실패 — 새로고침")
            self._click_refresh()
            return False
        return self._submit_advisor_answer(text)

    def _recognize_char_b64(self, b64: str) -> str:
        mime = getattr(self, "_last_image_mime", "image/png") or "image/png"
        prompts = [
            (
                "이미지의 왜곡된 영문 대문자와 숫자를 왼쪽부터 읽으세요. "
                "보통 4~6글자입니다. 예: 81PT5G . 공백·설명 없이 문자만 출력."
            ),
            (
                "Read ONLY the alphanumeric characters (A-Z, 0-9) left to right. "
                "About 4-6 chars. Output characters only, e.g. 81PT5G"
            ),
        ]
        for idx, prompt in enumerate(prompts, start=1):
            if self._stopped():
                return ""
            try:
                raw = _vision_answer(self.api_key, prompt, b64, mime=mime)
            except Exception as exc:
                self._log(f"  OpenAI Vision 오류: {exc}")
                return ""
            text = _normalize_char_text(raw)
            preview = (raw or "").replace("\n", " ")[:100]
            self._log(f"  Vision 원문({idx}): {preview}")
            if text:
                return text
        return ""

    def solve_char_once(self) -> bool:
        if self._stopped():
            return False
        if self.has_advisor_modal():
            return self.solve_advisor_modal_once()
        if self._receipt_question():
            self._log("  영수증 질문 감지 — 문자 인식 건너뜀")
            return False
        img = self._char_image()
        if not img:
            self._log("  문자 캡챠 이미지 없음")
            return False
        self._log("  문자 캡챠 인식 중...")
        text = self._recognize_char(img)
        if not text:
            self._log("  캡챠 인식 실패 — 새로고침")
            self._click_refresh()
            return False
        self._log(f"  캡챠 인식 결과: {text}")
        if not self._type_answer(text, receipt=False):
            return False
        time.sleep(0.35)
        if not self._click_confirm():
            self._log("  캡챠 확인 버튼 실패")
            return False
        time.sleep(2.0)
        if self._stopped():
            return False
        if self._has_char_captcha() and self._char_image():
            url = (self.page.url or "").lower()
            if "rcaptcha" in url or "nidlogin.captcha" in url or self._answer_input(receipt=False):
                self._log("  캡챠 오답 — 새로고침")
                self._clear_answer(receipt=False)
                self._click_refresh()
                return False
        return True

    def solve_receipt_once(self) -> bool:
        if self._stopped():
            return False
        q = self._receipt_question()
        img = self._receipt_image() or self._any_captcha_image()
        if not img:
            self._log("  영수증 이미지 없음")
            return False
        if q:
            self._log(f"  영수증 보안 질문: {q}")
        else:
            self._log("  영수증 질문 텍스트 없음 — 이미지만 분석")
        answer = self._solve_receipt_answer(q, img)
        low = (answer or "").lower()
        if not answer or any(k in low for k in ("sorry", "cannot", "unable", "죄송")):
            self._log("  영수증 답변 실패 — 새로고침")
            self._click_refresh()
            return False
        self._log(f"  영수증 답변: {answer}")
        if not self._type_answer(answer, receipt=True):
            return False
        time.sleep(0.35)
        if not self._click_confirm():
            return False
        time.sleep(2.0)
        if self._receipt_question() and self._any_captcha_image():
            self._log("  영수증 오답 — 새로고침")
            self._clear_answer(receipt=True)
            self._click_refresh()
            return False
        return True

    def try_solve(self, *, max_attempts: int = 6) -> bool:
        """캡챠가 있으면 풀고, 없으면 True. API 키 없으면 False."""
        if not self.enabled():
            self._log("  OpenAI API 키 없음 — 캡챠 자동해결 불가")
            return False
        if not self.looks_like_captcha():
            return True
        for attempt in range(1, max_attempts + 1):
            if self._stopped():
                self._log("  캡챠 해결 중단(정지)")
                return False
            self._log(f"  캡챠 자동해결 시도 {attempt}/{max_attempts}")
            if self.has_advisor_modal():
                self._log("  유형: 소유확인 보안문자 모달")
                ok = self.solve_advisor_modal_once()
            elif self._receipt_question() or self._has_receipt_captcha():
                self._log("  유형: 영수증/질문형 캡챠")
                ok = self.solve_receipt_once()
            elif self._has_char_captcha() or self._char_image():
                self._log("  유형: 문자 캡챠")
                ok = self.solve_char_once()
            else:
                time.sleep(1.0)
                continue
            if ok:
                self._log("  캡챠 통과")
                return True
            time.sleep(0.8)
        return False
