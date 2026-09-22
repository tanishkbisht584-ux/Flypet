"""The fly's hands: launch and close apps, run long background tasks, move/click the mouse,
type on the keyboard. Windows SendInput via ctypes, no dependencies.

SAFETY, because this thing drives your real mouse and keyboard:
  * Ctrl+Alt+F (polled by fly.py) or panic() -> everything stops, tasks killed, autonomy off.
  * While you are actively typing (last keystroke < TYPING_GUARD s) mouse/keyboard actions are
    refused, so the fly never fights you for the keyboard.
  * Every action is logged to hands.log for the panel.
"""
import ctypes, json, os, shlex, subprocess, threading, time
from ctypes import wintypes

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
TYPING_GUARD = 2.0                       # seconds since your last keystroke before it may touch input
CFG = 'apps.json'
NO_WINDOW = subprocess.CREATE_NO_WINDOW
CF_UNICODETEXT = 13
TYPE_DELAY = 0.01                        # seconds between characters; see the measurement in
                                          # git history / README before lowering this
# Both restype AND argtypes must be declared, or ctypes' 32-bit c_int default truncates these
# pointer-sized (64-bit) HANDLEs - as either an overflow on the way in or silent truncation out.
user32.GetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

# ---- SendInput plumbing ------------------------------------------------------------------
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOVE_ABS = 0x8000 | 0x0001
BTN = {'left': (0x0002, 0x0004), 'right': (0x0008, 0x0010), 'middle': (0x0020, 0x0040)}
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK = {'enter': 0x0D, 'tab': 0x09, 'esc': 0x1B, 'space': 0x20, 'back': 0x08, 'del': 0x2E,
      'ctrl': 0x11, 'alt': 0x12, 'shift': 0x10, 'win': 0x5B, 'up': 0x26, 'down': 0x28,
      'left': 0x25, 'right': 0x27, 'home': 0x24, 'end': 0x23,
      **{f'f{i}': 0x6F + i for i in range(1, 13)}}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.POINTER(wintypes.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.POINTER(wintypes.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]
    _anonymous_ = ('u',)
    _fields_ = [('type', wintypes.DWORD), ('u', _U)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, x=0, y=0):
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(x, y, 0, flags, 0, None))


def _key(vk=0, scan=0, flags=0):
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, None))


