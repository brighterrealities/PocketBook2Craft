"""EPUB CFI (Canonical Fragment Identifier) parsing.

Format example: ``epubcfi(/6/14!/4/2[chapter1]/1:42)``

Components:
  - ``/6/14``                 spine position (which document in the EPUB)
  - ``!/4/2[chapter1]/1``     DOM path within that document
  - ``:42``                   character offset within the text node

Ported from Swift CFIParser.swift — behavior preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import total_ordering


@dataclass(frozen=True)
class CFIComponent:
    """A single component in a CFI path (e.g. ``/6`` or ``/4[chapter1]``)."""

    index: int
    id: str | None = None

    def __str__(self) -> str:
        return f"/{self.index}[{self.id}]" if self.id else f"/{self.index}"


@total_ordering
@dataclass(frozen=True)
class CFIPosition:
    spine_components: tuple[CFIComponent, ...]
    document_path: tuple[CFIComponent, ...] = field(default_factory=tuple)
    character_offset: int | None = None
    raw_cfi: str = ""

    def __str__(self) -> str:
        spine = "".join(str(c) for c in self.spine_components)
        path = "".join(str(c) for c in self.document_path)
        offset = f":{self.character_offset}" if self.character_offset is not None else ""
        return f"CFI(spine: {spine}, path: {path}{offset})"

    # -- ordering ------------------------------------------------------------ #

    def _sort_key(self) -> tuple:
        return (
            tuple(c.index for c in self.spine_components),
            len(self.spine_components),
            tuple(c.index for c in self.document_path),
            len(self.document_path),
            self.character_offset or 0,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, CFIPosition):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    def __eq__(self, other: object) -> bool:  # noqa: D401
        if not isinstance(other, CFIPosition):
            return NotImplemented
        return (
            self.spine_components == other.spine_components
            and self.document_path == other.document_path
            and self.character_offset == other.character_offset
        )

    def __hash__(self) -> int:  # frozen dataclass + custom __eq__ needs this
        return hash(
            (
                self.spine_components,
                self.document_path,
                self.character_offset,
            )
        )


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #


def parse(cfi: str) -> CFIPosition | None:
    """Parse a CFI string into a :class:`CFIPosition`, or ``None`` if invalid."""
    content = cfi

    # Strip epubcfi(...) wrapper if present
    if "epubcfi(" in cfi:
        start = cfi.find("epubcfi(") + len("epubcfi(")
        end = cfi.rfind(")")
        if end > start:
            content = cfi[start:end]

    # Split spine vs document path on the single !
    if "!" in content:
        spine_str, _, doc_str = content.partition("!")
    else:
        spine_str, doc_str = content, ""

    spine_components = _parse_components(spine_str)
    if not spine_components:
        return None

    document_path: tuple[CFIComponent, ...] = ()
    character_offset: int | None = None

    if doc_str:
        if ":" in doc_str:
            doc_path_part, _, offset_part = doc_str.rpartition(":")
            digits = "".join(ch for ch in offset_part if ch.isdigit())
            character_offset = int(digits) if digits else None
            doc_str = doc_path_part
        document_path = _parse_components(doc_str)

    return CFIPosition(
        spine_components=spine_components,
        document_path=document_path,
        character_offset=character_offset,
        raw_cfi=cfi,
    )


def _parse_components(path: str) -> tuple[CFIComponent, ...]:
    """Walk a string of ``/N`` and ``/N[id]`` segments."""
    components: list[CFIComponent] = []
    i = 0
    n = len(path)

    while i < n:
        # Advance to next '/'
        while i < n and path[i] != "/":
            i += 1
        if i >= n:
            break
        i += 1  # skip the '/'

        # Read digits
        start = i
        while i < n and path[i].isdigit():
            i += 1
        if start == i:
            continue
        index = int(path[start:i])

        # Optional [id]
        component_id: str | None = None
        if i < n and path[i] == "[":
            close = path.find("]", i)
            if close != -1:
                component_id = path[i + 1:close]
                i = close + 1

        components.append(CFIComponent(index=index, id=component_id))

    return tuple(components)


# --------------------------------------------------------------------------- #
# Distance / adjacency                                                         #
# --------------------------------------------------------------------------- #


def distance(start: CFIPosition, end: CFIPosition) -> float:
    """Numeric distance between two CFI positions.

    Reproduces the Swift weighting:
      - spine diff dominates (×1,000,000)
      - document path diff next (×1,000)
      - character offset 1:1
    """
    for i, (s, e) in enumerate(zip(start.spine_components, end.spine_components)):
        if s.index != e.index:
            return (e.index - s.index) * 1_000_000.0 + i

    for i, (s, e) in enumerate(zip(start.document_path, end.document_path)):
        if s.index != e.index:
            return (e.index - s.index) * 1_000.0 + i

    start_off = start.character_offset or 0
    end_off = end.character_offset or 0
    return float(end_off - start_off)


def are_adjacent(first: CFIPosition, second: CFIPosition, threshold: float = 10.0) -> bool:
    return abs(distance(first, second)) <= threshold
