"""파일 통돌이 UI·안내·오류 메시지 중앙 카탈로그(메시지 ID 기반 관리)."""

from __future__ import annotations

import re

MESSAGES: dict[str, str] = {
    # ------------------------------------------------------------------
    # 1단계: 폴더 목록 구조 (/setup/structure)
    # ------------------------------------------------------------------
    "setup.structure.intro": (
        "많은 파일을 원하는 폴더로 분류할 수 있도록 폴더 구조를 정합니다. "
        "기존 폴더나 목록 파일에서 가져오거나 추천 목록을 불러와 원하는 대로 수정할 수 있습니다. "
        "이 단계에서는 목록만 정합니다. 실제로 파일을 모을 위치는 ‘저장 폴더’ 단계에서 선택합니다."
    ),
    "setup.import.title": "분류 희망 폴더 목록 가져오기",
    "setup.import.hint": "고르는 즉시 아래 목록에 바로 채워집니다",
    "setup.import.folder.title": "기존 폴더에서 가져오기",
    "setup.import.folder.desc": "선택한 폴더 안의 하위 폴더 목록을 불러옵니다.",
    "setup.import.folder.btn": "폴더 선택",
    "setup.import.file.title": "파일에서 가져오기",
    "setup.import.file.desc": "JSON·TXT 파일에서 폴더 목록을 불러옵니다.",
    "setup.import.file.btn": "파일 선택",
    "setup.import.file.aria": "목록·정의서 파일 선택",
    "setup.import.file.submit": "파일 읽기",
    "setup.import.preset.title": "추천 폴더 목록 사용",
    "setup.import.preset.desc": "개발 과제에 쓸 수 있는 예시 폴더 13개를 불러옵니다. 필요한 폴더를 추가하거나 빼고 이름을 바꿀 수 있습니다.",
    "setup.import.preset.btn": "추천 목록 불러오기",
    "setup.flow.link": "가져온 결과는 아래 폴더 목록과 오른쪽 미리보기에 바로 표시됩니다",

    "setup.list.title.imported": "분류 희망 폴더 목록 편집",
    "setup.list.title.default": "분류 희망 폴더 목록 편집",
    "setup.list.editable_note": "(직접 고칠 수 있음)",
    "setup.list.imported_done": "폴더 {count}개를 가져왔습니다.",
    "setup.list.imported_guide": "아래 목록을 확인하고 필요하면 수정하세요.",
    "setup.list.imported_preview_note": "오른쪽 미리보기도 함께 바뀌었습니다 →",
    "setup.list.source.folder": "폴더: {name}",
    "setup.list.source.file": "파일: {name}",
    "setup.list.total_folders": "전체 {count}개 폴더",
    "setup.list.total_empty": "입력된 폴더가 없습니다",
    "setup.list.required_count": "필수 {count}개",
    "setup.list.help_prefix": "한 줄에 폴더 경로 하나씩 적으세요. 필수 산출물을 보관할 폴더 경로 끝에 ",
    "setup.list.help_suffix": "를 붙이세요.",
    "setup.list.unnumbered_summary": "번호 없는 폴더 {count}개",
    "setup.list.unnumbered_hint": "가져온 이름을 그대로 적용합니다. 번호가 필요하면 확정 전에 목록에서 수정할 수 있습니다.",

    "setup.save.strip.title": "분류 희망 폴더 목록 저장",
    "setup.save.strip.optional": "(선택)",
    "setup.save.strip.desc": "분류 희망 폴더 목록을 파일로 저장하면 다음 작업에서 다시 사용할 수 있습니다.",
    "setup.save.strip.btn": "파일로 저장",
    "setup.actions.skip_hint": "저장하지 않아도 바로 다음 단계로 넘어갈 수 있습니다.",
    "setup.actions.confirm_next": "분류 희망 폴더 목록 확정하고 다음",

    # 1단계 저장 모달
    "setup.save.modal.title": "분류 희망 폴더 목록 저장",
    "setup.save.modal.close": "닫기",
    "setup.save.modal.cancel": "취소",
    "setup.save.modal.submit": "파일로 저장",
    "setup.save.modal.desc": "분류 희망 폴더 목록을 다음 작업에서 다시 쓸 수 있도록 파일로 저장합니다. 실제 폴더는 파일을 옮길 때 필요한 것만 만듭니다.",
    "setup.save.modal.fmt_legend": "저장 파일 형식",
    "setup.save.modal.fmt_json_title": "파일 통돌이 목록 파일 (.json)",
    "setup.save.modal.fmt_recommended": "기본",
    "setup.save.modal.fmt_json_desc": "폴더 순서와 [필수] 표시를 저장해 다음 작업에서 다시 사용할 수 있습니다.",
    "setup.save.modal.fmt_txt_title": "일반 텍스트 파일 (.txt)",
    "setup.save.modal.fmt_txt_desc": "메모장에서 바로 열어 고치거나 다른 사람과 주고받기 편합니다.",
    "setup.save.modal.name_label": "파일 이름 및 저장 위치",
    "setup.save.modal.dir_choose": "파일 저장 폴더 선택",
    "setup.save.modal.loc_label": "저장할 파일:",
    "setup.save.modal.loc_default": "기본 다운로드 폴더 / {filename}",
    "setup.save.modal.loc_custom": "{folder} / {filename}",
    "setup.save.modal.dir_reset": "다운로드 폴더로 변경",
    "setup.save.modal.preview_summary": "파일 내용 미리보기",
    "setup.save.modal.summary": "입력 {lines}줄 · 전체 폴더 {folders}개 · 필수 {required}개",
    "setup.save.done.disk": "저장 완료: {path}",
    "setup.save.done.download": "파일 내려받기 완료: {filename}",
    "setup.save.empty_warn": "먼저 저장할 폴더 목록을 입력하거나 가져와 주세요.",

    # 1단계 교체 확인 모달 & 토스트
    "setup.confirm.title": "현재 입력한 목록을 바꿀까요?",
    "setup.confirm.default_msg": "지금 목록 칸에 들어 있는 내용이 새 목록으로 바뀝니다. 파일에 오류가 있으면 현재 입력은 그대로 남겨 둡니다.",
    "setup.confirm.keep_note": "파일에 오류가 있으면 현재 입력은 그대로 남겨 둡니다.",
    "setup.confirm.replace_file": "'{filename}' 파일의 목록으로 현재 입력한 목록을 바꿀까요?",
    "setup.confirm.replace_file_generic": "선택한 파일의 목록으로 현재 입력한 목록을 바꿀까요?",
    "setup.confirm.replace_folder": "선택한 폴더의 하위 목록으로 현재 입력한 목록을 바꿀까요?",
    "setup.confirm.replace_template": "추천 폴더 목록으로 현재 입력한 목록을 바꿀까요?",
    "setup.confirm.cancel": "취소",
    "setup.confirm.ok": "목록 바꾸기",
    "setup.toast.md_converted": "Markdown 계층 목록을 폴더 경로로 변환했습니다.",
    "setup.toast.template_same": "이미 추천 폴더 목록이 들어 있습니다.",
    "setup.toast.template_filled": "추천 폴더 목록 13개를 채웠습니다.",
    "setup.draft.notice": "이전에 작성하던 내용이 있습니다. ",
    "setup.draft.restore": "작성 내용 복원",
    "setup.draft.discard": "작성 내용 삭제",

    # 우측 트리 패널 (1단계 보조 안내)
    "setup.tree.title": "분류 희망 폴더 목록",
    "setup.tree.desc": "왼쪽에서 고친 내용이 바로 보입니다.",
    "setup.tree.depth": "최대 {depth}단계",
    "setup.tree.lines": "입력 줄 {lines}개",
    "setup.tree.folder_count": "폴더 {count}개",
    "setup.tree.bottom.title": "목록을 저장하면",
    "setup.tree.bottom.line": "폴더 순서·계층·[필수] 표시가 파일에 그대로 남으며, 저장만으로는 PC에 실제 폴더가 생기지 않습니다.",

    # ------------------------------------------------------------------
    # 1단계 파서·검증·미리보기 오류
    # ------------------------------------------------------------------
    "setup.err.invalid_path": "하위 폴더 경로를 확인해 주세요: {raw}",
    "setup.err.invalid_line": "{lineno}번째 줄: 하위 폴더 경로를 확인해 주세요({raw})",
    "setup.err.duplicate_path": "같은 폴더 경로가 중복되어 있습니다: {path}",
    "setup.err.duplicate_line": "{lineno}번째 줄: 같은 폴더 경로가 중복되어 있습니다({first}번째 줄과 중복): {path}",
    "setup.err.more_errors": "{first} (외 오류 {extra}건)",
    "setup.err.max_nodes": "폴더 구조는 최대 1,000개까지 저장할 수 있습니다.",
    "setup.err.folder_over_limit": "폴더가 1,000개를 넘습니다. 더 작은 하위 폴더를 선택해 주세요.",
    "setup.err.empty_list": "목록에 저장할 폴더가 없습니다.",
    "setup.err.choose_one_source": "기존 폴더와 정의서 중 하나를 선택해 주세요.",
    "setup.err.doc_too_large": "정의서 파일은 8MB까지 읽을 수 있습니다.",
    "setup.err.doc_zip_too_large": "정의서 내부 데이터가 너무 큽니다. 필요한 표만 담은 파일을 사용해 주세요.",
    "setup.err.choose_sub_folder": "목록을 가져올 구체적인 하위 폴더를 선택해 주세요.",
    "setup.err.preview_read_failed": "폴더나 정의서를 읽을 수 없습니다. 경로, 파일 형식, 인코딩을 확인해 주세요.",
    "setup.err.kept_editing": "편집하던 목록은 아래 칸에 그대로 남겨 두었습니다.",
    "setup.err.unsupported_doc_ext": "json, txt, md, xlsx, docx 파일을 선택해 주세요.",
    "setup.err.unsupported_export_fmt": "지원하지 않는 파일 형식입니다. JSON 또는 TXT를 선택해 주세요.",
    "setup.err.save_failed": "파일을 저장할 수 없습니다.",
    "setup.err.md_tab": "Markdown {lineno}번째 줄: 들여쓰기는 탭 대신 공백(2칸 또는 4칸)을 사용해 주세요.",
    "setup.err.md_orphan": "Markdown {lineno}번째 줄: 상위 폴더 없이 들여쓰기된 항목이 있습니다.",
    "setup.err.md_unit": "Markdown {lineno}번째 줄: 들여쓰기는 2칸 또는 4칸으로 맞춰 주세요.",
    "setup.err.md_uneven": "Markdown {lineno}번째 줄: 들여쓰기 간격이 일정하지 않습니다({unit}칸 단위로 맞춰 주세요).",
    "setup.err.md_skip_level": "Markdown {lineno}번째 줄: 중간 단계를 건너뛴 들여쓰기입니다. 이전 줄의 하위 단계로 맞춰 주세요.",
    "setup.err.md_bullet_required": "Markdown {lineno}번째 줄: 들여쓰기 목록은 '- ' 기호로 시작해 주세요.",
    "setup.err.md_duplicate": "Markdown {lineno}번째 줄: 같은 폴더 경로가 중복되어 있습니다({first}번째 줄과 중복): {path}",
    "setup.err.json_parse": "JSON 파일 형식을 읽을 수 없습니다. UTF-8 JSON 파일인지 확인해 주세요.",
    "setup.err.json_not_object": "파일 통돌이 폴더 목록 JSON 형식이 아닙니다.",
    "setup.err.json_bad_header": "지원하지 않는 목록 파일입니다. 파일 통돌이에서 저장한 JSON 파일을 선택해 주세요.",
    "setup.err.json_empty": "JSON 파일에 불러올 폴더 목록이 비어 있습니다.",
    "setup.err.json_bad_item": "JSON {index}번째 항목 형식이 올바르지 않습니다.",
    "setup.err.json_empty_path": "JSON {index}번째 항목의 폴더 경로가 비어 있습니다.",
    "setup.err.json_bad_required": "JSON {index}번째 항목의 필수 값(required)은 true 또는 false여야 합니다.",
    "setup.err.json_duplicate": "JSON {index}번째 항목: 같은 폴더 경로가 중복되어 있습니다({first}번째 항목과 중복): {path}",

    # ------------------------------------------------------------------
    # 공용 파일 저장(방식 1) 오류
    # ------------------------------------------------------------------
    "export.err.need_abs_path": "저장할 폴더의 전체 경로를 선택해 주세요.",
    "export.err.no_symlink": "링크 경로 대신 실제 폴더를 선택해 주세요.",
    "export.err.no_drive_root": "최상위 드라이브 대신 구체적인 하위 폴더를 선택해 주세요.",
    "export.err.denied_dir": "시스템·보안·앱 데이터 폴더에는 저장할 수 없습니다.",
    "export.err.skip_or_src_dir": "소스·빌드·Git 폴더 안에는 저장할 수 없습니다.",
    "export.err.not_writable": "선택한 폴더가 없거나 쓰기 권한이 없습니다.",
    "export.err.too_many_collisions": "같은 이름의 파일이 너무 많아 저장할 수 없습니다. 파일 이름을 바꿔 주세요.",

    # ------------------------------------------------------------------
    # 4단계 완료(/s3?done=1) 및 인수인계(/s4) 최종 결과 목록표
    # ------------------------------------------------------------------
    "done.summary.title": "이번 정리 요약",
    "done.summary.hint": "저장 폴더: {root}",
    "done.status.done": "정리 완료",
    "done.status.undone": "되돌린 작업",
    "done.status.partial_undo": "일부 되돌림",
    "done.status.review_only": "검토 완료(이동 없음)",
    "done.status.partial_done": "부분 완료",
    "done.notice.undone": "이 작업 기록은 이미 원래대로 되돌려졌습니다. 아래 목록은 현재 저장 폴더의 최신 상태로 저장됩니다.",
    "done.notice.partial_undo": "이 작업 기록은 파일 {count}개가 되돌려진 상태입니다. 아래 수치는 현재 유지 중인 이동 건수 기준입니다.",
    "done.kpi.moved": "이동한 파일",
    "done.kpi.created_dirs": "새로 만든 폴더",
    "done.kpi.left": "원래 위치에 둔 파일",

    "catalog.card.title": "정리 결과 저장",
    "catalog.card.badge": "인수인계 · 재활용",
    "catalog.card.desc": "폴더별 파일 목록을 문서(.md)로 저장하거나, 다음 정리 계획에 사용할 폴더 목록(.json/.txt)을 저장합니다.",
    "catalog.card.btn": "파일 목록 저장",
    "catalog.card.handover_link": "인수인계·지식센터 →",
    "catalog.card.reuse_hint": "이 화면에서 저장한 .json 파일은 다음 과제 1단계 [파일 선택]에서 바로 다시 불러올 수 있습니다.",
    "catalog.card.l3_badge": "민감 파일 자동 제외",
    "catalog.card.truncated": "폴더가 1,000개를 넘어 폴더 목록 파일(.json/.txt)에는 중요·파일 있는 폴더 우선 1,000개까지 담습니다(생략 {count}개).",
    "catalog.table.title": "저장 폴더의 파일 현황",
    "catalog.table.hint": "폴더별 파일 목록입니다. 민감 파일은 표시하지 않습니다.",

    "catalog.modal.title": "정리 결과 저장",
    "catalog.modal.desc": "문서 형식에는 폴더별 파일 목록을, 목록 형식에는 다음 정리 계획에 사용할 폴더 목록을 저장합니다. 민감 파일은 목록에서 제외됩니다.",
    "catalog.modal.fmt_legend": "파일 형식",
    "catalog.modal.fmt_md_title": "인수인계용 문서 (.md)",
    "catalog.modal.fmt_md_desc": "폴더별 파일 목록과 필수 산출물 확인 현황을 담아 다음 담당자에게 전달할 수 있습니다.",
    "catalog.modal.fmt_json_title": "다음 과제에 사용할 폴더 목록 (.json)",
    "catalog.modal.fmt_json_desc": "다음 과제의 1단계에서 [파일 선택]으로 불러와 폴더 목록을 다시 사용할 수 있습니다.",
    "catalog.modal.fmt_txt_title": "텍스트 파일 (.txt)",
    "catalog.modal.fmt_txt_desc": "메모장에서 열거나 다음 과제의 ‘분류 희망 폴더 목록’ 단계에서 불러올 수 있습니다.",
    "catalog.modal.loading_summary": "저장 폴더 현황을 확인하는 중...",
    "catalog.modal.loading_preview": "불러오는 중...",
    "catalog.modal.undone_warn": "선택한 작업 기록은 이미 되돌려졌습니다. 현재 저장 폴더의 최신 상태로 목록표를 저장합니다.",
    "catalog.modal.partial_undo_warn": "선택한 작업 기록은 파일 {count}개가 되돌려진 상태입니다. 현재 저장 폴더의 최신 상태로 목록표를 저장합니다.",
    "catalog.modal.browser_title": "파일 저장 폴더 선택",
    "catalog.modal.browser_parent": "상위 폴더",
    "catalog.modal.browser_use": "이 폴더 선택",
    "catalog.done.disk": "정리 결과 저장 완료: {path}",
    "catalog.done.download": "정리 결과 내려받기 완료: {filename}",
    "catalog.err.need_setup": "저장 폴더와 분류 희망 폴더 목록을 먼저 확정해 주세요.",
    "catalog.err.unsupported_fmt": "지원하지 않는 파일 형식입니다. MD, JSON, TXT 중 하나를 선택해 주세요.",
    "catalog.err.unsupported_preview_fmt": "지원하지 않는 형식입니다.",
    "catalog.err.summary_failed": "요약을 불러오지 못했습니다.",
    "catalog.err.preview_failed": "미리보기를 불러오지 못했습니다.",
    "catalog.err.export_failed": "정리 결과를 저장하지 못했습니다.",
}


