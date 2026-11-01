"""Conservative, explainable ingestion gates; no claim to verify document facts."""
import re
from dataclasses import replace

PIPELINE_VERSION = "quality-context-v1"


class NeedsReview(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("文档解析质量不足，请查看质量报告并补充清晰文件")


def text_issues(text):
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return []
    replacements = text.count("\ufffd") / len(visible)
    controls = sum(ord(c) < 32 and c not in "\n\r\t" for c in text) / len(visible)
    issues = []
    if replacements >= .05 or controls >= .02:
        issues.append({"code": "corrupt_text", "severity": "blocking",
                       "replacement_ratio": round(replacements, 4), "control_ratio": round(controls, 4)})
    elif replacements > 0 or controls > 0:
        issues.append({"code": "suspicious_characters", "severity": "warning"})
    return issues


def normalize(text):
    # Preserve indentation, units, qualifiers and paragraph boundaries; do not guess line joins.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def prepare_pages(pages):
    report = {"pipeline_version": PIPELINE_VERSION, "status": "passed", "pages": [],
              "limitations": ["未验证原文事实、复杂版面阅读顺序和 OCR 置信度"]}
    cleaned = []
    for page in pages:
        # Inspect before removing control characters. Whitespace-only pages can be legitimate.
        issues = text_issues(page.text)
        if not page.text.strip():
            issues.append({"code": "empty_page", "severity": "warning"})
        report["pages"].append({"page": page.page, "characters": len(page.text), "issues": issues})
        blocks = tuple(replace(b, text=normalize(b.text), header=tuple(normalize(h) for h in b.header))
                       for b in page.blocks)
        cleaned.append(replace(page, text=normalize(page.text), blocks=blocks))
    if not any(p.text.strip() for p in cleaned):
        report["status"] = "needs_review"
        report["issues"] = [{"code": "no_searchable_text", "severity": "blocking"}]
    if any(i["severity"] == "blocking" for p in report["pages"] for i in p["issues"]):
        report["status"] = "needs_review"
    if report["status"] == "needs_review":
        raise NeedsReview(report)
    return cleaned, report


def validate_chunks(chunks, size):
    if not chunks or any(not c["text"].strip() or len(c["text"]) > size for c in chunks):
        raise ValueError("切块检查失败：存在空块或超长片段")
    if [c["ordinal"] for c in chunks] != list(range(len(chunks))):
        raise ValueError("切块检查失败：片段顺序不连续")
