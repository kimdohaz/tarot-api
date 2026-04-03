from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Tarot Draw API", version="1.0.0")

BASE_DIR = Path(__file__).resolve().parent
SAJU_FILE = BASE_DIR / "saju_oracle.json"
LENO_FILE = BASE_DIR / "lenormand.json"
ALLOWED_JSON_FILE = BASE_DIR / "allowed_cards.json"
ALLOWED_MD_FILE = BASE_DIR / "ALLOWED_CARDS.md"

# 같은 질문 재뽑기 금지용. 예시용이라 서버 재시작하면 초기화됨.
DRAW_CACHE: dict[str, dict[str, Any]] = {}


def _extract_card_names(obj: Any) -> list[str]:
    """
    JSON 구조가 조금 달라도 카드명을 최대한 뽑아낸다.

    지원 예시:
    - ["기수", "편지", ...]
    - [{"name": "기수"}, {"card_name": "편지"}, ...]
    - {"cards": [...]} / {"data": [...]} / {"items": [...]} / {"deck": [...]} / {"list": [...]} / {"names": [...]}
    """
    if isinstance(obj, list):
        result: list[str] = []
        for item in obj:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    result.append(s)
            elif isinstance(item, dict):
                for key in ("name", "card_name", "title", "card", "label"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        result.append(value.strip())
                        break
        return result

    if isinstance(obj, dict):
        for key in ("cards", "data", "items", "deck", "list", "names"):
            if key in obj:
                return _extract_card_names(obj[key])

        # 혹시 dict의 value 안쪽에 카드 정보가 들어있는 경우도 한 번 긁어본다.
        result: list[str] = []
        for value in obj.values():
            result.extend(_extract_card_names(value))
        return result

    return []


def _load_json_card_names(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"파일이 없음: {path.name}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    names = _extract_card_names(data)
    names = [name for name in names if isinstance(name, str) and name.strip()]

    # 순서 유지하면서 중복 제거
    seen: set[str] = set()
    unique_names: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)

    if not unique_names:
        raise ValueError(f"카드명을 찾지 못함: {path.name}")

    return unique_names


def _load_allowed_from_md(path: Path) -> list[str]:
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    names: list[str] = []

    # 불릿/번호/체크박스/헤딩 등을 꽤 느슨하게 파싱한다.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line = re.sub(r"^#{1,6}\s*", "", line)  # markdown heading 제거
        line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s*)", "", line)
        line = line.strip("` ")

        if not line:
            continue

        # 설명이 붙은 줄이면 앞부분만 카드명으로 추정
        for sep in (" : ", ": ", " - ", " | ", " => ", " → "):
            if sep in line:
                line = line.split(sep, 1)[0].strip()
                break

        if line:
            names.append(line)

    # 순서 유지 중복 제거
    seen: set[str] = set()
    unique_names: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)

    return unique_names


def _load_allowed_cards() -> set[str]:
    if ALLOWED_JSON_FILE.exists():
        return set(_load_json_card_names(ALLOWED_JSON_FILE))

    md_names = _load_allowed_from_md(ALLOWED_MD_FILE)
    if md_names:
        return set(md_names)

    # allowed_cards 파일이 없으면 덱 두 개의 합집합을 허용 목록으로 사용
    return set(SAJU_ORACLE + LENORMAND)


def _cache_key(deck: str, count: int, request_key: str | None) -> str | None:
    if not request_key:
        return None
    base = f"{deck}|{count}|{request_key.strip()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# 서버 시작 시 덱 미리 로드
SAJU_ORACLE = _load_json_card_names(SAJU_FILE)
LENORMAND = _load_json_card_names(LENO_FILE)
ALLOWED_CARDS = _load_allowed_cards()

DECKS: dict[str, list[str]] = {
    "saju_oracle": SAJU_ORACLE,
    "lenormand": LENORMAND,
}


class DrawRequest(BaseModel):
    deck: str = Field(description="saju_oracle 또는 lenormand")
    count: int = Field(ge=1, description="뽑을 카드 장수")
    spread: str | None = Field(default=None, description="예: SBA, Relationship, Decision")
    request_key: str | None = Field(
        default=None,
        description="같은 질문 재뽑기 금지용 키. 같은 질문이면 같은 문자열을 보내면 됨.",
    )


@app.get("/")
def home() -> dict[str, Any]:
    return {
        "message": "tarot api is running",
        "decks": {
            "saju_oracle": len(SAJU_ORACLE),
            "lenormand": len(LENORMAND),
        },
        "allowed_cards": len(ALLOWED_CARDS),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/privacy", response_class=HTMLResponse)
def privacy_policy() -> str:
    return """
    <html>
      <head>
        <meta charset="utf-8">
        <title>Privacy Policy</title>
      </head>
      <body style="font-family: sans-serif; max-width: 800px; margin: 40px auto; line-height: 1.7;">
        <h1>Privacy Policy</h1>
        <p>이 GPT는 카드 추첨을 위해 외부 API를 호출합니다.</p>
        <p>처리하는 정보:</p>
        <ul>
          <li>사용자가 입력한 질문 일부</li>
          <li>선택된 덱 정보</li>
          <li>스프레드 정보</li>
          <li>request_key 등 카드 추첨에 필요한 최소 정보</li>
        </ul>
        <p>이 정보는 카드 추첨 결과를 반환하기 위한 목적으로만 사용됩니다.</p>
        <p>민감한 개인정보의 장기 저장은 의도하지 않습니다.</p>
        <p>문의: ksr3595@naver.com</p>
      </body>
    </html>
    """


@app.post("/draw")
def draw_cards(req: DrawRequest) -> dict[str, Any]:
    if req.deck not in DECKS:
        raise HTTPException(status_code=400, detail="deck은 saju_oracle 또는 lenormand만 가능")

    deck = DECKS[req.deck]

    if req.count > len(deck):
        raise HTTPException(
            status_code=400,
            detail=f"count가 덱 장수보다 큼. deck={req.deck}, size={len(deck)}",
        )

    key = _cache_key(req.deck, req.count, req.request_key)
    if key and key in DRAW_CACHE:
        return DRAW_CACHE[key]

    cards = random.sample(deck, req.count)

    invalid_cards = [card for card in cards if card not in ALLOWED_CARDS]
    if invalid_cards:
        raise HTTPException(
            status_code=500,
            detail=f"허용 목록에 없는 카드가 감지됨: {invalid_cards}",
        )

    response: dict[str, Any] = {
        "draw_id": str(uuid.uuid4()),
        "deck": req.deck,
        "count": req.count,
        "spread": req.spread,
        "cards": cards,
    }

    if key:
        DRAW_CACHE[key] = response

    return response
