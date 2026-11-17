"""人工复核与回归候选接口；读取、审批均不请求模型。"""

from fastapi import Depends

from .review_schemas import HumanReviewCreate, RegressionDraftApproval, RegressionDraftCreate
from .reviews import approve_draft, create_draft, create_review, draft_list, review_case, review_case_list, review_consistency, review_history


def add_review_routes(router, get_session):
    @router.get("/extensions/reviews/cases")
    def human_review_cases(run_id: str, score_set_id: str | None = None, session=Depends(get_session)):
        return review_case_list(session, run_id, score_set_id)

    @router.get("/extensions/reviews/cases/{case_run_id}")
    def human_review_case(case_run_id: str, score_set_id: str | None = None, blind: bool = True, session=Depends(get_session)):
        return review_case(session, case_run_id, score_set_id, blind=blind)

    @router.post("/extensions/reviews", status_code=201)
    def human_review_create(data: HumanReviewCreate, session=Depends(get_session)):
        return create_review(session, data)

    @router.get("/extensions/reviews/consistency")
    def human_review_consistency(run_id: str, score_set_id: str | None = None, session=Depends(get_session)):
        return review_consistency(session, run_id, score_set_id)

    @router.get("/extensions/reviews")
    def human_review_history(run_id: str, score_set_id: str | None = None, session=Depends(get_session)):
        return review_history(session, run_id, score_set_id)

    @router.post("/extensions/regression-drafts", status_code=201)
    def regression_draft_create(data: RegressionDraftCreate, session=Depends(get_session)):
        return create_draft(session, data)

    @router.get("/extensions/regression-drafts")
    def regression_draft_list(target_dataset_id: int | None = None, session=Depends(get_session)):
        return draft_list(session, target_dataset_id)

    @router.post("/extensions/regression-drafts/{draft_id}/approve")
    def regression_draft_approve(draft_id: str, data: RegressionDraftApproval, session=Depends(get_session)):
        return approve_draft(session, draft_id, data)