def folder_reason(value: str | None) -> str:
    """이전 분석 기록도 현재 폴더 용어로 표시한다. 파일명·문서 제목은 바꾸지 않는다."""
    wording = {
        "구조 명세에 목적지 폴더가 없음 — S1에서 명세를 다시 확정하세요":
            "분류 희망 폴더 목록이 비어 있습니다. 정리 설정에서 목록을 다시 확정하세요.",
        "명세의 어느 폴더와도 확실히 맞지 않음": "분류 희망 폴더 목록에서 확실히 맞는 곳을 찾지 못함",
        "명세에 맞는 폴더 없음": "분류 희망 폴더 목록에서 맞는 곳을 찾지 못함",
        "명세 외 폴더 — 보존": "분류 희망 폴더 목록에 없는 폴더 — 그대로 유지",
        "맞는 기준 폴더를": "분류 희망 폴더 목록에서 맞는 곳을",
        "계획한 폴더 목록": "분류 희망 폴더 목록",
        "계획한 폴더 중 맞는 곳": "분류 희망 폴더 목록에서 맞는 곳",
        "명세 산출물": "분류 희망 폴더",
        "로컬 결과": "내 PC 분류 결과",
        "노드 차용 — 보고 아래 새 폴더 제안": "폴더 활용 — 보고 아래 새 폴더 제안",
    }
    text = value or ""
    for pattern, new in _REASON_PATTERNS:  # 문장 단위로 고정된 예전 근거만 바꾼다
        text = pattern.sub(new, text)
    # 근거 안의 파일명·폴더명·문서 제목은 작은따옴표로 감싸여 있다. 따옴표 밖에서만 용어를 바꾼다.
    parts = _QUOTED.split(text)
    for i in range(0, len(parts), 2):
        for old, new in wording.items():
            parts[i] = parts[i].replace(old, new)
    text = "".join(parts)
    for stage, label in _REASON_STAGE.items():
        text = text.replace(f"[{stage}]", f"[{label}]")
    return text


