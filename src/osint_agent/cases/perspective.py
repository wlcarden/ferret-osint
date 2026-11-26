"""Perspectives: investigation types defined as files.

A perspective says what a review is for, on what basis it may be run, which
sources it consults and what belongs in the brief. It is a YAML file, so adding
a new kind of review means copying one and editing it, with no code involved.

Two things a perspective cannot do: lift the requirement to state a basis, and
ask for a verdict. The engine has no way to record either exception.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from osint_agent.cases.models import Basis

# Default location: config/perspectives in the project root
DEFAULT_PERSPECTIVES_DIR = (
    Path(__file__).resolve().parents[3] / "config" / "perspectives"
)


class PerspectiveError(ValueError):
    """A perspective file is missing or does not say what the engine needs."""


class SourceSpec(BaseModel):
    """One source a perspective consults.

    Four kinds: "adapter" is a program that returns records; "manual" is a
    lookup a person does; "posts" is a program that collects the posts of the
    accounts the subject listed on one platform; "capture" is a person
    recording posts from listed accounts that no program may read.
    """

    id: str
    title: str
    kind: Literal["adapter", "manual", "posts", "capture"]
    query: Literal["name", "handle"] = "name"
    platform: str | None = None  # For "posts": which listed accounts it reads
    covers: str = ""  # What the source holds, and what it leaves out
    url: str | None = None  # Where a person performs a manual lookup
    instructions: str = ""  # What a person should search for and record

    @model_validator(mode="after")
    def _posts_name_their_platform(self) -> "SourceSpec":
        self.platform = (self.platform or "").strip().lower() or None
        if self.kind == "posts" and not self.platform:
            raise ValueError(
                f"source '{self.id}' collects posts, so it needs a platform"
            )
        return self


class ReportSpec(BaseModel):
    """What belongs in the brief, in the reviewer's own words."""

    include: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)


class StatementsSpec(BaseModel):
    """How collected posts are read: what to report, what to leave, how far back.

    The control post is a made-up post that must always be reported under these
    rules. It is slipped into every batch, and a batch whose answer leaves it
    out is not trusted. It is required: without it, a reader that reported
    nothing at all would pass for one that had read everything.
    """

    report: list[str] = Field(min_length=1)
    leave_out: list[str] = Field(default_factory=list)
    lookback_years: int | None = Field(default=None, ge=1, le=50)
    control_post: str = Field(min_length=10)
    readings: int = Field(default=2, ge=1, le=3)  # Times each post is read
    batch_size: int = Field(default=40, ge=5, le=100)


class Perspective(BaseModel):
    """An investigation type."""

    name: str
    title: str
    purpose: str
    subject_kind: Literal["person", "organisation"]
    allowed_bases: list[Basis] = Field(min_length=1)
    sources: list[SourceSpec] = Field(min_length=1)
    report: ReportSpec
    statements: StatementsSpec | None = None
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _source_ids_are_unique(self) -> "Perspective":
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f"source id '{source.id}' appears more than once")
            seen.add(source.id)
        return self

    @model_validator(mode="after")
    def _posts_have_reading_rules(self) -> "Perspective":
        collects = [s.id for s in self.sources if s.kind in ("posts", "capture")]
        if collects and self.statements is None:
            raise ValueError(
                f"source '{collects[0]}' collects posts, so the file needs a "
                "'statements' section saying what to report"
            )
        return self

    def source(self, source_id: str) -> SourceSpec | None:
        return next((spec for spec in self.sources if spec.id == source_id), None)


def available_perspectives(directory: Path | None = None) -> list[str]:
    """Names of the perspective files in a directory."""
    folder = directory or DEFAULT_PERSPECTIVES_DIR
    if not folder.is_dir():
        return []
    return sorted(path.stem for path in folder.glob("*.yaml"))


def load_perspective(name: str, directory: Path | None = None) -> Perspective:
    """Load and check one perspective by name."""
    folder = directory or DEFAULT_PERSPECTIVES_DIR
    known = available_perspectives(folder)
    # Only a name from the folder's own list: a name is never used as a path
    if not isinstance(name, str) or name not in known:
        raise PerspectiveError(
            f"no perspective named '{str(name)[:60]}' "
            f"(available: {', '.join(known) or 'none'})"
        )
    path = folder / f"{name}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise PerspectiveError(f"{path.name} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise PerspectiveError(f"{path.name} must hold a mapping of settings")
    try:
        perspective = Perspective(**raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or 'file'}: {issue['msg']}"
            for issue in error.errors()
        )
        raise PerspectiveError(
            f"{path.name} is incomplete or wrong: {problems}"
        ) from error
    if perspective.name != name:
        raise PerspectiveError(
            f"{path.name} declares name '{perspective.name}'; it must match the file name"
        )
    return perspective
