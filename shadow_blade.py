import sys
import time
import os
import psutil
from enum import Enum
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QMenu
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRectF, QPoint
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QMovie, QFont, QCursor

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

class AgentState(Enum):
    IDLE = 0
    ALERT = 1
    EXECUTE = 2

# Common process humanization mapping
PROCESS_MAP = {
    'chrome.exe': 'Google Chrome',
    'WeChat.exe': 'WeChat',
    'msedge.exe': 'Microsoft Edge',
    'firefox.exe': 'Firefox',
    'discord.exe': 'Discord',
    'steam.exe': 'Steam',
    'Code.exe': 'VS Code',
    'spotify.exe': 'Spotify'
}

# Strict Whitelist
WHITELIST_NAMES = {
    'explorer.exe', 'taskmgr.exe', 'svchost.exe', 'wininit.exe', 'csrss.exe',
    'idea64.exe', 'pycharm64.exe', 'python.exe', 'system', 'system idle process'
}

def get_window_title_for_pid(pid):
    """Fallback to get foreground window title associated with PID"""
    if sys.platform != 'win32':
        return None

    # Very rudimentary fallback. Getting true window titles for arbitrary background PIDs
    # requires deep EnumWindows iteration, which is slow in a 1s loop.
    # For now, we will just return None or check if it matches the current foreground window.
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if hwnd:
            window_pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value == pid:
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                    return buff.value
    except:
        pass
    return None

def humanize_process(name, pid):
    if name.lower() in [k.lower() for k in PROCESS_MAP]:
        # Case insensitive dict lookup
        key = next(k for k in PROCESS_MAP if k.lower() == name.lower())
        return PROCESS_MAP[key]

    win_title = get_window_title_for_pid(pid)
    if win_title:
        return f"{name} ({win_title[:15]}...)"

    return name

