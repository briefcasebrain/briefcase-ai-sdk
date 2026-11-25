import briefcase

briefcase.observe("chain:evidence.db")


@briefcase.capture(decision_type="score_claim", async_capture=False)
def score(claim_id: str) -> float:
    return 0.82


for claim in ("c-1", "c-2", "c-3"):
    score(claim)
briefcase.shutdown()
