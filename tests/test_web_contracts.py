from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_resume_upload_picker_only_advertises_server_supported_formats():
    text = (ROOT / "web/setup.html").read_text(encoding="utf-8")

    assert 'accept=".txt,.md,.pdf"' in text
    assert ".docx" not in text
    assert ".png" not in text


def test_auth_page_describes_the_same_minimum_password_length_as_the_server():
    text = (ROOT / "web/auth.html").read_text(encoding="utf-8")

    assert "至少 8 位" in text


def test_setup_page_says_uploaded_resume_content_is_not_persisted():
    text = (ROOT / "web/setup.html").read_text(encoding="utf-8")

    assert "不会保存完整简历原文" in text


def test_report_page_uses_a_limited_scope_label_for_early_exit_reports():
    text = (ROOT / "web/report.html").read_text(encoding="utf-8")

    assert "局部复盘报告" in text


def test_setup_role_options_do_not_interpolate_operator_supplied_role_names_as_html():
    text = (ROOT / "web/setup.html").read_text(encoding="utf-8")

    assert 'document.createElement("option")' in text
    assert "names.map(n => '<option value='" not in text


def test_interview_page_releases_camera_tracks_when_the_session_ends_or_page_closes():
    text = (ROOT / "web/interview.html").read_text(encoding="utf-8")

    assert "function cleanupCamera()" in text
    assert "cleanupCamera();" in text
    assert 'window.addEventListener("pagehide"' in text


def test_interview_page_locks_inflight_answers_and_keeps_text_for_a_retry():
    text = (ROOT / "web/interview.html").read_text(encoding="utf-8")

    assert "submitting: false" in text
    assert "if (!text || state.finished || state.submitting) return false;" in text
    assert "const accepted = await submitAnswer(t);" in text
    assert 'if (accepted) $("textAnswer").value = "";' in text


def test_setup_defaults_to_a_source_backed_role_while_other_roles_remain_available():
    text = (ROOT / "web/setup.html").read_text(encoding="utf-8")

    assert 'value="AI 产品经理"' in text
    assert '"后端开发岗", "前端开发岗", "算法工程师"' in text


def test_submission_page_explains_consent_and_sends_the_required_confirmation():
    text = (ROOT / "web/submit.html").read_text(encoding="utf-8")

    assert "contentRightsConfirmed" in text
    assert "匿名化题型" in text
    assert "content_rights_confirmed" in text


def test_admin_page_edits_abstract_drafts_and_never_renders_raw_questions():
    text = (ROOT / "web/admin.html").read_text(encoding="utf-8")

    assert "redacted_questions" in text
    assert "drafts" in text
    assert "draftInputs" in text
    assert "item.questions" not in text
