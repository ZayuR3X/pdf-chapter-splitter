from dataclasses import dataclass
from typing import Optional


@dataclass
class BookSection:
    title: str
    start_page: int
    end_page: int
    level: int = 1
    page_label: str = ""
    end_label: str = ""
    selected: bool = True
    is_front: bool = False
    next_title: str = ""
    next_start_page: Optional[int] = None
    toc_y: Optional[float] = None

    @property
    def page_count(self) -> int:
        return max(0, self.end_page - self.start_page + 1)