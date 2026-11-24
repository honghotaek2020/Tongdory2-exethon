"""화면 문구: 내부 단계 이름·판정 코드·예외 이름이 사용자 말로 보이는지 (저장된 기록은 그대로 둔다)."""

from tongdory2.messages import badge_label, error_text, folder_reason, reason_brief, stage_label


def test_progress_stage_names_are_user_words_and_unknown_stages_pass_through():
    assert stage_label("1차-1 파일명 매칭") == "Liner가 파일명으로 맞는 폴더 찾는 중"
    assert stage_label("목적지 대조") == "이동할 폴더 찾는 중"
    assert stage_label("과제 판정") == "이번 과제 자료인지 확인하는 중"
    assert stage_label("후보 노드 찾기") == "맞는 폴더 후보 찾는 중"
    assert stage_label("로컬 분류") == "내 PC 규칙으로 분류하는 중"
    assert stage_label("중복 확인") == "중복 확인"  # 바꾸지 않은 단계는 그대로


def test_badges_show_user_words():
    assert badge_label("로컬 파일명") == "내 PC 규칙(파일명)"
    assert badge_label("로컬 폴더명") == "내 PC 규칙(폴더명)"
    assert badge_label("2차-2 본문 새 폴더") == "Liner가 문서 내용으로 제안한 새 폴더"  # 완료형
    assert badge_label("민감") == "민감"


def test_old_stored_reasons_render_without_jargon():
    old = "[2차-2 본문 새 폴더] 유지보수형 프로필의 '회의록' 노드 차용 → 보고 아래 새 폴더 추천"
    shown = folder_reason(old)
    assert "노드" not in shown and "2차-2" not in shown and "'회의록' 폴더를 참고해" in shown
    assert folder_reason("HOLD — 이동 대상 아님") == "옮길 폴더가 정해지지 않아 이동하지 않음"
    assert "KeyError" not in folder_reason("분석 중 오류(KeyError) — 다시 분석하거나 직접 지정")
    assert reason_brief("[1차-1 파일명 매칭] 82% · 이름이 같음").startswith("82%")


def test_technical_errors_are_replaced_but_korean_messages_kept():
    assert error_text("ValueError: bad json") == "Liner 분석 중 오류가 발생했습니다. 다시 분석해 주세요."
    assert error_text("httpx.ConnectTimeout: timed out").startswith("Liner 분석 중 오류")
    assert error_text("이 묶음의 분석을 마치지 못했습니다.") == "이 묶음의 분석을 마치지 못했습니다."
    assert error_text(None) == ""


def test_file_names_inside_reasons_are_not_rewritten():
    """근거 안 따옴표 속 파일명·폴더명은 용어 변환이나 오류 이름 제거의 대상이 아니다."""
    assert "'report(ValueError).md'" in folder_reason("내용이 'report(ValueError).md'과 완전히 같음")
    assert "'계획한 폴더 목록.md'" in folder_reason("원본 '계획한 폴더 목록.md'과 내용이 같음")
    assert folder_reason("계획한 폴더 목록이 비어 있음") == "분류 희망 폴더 목록이 비어 있음"  # 따옴표 밖은 바꾼다
    assert folder_reason("새 폴더 이름도 정하지 못함(TimeoutError) — 직접 지정") == "새 폴더 이름도 정하지 못함 — 직접 지정"


def test_liner_errors_explain_what_to_do():
    assert "키를 확인" in error_text("LinerError: [401 UNAUTHORIZED] bad key")
    assert "크레딧" in error_text("LinerError: [402 payment_required] no credit")
    assert "키를 등록" in error_text("LinerError: [0 no_api_key] LINER_API_KEY is empty")


def test_save_modal_location_and_legacy_inplace_reasons():
    from tongdory2.messages import msg

    assert "~/Documents" in msg("setup.save.modal.loc_custom", folder="~/Documents", filename="list.json")
    assert "list.json" in msg("setup.save.modal.loc_default", filename="list.json")
    assert "명세" not in folder_reason("명세 산출물에 대응하지 않는 문서 유형 — 색인에는 포함")
    assert "로컬 결과" not in folder_reason("Liner에 보내지 않음(로컬 결과만) — 다시 분석해 전송을 승인하면 폴더를 매핑합니다")