_QUOTED = re.compile(r"('[^']*')")


# 저장된 분석 기록의 내부 표현을 화면용 말로 바꾼다 (기록 자체는 바꾸지 않는다).
_REASON_PATTERNS = (
    # 예전 근거: "유지보수형 프로필의 '회의록' 노드 차용 → 보고 아래 새 폴더 추천"
    (re.compile(r"유지보수형 프로필의 '([^']*)' 노드 차용 → 보고 아래 새 폴더 추천"),
     r"유지보수 과제 폴더 목록의 '\1' 폴더를 참고해 '보고' 아래 새 폴더를 추천"),
    # 예전 이동 계획 제외 사유: "HOLD — 이동 대상 아님"
    (re.compile(r"^[A-Z_]+ — 이동 대상 아님$"), "옮길 폴더가 정해지지 않아 이동하지 않음"),
    # 예외 클래스 이름: "분석 중 오류(KeyError)" → "분석 중 오류"
    # (예전 오류 근거 문장 바로 뒤의 괄호만 지운다. 파일명 안의 괄호는 건드리지 않는다.)
    (re.compile(r"(분석 중 오류|새 폴더 이름도 정하지 못함)\([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout)\)"), r"\1"),
)
_REASON_STAGE = {
    "1차-1 파일명 매칭": "파일명 분석",
    "1차-2 파일명 새 폴더": "파일명 분석·새 폴더",
    "2차-1 본문 매칭": "본문 분석",
    "2차-2 본문 새 폴더": "본문 분석·새 폴더",
}

