from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Course:
    code: str
    distance_m: int
    color: Optional[str] = None


@dataclass
class Checkpoint:
    point_id: int
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    course_distances: Dict[str, int] = field(default_factory=dict)


@dataclass
class Category:
    abbr: str
    name: str
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    sex: Optional[str] = None  # 'M', 'F', or None


@dataclass
class Split:
    point_id: int
    checkpoint_name: str
    cumulative_seconds: Optional[float]  # net elapsed time since this runner's own start
    clock_time_text: Optional[str]  # wall-clock time-of-day, as raw text (format varies)
    split_seconds: Optional[float] = None  # leg-only duration since the previous checkpoint


@dataclass
class Runner:
    bib: str
    name: str
    club: Optional[str]
    birth_year: Optional[int]
    sex: Optional[str]
    category: Optional[str]
    course_code: Optional[str]
    nationality: Optional[str]
    start_time_text: Optional[str]
    status: str  # 'finished', 'withdrawal', 'disqualified', 'no_result'
    finish_time_text: Optional[str] = None
    finish_seconds: Optional[float] = None
    pace: Optional[float] = None
    finish_clock_text: Optional[str] = None
    gap_text: Optional[str] = None
    gap_seconds: Optional[float] = None
    splits: List[Split] = field(default_factory=list)
    rank_course: Optional[int] = None  # overall/"scratch" rank within this runner's course
    rank_category: Optional[int] = None  # rank within course + age/gender category
    rank_gender: Optional[int] = None  # rank within course + gender


@dataclass
class Race:
    slug: str
    name: str
    organizer: Optional[str]
    date: Optional[str]
    source_url: str
    courses: List[Course] = field(default_factory=list)
    checkpoints: List[Checkpoint] = field(default_factory=list)
    categories: List[Category] = field(default_factory=list)
    runners: List[Runner] = field(default_factory=list)