class Hands:
    def __init__(self, senses):
        self.senses = senses
        self.stopped = False
        self.log = []                                   # (time, text) newest last
        self.mine = {}                                  # name -> Popen for apps the fly opened
        self.task = None                                # (name, Popen, started)
        self.result = None                              # set when a task finishes; fly announces it
        self.cfg = json.load(open(CFG)) if os.path.exists(CFG) else {'apps': {}, 'tasks': {}, 'autonomous': []}

    # ---- safety ---------------------------------------------------------------------------
    def note(self, text):
        self.log.append((time.strftime('%H:%M:%S'), text))
        del self.log[:-200]

    def panic(self, why='STOP pressed'):
        self.stopped = True
        if self.task:
            name, p, _ = self.task
            p.kill()
            self.task = None
            self.note(f'killed task {name}')
        for vk in (VK['ctrl'], VK['alt'], VK['shift'], VK['win']):   # release anything held
            _send(_key(vk=vk, flags=KEYEVENTF_KEYUP))
        self.note(f'PANIC: {why}')

    def resume(self):
        self.stopped = False
        self.note('resumed')

    def can_touch(self):
        """Refuse mouse/keyboard while the human is actively using them."""
        return not self.stopped and self.senses.idle >= TYPING_GUARD

    # ---- apps ----------------------------------------------------------------------------
    def open_app(self, name):
        if self.stopped:
            return 'stopped'
        cmd = self.cfg['apps'].get(name.lower())
        if not cmd:
            return f'I do not know "{name}" (add it to apps.json)'
        try:
            # Launch without a shell where possible: `cmd /c foo` exits at once and its PID is
            # useless, which would make close_app fall back to taskkill and kill YOUR copy too.
            # shlex.split() defaults to POSIX escaping, which treats \ as an escape character and
            # corrupts every Windows path (C:\Program Files\... -> C:Program Files...). posix=False
            # keeps backslashes literal; still splits on quoted spaces the way apps.json expects.
            shell = any(ch in cmd for ch in '&|><"') or cmd.lower().startswith('start ')
            args = cmd if shell else shlex.split(cmd, posix=False)
            self.mine[name.lower()] = subprocess.Popen(args, shell=shell)
            self.note(f'opened {name}')
            return f'opened {name}'
        except Exception as e:
            self.note(f'failed to open {name}: {e}')
            return f'could not open {name}'

    def close_app(self, name):
        if self.stopped:
            return 'stopped'
        n = name.lower()
        p = self.mine.pop(n, None)
        if p and p.poll() is None:
            p.kill()
            self.note(f'closed {name} (the one I opened)')
            return f'closed {name}'
        exe = self.cfg['apps'].get(n, n)
        exe = os.path.basename(shlex.split(exe)[0] if exe else n)
        if not exe.lower().endswith('.exe'):
            exe += '.exe'
        r = subprocess.run(['taskkill', '/IM', exe, '/F'], capture_output=True, text=True, creationflags=NO_WINDOW)
        ok = r.returncode == 0
        self.note(f'{"closed" if ok else "could not close"} {exe}')
        return f'closed {name}' if ok else f'could not close {name}'

    # ---- long tasks ------------------------------------------------------------------------
    def run_task(self, name):
        if self.stopped:
            return 'stopped'
        if self.task:
            return f'already working on {self.task[0]}'
        cmd = self.cfg['tasks'].get(name.lower())
        if not cmd:
            return f'I do not know the task "{name}" (add it to apps.json)'
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, creationflags=NO_WINDOW)
        self.task = (name, p, time.time())
        self.note(f'started task {name}')
        threading.Thread(target=self._watch, args=(name, p), daemon=True).start()
        return f'working on {name}'

    def _watch(self, name, p):
        out = (p.communicate()[0] or '').strip()
        if self.task and self.task[1] is p:
            mins = (time.time() - self.task[2]) / 60
            self.task = None
            tail = out.splitlines()[-1][:80] if out else ''
            self.result = f'{name} finished in {mins:.1f} min' + (f': {tail}' if tail else '')
            self.note(self.result)

    @property
    def busy(self):
        return self.task[0] if self.task else None

    # ---- mouse + keyboard -------------------------------------------------------------------
    def move(self, x, y):
        if not self.can_touch():
            return 'not now, you are using the keyboard'
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        _send(_mouse(MOVE_ABS, int(x * 65535 / max(w - 1, 1)), int(y * 65535 / max(h - 1, 1))))
        return f'moved to {x},{y}'

    def click(self, x=None, y=None, button='left', double=False):
        if not self.can_touch():
            return 'not now, you are using the keyboard'
        if x is not None:
            self.move(int(x), int(y))
            time.sleep(0.05)
        down, up = BTN.get(button, BTN['left'])
        for _ in range(2 if double else 1):
            _send(_mouse(down), _mouse(up))
            time.sleep(0.05)
        self.note(f'{"double-" if double else ""}{button} click at {x},{y}' if x is not None else f'{button} click')
        return 'clicked'

    def type_text(self, text):
        if not self.can_touch():
            return 'not now, you are using the keyboard'
        for ch in text:
            code = ord(ch)
            _send(_key(scan=code, flags=KEYEVENTF_UNICODE),
                  _key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            time.sleep(TYPE_DELAY)
        self.note(f'typed {text[:40]!r}')
        return 'typed'

    def press(self, combo):
        """e.g. 'ctrl+s', 'enter', 'alt+tab'."""
        if not self.can_touch():
            return 'not now, you are using the keyboard'
        keys = [k.strip().lower() for k in combo.split('+')]
        vks = [VK.get(k, ord(k.upper()) if len(k) == 1 else 0) for k in keys]
        vks = [v for v in vks if v]
        if not vks:
            return f'unknown key {combo}'
        _send(*[_key(vk=v) for v in vks])
        _send(*[_key(vk=v, flags=KEYEVENTF_KEYUP) for v in reversed(vks)])
        self.note(f'pressed {combo}')
        return f'pressed {combo}'

    def read_clipboard(self):
        """Text currently on the Windows clipboard, or '' if empty/non-text. Plain ctypes,
        no new dependency (pyperclip etc.) - same approach as the rest of this file."""
        for _ in range(5):                          # another app may hold it for a moment
            if user32.OpenClipboard(0):
                break
            time.sleep(0.05)
        else:
            return ''
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ''
            ptr = kernel32.GlobalLock(h)
            text = ctypes.wstring_at(ptr) if ptr else ''
            kernel32.GlobalUnlock(h)
            return text
        finally:
            user32.CloseClipboard()

    # ---- dispatch ----------------------------------------------------------------------------
    def do(self, verb, arg=''):
        """One entry point for everything the LLM or the panel can ask for."""
        if self.stopped and verb != 'none':
            return 'I am stopped'
        try:
            if verb == 'open':   return self.open_app(arg)
            if verb == 'close':  return self.close_app(arg)
            if verb == 'run':    return self.run_task(arg)
            if verb == 'type':   return self.type_text(arg)
            if verb == 'press':  return self.press(arg)
            if verb in ('click', 'doubleclick', 'rightclick'):
                parts = arg.split()
                xy = (int(parts[0]), int(parts[1])) if len(parts) >= 2 else (None, None)
                return self.click(*xy, button='right' if verb == 'rightclick' else 'left',
                                  double=(verb == 'doubleclick'))
            if verb == 'move':
                parts = arg.split()
                return self.move(int(parts[0]), int(parts[1]))
        except Exception as e:
            self.note(f'{verb} {arg} failed: {e}')
            return f'that did not work: {e}'
        return ''
