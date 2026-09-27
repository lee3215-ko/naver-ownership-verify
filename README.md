# 네이버 소유확인

네이버 서치어드바이저 보드의「소유확인 진행」을 일괄 처리하는 Windows 앱입니다.

## 다른 PC에서 사용

1. [최신 버전 다운로드 (NaverOwnership.zip)](https://github.com/lee3215-ko/naver-ownership-verify/releases/latest/download/NaverOwnership.zip)
2. zip 압축 해제
3. `NaverOwnership.exe` 또는 `실행.bat` 실행
4. 네이버 계정 · OpenAI API 키 입력 후「소유확인 일괄 실행」

**필요 환경:** Windows 10+, Chrome 또는 Edge 설치

설정·로그인 세션은 `data/` 폴더에 저장됩니다 (업데이트 시에도 유지).

## 개발 실행

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

## 배포

```bat
deploy.bat
```
