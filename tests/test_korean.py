from worklog.korean import build_title, check_paragraph, has_final_consonant, object_particle


def test_final_consonant():
    assert has_final_consonant("성") is True  # 작성 -> ㅇ
    assert has_final_consonant("토") is False  # 검토
    assert has_final_consonant("A") is None


def test_object_particle():
    assert object_particle("A사 제안서 작성") == "을"
    assert object_particle("예산안 검토") == "를"
    assert object_particle("제안서(1차)") == "를"  # particle follows the word before the parentheses
    assert object_particle("'3분기 실적 분석'") == "을"
    assert object_particle("ERP API") is None


def test_build_title():
    assert build_title("A사 제안서 작성", "진행중입니다") == ("A사 제안서 작성을 진행중입니다.", [])
    assert build_title("교육 자료 준비", "완료하였습니다.") == ("교육 자료 준비를 완료하였습니다.", [])
    assert build_title("API 연동", "착수하였습니다")[0] == "API 연동을 착수하였습니다."
    title, errs = build_title("ERP API", "진행중입니다")
    assert title is None and "particle" in errs[0]
    assert build_title("ERP API", "진행중입니다", "를")[0] == "ERP API를 진행중입니다."


def test_build_title_rejects_repetition_and_particle():
    title, errs = build_title("예산안 검토", "검토중입니다")
    assert title is None and "repeat" in errs[0]
    title, errs = build_title("A사 제안서 작성을", "진행중입니다")
    assert title is None and "particle" in errs[0]


GOOD = ("10월 28일 착수한 A사 제안서 초안 작성을 이어서 진행하였습니다. 요구사항을 반영하여 "
        "인건비 단가를 1,200만 원에서 1,350만 원으로 조정하였습니다. 다음 주 중 내부 검토를 진행할 예정입니다.")


def test_good_paragraph():
    errors, warnings = check_paragraph(GOOD)
    assert errors == [] and warnings == []


def test_paragraph_style_errors():
    errors, _ = check_paragraph("제안서 작성 진행함. 단가 조정함.")
    assert len(errors) == 2 and "개조식" in errors[0]
    errors, _ = check_paragraph("첫 문장입니다.\n두 번째 문장입니다.")
    assert any("line breaks" in e for e in errors)
    errors, _ = check_paragraph("- 제안서를 작성하였습니다.")
    assert any("bullet" in e for e in errors)
    errors, _ = check_paragraph("**제안서**를 작성하였습니다.")
    assert any("markdown" in e for e in errors)
    errors, _ = check_paragraph("제안서를 작성했어요.")
    assert errors


def test_numbers_are_not_bullets():
    errors, _ = check_paragraph("3.5% 개선하였습니다. 2026년 계획을 수립하였습니다.")
    assert errors == []
