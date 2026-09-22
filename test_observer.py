"""Checks for observer.py: clipboard redaction, encryption at rest, retention.
Uses obviously-synthetic placeholder strings (all zeros / repeated chars) to exercise the
redaction regexes without embedding realistic-looking credentials."""
import os
import time
import observer
from observer import _redact, _Vault

FAILS = []
def check(name, cond):
    print(('OK  ' if cond else 'FAIL') + '  ' + name)
    if not cond:
        FAILS.append(name)

# 1. secret-SHAPED placeholder strings are redacted before storage
check('key-shape redacted', '[REDACTED]' in _redact('key sk-' + 'x' * 20))
check('long-token-shape redacted', '[REDACTED]' in _redact('t ' + 'z' * 40))
check('number-shape redacted', '[REDACTED]' in _redact('n 0000 0000 0000 0000'))
check('hex-shape redacted', '[REDACTED]' in _redact('h ' + 'a' * 32))
check('normal text survives', _redact('copied a phone number 555 1234') != '[REDACTED]')

# 2. encryption at rest: plaintext not on disk; needs the right passphrase
TESTF = 'test_activity.enc'
if os.path.exists(TESTF):
    os.remove(TESTF)
v = _Vault(TESTF, 'test-passphrase-one')
canary = 'PLAINTEXT_CANARY_STRING'
v.append([{'t': time.time(), 'type': 'clip', 'text': canary}])
disk = open(TESTF, 'rb').read()
check('plaintext not on disk', canary.encode() not in disk)
check('right passphrase decrypts', any(r.get('text') == canary for r in v.read_all()))
check('wrong passphrase yields nothing', _Vault(TESTF, 'other-passphrase').read_all() == [])

# 3. retention prune drops old rows, keeps recent
now = time.time()
v.rewrite([
    {'t': now - 10 * 86400, 'type': 'clip', 'text': 'old_entry'},
    {'t': now - 3600, 'type': 'clip', 'text': 'new_entry'},
])
obs = observer.Observer()
obs.vault = v
texts = [r['text'] for r in obs._prune()]
check('old row pruned', 'old_entry' not in texts)
check('recent row kept', 'new_entry' in texts)

os.remove(TESTF)
if os.path.exists(TESTF + '.tmp'):
    os.remove(TESTF + '.tmp')

print()
print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
raise SystemExit(1 if FAILS else 0)
