# PDF Chapter Splitter

A desktop utility for splitting large PDF books and technical documents into individual chapters or custom study packets using their embedded Table of Contents (TOC).

![](assets/screenshot.png)

## Features

- **TOC-Based Splitting:** Browse chapters and subsections in a tree view, filter headings in real time, rename titles inline, or remove unwanted entries with undo support (`Ctrl+Z`).
- **Batch Export & Folder Grouping:** Export checked sections as separate PDF files with customizable filename templates (`{index}`, `{title}`, `{book}`, `{start}`, `{end}`, `{label}`) and optional grouping by parent chapter.
- **Merge Selected Sections:** Combine multiple (even non-contiguous) sections into a single PDF with a clean table of contents.
- **Section Masking:** Automatically masks unrelated content above mid-page section starts and below the next heading on transition pages.
- **Quick Split & Preview:** Preview pages asynchronously, lock custom start/end page ranges, and export directly or append to the section list.
- **Bilingual UI:** English and Turkish interface (`EN / TR` switcher in the top bar) with drag-and-drop PDF loading.

## Download & Run (No Installation)

Download the standalone executable for your platform from the [Releases](https://github.com/ZayuR3X/pdf-chapter-splitter/releases) page:

- **Windows:** Download `pdf-chapter-splitter-windows.exe` and double-click to run.
- **Linux:** Download `pdf-chapter-splitter-linux`, grant execute permission, and run:
  ```bash
  chmod +x pdf-chapter-splitter-linux
  ./pdf-chapter-splitter-linux
  ```

## Run from Source

Requires Python 3.10+.

```bash
git clone https://github.com/ZayuR3X/pdf-chapter-splitter.git
cd pdf-chapter-splitter
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## License

This project is licensed under the [MIT License](LICENSE).