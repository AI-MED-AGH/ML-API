from typing import Optional, Any

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Error response model."""

    success: bool = Field(default=False)
    error: str = Field(..., description="Error message")
    error_type: str = Field(..., description="Type of error")
    details: Optional[dict[str, Any]] = Field(
        default=None,
        description="Additional error details"
    )
