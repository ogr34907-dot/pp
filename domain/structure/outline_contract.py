"""Versioned five-level planning contracts.

The existing ``story_nodes`` table stores the physical Part → Volume → Act →
Chapter hierarchy.  This module models the additional logical root (the
overall outline) and the immutable plan contract that every level contributes
to.  Persistence deliberately lives in the application/infrastructure layer
so the generation gate can be tested without a database.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Optional


class OutlineLevel(str, Enum):
    """The only valid levels in a prose planning chain."""

    OUTLINE = "outline"
    PART = "part"
    VOLUME = "volume"
    ACT = "act"
    CHAPTER = "chapter"

    @classmethod
    def ordered(cls) -> tuple["OutlineLevel", ...]:
        return (cls.OUTLINE, cls.PART, cls.VOLUME, cls.ACT, cls.CHAPTER)

    @property
    def child_level(self) -> Optional["OutlineLevel"]:
        levels = self.ordered()
        index = levels.index(self)
        return levels[index + 1] if index + 1 < len(levels) else None


class OutlineStatus(str, Enum):
    """Lifecycle of the active revision, not a prose/chapter status."""

    DRAFT = "draft"
    PUBLISHED = "published"
    SYNCING = "syncing"
    SYNCED = "synced"
    STALE = "stale"
    CONFLICT = "conflict"
    SUPERSEDED = "superseded"


class OutlineSource(str, Enum):
    AI = "ai"
    AUTHOR = "author"
    IMPORTED = "imported"


@dataclass
class OutlinePayload:
    """Structured plan fields shared by all five levels.

    A dictionary-shaped ``state_changes`` is intentionally used for entities
    whose schema evolves independently (characters, relationships, locations,
    props and world state).  The chapter-only fields are empty at macro levels.
    """

    title: str = ""
    narrative_text: str = ""
    creative_goal: str = ""
    entry_state: str = ""
    exit_state: str = ""
    required_events: list[str] = field(default_factory=list)
    forbidden_events: list[str] = field(default_factory=list)
    state_changes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    foreshadowing: dict[str, list[str]] = field(default_factory=dict)
    chapter_start: Optional[int] = None
    chapter_end: Optional[int] = None
    word_budget: Optional[int] = None
    handoff_conditions: list[str] = field(default_factory=list)
    pov: str = ""
    scenes: list[str] = field(default_factory=list)
    beats: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    ending_hook: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def canonical_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-safe plan representation for revisions."""

        return asdict(self)

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "OutlinePayload":
        data = dict(raw or {})
        known = {field_name for field_name in cls.__dataclass_fields__}
        values = {name: data.pop(name) for name in tuple(data) if name in known}
        if data:
            values["extra"] = {**dict(values.get("extra") or {}), **data}
        return cls(**values)


@dataclass
class OutlineContract:
    """Current working revision for one logical outline node."""

    id: str
    novel_id: str
    level: OutlineLevel
    revision: int
    payload: OutlinePayload
    status: OutlineStatus = OutlineStatus.DRAFT
    parent_id: Optional[str] = None
    story_node_id: Optional[str] = None
    parent_revision_digest: str = ""
    source: OutlineSource = OutlineSource.AI
    author_locked: bool = False
    has_author_edits: bool = False
    published_digest: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.level, str):
            self.level = OutlineLevel(self.level)
        if isinstance(self.status, str):
            self.status = OutlineStatus(self.status)
        if isinstance(self.source, str):
            self.source = OutlineSource(self.source)
        if isinstance(self.payload, dict):
            self.payload = OutlinePayload.from_dict(self.payload)

    @property
    def digest(self) -> str:
        return self.payload.digest

    @property
    def can_generate_child(self) -> bool:
        return self.status == OutlineStatus.SYNCED and self.level.child_level is not None

    @property
    def next_generatable_level(self) -> Optional[OutlineLevel]:
        return self.level.child_level if self.can_generate_child else None

    def publish(self) -> None:
        """Mark the author-approved revision as awaiting projection sync."""

        self.status = OutlineStatus.PUBLISHED
        self.published_digest = self.digest

    def begin_sync(self) -> None:
        if self.status != OutlineStatus.PUBLISHED:
            raise ValueError("only a published outline revision can start sync")
        self.status = OutlineStatus.SYNCING

    def mark_synced(self) -> None:
        if self.status not in (OutlineStatus.PUBLISHED, OutlineStatus.SYNCING):
            raise ValueError("only a published outline revision can be synced")
        self.status = OutlineStatus.SYNCED
        self.published_digest = self.digest


@dataclass
class OutlineChain:
    """A complete current plan lineage used by prose/candidate generation."""

    contracts: Iterable[OutlineContract]

    def __post_init__(self) -> None:
        indexed: dict[OutlineLevel, OutlineContract] = {}
        for contract in self.contracts:
            if contract.level in indexed:
                raise ValueError(f"duplicate outline level: {contract.level.value}")
            indexed[contract.level] = contract
        self._contracts = indexed

    def contract_for(self, level: OutlineLevel) -> OutlineContract:
        return self._contracts[level]

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        for level in OutlineLevel.ordered():
            contract = self._contracts.get(level)
            if contract is None:
                blockers.append(f"{level.value}:missing")
            elif contract.status != OutlineStatus.SYNCED:
                blockers.append(f"{level.value}:{contract.status.value}")
        return tuple(blockers)

    @property
    def ready_for_prose(self) -> bool:
        return not self.blockers

    def to_prompt_context(self) -> dict[str, Any]:
        """Only synced, active revisions may cross the planning/fact boundary."""

        if not self.ready_for_prose:
            raise ValueError("outline chain is not ready for prose generation")
        return {
            level.value: {
                "contract_id": self._contracts[level].id,
                "revision": self._contracts[level].revision,
                "digest": self._contracts[level].published_digest or self._contracts[level].digest,
                "payload": self._contracts[level].payload.canonical_dict(),
            }
            for level in OutlineLevel.ordered()
        }
