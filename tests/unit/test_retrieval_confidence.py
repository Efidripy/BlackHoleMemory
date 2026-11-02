from blackholememory.retrieval_confidence import AnswerabilityThresholds, assess_answerability


def test_rerank_grade_rescues_low_overlap_answer():
    hits = [{"title": "x", "content": "y", "score": 0.5, "metadata": {"llm_rerank_score": 8.0}}]
    plain = assess_answerability("совсем другие слова", hits, thresholds=AnswerabilityThresholds())
    rescued = assess_answerability("совсем другие слова", hits, thresholds=AnswerabilityThresholds(rerank_rescue_score=6.0))
    assert plain["status"] == "none"
    assert rescued["status"] == "weak" and rescued["reason"] == "rerank_graded_match"
