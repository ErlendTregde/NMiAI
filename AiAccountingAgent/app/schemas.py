"""Pydantic models for the /solve request and response."""
from pydantic import BaseModel, Field


class SolveFile(BaseModel):
    filename: str
    content_base64: str
    mime_type: str


class TripletexCredentials(BaseModel):
    base_url: str = Field(..., description="Proxy API base URL, e.g. https://<host>/v2")
    session_token: str = Field(..., description="Session token used as Basic Auth password")


class SolveRequest(BaseModel):
    prompt: str
    files: list[SolveFile] = []
    tripletex_credentials: TripletexCredentials


class SolveResponse(BaseModel):
    status: str = "completed"
