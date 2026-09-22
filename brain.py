"""Headless leaky integrate-and-fire sim over the FlyWire FAFB v783 connectome.

Port of snedea/flybrain js/sim-worker.js tick(), numpy-vectorized. No CSR, no group
sorting, no group gating: those were JS-loop optimizations; numpy does the whole
139K-neuron array in one op, and dropping gating lets silent regions be reached.
"""
import gzip, json, threading, time
import numpy as np

LEAK, THRESH, REFRAC, WEIGHT_SCALE, TICK_HZ = 0.95, 1.0, 3, 0.15, 10
GAIN = 100  # global synaptic gain. Upstream's max-normalization leaves the median neuron with
            # 0.006 total input vs threshold 1.0 -> nothing past the photoreceptors ever fires.
            # Swept in test_brain: 100 = light reaches GNG_DESC, KCs sparse, no runaway.


class Brain:
    def __init__(self, bin_path='data/connectome.bin.gz', meta_path='data/neuron_meta.json', gain=GAIN):
        buf = gzip.open(bin_path).read()
        self.N, self.E = (int(x) for x in np.frombuffer(buf, '<u4', 2, 0))
        edges = np.frombuffer(buf, [('pre', '<u4'), ('post', '<u4'), ('w', '<f4')], self.E, 8)
        meta = np.frombuffer(buf, [('region', 'u1'), ('group', '<u2')], self.N, 8 + 12 * self.E)
        self.pre, self.post = edges['pre'].copy(), edges['post'].copy()
        self._post = self.post                                  # pristine wiring; lesion() swaps self.post
        self.w = (edges['w'] / np.abs(edges['w']).max() * WEIGHT_SCALE * gain).astype(np.float32)
        self.group_id = meta['group'].astype(np.int64)
        m = json.load(open(meta_path))
        self.groups = {g['name']: g['id'] for g in m['groups']}
        self.names = [g['name'] for g in m['groups']]
        self.n_groups = m['group_count']
        sizes = np.bincount(self.group_id, minlength=self.n_groups)
        assert sizes.tolist() == m['group_sizes'], 'binary layout does not match neuron_meta.json'
        self.size = np.maximum(sizes, 1)                        # avoid /0 for empty groups
        self._idx = {}
        self.counts = np.zeros(self.N, np.int32)                # per-neuron spike accumulator (features)
        self.rates = np.zeros(self.n_groups)                    # EMA spikes/tick/neuron per group
        self._pending = []                                      # (group, intensity) from other threads
        self.lock = threading.RLock()
        self.hook = None                                        # optional per-tick callback (plasticity)
        self.hz, self.running = 0.0, False
        self.reset()

    # ---- state ------------------------------------------------------------
    def reset(self):
        self.V = np.zeros(self.N)
        self.fired = np.zeros(self.N, bool)
        self.refrac = np.zeros(self.N, np.int8)
        self.sustained = np.zeros(self.N)
        self.group_spikes = np.zeros(self.n_groups, np.int64)

    def idx(self, group):
        if group not in self._idx:
            self._idx[group] = np.flatnonzero(self.group_id == self.groups[group])
        return self._idx[group]

    def stimulate(self, group, intensity):      # transient; thread-safe (drained at tick start)
        self._pending.append((group, intensity))

    def set_sustained(self, group, intensity):  # applied every tick until changed
        self.sustained[self.idx(group)] = intensity

    # ---- the model --------------------------------------------------------
    def tick(self):
        V, refrac = self.V, self.refrac
        while self._pending:
            g, I = self._pending.pop()
            V[self.idx(g)] += I
        rf = refrac > 0
        V[rf] = 0
        refrac[rf] -= 1
        V *= LEAK
        V += self.sustained
        m = self.fired[self.pre]                                 # edges whose source fired last tick
        V += np.bincount(self.post[m], weights=self.w[m], minlength=self.N)
        fired = (V >= THRESH) & (refrac == 0)
        V[fired] = 0
        refrac[fired] = REFRAC
        self.fired = fired
        self.counts += fired
        gs = np.bincount(self.group_id[fired], minlength=self.n_groups)
        self.group_spikes = gs
        self.rates = 0.9 * self.rates + 0.1 * gs / self.size
        if self.hook:
            self.hook(self)
        return gs

    def rate(self, group):
        return float(self.rates[self.groups[group]])

    def total(self, group):
        return int(self.counts[self.idx(group)].sum())

    # ---- the control experiment ------------------------------------------
    def lesion(self):
        """Shuffle targets: same in/out degrees, wiring destroyed."""
        self.post = self._post.copy()
        np.random.default_rng(0).shuffle(self.post)

    def restore(self):
        self.post = self._post

    @property
    def lesioned(self):
        return self.post is not self._post

    # ---- running ------------------------------------------------------------
    def burst(self, sustained, n):
        """Run n ticks from a clean state with a given per-neuron sustained input; return
        per-neuron spike counts. Live state is saved and restored, so the fly resumes unbothered."""
        with self.lock:
            saved = (self.V, self.fired, self.refrac, self.sustained, self.counts, self.rates)
            self.reset()
            self.counts, self.rates = np.zeros(self.N, np.int32), np.zeros(self.n_groups)
            self.sustained = sustained
            for _ in range(n):
                self.tick()
            out = self.counts
            self.V, self.fired, self.refrac, self.sustained, self.counts, self.rates = saved
        return out

    def _loop(self):
        period, last = 1.0 / TICK_HZ, time.perf_counter()
        while self.running:
            t0 = time.perf_counter()
            with self.lock:
                self.tick()
            now = time.perf_counter()
            self.hz = 0.9 * self.hz + 0.1 / max(now - last, 1e-6)
            last = now
            time.sleep(max(0.0, period - (now - t0)))

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
