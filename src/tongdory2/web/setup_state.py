"""분석과 정리 계획에 저장하는 확정 설정 식별값."""

import hashlib
import hmac
import json
import os

from tongdory2.fsx.roots import Role

ROLES = {Role.CURRENT, Role.COLLECT, Role.STORAGE, Role.BASELINE}
SETTINGS_BUSY = "파일 분류나 분석이 진행 중입니다. 작업이 끝난 뒤 설정을 바꿔 주세요."
ARCHIVE_BUSY = "지식센터에 복사 중입니다. 복사가 끝난 뒤 다시 시도해 주세요."
_WINDOWS = os.name == "nt"

def pending_setup_step(roots, spec) -> str | None:
    if not spec or not any(n["status"] == "ADOPTED" and n["role"] == "DEST" for n in spec["body"]["nodes"]):
        return "structure"
    sources = [r for r in roots if r.role == Role.COLLECT]
    if not sources or not all(r.confirmed for r in sources):
        return "sources"
    if not any(r.role == Role.CURRENT and r.confirmed for r in roots) or not all(
        r.confirmed for r in roots if r.role in ROLES
    ):
        return "destination"
    return None


def approval_token(t, purpose: str, *values: str) -> str:
    """화면에 표시한 전송 내용과 서버의 승인 요청을 묶는다."""
    message = json.dumps((purpose, *values), ensure_ascii=False).encode()
    return hmac.new(t.approval_key, message, hashlib.sha256).hexdigest()


def approved(t, signature: str, purpose: str, *values: str) -> bool:
    return hmac.compare_digest(signature.encode(), approval_token(t, purpose, *values).encode())


def archive_running(t) -> bool:
    return bool((getattr(t, "kc_job", None) or {}).get("running"))


def settings_change_notice(t, busy_message: str = SETTINGS_BUSY) -> str | None:
    if archive_running(t):
        return ARCHIVE_BUSY
    if any(getattr(t, job, {}).get("running") for job in ("job", "prep_job", "handover_job", "ip_job", "auto_job")):
        return busy_message
    return None


def scope_key(roots, spec) -> str:
    paths = sorted((root.role.value, root.root_id, str(root.path), root.confirmed)
                   for root in roots if root.role in ROLES)
    snapshot = [spec["version"] if spec else 0, paths]
    if _WINDOWS:
        # 텍스트 모드로 계산한 예전 중복 판정과 실행 계획은 다시 분석한다.
        snapshot.append("binary-file-io")
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False).encode()).hexdigest()


def current_run(run, roots, spec) -> bool:
    return bool(run and spec and roots and all(r.confirmed for r in roots if r.role in ROLES)
                and run["status"] == "DONE" and run["spec_version"] == spec["version"]
                and json.loads(run["notice"] or "{}").get("scope_key") == scope_key(roots, spec))
