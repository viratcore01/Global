"""Pydantic schemas and shared enums for validated opportunities."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OPPORTUNITY_TYPES = {"casting", "festival", "crew", "voiceover", "modelling", "other"}
COMP_TYPES = {"day_rate", "flat", "deferred", "credit_only", "unpaid", "unknown"}
USAGE_RIGHTS = {"perpetual_buyout", "limited", "unknown"}
NUDITY = {"none", "implied", "topless", "full", "unknown"}
AUDITION_FORMATS = {"self_tape", "in_person", "zoom", "phone", "unknown"}
CONTACT_CHANNELS = {"form", "email", "dm", "whatsapp", "phone", "telegram", "instagram", "unknown"}
GENDERS = {"any", "male", "female", "nonbinary", "unknown"}
DEADLINE_CONFIDENCE = {"high", "medium", "low", "unknown"}

STATUSES = {"new", "reviewed", "applied", "replied", "booked", "ignored", "scam", "expired", "archived"}
ACTIONS = {"apply", "ignore", "scam", "replied", "booked", "archive", "undo"}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2000)
    start: int | None = None
    end: int | None = None
    method: str = "rule"


class OpportunityRecord(BaseModel):
    """A validated opportunity. Unknown is always a valid value."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    canonical_url: str = Field(min_length=4, max_length=4000)
    canonical_hash: str | None = None
    cluster_id: int | None = None
    source_id: int | None = None
    source_type: str = "web"
    opportunity_type: Literal["casting", "festival", "crew", "voiceover", "modelling", "other"] = "other"
    title: str = Field(min_length=2, max_length=400)
    description: str | None = Field(default=None, max_length=20000)
    published_at: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None

    deadline: str | None = None
    deadline_confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    remote_ok: bool | None = None

    languages: list[str] = Field(default_factory=list)
    role_types: list[str] = Field(default_factory=list)
    comp_type: Literal["day_rate", "flat", "deferred", "credit_only", "unpaid", "unknown"] = "unknown"
    pay_amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    fee_required: bool | None = None
    usage_rights: Literal["perpetual_buyout", "limited", "unknown"] = "unknown"
    nudity: Literal["none", "implied", "topless", "full", "unknown"] = "unknown"
    travel_covered: bool | None = None
    audition_format: Literal["self_tape", "in_person", "zoom", "phone", "unknown"] = "unknown"
    shoot_window: list[str] = Field(default_factory=list)

    age_min: int | None = Field(default=None, ge=0, le=120)
    age_max: int | None = Field(default=None, ge=0, le=120)
    gender_presentation: Literal["any", "male", "female", "nonbinary", "unknown"] = "any"
    contact_channel: Literal["form", "email", "dm", "whatsapp", "phone", "telegram", "instagram", "unknown"] = "unknown"
    contact_value: str | None = Field(default=None, max_length=500)

    evidence: dict[str, Evidence] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list)
    trust_tier: int = Field(default=3, ge=1, le=3)
    raw_document_id: int | None = None
    raw_text_hash: str | None = None
    minhash_signature: str | None = None
    structured_key: str | None = None
    score: float = Field(default=0.0, ge=0, le=100)
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    status: Literal["new", "reviewed", "applied", "replied", "booked", "ignored", "scam", "expired", "archived"] = "new"
    extraction_method: str | None = None
    llm_model: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("languages", "role_types", "shoot_window", mode="before")
    @classmethod
    def _listify(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _flags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, value: Any) -> dict[str, Any]:
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("evidence must be a mapping of field -> quote")
        cleaned: dict[str, Any] = {}
        for field, item in value.items():
            if isinstance(item, str) and item.strip():
                cleaned[field] = {"text": item.strip(), "method": "rule"}
            elif hasattr(item, "model_dump"):
                dumped = item.model_dump()
                if str(dumped.get("text", "")).strip():
                    cleaned[field] = dumped
            elif isinstance(item, dict) and str(item.get("text", "")).strip():
                cleaned[field] = item
        return cleaned

    @field_validator("gender_presentation", mode="before")
    @classmethod
    def _gender(cls, value: Any) -> str:
        value = str(value or "any").lower().strip()
        aliases = {
            "male": "male",
            "man": "male",
            "boy": "male",
            "female": "female",
            "woman": "female",
            "girl": "female",
            "trans": "nonbinary",
            "non-binary": "nonbinary",
            "nonbinary": "nonbinary",
            "any": "any",
            "all": "any",
            "open": "any",
        }
        return aliases.get(value, "any" if value in {"", "none", "none specified"} else "unknown")

    @model_validator(mode="after")
    def _age_order(self) -> "OpportunityRecord":
        if self.age_min is not None and self.age_max is not None and self.age_min > self.age_max:
            self.age_min, self.age_max = self.age_max, self.age_min
        return self

    def to_repo_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="python")
        data["evidence"] = {
            key: value if isinstance(value, dict) else value.model_dump()
            for key, value in self.evidence.items()
        }
        return data

    def key_fields_present(self) -> int:
        keys = (
            self.title,
            self.deadline,
            self.city,
            self.comp_type != "unknown",
            self.contact_value,
            bool(self.languages),
            bool(self.role_types),
            self.age_min is not None or self.age_max is not None,
            self.audition_format != "unknown",
            self.shoot_window,
        )
        return sum(1 for value in keys if value)


class ActionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0)
    action: Literal["apply", "ignore", "scam", "replied", "booked", "archive", "undo"]
    sig: str = ""
    note: str | None = None
    ts: int | None = None
