"""목록화 묶음 전체가 들어가는 선택 폼의 서버 쪽 한도."""

from fastapi import HTTPException, Request

from tongdory2.catalog.pipeline import MAX_ITEMS


async def read_selection(request: Request, field: str, *required: str,
                         optional: tuple[str, ...] = ()) -> tuple[dict[str, str], list[str]]:
    async with request.form(max_files=0, max_fields=MAX_ITEMS + len(required) + len(optional)) as form:
        values = {key: form.get(key) for key in required}
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise HTTPException(422, "분석 결과 또는 지정 항목을 다시 확인해 주세요.")
        selected = form.getlist(field)
        for key in optional:
            if isinstance(form.get(key), str):
                values[key] = form.get(key)
        if len(selected) > MAX_ITEMS:
            raise HTTPException(422, f"한 묶음에서 {MAX_ITEMS:,}개까지 선택할 수 있습니다.")
        return values, selected
