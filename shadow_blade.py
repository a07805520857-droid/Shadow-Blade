import sys
import time
import psutil
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

class NetworkGuardWorker(QThread):
    """
    Background worker thread to monitor global network speed
    and identify processes with high CPU or I/O usage.
    """
    update_signal = pyqtSignal(str, str, int)  # net_speed_str, top_process_str, top_pid

    def __init__(self):
        super().__init__()
        self.running = True
        self.last_net_io = psutil.net_io_counters()
        self.process_io = {}

    def run(self):
        while self.running:
            # 1. Global Network Speed
            current_net_io = psutil.net_io_counters()
            dl_speed = current_net_io.bytes_recv - self.last_net_io.bytes_recv
            ul_speed = current_net_io.bytes_sent - self.last_net_io.bytes_sent
            self.last_net_io = current_net_io

            dl_str = self.format_speed(dl_speed)
            ul_str = self.format_speed(ul_speed)
            net_speed_str = f"D: {dl_str} | U: {ul_str}"

            # 2. Find Top Process (Highest I/O or CPU)
            top_process_name = "System Idle"
            top_pid = 0
            max_io_delta = -1

            current_process_io = {}
            top_cpu_process = None
            max_cpu = -1.0

            # Iterate through processes to find the ones causing spikes
            for p in psutil.process_iter(['pid', 'name', 'io_counters', 'cpu_percent']):
                try:
                    pid = p.info['pid']
                    name = p.info['name']

                    # Track CPU as fallback (since network per-process is hard to pinpoint without ETW)
                    cpu = p.info.get('cpu_percent')
                    if cpu is not None and cpu > max_cpu and pid > 4:
                        max_cpu = cpu
                        top_cpu_process = (name, pid)

                    # Track I/O as a proxy for network/disk usage spikes
                    io_counters = p.info.get('io_counters')
                    if io_counters:
                        total_io = io_counters.read_bytes + io_counters.write_bytes
                        current_process_io[pid] = total_io

                        if pid in self.process_io:
                            io_delta = total_io - self.process_io[pid]
                            # Exclude System Idle Process (pid 0) and System (pid 4)
                            if io_delta > max_io_delta and pid > 4:
                                max_io_delta = io_delta
                                top_process_name = name
                                top_pid = pid

                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            # If no significant I/O difference found, fallback to highest CPU
            if max_io_delta <= 0 and top_cpu_process:
                top_process_name, top_pid = top_cpu_process

            self.process_io = current_process_io

            # Process string to display
            top_process_str = f"Rogue: {top_process_name} (PID: {top_pid})"

            # Safely emit to GUI thread
            self.update_signal.emit(net_speed_str, top_process_str, top_pid)

            time.sleep(1)

    def format_speed(self, bytes_per_sec):
        if bytes_per_sec >= 1024 * 1024:
            return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        elif bytes_per_sec >= 1024:
            return f"{bytes_per_sec / 1024:.1f} KB/s"
        else:
            return f"{bytes_per_sec} B/s"

    def stop(self):
        self.running = False
        self.wait()

class ShadowBladeWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.top_pid = None
        self.initUI()
        self.start_worker()

        # Timer to check for fullscreen application (Windows only)
        if sys.platform == 'win32':
            self.fullscreen_timer = QTimer(self)
            self.fullscreen_timer.timeout.connect(self.check_fullscreen)
            self.fullscreen_timer.start(2000)

        self.drag_position = None

    def initUI(self):
        # Frameless, Always on Top, Tool window (hide from taskbar)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.resize(260, 80)

        layout = QVBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)

        # Line 1: Network Speed
        self.lbl_net = QLabel("D: 0.0 KB/s | U: 0.0 KB/s")
        self.lbl_net.setStyleSheet("color: #00E676; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 13px;")
        self.lbl_net.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Line 2: Top Process
        self.lbl_proc = QLabel("Scanning processes...")
        self.lbl_proc.setStyleSheet("color: #FF5252; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 12px;")
        self.lbl_proc.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.lbl_net)
        layout.addWidget(self.lbl_proc)
        self.setLayout(layout)

        # Frosted glass styling with dark transparent background
        self.setStyleSheet("""
            QWidget {
                background-color: rgba(25, 25, 30, 210);
                border-radius: 10px;
                border: 1px solid rgba(80, 80, 80, 180);
            }
        """)

    def start_worker(self):
        self.worker = NetworkGuardWorker()
        self.worker.update_signal.connect(self.update_ui)
        self.worker.start()

    def update_ui(self, net_speed, top_process, pid):
        self.lbl_net.setText(net_speed)
        # Only update process label if we haven't just killed something (to show the "Killed" feedback)
        if not self.lbl_proc.text().startswith("Killed:") and not self.lbl_proc.text().startswith("Error:") and not self.lbl_proc.text().startswith("Access Denied") and not self.lbl_proc.text().startswith("Process already gone"):
            self.lbl_proc.setText(top_process)
        self.top_pid = pid

    def mousePressEvent(self, event):
        # Allow dragging the frameless window
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        # Move window on drag
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_position:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        # Double click to instantly terminate the flagged rogue process
        if event.button() == Qt.MouseButton.LeftButton and self.top_pid:
            self.kill_process(self.top_pid)

    def kill_process(self, pid):
        # Prevent killing system critical processes or ourself
        if pid <= 4 or pid == psutil.Process().pid:
            return

        try:
            p = psutil.Process(pid)
            name = p.name()
            # Terminate the process (equivalent to taskkill /f)
            p.kill()
            self.lbl_proc.setText(f"Killed: {name}")
            self.lbl_proc.setStyleSheet("color: #00E676; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 12px;")

            # Reset color and text after 2 seconds
            QTimer.singleShot(2000, self.reset_process_label)

        except psutil.AccessDenied:
            self.lbl_proc.setText("Access Denied (Needs Admin)")
            self.lbl_proc.setStyleSheet("color: #FFB300; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 12px;")
            QTimer.singleShot(2000, self.reset_process_label)
        except psutil.NoSuchProcess:
            self.lbl_proc.setText("Process already gone")
            self.lbl_proc.setStyleSheet("color: #FFB300; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 12px;")
            QTimer.singleShot(2000, self.reset_process_label)
        except Exception as e:
            self.lbl_proc.setText("Error killing process")
            self.lbl_proc.setStyleSheet("color: #FF5252; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 12px;")
            QTimer.singleShot(2000, self.reset_process_label)

    def reset_process_label(self):
        self.lbl_proc.setText("Scanning processes...")
        self.lbl_proc.setStyleSheet("color: #FF5252; font-family: 'Segoe UI', Arial; font-weight: bold; font-size: 12px;")
        # The text will be overwritten by the next worker update

    def check_fullscreen(self):
        """
        Check if the active foreground window is fullscreen.
        If yes, hide the widget to not interrupt gaming or movies.
        """
        if sys.platform != 'win32':
            return

        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return

            # Avoid hiding when Desktop or Taskbar is active
            class_name = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value in ("WorkerW", "Progman", "Shell_TrayWnd"):
                if self.isHidden():
                    self.show()
                return

            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            screen_height = ctypes.windll.user32.GetSystemMetrics(1)

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

            width = rect.right - rect.left
            height = rect.bottom - rect.top

            if width >= screen_width and height >= screen_height:
                if not self.isHidden():
                    self.hide()
            else:
                if self.isHidden():
                    self.show()
        except Exception:
            pass

    def closeEvent(self, event):
        self.worker.stop()
        super().closeEvent(event)

def main():
    app = QApplication(sys.argv)

    # Set high DPI scaling for Windows
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt.ApplicationAttribute, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    widget = ShadowBladeWidget()
    widget.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
