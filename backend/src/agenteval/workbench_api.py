"""实验比较和七页工作台接口。所有 GET 只查询持久记录。"""

from typing import Literal

from fastapi import Depends, Query
from pydantic import Field, field_validator

from .comparisons import compare_runs
from .privacy import known_secrets, sanitize
from .schemas import Input, METRICS, Text, canonical_id
from .workbench import AUTOMATIC_TAGS, dashboard, detail, evaluations, report, save_annotation


class AnnotationInput(Input):
    score_set_id: str
    manual_tags: list[Text] = Field(default_factory=list, max_length=30)
    note: str = Field(default="", max_length=20000)

    @field_validator("score_set_id")
    @classmethod
    def valid_id(cls, value):
        return canonical_id(value)

    @field_validator("manual_tags")
    @classmethod
    def unique(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("人工原因不能重复")
        return value


def add_workbench_routes(router, get_session):
    @router.get("/evaluations")
    def evaluation_list(run_id: str | None = None, score_set_id: str | None = None,
                        conclusion: Literal["PASS", "FAIL", "UNDETERMINED"] | None = None,
                        metric: str | None = Query(None, max_length=100), category: str | None = Query(None, max_length=1000),
                        case_run_ids: str | None = Query(None, max_length=10000), page: int = Query(1, ge=1),
                        page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        return evaluations(session, run_id=run_id, score_set_id=score_set_id, conclusion=conclusion, metric=metric,
                           category=category, case_run_ids=case_run_ids, page=page, page_size=page_size)

    @router.get("/case-runs/{case_run_id}/detail")
    def case_detail(case_run_id: str, score_set_id: str | None = None, session=Depends(get_session)):
        return detail(session, case_run_id, score_set_id)

    @router.get("/badcases")
    def badcase_list(run_id: str | None = None, score_set_id: str | None = None,
                     category: str | None = Query(None, max_length=1000), manual_tag: str | None = Query(None, max_length=200),
                     issue: str | None = Query(None, max_length=100), page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100), session=Depends(get_session)):
        return evaluations(session, run_id=run_id, score_set_id=score_set_id, category=category, manual_tag=manual_tag,
                           issue=issue, page=page, page_size=page_size, badcases_only=True)

    @router.put("/case-runs/{case_run_id}/annotation")
    def annotation(case_run_id: str, data: AnnotationInput, session=Depends(get_session)):
        return save_annotation(session, case_run_id, data)

    @router.get("/comparisons")
    def comparisons(left_run_id: str, right_run_id: str, left_score_set_id: str | None = None,
                     right_score_set_id: str | None = None, session=Depends(get_session)):
        result = compare_runs(session, left_run_id, right_run_id, left_score_set_id, right_score_set_id)
        return sanitize(result, known_secrets())

    @router.get("/reports/{run_id}")
    def run_report(run_id: str, score_set_id: str | None = None, compare_run_id: str | None = None,
                   compare_score_set_id: str | None = None, session=Depends(get_session)):
        return report(session, run_id, score_set_id, compare_run_id, compare_score_set_id)

    @router.get("/dashboard")
    def dashboard_summary(source_type: Literal["REAL_EXTERNAL", "PROTOCOL_DOUBLE", "MOCK", "ALL"] = "REAL_EXTERNAL", session=Depends(get_session)):
        return dashboard(session, source_type)
