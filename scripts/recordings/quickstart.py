import briefcase

briefcase.observe("console")          # send records to stderr (or "memory" / "runs.jsonl" / an https:// URL)


@briefcase.capture(decision_type="classify_text")
def classify(text: str) -> str:
    return text.upper()


classify("hello world")               # a decision record is emitted
