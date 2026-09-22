"""The learning claims, each with its shuffled-wiring control. Prints the numbers; asserts
only what must hold. A null result is printed as a finding, not tuned away."""
import time
import numpy as np
from brain import Brain
from learn import Learner

LABELS = ['come', 'sleep', 'play', 'stay']
PHRASES = {
    'come':  ['come here', 'come to me', 'over here', 'get over here', 'come', 'come to my cursor'],
    'sleep': ['go to sleep', 'sleep now', 'rest', 'take a nap', 'bed time', 'go rest'],
    'play':  ['lets play', 'play with me', 'play time', 'wanna play', 'play', 'have fun'],
    'stay':  ['stay there', 'stay', 'dont move', 'hold still', 'stay put', 'freeze'],
}

def shape(kind, rng):
    img = np.zeros((16, 16))
    if kind == 'line':
        i = rng.integers(3, 13); img[i, 2:14] = 1 if rng.random() < .5 else 0; img[2:14, i] += 1 - img[i, 2:14].max()
    elif kind == 'circle':
        yy, xx = np.mgrid[:16, :16]; r = np.hypot(yy - 7.5, xx - 7.5); img[(r > 4) & (r < 6)] = 1
    elif kind == 'cross':
        img[7:9, 2:14] = 1; img[2:14, 7:9] = 1
    return np.clip(img + rng.random((16, 16)) * 0.15, 0, 1)

b = Brain(); L = Learner(b)

# 1. commands via smell -------------------------------------------------------------
L.add_task('commands', L.smell, LABELS)
kc = b.burst(L.smell('come here'), 20)[b.idx('MB_KC')].sum()
print(f'smell -> MB_KC spikes in one presentation: {int(kc)}')
assert kc > 0, 'odor never reaches the Kenyon cells: the commands task would read nothing'
t0 = time.perf_counter()
for lab, ps in PHRASES.items():
    for p in ps:
        L.teach('commands', p, lab)
print(f'taught {sum(map(len, PHRASES.values()))} phrases in {time.perf_counter() - t0:.1f}s')
real, shuf, chance = L.evaluate('commands')
print(f'commands: real wiring {real:.0%} | shuffled wiring {shuf:.0%} | chance {chance:.0%}')
assert real > chance, 'readout no better than chance'
if abs(real - shuf) < 0.1:
    print('  FINDING: real ~ shuffled -> the connectome is not contributing; the readout does the work')
print('predict "come over here" ->', L.predict('commands', 'come over here'))

# 2. shapes via the eye ---------------------------------------------------------------
rng = np.random.default_rng(1)
L.add_task('shapes', L.see, ['line', 'circle', 'cross', 'blank'], pop=L.vis)
for k in L.tasks['shapes']['labels']:
    for _ in range(8):
        L.teach('shapes', shape(k, rng), k)
real, shuf, chance = L.evaluate('shapes')
print(f'shapes:   real wiring {real:.0%} | shuffled wiring {shuf:.0%} | chance {chance:.0%}')
assert real > chance, 'readout no better than chance'

# 3. mushroom-body plasticity assay -----------------------------------------------------
mb = L.mb
print(f'KC->MBON_APP synapses: {len(mb.mask)}')
mb.enable(True)
A, B = L.smell('play'), L.smell('sleep')
a0, b0 = mb.assay(A), mb.assay(B)
for _ in range(10):
    b.burst(A, 20); mb.reward(+1)          # present A, reward it; B never rewarded
a1, b1 = mb.assay(A), mb.assay(B)
w = b.w[mb.mask]
print(f'MB: weights changed {mb.delta():.1%} | MBON_APP response A {a0:.3f}->{a1:.3f}, B {b0:.3f}->{b1:.3f} | dA-dB = {(a1 - a0) - (b1 - b0):+.4f}')
assert mb.delta() > 0 and w.min() >= 0 and w.max() <= mb.wmax + 1e-6
mb.reset(); mb.enable(False)
print('OK')
