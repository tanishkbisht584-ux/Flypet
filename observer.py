"""Background activity observer for FlyPet — the "learns what I'm doing" layer.

Deliberately NOT a keylogger. It records only:
  * which app/window is focused over time (GetForegroundWindow + title)
  * clipboard changes (what you copy), with secrets redacted at capture

That is enough for the fly to know what you're doing ("editing fly.py in VS Code", "reading
gmail", "copied a phone number") without capturing keystrokes, passwords, or field content.

Still built defensively, because clipboard content can be sensitive:
  * OFF by default; started explicitly with a passphrase each session.
  * Secret-shaped clipboard strings are redacted AT CAPTURE, before touching disk.
  * Everything written is AES-GCM encrypted at rest (scrypt-derived key; passphrase never stored).
  * Auto-deletes entries older than RETENTION_DAYS; wipe() purges instantly. Local file only.
  # note: encryption-at-rest, not in-use. The rolling summary is plaintext by necessity
  #           (the LLM must read it). This protects the log at rest, not a live-compromised box.

Only the ROLLING SUMMARY (a short LLM-condensed note) feeds the fly's reasoning — never the raw
event log. See summarize().
"""
import ctypes
import json
import os
import re
import threading
import time
from base64 import b64encode, b64decode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

user32 = ctypes.windll.user32

RAW_LOG = 'activity.enc'
SUMMARY_FILE = 'activity_summary.txt'
RETENTION_DAYS = 3
POLL_S = 2.0
CLIP_POLL_S = 1.5
SUMMARY_EVERY_S = 600
FLUSH_EVERY = 25

SECRET_PATTERNS = [
    re.compile(r'sk-[A-Za-z0-9]{16,}'),                 # OpenAI-style keys
    re.compile(r'\b[A-Za-z0-9_-]{32,}\b'),              # long high-entropy tokens
    re.compile(r'\b(?:\d[ -]?){13,19}\b'),              # card numbers
    re.compile(r'\b[A-Fa-f0-9]{32,}\b'),               # hex secrets / hashes
]


def _redact(text):
    for p in SECRET_PATTERNS:
        text = p.sub('[REDACTED]', text)
    return text


def _derive_key(passphrase, salt):
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(passphrase.encode())


class _Vault:
    """Append-only encrypted store. Each line is base64(salt|nonce|ciphertext) of one JSON record,
    self-contained so we append without re-encrypting the whole file."""
    def __init__(self, path, passphrase):
        self.path = path
        self.pw = passphrase

    def append(self, records):
        with open(self.path, 'a', encoding='utf-8') as f:
            for rec in records:
                salt = os.urandom(16)
                nonce = os.urandom(12)
                key = _derive_key(self.pw, salt)
                ct = AESGCM(key).encrypt(nonce, json.dumps(rec).encode(), None)
                f.write(b64encode(salt + nonce + ct).decode() + '\n')

    def read_all(self):
        if not os.path.exists(self.path):
            return []
        out = []
        for line in open(self.path, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                blob = b64decode(line)
                salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
                key = _derive_key(self.pw, salt)
                out.append(json.loads(AESGCM(key).decrypt(nonce, ct, None)))
            except Exception:
                continue          # wrong passphrase or corrupt line -> skip, don't crash
        return out

    def rewrite(self, records):
        tmp = self.path + '.tmp'
        open(tmp, 'w').close()
        _Vault(tmp, self.pw).append(records)
        os.replace(tmp, self.path)


class Observer:
    def __init__(self, ask_llm=None):
        """ask_llm(prompt)->str is injected (voice.py) so observer has no LLM dependency itself."""
        self.ask_llm = ask_llm
        self.on = False
        self.vault = None
        self.buf = []
        self.summary = self._load_summary()
        self.llm_ok = True            # flips False if a summary call can't reach the LLM
        self._last_win = ''
        self._last_clip = ''
        self._last_summary_t = 0
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self, passphrase):
        if self.on:
            return
        if not passphrase:
            raise ValueError('passphrase required to start the observer')
        self.vault = _Vault(RAW_LOG, passphrase)
        self.on = True
        self._last_summary_t = time.time()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        if not self.on:
            return
        self.on = False
        self._flush()

    def wipe(self):
        with self._lock:
            self.buf = []
        if os.path.exists(RAW_LOG):
            os.remove(RAW_LOG)
        self.summary = ''
        if os.path.exists(SUMMARY_FILE):
            os.remove(SUMMARY_FILE)

    # ---- capture ---------------------------------------------------------------------------
    def _foreground_title(self):
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value or ''

    def _read_clipboard(self):
        # reuse hands.read_clipboard if available; import lazily to avoid a hard dependency
        try:
            import hands
            return hands.Hands.read_clipboard(hands.Hands.__new__(hands.Hands))
        except Exception:
            return ''

    def _loop(self):
        clip_t = 0
        while self.on:
            title = self._foreground_title()
            if title != self._last_win:
                self._record({'t': time.time(), 'type': 'window', 'title': title})
                self._last_win = title
            if time.time() - clip_t > CLIP_POLL_S:
                clip_t = time.time()
                clip = self._read_clipboard()
                if clip and clip != self._last_clip:
                    self._last_clip = clip
                    text = _redact(clip.strip())[:300]
                    if text and text != '[REDACTED]':
                        self._record({'t': time.time(), 'type': 'clip', 'win': self._last_win, 'text': text})
            if time.time() - self._last_summary_t > SUMMARY_EVERY_S:
                self.summarize()
            time.sleep(POLL_S)

    # ---- storage ---------------------------------------------------------------------------
    def _record(self, rec):
        with self._lock:
            self.buf.append(rec)
            if len(self.buf) >= FLUSH_EVERY:
                self._flush_locked()

    def _flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if self.buf and self.vault:
            self.vault.append(self.buf)
        self.buf = []

    def _prune(self):
        if not self.vault:
            return []
        cutoff = time.time() - RETENTION_DAYS * 86400
        recs = [r for r in self.vault.read_all() if r.get('t', 0) >= cutoff]
        self.vault.rewrite(recs)
        return recs

    # ---- rolling summary (the ONLY thing that feeds the fly) --------------------------------
    def _load_summary(self):
        return open(SUMMARY_FILE, encoding='utf-8').read() if os.path.exists(SUMMARY_FILE) else ''

    def summarize(self):
        self._last_summary_t = time.time()
        self._flush()
        recs = self._prune()
        recent = recs[-120:]
        if not recent or not self.ask_llm:
            return self.summary
        lines = []
        for r in recent:
            hhmm = time.strftime('%H:%M', time.localtime(r['t']))
            if r['type'] == 'window':
                lines.append(f'[{hhmm}] window: {r["title"]}')
            elif r['type'] == 'clip':
                lines.append(f'[{hhmm}] copied: {r["text"]}')
        prompt = ('Condense this recent computer-activity log into 3-4 short sentences describing '
                  'what the user has been doing. Be factual, no speculation.\n\n' + '\n'.join(lines))
        try:
            self.summary = self.ask_llm(prompt).strip()
            self.llm_ok = True
            with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
                f.write(self.summary)
        except Exception:
            self.llm_ok = False       # LLM unreachable — panel shows this instead of "nothing yet"
        return self.summary
