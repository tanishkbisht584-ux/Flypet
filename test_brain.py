"""Smallest check that fails if the brain is wrong: loads, light reaches the output, activity
stays bounded, and scrambling the wiring changes the output. Prints the calibration numbers."""
import time
import numpy as np
from brain import Brain

I_LIGHT, TICKS = 0.3, 100

b = Brain()
assert (b.N, b.E) == (139255, 2698236), (b.N, b.E)

t0 = time.perf_counter()
b.set_sustained('VIS_R1R6', I_LIGHT)
for _ in range(TICKS):
    b.tick()
hz = TICKS / (time.perf_counter() - t0)
real = b.total('GNG_DESC')
top = np.argsort(b.group_spikes)[::-1][:8]
print(f'{hz:.1f} ticks/s | fired total {int(b.counts.sum()):,} | last tick {int(b.group_spikes.sum()):,}')
print('GNG_DESC', real, '| MB_KC', b.total('MB_KC'), '| MB_MBON_APP', b.total('MB_MBON_APP'))
print('busiest groups:', ', '.join(f'{b.names[g]}={b.group_spikes[g]}' for g in top if b.group_spikes[g]))
assert real > 0, 'light never reached the descending neurons'

for stop in (300, 500):                                   # stability: no runaway over a longer run
    while b.counts.sum() and TICKS < stop:
        b.tick(); TICKS += 1
    print(f'tick {stop}: {int(b.group_spikes.sum()):,} fired ({b.group_spikes.sum() / b.N:.1%} of brain)')
assert b.group_spikes.sum() < 0.3 * b.N, 'runaway excitation'

b.reset(); b.counts[:] = 0; b.lesion()
b.set_sustained('VIS_R1R6', I_LIGHT)
for _ in range(100):
    b.tick()
shuf = b.total('GNG_DESC')
delta = abs(shuf - real) / max(real, 1)
print(f'lesioned GNG_DESC {shuf}  (delta {delta:.0%} vs real wiring)')
assert delta > 0.2, 'scrambling the wiring made no difference to the output'
b.restore()
print('OK')
