"""Trainable tasks on the frozen brain (centered ridge readout over spike counts of a task's
readout population) and an experimental mushroom-body plasticity rule. Every accuracy comes
with its shuffled-wiring control.

Calibration facts (see scratch sweeps, Sept 2026): the real olfactory circuit's response to
any odor shares a huge common mode (cosine ~0.94 between different phrases), so features are
mean-centered before the ridge. Odors must drive whole glomeruli, not random ORNs, or the
convergent wiring maps every input to the same PN pattern."""
import csv, gzip, os, zlib
import numpy as np

K_TICKS = 20                 # ticks per presentation (2 s sim time, ~0.3 s wall)
I_SMELL, I_SEE = 0.4, 0.3
N_GLOM = 50                  # glomeruli recovered from ORN->PN convergence (real fly: ~50)
LAMBDA = 1e-2                # ridge strength, relative to feature scale
RETINA = 16                  # eye map is RETINA x RETINA
TASKS = 'data/tasks.npz'


class Learner:
    def __init__(self, brain):
        self.b = brain
        self.orn = np.concatenate([brain.idx('OLF_ORN_FOOD'), brain.idx('OLF_ORN_DANGER')])
        self.olf = np.concatenate([brain.idx('OLF_PN'), brain.idx('MB_KC')])           # smell readout
        self.vis = np.concatenate([brain.idx(g) for g in ('VIS_LO', 'VIS_LPTC', 'MB_KC', 'GNG_DESC')])  # sight readout
        self.tasks = {}
        self._glom = self._retina = None
        self.mb = MBPlasticity(brain)

    # ---- encoders: input -> per-neuron sustained input vector -------------------------
    def glomeruli(self):
        """Glomerulus id per ORN, recovered from the wiring: ORNs of one receptor type converge
        on the same projection neurons, so k-means on each ORN's PN-target vector finds them."""
        if self._glom is not None:
            return self._glom
        path = 'data/glom.npy'
        if os.path.exists(path):
            self._glom = np.load(path)
            return self._glom
        b, orn, pn = self.b, self.orn, self.b.idx('OLF_PN')
        opos, ppos = np.full(b.N, -1), np.full(b.N, -1)
        opos[orn], ppos[pn] = np.arange(len(orn)), np.arange(len(pn))
        m = (opos[b.pre] >= 0) & (ppos[b.post] >= 0)
        A = np.zeros((len(orn), len(pn)), np.float32)
        np.add.at(A, (opos[b.pre[m]], ppos[b.post[m]]), b.w[m])
        has = A.sum(1) > 0
        X = A[has] / (np.linalg.norm(A[has], axis=1, keepdims=True) + 1e-9)
        C = X[np.random.default_rng(0).choice(len(X), N_GLOM, replace=False)]
        for _ in range(30):                                            # spherical k-means
            lab = np.argmax(X @ C.T, 1)
            C = np.stack([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(N_GLOM)])
            C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
        glom = np.full(len(orn), -1)
        glom[has] = lab
        np.save(path, glom)
        self._glom = glom
        return glom

    def smell(self, text):
        """Each character bigram drives one whole glomerulus, the way an odorant drives receptor
        types: a phrase becomes a combinatorial glomerular code."""
        s, glom = np.zeros(self.b.N), self.glomeruli()
        t = ' ' + ''.join(c for c in text.lower() if c.isalnum() or c == ' ') + ' '
        for i in range(len(t) - 1):
            s[self.orn[glom == zlib.crc32(t[i:i + 2].encode()) % N_GLOM]] += I_SMELL
        return np.minimum(s, 2 * I_SMELL)

    def retina(self):
        """Grid-bin index per VIS_R1R6 neuron from its FlyWire position (PCA to 2-D). Cached."""
        if self._retina is not None:
            return self._retina
        path = 'data/retina.npy'
        if os.path.exists(path):
            self._retina = np.load(path)
            return self._retina
        rid = [r[0] for r in csv.reader(gzip.open('data/neurons.csv.gz', 'rt'))][1:]
        pos = {}
        for r in csv.reader(gzip.open('data/coordinates.csv.gz', 'rt')):
            if r[0].isdigit():
                pos[r[0]] = [float(v) for v in r[1].strip('[]').split()]
        eye = self.b.idx('VIS_R1R6')
        xyz = np.array([pos.get(rid[i], [np.nan] * 3) for i in eye])
        ok = ~np.isnan(xyz[:, 0])
        c = xyz[ok] - xyz[ok].mean(0)
        _, _, vt = np.linalg.svd(c, full_matrices=False)
        uv = c @ vt[:2].T
        q = ((uv - uv.min(0)) / (np.ptp(uv, 0) + 1e-9) * (RETINA - 1e-6)).astype(int)
        bins = np.full(len(eye), -1)
        bins[ok] = q[:, 1] * RETINA + q[:, 0]
        np.save(path, bins)
        self._retina = bins
        return bins

    def see(self, img):
        """RETINA x RETINA image (0-1) -> photoreceptors in each grid cell."""
        s = np.zeros(self.b.N)
        eye, bins = self.b.idx('VIS_R1R6'), self.retina()
        ok = bins >= 0
        s[eye[ok]] = I_SEE * np.asarray(img, float).ravel()[bins[ok]]
        return s

    # ---- features + readout -------------------------------------------------------------
    def features(self, sustained, pop):
        x = self.b.burst(sustained, K_TICKS)[pop].astype(np.float32)
        return x / (np.linalg.norm(x) + 1e-9)

    def add_task(self, name, encoder, labels, pop=None):
        pop = self.olf if pop is None else pop
        self.tasks[name] = dict(enc=encoder, labels=list(labels), pop=pop, X=[], y=[], inputs=[])

    def teach(self, name, inp, label):
        t = self.tasks[name]
        t['X'].append(self.features(t['enc'](inp), t['pop']))
        t['y'].append(t['labels'].index(label))
        t['inputs'].append(inp)

    @staticmethod
    def fit(X, y, n_classes):
        """Mean-centered ridge in the dual: an n_samples x n_samples solve instead of d x d."""
        X = np.asarray(X, np.float64)
        mu = X.mean(0)
        Xc = X - mu
        Y = np.eye(n_classes)[np.asarray(y)]
        G = Xc @ Xc.T
        lam = LAMBDA * np.trace(G) / len(X) + 1e-9
        return mu, Xc.T @ np.linalg.solve(G + lam * np.eye(len(X)), Y)

    @staticmethod
    def apply(model, x):
        mu, W = model
        return int(np.argmax((x - mu) @ W))

    def ready(self, name):
        t = self.tasks[name]
        return len(t['y']) >= 2 and len(set(t['y'])) >= 2

    def predict(self, name, inp):
        t = self.tasks[name]
        model = self.fit(t['X'], t['y'], len(t['labels']))
        return t['labels'][self.apply(model, self.features(t['enc'](inp), t['pop']))]

    def loo(self, name, X=None):
        """Leave-one-out accuracy on cached features (instant). NaN if there is no activity."""
        t = self.tasks[name]
        if not self.ready(name):
            return float('nan')
        X = np.asarray(t['X'] if X is None else X)
        if not X.any():
            return float('nan')
        y = np.array(t['y'])
        n, hits = len(y), 0
        for i in range(n):
            m = np.arange(n) != i
            hits += int(self.apply(self.fit(X[m], y[m], len(t['labels'])), X[i]) == y[i])
        return hits / n

    def evaluate(self, name):
        """(real accuracy, shuffled-wiring accuracy, chance). If real ~ shuffled, the brain
        isn't contributing and the readout is doing all the work."""
        t = self.tasks[name]
        real = self.loo(name)
        with self.b.lock:
            self.b.lesion()
            try:
                Xs = [self.features(t['enc'](i), t['pop']) for i in t['inputs']]
            finally:
                self.b.restore()
        return real, self.loo(name, Xs), 1 / len(t['labels'])

    # ---- persistence (string-input tasks + MB weights) ----------------------------------
    def save(self, path=TASKS):
        out = {}
        for n, t in self.tasks.items():
            if t['y'] and all(isinstance(i, str) for i in t['inputs']):
                out[f'{n}_X'] = np.asarray(t['X'], np.float32)
                # store label NAMES, not indices: the label list grows when apps.json is edited,
                # and indices would silently point at the wrong label after that.
                out[f'{n}_lab'] = np.asarray([t['labels'][i] for i in t['y']], str)
                out[f'{n}_in'] = np.asarray(t['inputs'], str)
        if len(self.mb.mask):
            out['mb_w'] = self.b.w[self.mb.mask]
        np.savez(path, **out)

    def load(self, path=TASKS):
        if not os.path.exists(path):
            return
        z = np.load(path)
        for n, t in self.tasks.items():
            if f'{n}_X' not in z or z[f'{n}_X'].shape[1:] != (len(t['pop']),):
                continue
            X, ins = list(z[f'{n}_X']), list(z[f'{n}_in'])
            if f'{n}_lab' in z:
                keep = [(x, t['labels'].index(str(l)), i)                 # drop labels that vanished
                        for x, l, i in zip(X, z[f'{n}_lab'], ins) if str(l) in t['labels']]
            elif f'{n}_y' in z:                                           # pre-name save files
                keep = [(x, int(y), i) for x, y, i in zip(X, z[f'{n}_y'], ins) if int(y) < len(t['labels'])]
            else:
                continue
            t['X'] = [k[0] for k in keep]
            t['y'] = [k[1] for k in keep]
            t['inputs'] = [k[2] for k in keep]
        if 'mb_w' in z and len(z['mb_w']) == len(self.mb.mask):
            self.b.w[self.mb.mask] = z['mb_w']


