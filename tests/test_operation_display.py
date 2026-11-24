"""실행 상태를 실패로 뭉뚱그리거나 파일의 위치를 단정하지 않는다."""

from tongdory2.web.operation_display import operation_display


def test_skip_deny_and_failure_have_distinct_user_results():
    results = [operation_display({"op": "MOVE", "status": status, "reason": reason})
               for status, reason in (("SKIP", "NAME_CONFLICT"), ("DENY", "DEST_PROTECTED"),
                                      ("FAILED", "PermissionError"))]
    assert [result["label"] for result in results] == ["건너뜀", "차단됨", "실패"]
    assert "같은 이름" in results[0]["reason"]
    assert "보호된 저장 위치" in results[1]["reason"]
    assert "접근 권한" in results[2]["reason"]
    assert all("원래 자리에 남음" not in result["label"] for result in results)


def test_failed_cleanup_and_unknown_status_do_not_claim_success():
    result = operation_display({"op": "MOVE", "status": "FAILED", "reason": "BOTH_PATHS:PermissionError"})
    assert result["label"] == "실패" and "모두 남아" in result["reason"]
    assert operation_display({"op": "COPY", "status": "UNEXPECTED"})["label"] == "결과 확인 필요"


def test_undo_overrides_original_success_and_keeps_undo_problem_visible():
    assert operation_display({"op": "MOVE", "status": "DONE", "undo_status": "UNDONE"})["label"] == "원래 위치로 되돌림"
    result = operation_display({"op": "MOVE", "status": "DONE", "undo_status": "CHANGED_AFTER_BATCH"})
    assert result["label"] == "이동 완료"
    assert "변경돼 건너뜀" in result["undo_label"]
