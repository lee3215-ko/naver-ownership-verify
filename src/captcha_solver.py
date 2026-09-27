"""OpenAI GPT-4o Vision 캡챠 해석 (티스토리 자동배포 로직)."""

from __future__ import annotations

import json
import re
from typing import Callable

LogFn = Callable[[str], None]


def _clean_json(raw: str) -> dict:
    text = (raw or "").replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        raise


class CaptchaSolver:
    def __init__(self, openai_api_key: str | None = None, on_log: LogFn | None = None):
        self.openai_api_key = (openai_api_key or "").strip()
        self.on_log = on_log or (lambda msg: None)

    def enabled(self) -> bool:
        return bool(self.openai_api_key)

    def solve_login(self, image_b64: str, prompt: str) -> dict:
        return self._solve(image_b64, prompt)

    def _solve(self, image_b64: str, prompt: str) -> dict:
        if not self.openai_api_key:
            raise RuntimeError("OpenAI API 키가 없습니다.")
        from openai import OpenAI

        client = OpenAI(api_key=self.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0,
            max_tokens=250,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
        )
        text = response.choices[0].message.content or ""
        self.on_log(f"  → Vision 원문: {(text or '')[:120]}")
        return _clean_json(text)
