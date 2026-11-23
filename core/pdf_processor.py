from collections import OrderedDict
from pathlib import Path
import re
import threading
from typing import Callable, List, Optional, Tuple
import unicodedata
import pymupdf as fitz

from .models import BookSection


IGNORED_TITLE_KEYWORDS = {
    "cover", "kapak", "contents", "table of contents", "içindekiler",
    "preface", "önsöz", "foreword", "acknowledgments", "teşekkür",
    "index", "dizin", "bibliography", "kaynakça", "title page", "copyright",
    "brief contents"
}


class PDFProcessor:
    def __init__(self) -> None:
        self.doc: Optional[fitz.Document] = None
        self.file_path: Optional[Path] = None
        self._raw_toc: List[Tuple[int, str, int, Optional[float]]] = []
        self._lock = threading.Lock()
        self._render_cache: OrderedDict[Tuple[int, float], bytes] = OrderedDict()
        self._max_cache_items = 35

    @property
    def total_pages(self) -> int:
        return len(self.doc) if self.doc else 0

    @property
    def book_stem(self) -> str:
        if not self.file_path:
            return "kitap"
        return self.sanitize_filename(self.file_path.stem)

    @staticmethod
    def clean_toc_title(raw_title: str) -> str:
        cleaned_chars = [
            " " if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn") else ch
            for ch in raw_title
        ]
        return re.sub(r"\s+", " ", "".join(cleaned_chars)).strip()

    def load_pdf(self, path: str | Path) -> List[BookSection]:
        with self._lock:
            if self.doc:
                self.doc.close()
            self._render_cache.clear()
            self.file_path = Path(path)
            self.doc = fitz.open(self.file_path)

            raw = self.doc.get_toc(simple=False)
            total = len(self.doc)
            parsed: List[Tuple[int, str, int, Optional[float]]] = []

            for item in raw:
                level, title, page = item[0], item[1], item[2]
                dest = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
                clean_t = self.clean_toc_title(title)
                if not clean_t:
                    continue

                y_coord: Optional[float] = None
                to_pt = dest.get("to")
                if to_pt is not None and hasattr(to_pt, "y") and to_pt.y > 0:
                    y_coord = float(to_pt.y)

                parsed.append((level, clean_t, max(1, min(page, total)), y_coord))

            self._raw_toc = parsed

        return self.build_sections_from_toc()

    def build_sections_from_toc(self) -> List[BookSection]:
        if not self.doc or not self._raw_toc:
            return []

        sections: List[BookSection] = []
        total = self.total_pages

        for idx, (level, title, start_page, toc_y) in enumerate(self._raw_toc):
            end_page = total
            next_title = ""
            next_start_page: Optional[int] = None

            for next_idx in range(idx + 1, len(self._raw_toc)):
                next_level, n_title, next_start, _ = self._raw_toc[next_idx]
                if next_level <= level and next_start > start_page:
                    end_page = next_start - 1
                    next_title = n_title
                    next_start_page = next_start
                    break
                if next_level <= level and next_start == start_page:
                    end_page = start_page
                    next_title = n_title
                    next_start_page = next_start
                    break

            final_end = max(start_page, min(total, end_page))
            front = self.is_front_matter(start_page, title)

            sections.append(
                BookSection(
                    title=title,
                    start_page=start_page,
                    end_page=final_end,
                    level=level,
                    page_label=self.get_page_label(start_page),
                    end_label=self.get_page_label(final_end),
                    selected=not front,
                    is_front=front,
                    next_title=next_title,
                    next_start_page=next_start_page,
                    toc_y=toc_y,
                )
            )

        return sections

    @staticmethod
    def _find_heading_y(
        page: fitz.Page, title: str, fallback_y: Optional[float] = None
    ) -> Optional[float]:
        clean_target = re.sub(r"\s+", " ", title).strip().lower()
        if not clean_target:
            return fallback_y

        header_limit = 52.0
        footer_limit = page.rect.height - 38.0

        num_match = re.match(r"^(\d+(?:\.\d+)+)\b", clean_target)
        section_num = num_match.group(1) if num_match else ""
        short_prefix = clean_target[:24]

        candidates: List[Tuple[float, float]] = []
        text_dict = page.get_text("dict")

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                y0 = float(line.get("bbox", (0, 0, 0, 0))[1])
                if y0 < header_limit or y0 > footer_limit:
                    continue

                spans = line.get("spans", [])
                line_text = re.sub(
                    r"\s+", " ", "".join(s.get("text", "") for s in spans)
                ).strip().lower()
                if not line_text:
                    continue

                max_size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)

                if (
                    line_text == clean_target
                    or line_text.startswith(short_prefix)
                    or (clean_target.startswith(line_text) and len(line_text) >= 6)
                ):
                    candidates.append((max_size + 5.0, y0))
                elif (
                    section_num
                    and re.match(rf"^{re.escape(section_num)}(?:\s|$)", line_text)
                    and max_size >= 10.8
                ):
                    candidates.append((max_size, y0))

        if candidates:
            candidates.sort(key=lambda item: (-round(item[0], 1), item[1]))
            return candidates[0][1]

        for query in (title, title[:28], section_num):
            if not query or len(query) < 2:
                continue
            rects = page.search_for(query)
            valid = [r.y0 for r in rects if header_limit <= r.y0 <= footer_limit]
            if valid:
                return min(valid)

        if fallback_y is not None and header_limit <= fallback_y <= footer_limit:
            return fallback_y
        return None

    def _build_masked_section_doc(
        self,
        start_page: int,
        end_page: int,
        title: str = "",
        next_title: str = "",
        next_start_page: Optional[int] = None,
        toc_y: Optional[float] = None,
        smart_crop: bool = True,
    ) -> fitz.Document:
        assert self.doc is not None
        doc_len = len(self.doc)
        s_idx = max(0, start_page - 1)
        e_idx = min(doc_len - 1, end_page - 1)

        include_transition = False
        bottom_cut_y: Optional[float] = None

        if smart_crop and next_title and next_start_page and 1 <= next_start_page <= doc_len:
            y_next = self._find_heading_y(self.doc[next_start_page - 1], next_title)

            if next_start_page == end_page + 1 and y_next is not None and y_next > 78.0:
                e_idx = next_start_page - 1
                include_transition = True
                bottom_cut_y = y_next
            elif next_start_page == start_page == end_page and y_next is not None and y_next > 78.0:
                include_transition = True
                bottom_cut_y = y_next

        part_doc = fitz.open()
        part_doc.insert_pdf(self.doc, from_page=s_idx, to_page=e_idx)

        if not smart_crop or len(part_doc) == 0:
            return part_doc

        if title:
            first_page = part_doc[0]
            y_top = self._find_heading_y(first_page, title, fallback_y=toc_y)
            if y_top is not None and y_top > 68.0:
                top_rect = fitz.Rect(0, 0, first_page.rect.width, max(0.0, y_top - 6.0))
                first_page.add_redact_annot(top_rect, fill=(1, 1, 1))
                first_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)

        if include_transition and bottom_cut_y is not None:
            last_page = part_doc[-1]
            cut_bottom = max(0.0, bottom_cut_y - 6.0)
            if cut_bottom < last_page.rect.height - 20.0:
                bot_rect = fitz.Rect(0, cut_bottom, last_page.rect.width, last_page.rect.height)
                last_page.add_redact_annot(bot_rect, fill=(1, 1, 1))
                last_page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)

        return part_doc

    def get_page_label(self, physical_page: int) -> str:
        with self._lock:
            if not self.doc or not (1 <= physical_page <= len(self.doc)):
                return ""
            label = self.doc[physical_page - 1].get_label()
            return label if label else str(physical_page)

    def is_front_matter(self, physical_page: int, title: str = "") -> bool:
        with self._lock:
            if not self.doc or not (1 <= physical_page <= len(self.doc)):
                return False
            raw_label = self.doc[physical_page - 1].get_label()

        if raw_label and not raw_label.strip().isdigit():
            return True

        clean_title = title.strip().lower()
        return any(keyword in clean_title for keyword in IGNORED_TITLE_KEYWORDS)

    def render_page_png(self, physical_page: int, zoom: float = 1.45) -> Optional[bytes]:
        cache_key = (physical_page, zoom)
        with self._lock:
            if cache_key in self._render_cache:
                self._render_cache.move_to_end(cache_key)
                return self._render_cache[cache_key]

            if not self.doc or not (1 <= physical_page <= len(self.doc)):
                return None

            page = self.doc[physical_page - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            png_bytes = pixmap.tobytes("png")

            self._render_cache[cache_key] = png_bytes
            if len(self._render_cache) > self._max_cache_items:
                self._render_cache.popitem(last=False)

            return png_bytes

    @staticmethod
    def sanitize_filename(name: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
        cleaned = re.sub(r"\s+", "_", cleaned.strip())
        return cleaned[:120] or "bolum"

    def format_output_filename(self, pattern: str, index: int, section: BookSection) -> str:
        safe_title = self.sanitize_filename(section.title)
        safe_label = self.sanitize_filename(section.page_label or str(section.start_page))
        template = pattern.strip() or "{index}_{title}"

        values = {
            "index": f"{index:02d}",
            "title": safe_title,
            "book": self.book_stem,
            "start": str(section.start_page),
            "end": str(section.end_page),
            "label": safe_label,
        }

        try:
            rendered = template.format(**values)
        except Exception:
            rendered = f"{index:02d}_{safe_title}"

        rendered = self.sanitize_filename(rendered)
        if not rendered.lower().endswith(".pdf"):
            rendered += ".pdf"
        return rendered

    def export_sections(
        self,
        sections: List[BookSection],
        output_dir: str | Path,
        filename_pattern: str = "{index}_{title}",
        smart_crop: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> List[Path]:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        selected_sections = [s for s in sections if s.selected]
        created_files: List[Path] = []
        total = len(selected_sections)

        with self._lock:
            if not self.doc:
                raise RuntimeError("Yüklü bir PDF dosyası bulunamadı.")

            for idx, section in enumerate(selected_sections, start=1):
                if section.start_page > section.end_page:
                    continue

                filename = self.format_output_filename(filename_pattern, idx, section)
                target_file = out_path / filename

                part_doc = self._build_masked_section_doc(
                    start_page=section.start_page,
                    end_page=section.end_page,
                    title=section.title,
                    next_title=section.next_title,
                    next_start_page=section.next_start_page,
                    toc_y=section.toc_y,
                    smart_crop=smart_crop,
                )
                part_doc.save(target_file, garbage=3, deflate=True)
                part_doc.close()

                created_files.append(target_file)
                if progress_callback:
                    progress_callback(idx, total, filename)

        return created_files

    def export_merged_range(
        self,
        start_page: int,
        end_page: int,
        target_file: str | Path,
        title: str = "",
        next_title: str = "",
        next_start_page: Optional[int] = None,
        smart_crop: bool = True,
    ) -> Path:
        out_file = Path(target_file)
        with self._lock:
            if not self.doc:
                raise RuntimeError("Yüklü bir PDF dosyası bulunamadı.")
            if start_page > end_page:
                raise ValueError("Başlangıç sayfası bitiş sayfasından büyük olamaz.")

            part_doc = self._build_masked_section_doc(
                start_page=start_page,
                end_page=end_page,
                title=title,
                next_title=next_title,
                next_start_page=next_start_page,
                smart_crop=smart_crop,
            )
            part_doc.save(out_file, garbage=3, deflate=True)
            part_doc.close()
        return out_file

    def close(self) -> None:
        with self._lock:
            if self.doc:
                self.doc.close()
                self.doc = None
            self._render_cache.clear()