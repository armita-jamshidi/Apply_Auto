"""Validated shape for private candidate profile YAML."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PersonalProfile(BaseModel):
    """Personal details used for application fields and grounded answers."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    website: str | None = None
    linkedin: str | None = None
    github: str | None = None


class ProjectProfile(BaseModel):
    """A candidate project and its source-grounded details."""

    model_config = ConfigDict(extra="allow")

    name: str
    date: str | None = None
    url: str | None = None
    summary: str | None = None
    skills: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    """Top-level profile schema, preserving additional profile sections."""

    model_config = ConfigDict(extra="allow")

    personal: PersonalProfile
    projects: list[ProjectProfile] = Field(default_factory=list)

    def as_profile_dict(self) -> dict[str, Any]:
        """Return the validated profile as a plain nested mapping."""
        return self.model_dump(mode="python")