class MBPlasticity:
    """Reward-modulated Hebbian rule on KC->MBON_APP synapses: the fly's real learning site,
    with real dopamine cells (MB_DAN_REW) fired on reward. Experimental; prior projects report
    null results and so might we. delta() and assay() are the honest measurements.
    # note: one pathway, 3-factor rule. Upgrade = MBON_AV + DAN_PUN (0 cells in FAFB -> MaleCNS)."""
    ETA, TRACE, WMAX_X, I_DAN = 0.05, 0.9, 3.0, 1.5

    def __init__(self, b):
        self.b = b
        g = b.group_id
        self.mask = np.flatnonzero((g[b.pre] == b.groups['MB_KC']) & (g[b.post] == b.groups['MB_MBON_APP']))
        self.w0 = b.w[self.mask].copy()
        self.wmax = float(self.w0.max() * self.WMAX_X) if len(self.mask) else 0.0
        self.elig = np.zeros(len(self.mask))
        self.on, self.rewards = False, 0

    def enable(self, on=True):
        self.on = on
        self.b.hook = self._tick if on else None

    def _tick(self, b):                                  # eligibility = decaying trace of KC spikes
        self.elig = self.TRACE * self.elig + b.fired[b.pre[self.mask]]

    def reward(self, r):
        if not self.on or not len(self.mask):
            return
        with self.b.lock:
            w = self.b.w
            w[self.mask] = np.clip(w[self.mask] + self.ETA * self.w0.mean() * r * self.elig, 0, self.wmax)
        self.b.stimulate('MB_DAN_REW', self.I_DAN)
        self.rewards += 1

    def delta(self):                                     # relative change of the pathway's weights
        if not len(self.mask):
            return 0.0
        return float(np.abs(self.b.w[self.mask] - self.w0).sum() / (self.w0.sum() + 1e-9))

    def assay(self, sustained):                          # mean MBON_APP spikes/cell for one presentation
        c = self.b.burst(sustained, K_TICKS)
        return float(c[self.b.idx('MB_MBON_APP')].mean())

    def reset(self):
        self.b.w[self.mask] = self.w0
        self.elig[:] = 0