# 진행 화면 제목: 내부 단계 이름 → 사용자 말 (단계 판별에는 내부 이름을 그대로 쓴다)
STAGE_LABEL = {
    "폴더 스캔": "폴더 훑어보는 중",
    "로컬 분류": "내 PC 규칙으로 분류하는 중",
    "목적지 대조": "이동할 폴더 찾는 중",
    "과제 판정": "이번 과제 자료인지 확인하는 중",
    "후보 노드 찾기": "맞는 폴더 후보 찾는 중",
    "1차-1 파일명 매칭": "Liner가 파일명으로 맞는 폴더 찾는 중",
    "1차-2 파일명 새 폴더": "Liner가 파일명으로 새 폴더 이름 정하는 중",
    "2차-1 본문 매칭": "Liner가 문서 내용으로 맞는 폴더 찾는 중",
    "2차-2 본문 새 폴더": "Liner가 문서 내용으로 새 폴더 이름 정하는 중",
    "가상 매핑": "Liner로 관련 폴더 찾는 중",
    "로컬 정리": "내 PC에서 문서 목록 정리하는 중",
}

# 검토 표 배지: 저장된 배지 값 → 화면 표시 (이미 끝난 결과라 완료형으로 쓴다)
BADGE_LABEL = {
    "로컬 파일명": "내 PC 규칙(파일명)",
    "로컬 폴더명": "내 PC 규칙(폴더명)",
    "1차-1 파일명 매칭": "Liner가 파일명으로 찾은 폴더",
    "1차-2 파일명 새 폴더": "Liner가 파일명으로 제안한 새 폴더",
    "2차-1 본문 매칭": "Liner가 문서 내용으로 찾은 폴더",
    "2차-2 본문 새 폴더": "Liner가 문서 내용으로 제안한 새 폴더",
}