class NetworkGuardWorker(QThread):
    update_signal = pyqtSignal(str, list)  # net_speed_str, top_3_processes (list of dicts)

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

            # 2. Find Top 3 Processes (Highest I/O or CPU)
            current_process_io = {}
            process_candidates = []

            for p in psutil.process_iter(['pid', 'name', 'io_counters', 'cpu_percent']):
                try:
                    pid = p.info['pid']
                    name = p.info['name']
                    if pid <= 4: continue

                    cpu = p.info.get('cpu_percent', 0)
                    io_counters = p.info.get('io_counters')

                    io_delta = 0
                    if io_counters:
                        total_io = io_counters.read_bytes + io_counters.write_bytes
                        current_process_io[pid] = total_io
                        if pid in self.process_io:
                            io_delta = total_io - self.process_io[pid]

                    process_candidates.append({
                        'pid': pid,
                        'name': name,
                        'io_delta': io_delta,
                        'cpu': cpu
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            self.process_io = current_process_io

            # Sort by IO first, then CPU
            process_candidates.sort(key=lambda x: (x['io_delta'], x['cpu']), reverse=True)
            top_3 = process_candidates[:3]

            self.update_signal.emit(net_speed_str, top_3)
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


class BubbleWidget(QWidget):
    """Custom frameless dark frosted glass dialogue bubble"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(220, 80)

        layout = QVBoxLayout()
        layout.setContentsMargins(15, 10, 15, 20) # Bottom margin for tail

        self.lbl_net = QLabel("D: 0.0 B/s | U: 0.0 B/s")
        self.lbl_net.setStyleSheet("color: #00E676; font-family: 'Consolas', monospace; font-weight: bold; font-size: 12px;")
        self.lbl_net.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_msg = QLabel("Scanning...")
        self.lbl_msg.setStyleSheet("color: #FFFFFF; font-family: 'Segoe UI'; font-size: 11px;")
        self.lbl_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_msg.setWordWrap(True)

        layout.addWidget(self.lbl_net)
        layout.addWidget(self.lbl_msg)
        self.setLayout(layout)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        path = QPainterPath()
        rect = QRectF(0, 0, self.width(), self.height() - 15)
        path.addRoundedRect(rect, 10, 10)

        # Add tail
        path.moveTo(self.width() / 2 - 10, self.height() - 15)
        path.lineTo(self.width() / 2, self.height())
        path.lineTo(self.width() / 2 + 10, self.height() - 15)

        painter.fillPath(path, QColor(25, 25, 30, 190))
        painter.setPen(QColor(80, 80, 80, 180))
        painter.drawPath(path)

class ShadowBladePet(QWidget):
    def __init__(self):
        super().__init__()
        self.current_state = AgentState.IDLE
        self.top_processes = []
        self.locked_pid = None
        self.locked_time = 0
        self.drag_position = None
        self.message_lock = False

        self.initUI()
        self.start_worker()

        if sys.platform == 'win32':
            self.fullscreen_timer = QTimer(self)
            self.fullscreen_timer.timeout.connect(self.check_fullscreen)
            self.fullscreen_timer.start(2000)

    def initUI(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Overall container size (Bubble + Character)
        self.resize(250, 250)

        # Bubble
        self.bubble = BubbleWidget(self)
        self.bubble.move(15, 0)

        # Character Display
        self.char_lbl = QLabel(self)
        self.char_lbl.setGeometry(25, 80, 200, 170)
        self.char_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_character_state(AgentState.IDLE)

        # Context Menu
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def set_character_state(self, state):
        self.current_state = state

        if state == AgentState.IDLE or state == AgentState.ALERT:
            if os.path.exists("griffith_idle.gif"):
                self.movie = QMovie("griffith_idle.gif")
                self.char_lbl.setMovie(self.movie)
                self.movie.start()
            else:
                self.char_lbl.setText("GRIFFITH\n(IDLE/ALERT)")
                self.char_lbl.setStyleSheet("color: white; background-color: rgba(50, 50, 100, 150); border-radius: 85px;")

        elif state == AgentState.EXECUTE:
            if os.path.exists("femto_execute.gif"):
                self.movie = QMovie("femto_execute.gif")
                self.char_lbl.setMovie(self.movie)
                self.movie.start()
            else:
                self.char_lbl.setText("FEMTO\n(EXECUTE)")
                self.char_lbl.setStyleSheet("color: white; background-color: rgba(150, 0, 50, 180); border-radius: 85px;")

            # Auto reset to IDLE after 2 seconds
            QTimer.singleShot(2000, lambda: self.set_character_state(AgentState.IDLE))
            QTimer.singleShot(2000, lambda: self.bubble.lbl_msg.setStyleSheet("color: #FFFFFF; font-family: 'Segoe UI'; font-size: 11px;"))

    def start_worker(self):
        self.worker = NetworkGuardWorker()
        self.worker.update_signal.connect(self.update_ui)
        self.worker.start()

    def update_ui(self, net_speed, top_3):
        self.bubble.lbl_net.setText(net_speed)
        self.top_processes = top_3

        if self.current_state == AgentState.EXECUTE or self.message_lock:
            return # Don't update msg during execution animation or message locks

        # Lock the highest process for 5 seconds to prevent flickering
        current_time = time.time()
        if self.locked_pid is None or current_time - self.locked_time > 5:
            if top_3 and top_3[0]['io_delta'] > 1024 * 50: # Threshold for alert
                self.locked_pid = top_3[0]['pid']
                self.locked_time = current_time
                self.set_character_state(AgentState.ALERT)
            else:
                self.locked_pid = None
                self.set_character_state(AgentState.IDLE)

        # Update Bubble text
        if self.locked_pid:
            # Find the locked process in current top_3 or fallback to name
            locked_proc = next((p for p in top_3 if p['pid'] == self.locked_pid), None)
            if locked_proc:
                h_name = humanize_process(locked_proc['name'], locked_proc['pid'])
                self.bubble.lbl_msg.setText(f"Target locked:\n{h_name}")
                self.bubble.lbl_msg.setStyleSheet("color: #FF5252;")
        else:
            self.bubble.lbl_msg.setText("Monitoring the realm...")
            self.bubble.lbl_msg.setStyleSheet("color: #FFFFFF;")

    def is_protected(self, name, pid):
        if name.lower() in WHITELIST_NAMES:
            return True

        # Check Proxy Port 7897
        try:
            p = psutil.Process(pid)
            for conn in p.connections(kind='inet'):
                if conn.laddr.port == 7897:
                    return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass

        return False

    def reset_message_lock(self):
        self.message_lock = False

    def kill_process(self, pid, name):
        if pid <= 4 or pid == os.getpid():
            return

        if self.is_protected(name, pid):
            self.message_lock = True
            self.bubble.lbl_msg.setText("This thread sustains our world;\nit cannot be crushed.")
            self.bubble.lbl_msg.setStyleSheet("color: #FFB300;")
            QTimer.singleShot(3000, lambda: self.set_character_state(AgentState.IDLE))
            QTimer.singleShot(3000, self.reset_message_lock)
            return

        self.set_character_state(AgentState.EXECUTE)
        h_name = humanize_process(name, pid)

        try:
            p = psutil.Process(pid)
            p.kill()
            self.message_lock = True
            self.bubble.lbl_msg.setText(f"Crushed:\n{h_name}")
            self.bubble.lbl_msg.setStyleSheet("color: #9C27B0; font-weight: bold;")
            QTimer.singleShot(3000, self.reset_message_lock)
        except psutil.AccessDenied:
            self.message_lock = True
            self.bubble.lbl_msg.setText("Access Denied.\nThe mortal plane resists.")
            self.bubble.lbl_msg.setStyleSheet("color: #FFB300;")
            QTimer.singleShot(3000, self.reset_message_lock)
        except Exception:
            self.message_lock = True
            self.bubble.lbl_msg.setText("Target slipped away.")
            self.bubble.lbl_msg.setStyleSheet("color: #9E9E9E;")
            QTimer.singleShot(3000, self.reset_message_lock)

        self.locked_pid = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.locked_pid and self.current_state == AgentState.ALERT:
            # Find the name of the locked pid
            locked_proc = next((p for p in self.top_processes if p['pid'] == self.locked_pid), None)
            if locked_proc:
                self.kill_process(self.locked_pid, locked_proc['name'])

    def show_context_menu(self, pos):
        if not self.top_processes:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background-color: rgba(25, 25, 30, 240); color: white; border: 1px solid #555; }
            QMenu::item:selected { background-color: rgba(150, 0, 50, 200); }
        """)

        for proc in self.top_processes:
            h_name = humanize_process(proc['name'], proc['pid'])
            io_mb = proc['io_delta'] / (1024 * 1024)
            action = menu.addAction(f"{h_name} ({io_mb:.1f} MB/s | {proc['cpu']:.1f}%)")
            action.triggered.connect(lambda checked, p=proc['pid'], n=proc['name']: self.kill_process(p, n))

        menu.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_position:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def check_fullscreen(self):
        if sys.platform != 'win32': return
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd: return

            class_name = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
            if class_name.value in ("WorkerW", "Progman", "Shell_TrayWnd"):
                if self.isHidden(): self.show()
                return

            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            screen_height = ctypes.windll.user32.GetSystemMetrics(1)

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

            width = rect.right - rect.left
            height = rect.bottom - rect.top

            if width >= screen_width and height >= screen_height:
                if not self.isHidden(): self.hide()
            else:
                if self.isHidden(): self.show()
        except:
            pass

    def closeEvent(self, event):
        self.worker.stop()
        super().closeEvent(event)

def main():
    app = QApplication(sys.argv)
    if hasattr(Qt.ApplicationAttribute, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

    widget = ShadowBladePet()
    widget.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()