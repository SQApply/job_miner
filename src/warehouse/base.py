from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound="MongoDocument")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MongoDocument(BaseModel):
    id: str = Field(alias="_id")
    schema_version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    collection_name: ClassVar[str]
    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}

    def to_mongo(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="python")

    @classmethod
    def from_mongo(cls: type[T], payload: dict[str, Any]) -> T:
        if not payload:
            raise ValueError("Cannot build document from empty Mongo payload.")
        return cls.model_validate(payload)
