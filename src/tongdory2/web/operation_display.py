"""작업 기록의 상태·사유를 사용자에게 보여 주는 문구."""

STATUS_LABELS = {
    "DONE": "완료", "FAILED": "실패", "SKIP": "건너뜀", "SKIPPED": "건너뜀",
    "DENY": "차단됨", "DENIED": "차단됨", "PENDING": "처리 대기", "RUNNING": "처리 중",
}
REASONS = {
    "INTERRUPTED": "실행이 중단된 뒤 파일 상태를 확인했습니다.",
    "NAME_CONFLICT": "같은 이름의 파일이 이미 있습니다.",
    "SRC_CHANGED": "계획을 만든 뒤 파일이 변경됐습니다.",
    "SRC_MISSING": "원래 위치에 파일이 없거나 링크 파일입니다.",
    "RECENTLY_MODIFIED": "최근 수정된 파일이라 이번 이동에서 제외했습니다.",
    "DEST_PROTECTED": "보호된 저장 위치로는 옮길 수 없습니다.",
    "OUT_OF_SCOPE": "등록한 폴더 범위를 벗어났습니다.",
    "DENIED_PATH": "접근이 제한된 경로입니다.",
    "ROLE_DENIED": "이 용도로 등록한 폴더에서는 해당 작업을 할 수 없습니다.",
    "SYMLINK_IN_PATH": "경로에 링크가 포함돼 있습니다.",
    "NOT_CONFIRMED": "폴더 설정이 확정되지 않았습니다.",
    "NOT_A_FILE": "일반 파일이 아닙니다.",
    "MKDIR_NOT_IN_SPEC": "생성할 폴더가 승인한 계획에 없습니다.",
    "DEST_NOT_IN_SPEC": "저장 위치가 승인한 계획에 없습니다.",
    "CROSS_VOLUME": "서로 다른 저장 장치 사이의 이동은 지원하지 않습니다.",
    "SENSITIVE": "민감 파일은 이동하지 않습니다.",
    "ACK_REQUIRED": "이동에 필요한 추가 확인이 없습니다.",
    "OP_NOT_ALLOWED": "지원하지 않는 작업입니다.",
    "NOT_EMPTY": "폴더가 비어 있지 않아 유지했습니다.",
    "PermissionError": "파일 접근 권한이 없습니다.",
    "FileNotFoundError": "파일이나 폴더를 찾을 수 없습니다.",
    "FileExistsError": "같은 이름의 파일이나 폴더가 이미 있습니다.",
    "OSError": "파일을 처리하는 중 오류가 발생했습니다.",
}
UNDO_LABELS = {
    "INTERRUPTED_UNCERTAIN": "중단 전 상태를 확인할 수 없어 파일과 폴더를 유지했습니다.",
    "UNDONE": "원래 상태로 되돌림", "CHANGED_AFTER_BATCH": "작업 후 변경돼 건너뜀",
    "MISSING": "파일이 없어 건너뜀", "NOT_EMPTY": "폴더가 비어 있지 않아 유지",
    "ORIGINAL_OCCUPIED": "원래 위치에 같은 이름의 파일이 있어 건너뜀",
    "OUT_OF_SCOPE": "등록한 폴더 범위 밖이라 건너뜀",
}


def operation_display(op: dict) -> dict:
    moving = op["op"] in ("MOVE", "TRASH")
    undone = op.get("undo_status") == "UNDONE"
    status = op["status"]
    label = ("원래 위치로 되돌림" if moving and undone else "원래 상태로 되돌림" if undone
             else "이동 완료" if moving and status == "DONE" else STATUS_LABELS.get(status, "결과 확인 필요"))
    code = op.get("reason") or ""
    reason = ("원본과 목적지에 파일이 모두 남아 있습니다. 작업 내역을 확인해 주세요."
              if code.startswith("BOTH_PATHS:") else REASONS.get(code, "처리 사유를 확인해 주세요."))
    undo_code = op.get("undo_status") or ""
    return {"label": label, "tone": "" if undone else "ok" if status == "DONE" else "warn",
            "reason": reason if code and code != "OK" else "", "code": code if code != "OK" else "",
            "undo_label": UNDO_LABELS.get(undo_code, "되돌리기를 마치지 못했습니다.") if undo_code and not undone else ""}
