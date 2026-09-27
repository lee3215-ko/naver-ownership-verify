"""간단한 앱 아이콘 생성 (없으면 assets/app_icon.ico)."""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets" / "app_icon.ico"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.is_file() and OUT.stat().st_size > 100:
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return

    size = 256
    img = Image.new("RGBA", (size, size), (3, 199, 90, 255))  # Naver green
    draw = ImageDraw.Draw(img)
    margin = 36
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=40,
        fill=(255, 255, 255, 255),
    )
    draw.ellipse((88, 72, 168, 152), fill=(3, 199, 90, 255))
    draw.rectangle((118, 140, 138, 200), fill=(3, 199, 90, 255))
    img.save(OUT, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
    print(f"icon: {OUT}")


if __name__ == "__main__":
    main()
