"""Validate and load versioned RAG release packages into PostgreSQL."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

from embeddings import embed_texts
from rag_schema import content_hash


ALLOWED_LICENSES = {"MIT", "Apache-2.0", "user-authorized"}


def verify_release(documents_path: Path, manifest_path: Path) -> list[dict]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    expected = manifest.get("files", {}).get("production_documents.jsonl", {})
    data = Path(documents_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected.get("sha256"):
        raise ValueError("生产题库文件的 SHA-256 与导入清单不一致")
    documents = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    if len(documents) != expected.get("rows") or len(documents) != manifest.get("production_document_count"):
        raise ValueError("生产题库行数与导入清单不一致")
    return documents


def _stage(question: str) -> str:
    if re.search(r"自我介绍|介绍.{0,4}自己|个人经历", question):
        return "自我介绍"
    if re.search(r"项目|实习|经历|案例|做过|负责过|最有挑战", question):
        return "项目深挖"
    if re.search(r"如果|假设|怎么|如何|设计一个|遇到.*办|方案|故障|异常", question):
        return "场景问题"
    return "能力匹配"


def _validated_documents(documents: list[dict]) -> list[dict]:
    valid = []
    for index, document in enumerate(documents, start=1):
        if document.get("review_state") != "reviewed":
            continue
        text = str(document.get("text") or "").strip()
        source_url = str(document.get("source_url") or "").strip()
        roles = [str(role).strip()[:80] for role in document.get("role_tags") or [] if str(role).strip()]
        metadata = document.get("metadata") or {}
        license_name = str(metadata.get("license") or "")
        source_type = str(document.get("source_type") or "")
        if len(text) < 8 or not roles or not source_url.startswith("https://"):
            raise ValueError(f"第 {index} 条缺少有效题干、岗位标签或 HTTPS 来源")
        if license_name not in ALLOWED_LICENSES:
            raise ValueError(f"第 {index} 条许可证未确认，拒绝导入")
        if source_type == "xiaohongshu_authorized_question":
            if metadata.get("authorization_status") != "user_declared_authorized" or not metadata.get("authorization_evidence_ref"):
                raise ValueError(f"第 {index} 条小红书内容缺少授权记录，拒绝导入")
        valid.append({**document, "text": text, "role_tags": list(dict.fromkeys(roles)), "metadata": metadata})
    if not valid:
        raise ValueError("导入包中没有可用的已审核题目")
    return valid


def prepare_import_rows(documents: list[dict], embed=embed_texts) -> list[dict]:
    valid = _validated_documents(documents)
    texts = [
        " ".join([*document["role_tags"], str(document["metadata"].get("topic") or ""), document["text"]])
        for document in valid
    ]
    vectors = embed(texts)
    if not vectors or len(vectors) != len(valid):
        raise RuntimeError("Embedding 服务未返回完整向量，题库未写入")
    expected_dimension = int(os.environ.get("EMBEDDING_DIMENSIONS", "1536"))
    for vector in vectors:
        if len(vector) != expected_dimension:
            raise ValueError(f"Embedding 向量维度错误，预期 {expected_dimension} 维")
        if any(not math.isfinite(float(value)) for value in vector):
            raise ValueError("Embedding 向量包含无效数值")

    rows = []
    for document, vector in zip(valid, vectors):
        metadata = document["metadata"]
        source_type = document["source_type"]
        if source_type == "xiaohongshu_authorized_question":
            source_name = "小红书 · 用户授权"
        else:
            source_name = f"GitHub · {metadata['license']}"
        rows.append({
            "question": document["text"],
            "role_tags": document["role_tags"],
            "stage": _stage(document["text"]),
            "topic": str(metadata.get("topic") or "通用能力")[:120],
            "source_name": source_name,
            "source_url": document["source_url"],
            "published_at": document.get("published_at"),
            "answer_signals": [],
            "followup_templates": [],
            "content_hash": content_hash(document["text"]),
            "embedding": vector,
            "source_metadata": {
                **metadata,
                "document_id": document.get("document_id"),
                "source_type": source_type,
                "source_url": document["source_url"],
                "review_state": document["review_state"],
                "retrieval_weight": float(document.get("retrieval_weight", 1.0)),
            },
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify, embed and import a versioned RAG package")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--documents", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        documents = verify_release(args.documents, args.manifest)
        import db

        db.init_schema()
        if not db.is_ready():
            raise RuntimeError("PostgreSQL 尚未就绪，题库未写入")
        valid = _validated_documents(documents)
        hashes = {content_hash(document["text"]) for document in valid}
        existing = db.question_hashes(sorted(hashes))
        unseen, unseen_hashes = [], set()
        for document in valid:
            digest = content_hash(document["text"])
            if digest not in existing and digest not in unseen_hashes:
                unseen.append(document)
                unseen_hashes.add(digest)
        rows = prepare_import_rows(unseen) if unseen else []
        if rows:
            inserted = db.insert_manual_questions(rows, "rag-release-v21")
            if inserted is None:
                raise RuntimeError("数据库未确认导入，题库未写入")
        else:
            inserted = 0
        print(json.dumps({
            "release_documents": len(documents),
            "already_present": len(valid) - len(unseen),
            "embedded_and_inserted": inserted,
            "skipped_unreviewed": len(documents) - len(valid),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Never print connection strings, provider responses, or source text.
        print(f"RAG 导入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
