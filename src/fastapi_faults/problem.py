from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._types import CODE_PATTERN, JsonValue, is_absolute_uri, is_uri_reference


class Problem(BaseModel):
    """The stable Problem Details profile emitted by fastapi-faults."""

    model_config = ConfigDict(extra="allow", frozen=True)

    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    type: str
    title: str
    status: int
    code: str
    detail: str | None = None
    instance: str | None = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if not is_absolute_uri(value):
            msg = "type must be an absolute URI without a fragment"
            raise ValueError(msg)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value.strip() or "\r" in value or "\n" in value:
            msg = "title must be a non-empty, single-line string"
            raise ValueError(msg)
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: int) -> int:
        if isinstance(value, bool) or not 100 <= value <= 599:
            msg = "status must be an HTTP status code between 100 and 599"
            raise ValueError(msg)
        return value

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        if CODE_PATTERN.fullmatch(value) is None:
            msg = "code must match ^[a-z][a-z0-9_]{2,}$"
            raise ValueError(msg)
        return value

    @field_validator("instance")
    @classmethod
    def _validate_instance(cls, value: str | None) -> str | None:
        if value is not None and not is_uri_reference(value):
            msg = "instance must be a non-empty URI reference"
            raise ValueError(msg)
        return value

    def as_dict(self) -> dict[str, JsonValue]:
        """Serialize to a JSON-native object, omitting absent optional members."""
        return self.model_dump(mode="json", exclude_none=True)
