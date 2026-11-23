import gc
import os
import sys
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow


def main() -> None:
    if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    app = QApplication(sys.argv)
    app.setApplicationName("PDF Chapter Splitter")
    window = MainWindow()
    window.show()
    exit_code = app.exec()
    del window
    gc.collect()
    del app
    sys.exit(exit_code)


if __name__ == "__main__":
    main()