def stage_label(value: str | None) -> str:
    return STAGE_LABEL.get(value or "", value or "")


def badge_label(value: str | None) -> str:
    return BADGE_LABEL.get(value or "", value or "")


_TECH_ERROR = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout)\b|Traceback|Errno")


def error_text(value: str | None) -> str:
    """기술 오류(예외 이름·영문 메시지)는 사용자 말로 바꾸고, 이미 한국어 안내인 오류는 그대로 둔다."""
    text = value or ""
    # LinerError는 "[상태코드 코드] 메시지" 형식이다. 다시 해도 같은 결과인 오류는 해결 방법을 알린다.
    if re.search(r"\[(?:401|403)\b", text):
        return "Liner API 키가 올바르지 않거나 권한이 없습니다. [Liner API 키] 화면에서 키를 확인하세요."
    if re.search(r"\[402\b", text):
        return "Liner 크레딧이 부족합니다. Liner Platform에서 잔액을 확인하세요."
    if "no_api_key" in text:
        return "Liner API 키가 없습니다. [Liner API 키] 화면에서 키를 등록하세요."
    if _TECH_ERROR.search(text):
        return "Liner 분석 중 오류가 발생했습니다. 다시 분석해 주세요."
    return text


def reason_brief(value: str | None) -> str:
    """검토 목록용 짧은 근거: 배지·'이동할 폴더' 칸과 겹치는 앞머리([1차-2 …] 맞는 폴더 없음 → 새 폴더 'X' 추천)를 뗀다."""
    text = folder_reason(value)
    text = re.sub(r"^\[[^\]]*\]\s*", "", text)
    text = re.sub(r"^맞는 폴더 없음 → 새 폴더 '[^']*' 추천\s*(?:[·:]\s*)?", "", text)
    return text.strip() or folder_reason(value)


def msg(msg_id: str, **params: object) -> str:
    """메시지 ID에 해당하는 문구를 반환한다. 없는 ID는 즉시 KeyError를 낸다."""
    template = MESSAGES[msg_id]
    return template.format(**params) if params else template


def js_messages(*prefixes: str) -> dict[str, str]:
    """프론트엔드 JS로 전달할 메시지 ID 부분 사전을 만든다."""
    if not prefixes:
        return dict(MESSAGES)
    return {k: v for k, v in MESSAGES.items() if k.startswith(prefixes)}
