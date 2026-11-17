"""只读的回归门禁、稳定性和下载接口。"""

from typing import Literal

from fastapi import Depends, Response

from .analysis import export_report, regression_check, stability_analysis
from .analysis_schemas import RegressionCheckInput, StabilityAnalysisInput


def add_analysis_routes(router, get_session):
    @router.post("/extensions/regression-checks")
    def check_regression(data: RegressionCheckInput, session=Depends(get_session)):
        return regression_check(session, data)

    @router.post("/extensions/stability-analysis")
    def analyze_stability(data: StabilityAnalysisInput, session=Depends(get_session)):
        return stability_analysis(session, data)

    @router.get("/extensions/reports/{run_id}/export")
    def download_report(run_id: str, score_set_id: str | None = None,
                        format: Literal["json", "csv", "html"] = "json", session=Depends(get_session)):
        value = export_report(session, run_id, score_set_id, format)
        return Response(content=value["content"], media_type=value["media_type"],
                        headers={"Content-Disposition": f'attachment; filename="{value["filename"]}"',
                                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
