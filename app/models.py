"""
Internal data model consumed by the PPTX fill engine.

This layer is intentionally decoupled from Ezekia. The Ezekia client
(app/ezekia.py) is the ONLY place that knows Ezekia's field names; it
translates a raw Ezekia assignment payload into these objects. The PPTX
engine only ever sees these classes, so if Ezekia changes its schema we
only touch ezekia.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class Stage(str, Enum):
    """Where a candidate sits in the search. Drives which slide(s) they land on."""
    ENGAGED = "engaged"        # -> engaged profile slides
    PIPELINE = "pipeline"      # -> "Select pipeline candidates" table
    TARGET = "target"          # -> "Select target candidates" table
    DISCOUNTED = "discounted"  # -> discounted profile slides AND/OR discounted table


@dataclass
class CareerEntry:
    """One (role, dates) line within a company group."""
    role: str = ""
    dates: str = ""


@dataclass
class CareerGroup:
    """A company and the consecutive roles held there, newest first.

    Renders as:
        Google              (company, bold)
        Director   2024 - P
        VP         2022 - 2024
        Associate  2020 - 2022
    """
    company: str
    roles: List[CareerEntry] = field(default_factory=list)


@dataclass
class Candidate:
    name: str
    stage: Stage

    # --- List-table fields (pipeline / target / discounted tables) ---
    role: str = ""            # current role/title
    company: str = ""         # current company
    status: str = ""          # e.g. "In conversation", "Actively approaching", "Not open to a move"

    # --- Profile-slide fields (engaged & some discounted) ---
    has_profile: bool = False
    name_url: str = ""        # LinkedIn URL -> name becomes a hyperlink on profile slides
    salary: str = ""          # "£220,000 base, bonus, LTIP"
    location: str = ""        # "London, open to commuting"
    availability: str = ""    # "4 months" / "Immediately available" / "TBC"
    education: str = ""       # free text; tabs/newlines preserved
    career: List[CareerGroup] = field(default_factory=list)


@dataclass
class Assignment:
    """A single Ezekia search/assignment plus its candidates."""
    name: str                                   # company / project name, e.g. "Campfire"
    title: str = ""                             # deck title, e.g. "Search Update – Campfire"
    prepared_for: List[str] = field(default_factory=list)  # each -> its own paragraph on the cover
    date: str = ""                              # "13/08/2026"
    candidates: List[Candidate] = field(default_factory=list)

    # convenience selectors used by the engine ------------------------------
    def engaged(self) -> List[Candidate]:
        return [c for c in self.candidates if c.stage == Stage.ENGAGED]

    def pipeline(self) -> List[Candidate]:
        return [c for c in self.candidates if c.stage == Stage.PIPELINE]

    def targets(self) -> List[Candidate]:
        return [c for c in self.candidates if c.stage == Stage.TARGET]

    def discounted(self) -> List[Candidate]:
        return [c for c in self.candidates if c.stage == Stage.DISCOUNTED]

    def discounted_profiles(self) -> List[Candidate]:
        # "Praeva discounted" -> full profile format.
        return [c for c in self.discounted() if c.has_profile]

    def discounted_table(self) -> List[Candidate]:
        # "Not Interested" -> table format. Kept separate from the profile group.
        return [c for c in self.discounted() if not c.has_profile]
