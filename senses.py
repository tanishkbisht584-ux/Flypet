"""Windows signals the fly can feel. stdlib ctypes + winreg, psutil for CPU."""
import ctypes, ctypes.wintypes as wt, subprocess, threading, time, winreg
import psutil

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
kernel32.GetTickCount.restype = wt.DWORD


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('dwTime', wt.DWORD)]


def cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def idle_seconds():
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return (kernel32.GetTickCount() - lii.dwTime) / 1000


def brightness():
    """Laptop panel brightness 0-1 via WMI; falls back to light/dark theme."""
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             '(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness'],
            capture_output=True, text=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        return int(out.split()[0]) / 100
    except Exception:
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
            return 1.0 if winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] else 0.2
        except Exception:
            return 0.7


class Senses:
    """Slow signals polled on a daemon thread; cursor() is read directly per frame."""
    cursor = staticmethod(cursor)

    def __init__(self):
        self.cpu, self.idle, self.light = 0.0, 0.0, 0.7
        psutil.cpu_percent(None)                                # prime; first call is always 0
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        n = 0
        while True:
            self.cpu = psutil.cpu_percent(None)
            self.idle = idle_seconds()
            if n % 5 == 0:
                self.light = brightness()
            n += 1
            time.sleep(1)
    # note: CPU temperature skipped — Windows only exposes it through LibreHardwareMonitor.
    # Load is the heat proxy. Add a temp reader here if that driver is ever installed.
