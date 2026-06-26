from fastapi import Header, HTTPException, status
from typing import Optional
from app.config import settings

async def verify_admin_token(x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")):
    """
    FastAPI dependency that validates the X-Admin-Token header.
    Only enforces authentication if ADMIN_TOKEN is configured.
    """
    if settings.ADMIN_TOKEN:
        if not x_admin_token or x_admin_token != settings.ADMIN_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-Admin-Token header"
            )
    return x_admin_token
