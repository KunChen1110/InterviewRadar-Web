import csv

from backend.question_staging import historical_questions_to_import_rows, write_import_csv


def test_historical_questions_become_reviewable_import_rows(tmp_path):
    rows = historical_questions_to_import_rows([{
        "text": "如果候选人临时爽约，你会如何调整招聘节奏？",
        "source_refs": ["https://example.com/hr-1"],
        "latest_posted_at": "2025-07-30",
        "role_tags": ["HR", "招聘"],
        "topic": "招聘流程",
    }])

    assert rows == [{
        "question": "如果候选人临时爽约，你会如何调整招聘节奏？",
        "role_tags": "人力资源|HR|招聘",
        "stage": "场景问题",
        "topic": "招聘流程",
        "source_name": "历史采集候选（待人工审核）",
        "source_url": "https://example.com/hr-1",
        "published_at": "2025-07-30",
    }]

    output = tmp_path / "hr_candidates.csv"
    write_import_csv(rows, output)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == rows


def test_historical_question_strips_post_title_but_preserves_question():
    rows = historical_questions_to_import_rows([{
        "text": "小天才人力资源面经（2025-10-20）可见题目：人才画像怎么做？",
        "source_refs": ["https://example.com/hr-1"],
        "role_tags": ["HR"],
        "topic": "人才画像",
    }])

    assert rows[0]["question"] == "人才画像怎么做？"
