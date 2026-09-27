"""Tkinter update dialog (works with or without CustomTkinter)."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import urllib.error
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from updater import (
    UpdateInfo,
    can_auto_update,
    check_for_update,
    download_file_with_fallbacks,
    fetch_version_payload,
    format_network_error,
    get_update_log_path,
    schedule_apply_update,
    validate_zip_file,
)


def schedule_update_check(
    root,
    *,
    version_url: str,
    current_version: str,
    app_name: str,
    exe_name: str,
    delay_ms: int = 2500,
    zip_inner_folder: str | None = None,
    log_callback=None,
) -> None:
    if not version_url.strip():
        return

    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    def worker() -> None:
        try:
            info = check_for_update(version_url, current_version, app_name=app_name)
        except Exception as exc:
            root.after(0, lambda: log(f"[업데이트] 확인 오류: {exc}"))
            return
        if info is not None:
            root.after(
                0,
                lambda: _show_dialog(
                    root, info, current_version, app_name, exe_name, zip_inner_folder, log,
                ),
            )
        else:
            payload = fetch_version_payload(version_url, f"{app_name}/{current_version}")
            if payload is None:
                root.after(0, lambda: log("[업데이트] version.json 조회 실패 (네트워크 또는 GitHub 접근 확인)"))

    root.after(delay_ms, lambda: threading.Thread(target=worker, daemon=True).start())


def _show_dialog(
    root,
    info: UpdateInfo,
    current_version: str,
    app_name: str,
    exe_name: str,
    zip_inner_folder,
    log,
):
    try:
        root.update_idletasks()
        root.lift()
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    message = f"새 버전 {info.version}이 있습니다.\n(현재: {current_version})"
    if info.notes:
        message += f"\n\n{info.notes}"

    if can_auto_update() and info.url:
        message += "\n\n「예」= 자동 업데이트 후 재실행\n「아니오」= 브라우저에서 받기"
        choice = messagebox.askyesnocancel("업데이트", message, parent=root)
        if choice is True:
            _auto_update(root, info, app_name, exe_name, zip_inner_folder, log)
        elif choice is False:
            webbrowser.open(info.url)
        return

    message += "\n\nzip을 받아 설치 폴더에 덮어쓴 뒤 다시 실행하세요.\n다운로드 페이지를 열까요?"
    if messagebox.askyesno("업데이트", message, parent=root) and info.url:
        webbrowser.open(info.url)


def _auto_update(root, info: UpdateInfo, app_name: str, exe_name: str, zip_inner_folder, log):
    dialog = __import__("tkinter").Toplevel(root)
    dialog.title("업데이트 중")
    dialog.geometry("380x110")
    dialog.transient(root)
    dialog.grab_set()

    status = ttk.Label(dialog, text="다운로드 중...")
    status.pack(padx=16, pady=(16, 8))
    bar = ttk.Progressbar(dialog, length=340, mode="determinate")
    bar.pack(padx=16, pady=8)

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            pct = min(int(done * 100 / total), 100)
            root.after(
                0,
                lambda p=pct: (bar.configure(value=p), status.configure(text=f"다운로드 {p}%")),
            )
        else:
            root.after(0, lambda: status.configure(text="다운로드 중..."))

    def worker() -> None:
        zip_path = Path(tempfile.gettempdir()) / f"{app_name}-{info.version}.zip"
        log_path = get_update_log_path()
        try:
            log(f"[업데이트] 다운로드 시작: {info.version}")
            urls = list(info.download_urls) if info.download_urls else [info.url]
            used_url = download_file_with_fallbacks(
                urls,
                zip_path,
                user_agent=f"{app_name}/{info.version}",
                on_progress=on_progress,
            )
            validate_zip_file(zip_path, min_bytes=1024 * 1024)
            log(f"[업데이트] 다운로드 완료 ({zip_path.stat().st_size // 1024 // 1024} MB) via {used_url}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            detail = format_network_error(exc)
            root.after(0, dialog.destroy)
            root.after(
                0,
                lambda: messagebox.showerror(
                    "업데이트 실패",
                    f"다운로드 실패:\n{detail}\n\n「아니오」로 브라우저에서 직접 받아 주세요.",
                    parent=root,
                ),
            )
            log(f"[업데이트] 다운로드 실패: {exc}")
            return

        def finish() -> None:
            try:
                status.configure(text="설치 준비 중... 잠시 후 다시 실행됩니다.")
                dialog.update_idletasks()
                schedule_apply_update(
                    zip_path,
                    exe_name=exe_name,
                    zip_inner_folder=zip_inner_folder,
                    app_slug=app_name,
                )
                log(f"[업데이트] 설치 스크립트 실행 (로그: {log_path})")
            except (RuntimeError, OSError) as exc:
                messagebox.showerror("업데이트 실패", str(exc), parent=root)
                dialog.destroy()
                log(f"[업데이트] 설치 준비 실패: {exc}")
                return

            dialog.destroy()
            time.sleep(1.5)
            os._exit(0)

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()
