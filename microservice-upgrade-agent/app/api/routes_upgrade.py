from fastapi import APIRouter, HTTPException

from app.api.deps import DbSession
from app.schemas.upgrade import UpgradeRequest, UpgradeResponse
from app.services import upgrade_service

router = APIRouter(tags=["upgrade"])


@router.post("/upgrade", response_model=UpgradeResponse)
async def upgrade_microservice(request: UpgradeRequest, db: DbSession) -> UpgradeResponse:
    """Runs the microservice-upgrade agent against a local repo path.

    Warning: this executes real shell commands (build tool, git, java) and
    edits real files under repo_path -- run it only against repositories you
    own/trust, ideally under version control so changes are reviewable and
    revertible. It can also run for a long time (multiple build + startup
    attempts); this endpoint blocks for the duration of the run -- see the
    README's note on the platform having no background job queue yet.
    """
    try:
        run = await upgrade_service.run_upgrade(db, request)
    except upgrade_service.UpgradeAgentUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if run.status != "succeeded":
        raise HTTPException(
            status_code=502, detail=f"Upgrade agent failed: {run.error_message}"
        )

    output = run.output_payload
    return UpgradeResponse(
        approved=output["approved"],
        cycles_used=output["cycles_used"],
        build_tool=output["build_tool"],
        summary=output["summary"],
        agent_run_id=run.id,
    )
