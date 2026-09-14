from dataclasses import dataclass, field, asdict
from typing import Any
import json

@dataclass
class NormalizedEvent:
    schema_version: str
    provider: str
    session_id: str
    event_id: str
    timestamp: str | None
    event_type: str
    source_file: str
    source_line: int
    raw_type: str
    data: dict[str, Any] = field(default_factory=dict)
    def to_dict(self): return asdict(self)

@dataclass
class ParseWarning:
    source_line: int
    kind: str
    message: str
    raw: str | None = None
    def to_dict(self): return asdict(self)

@dataclass
class ParsedSession:
    provider: str
    session_id: str
    cwd: str | None
    timestamp: str | None
    cli_version: str | None
    events: list[NormalizedEvent]
    warnings: list[ParseWarning]
    raw_type_counts: dict[str, int]
    user_messages: list[str]
    def to_json(self): return json.dumps(asdict(self), ensure_ascii=False, indent=2)
