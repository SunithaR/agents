from pydantic import BaseModel, Field


class UpgradeRequest(BaseModel):
    repo_path: str = Field(..., description="Absolute or relative filesystem path to the microservice repo")
    target_java_version: str = Field(default="21")
    target_spring_framework_version: str = Field(default="6")
    target_spring_boot_version: str = Field(default="3.5")
    max_verification_cycles: int | None = Field(
        default=None, ge=1, le=20, description="Override the agent's default cycle cap"
    )
    max_tool_turns_per_cycle: int | None = Field(default=None, ge=1, le=100)
    build_timeout_seconds: int | None = Field(default=None, ge=30, le=3600)
    startup_timeout_seconds: int | None = Field(default=None, ge=10, le=600)


class UpgradeResponse(BaseModel):
    approved: bool
    cycles_used: int
    build_tool: str
    summary: str
    agent_run_id: int
