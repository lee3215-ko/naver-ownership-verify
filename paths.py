"""앱 경로 · 버전 (개발/PyInstaller exe 공통)."""
from __future__ import annotations

import os
import sys

APP_NAME = "NaverOwnership"
APP_DISPLAY_NAME = "네이버 소유확인"
APP_VERSION = "1.0.0"
UPDATE_VERSION_URL = (
    "https://raw.githubusercontent.com/lee3215-ko/naver-ownership-verify/main/version.json"
)


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def get_app_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_data_dir() -> str:
    data_dir = os.path.join(get_app_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def data_path(filename: str) -> str:
    return os.path.join(get_data_dir(), filename)


def get_resource_path(*parts: str) -> str:
    if is_frozen():
        base = getattr(sys, "_MEIPASS", get_app_dir())
    else:
        base = get_app_dir()
    return os.path.join(base, *parts)


def get_icon_path() -> str | None:
    ico = get_resource_path("assets", "app_icon.ico")
    if os.path.isfile(ico):
        return ico
    return None


def init_runtime_paths() -> None:
    os.chdir(get_app_dir())
    os.makedirs(get_data_dir(), exist_ok=True)
