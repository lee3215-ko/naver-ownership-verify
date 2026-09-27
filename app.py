"""네이버 서치어드바이저 소유확인 전용 GUI."""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from ownership_verify import (
    OwnershipBatchClient,
    load_json,
    normalize_site,
    save_json,
)
from paths import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    UPDATE_VERSION_URL,
    get_data_dir,
    get_icon_path,
)

DATA = Path(get_data_dir())
SETTINGS_PATH = DATA / "settings.json"


class App:
    def __init__(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        self.settings = load_json(
            SETTINGS_PATH,
            {
                "naver_id": "",
                "naver_pw": "",
                "openai_api_key": "",
                "site_urls": "",
                "headless": False,
            },
        )
        self._busy = False
        self._stop = threading.Event()

        self.root = tk.Tk()
        self.root.title(f"{APP_DISPLAY_NAME}  v{APP_VERSION}")
        self.root.geometry("760x640")
        self.root.minsize(640, 520)
        icon = get_icon_path()
        if icon:
            try:
                self.root.iconbitmap(icon)
            except Exception:
                pass

        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        acc = ttk.LabelFrame(frm, text="네이버 계정 · OpenAI", padding=10)
        acc.pack(fill=tk.X)

        row1 = ttk.Frame(acc)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="네이버 아이디", width=14).pack(side=tk.LEFT)
        self.var_id = tk.StringVar(value=self.settings.get("naver_id", ""))
        ttk.Entry(row1, textvariable=self.var_id, width=28).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row1, text="비밀번호").pack(side=tk.LEFT)
        self.var_pw = tk.StringVar(value=self.settings.get("naver_pw", ""))
        ttk.Entry(row1, textvariable=self.var_pw, show="*", width=22).pack(side=tk.LEFT)

        row2 = ttk.Frame(acc)
        row2.pack(fill=tk.X, pady=4)
        ttk.Label(row2, text="OpenAI API 키", width=14).pack(side=tk.LEFT)
        raw_key = self.settings.get("openai_api_key", "") or ""
        # • 문자로 오염된 키 방지
        if raw_key and not "".join(c for c in raw_key if ord(c) < 128).strip().startswith("sk-"):
            raw_key = ""
        self.var_key = tk.StringVar(value=raw_key)
        ttk.Entry(row2, textvariable=self.var_key, show="*").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        self.var_headless = tk.BooleanVar(value=bool(self.settings.get("headless", False)))
        ttk.Checkbutton(acc, text="헤드리스(비표시)", variable=self.var_headless).pack(
            anchor="w", pady=(4, 0)
        )

        sites = ttk.LabelFrame(
            frm,
            text="대상 사이트 (한 줄에 하나 · 비우면 보드의「소유확인 진행」전체)",
            padding=10,
        )
        sites.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.txt_sites = scrolledtext.ScrolledText(sites, height=10, wrap=tk.NONE)
        self.txt_sites.pack(fill=tk.BOTH, expand=True)
        self.txt_sites.insert("1.0", self.settings.get("site_urls", "") or "")

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=10)
        self.btn_run = ttk.Button(btns, text="소유확인 일괄 실행", command=self._start)
        self.btn_run.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(btns, text="정지", command=self._request_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="설정 저장", command=self._save_settings).pack(side=tk.LEFT)

        logf = ttk.LabelFrame(frm, text="로그", padding=8)
        logf.pack(fill=tk.BOTH, expand=True)
        self.txt_log = scrolledtext.ScrolledText(logf, height=14, state=tk.DISABLED)
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def log(self, msg: str) -> None:
        def _append():
            self.txt_log.configure(state=tk.NORMAL)
            self.txt_log.insert(tk.END, msg + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.configure(state=tk.DISABLED)

        self.root.after(0, _append)

    def _save_settings(self) -> None:
        key = "".join(c for c in self.var_key.get() if ord(c) < 128).strip()
        if key and not key.startswith("sk-"):
            messagebox.showwarning(
                "API 키",
                "OpenAI API 키는 sk- 로 시작해야 합니다.\n다시 입력해 주세요.",
                parent=self.root,
            )
            return
        if not key:
            # 입력란이 비었으면 기존 정상 키 유지
            prev = "".join(
                c for c in (self.settings.get("openai_api_key") or "") if ord(c) < 128
            ).strip()
            if prev.startswith("sk-"):
                key = prev
        self.settings = {
            "naver_id": self.var_id.get().strip(),
            "naver_pw": self.var_pw.get(),
            "openai_api_key": key,
            "site_urls": self.txt_sites.get("1.0", tk.END).strip(),
            "headless": bool(self.var_headless.get()),
        }
        save_json(SETTINGS_PATH, self.settings)
        if key:
            self.var_key.set(key)
        self.log("[설정] 저장됨")

    def _request_stop(self) -> None:
        self._stop.set()
        self.log("[제어] 정지 요청")

    def _checkpoint(self) -> bool:
        return not self._stop.is_set()

    def _start(self) -> None:
        if self._busy:
            messagebox.showinfo("진행 중", "이미 실행 중입니다.", parent=self.root)
            return
        self._save_settings()
        raw = self.txt_sites.get("1.0", tk.END)
        sites = [normalize_site(line) for line in raw.splitlines() if normalize_site(line)]
        nid = self.var_id.get().strip()
        npw = self.var_pw.get()
        if not nid or not npw:
            if not messagebox.askyesno(
                "계정",
                "네이버 계정이 비어 있습니다. 브라우저에서 수동 로그인할까요?",
                parent=self.root,
            ):
                return

        self._busy = True
        self._stop.clear()
        self.btn_run.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.log("======== 소유확인 일괄 시작 ========")

        api_key = "".join(c for c in self.var_key.get() if ord(c) < 128).strip()
        if not api_key.startswith("sk-"):
            prev = "".join(
                c for c in (self.settings.get("openai_api_key") or "") if ord(c) < 128
            ).strip()
            api_key = prev if prev.startswith("sk-") else ""
        if not api_key:
            messagebox.showwarning(
                "API 키",
                "OpenAI API 키가 없습니다. 캡챠 자동해결이 안 됩니다.\n설정에 키를 입력해 주세요.",
                parent=self.root,
            )
            self._busy = False
            self.btn_run.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)
            return

        def worker():
            results = []
            err = ""
            try:
                with OwnershipBatchClient(
                    headless=bool(self.var_headless.get()),
                    openai_api_key=api_key,
                    on_log=lambda m: self.root.after(0, lambda msg=m: self.log(msg)),
                    checkpoint=self._checkpoint,
                ) as client:
                    client.login(nid, npw)
                    results = client.run_batch(sites or None)
            except Exception as exc:
                err = str(exc)
                self.root.after(0, lambda e=err: self.log(f"[오류] {e}"))
            self.root.after(0, lambda: self._finish(results, err))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, results, err: str) -> None:
        self._busy = False
        self.btn_run.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        ok = sum(1 for r in results if r.ok)
        for r in results:
            self.log(f"{'✓' if r.ok else '✗'} {r.site_url} — {r.message}")
        if err:
            self.log(f"종료(오류): {err}")
        else:
            self.log(f"======== 완료: 성공 {ok}/{len(results)} ========")
        if err and "보호조치" in err:
            messagebox.showwarning(
                "보호조치",
                "네이버 계정 보호조치가 감지되었습니다.\n\n"
                "브라우저에서 본인인증으로 보호조치를 해제한 뒤\n"
                "다시「소유확인 일괄 실행」을 눌러 주세요.\n\n"
                f"{err}",
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "완료",
                f"소유확인 성공 {ok}/{len(results)}" + (f"\n{err}" if err else ""),
                parent=self.root,
            )

    def _on_close(self) -> None:
        self._stop.set()
        self.root.destroy()

    def run(self) -> None:
        try:
            from update_ui import schedule_update_check

            schedule_update_check(
                self.root,
                version_url=UPDATE_VERSION_URL,
                current_version=APP_VERSION,
                app_name=APP_NAME,
                exe_name=f"{APP_NAME}.exe",
                zip_inner_folder=APP_NAME,
                log_callback=self.log,
            )
        except Exception:
            pass
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
