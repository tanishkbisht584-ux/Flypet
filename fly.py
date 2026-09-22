"""FlyPet: a fly on your desktop with a real connectome brain, affective states, memory,
trainable commands, a local-LLM voice, and hands that act on the machine.

KILL SWITCH: Ctrl+Alt+F, or the STOP button in the panel. Stops every action instantly.
Run:  python fly.py"""
import collections, ctypes, json, math, os, random, threading, time
import tkinter as tk
from tkinter import messagebox, simpledialog
from brain import Brain
from senses import Senses
from learn import Learner
import hands as hands_mod
import observer as observer_mod
import sprite, voice

os.chdir(os.path.dirname(os.path.abspath(__file__)))
user32 = ctypes.windll.user32

# ---- knobs ---------------------------------------------------------------------------------
I_LIGHT, I_TOUCH, I_THERMO, I_DRIVE, I_SWEET = 0.3, 1.5, 0.3, 0.3, 1.5
AROUSAL_REF = 0.03                # GNG_DESC spikes/tick/cell that counts as fully aroused (at GAIN 100)
TOUCH_R, FEED_R, FLEE_R = 80, 25, 250
HUNGER_RATE, HUNGER_NEGLECT_MAX = 0.004, 2.5      # ~4 min to hungry; at most 2.5x when neglected
FATIGUE_UP, FATIGUE_DOWN, FATIGUE_IDLE = 0.002, 0.012, 0.003
SLEEP_DARK, SLEEP_BRIGHT, WAKE_AT = 0.45, 0.90, 0.25   # sleep threshold: dark -> bright, and wake-up
FEAR_HIT, FEAR_DECAY, CURIO_RATE, CURIO_SPEND = 0.3, 0.98, 0.005, 0.01
OVERRIDE_S, THOUGHT_GAP_S, SAVE_S = 10, 90, 30
BEHAVIORS = ['come', 'sleep', 'play', 'stay']
# Plain-English verbs -> hand verbs, so "launch notepad" and "open notepad" both work without
# involving the LLM. Deterministic, instant, and still fine when Ollama is down.
VERB_WORDS = {'open': 'open', 'launch': 'open', 'start': 'open', 'run': 'run', 'do': 'run',
              'execute': 'run', 'close': 'close', 'quit': 'close', 'kill': 'close', 'stop': 'close'}


def all_labels(cfg):
    """Everything the fly can be TAUGHT: body behaviours plus one label per app and task."""
    return (BEHAVIORS + [f'open {a}' for a in cfg.get('apps', {})]
            + [f'run {t}' for t in cfg.get('tasks', {})])


def parse_direct(text, cfg):
    """Exact command match, no LLM. -> (verb, arg) or None."""
    t = ' '.join(text.lower().split())
    if not t:
        return None
    if t in ('reply', 'draft reply', 'draft a reply', 'draft'):
        return 'draft', ''
    for word, verb in VERB_WORDS.items():
        if word not in t.split():
            continue
        table = cfg.get('tasks' if verb == 'run' else 'apps', {})
        hit = [n for n in table if n in t]
        if hit:
            return verb, max(hit, key=len)          # longest name wins ("task manager" > "task")
    for kind in ('type', 'press'):                  # "type hello world" / "press ctrl+s"
        if t.startswith(kind + ' '):
            return kind, text.split(None, 1)[1]
    if t.startswith('click ') or t.startswith('move '):
        return t.split()[0], ' '.join(t.split()[1:])
    return None
SPEED = dict(startle=12, flee=6, come=4, play=5, seek=2.5, wander=1.5, phototaxis=2, work=0.8)  # base px/frame
URGE_GAP_S, URGE_IDLE_S, URGE_CURIOSITY = 120, 180, 0.7   # autonomy: only when truly bored and you are away
# saccadic-motion tuning (real flies: straight glides + quick ~15-40 deg turns, stop-and-go)
TURN_EASE, SPEED_EASE = 0.28, 0.15        # per-frame lerp toward heading/speed targets
SACCADE_MIN, SACCADE_MAX = 0.6, 1.6       # seconds between wander turns
SACCADE_DEG = (15, 40)                     # turn magnitude range
MEM, KEY, SIZE = 'memory.json', '#ff00ff', sprite.SIZE

# ---- matrix theme ---------------------------------------------------------------------------
BG, PANEL, FG = '#000000', '#050d05', '#00ff41'      # phosphor green on black
DIM, DARK, HOT, PALE = '#0a7a24', '#04340f', '#ff3131', '#b9ffc8'
MONO, MONO_S, MONO_B = ('Consolas', 9), ('Consolas', 7), ('Consolas', 9, 'bold')
GLYPHS = '01ABCDEF<>/\\|=+*#$%&'
RAIN_W, RAIN_H, RAIN_COLS, RAIN_ROWS = 336, 54, 28, 6


def theme(root):
    """One block instead of per-widget kwargs. Classic tk widgets honour these; must run
    before any widget is created."""
    o = root.option_add
    for pat, val in (('*background', BG), ('*foreground', FG), ('*font', MONO),
                     ('*highlightBackground', BG), ('*highlightColor', DIM),
                     ('*activeBackground', DARK), ('*activeForeground', PALE),
                     ('*Entry.background', PANEL), ('*Entry.foreground', PALE),
                     ('*Entry.insertBackground', FG), ('*Entry.relief', 'solid'),
                     ('*Entry.borderWidth', 1), ('*Button.relief', 'solid'),
                     ('*Button.borderWidth', 1), ('*Button.background', PANEL),
                     ('*Checkbutton.selectColor', DARK), ('*Menubutton.background', PANEL),
                     ('*Menu.background', PANEL), ('*Menu.foreground', FG),
                     ('*Menu.activeBackground', DIM), ('*Menu.activeForeground', BG),
                     ('*Labelframe.foreground', DIM), ('*Labelframe.font', MONO_S)):
        o(pat, val)
    root.configure(bg=BG)


def rule(text, width=46):
    return f'-- {text} '.ljust(width, '-')


def bar(v, n=16):
    return '[' + '#' * round(v * n) + '.' * (n - round(v * n)) + ']'


class Rain:
    """Matrix rain whose fall speed is the fly's real neural activity (GNG_DESC rate).
    Flicker-free: one persistent text item per cell, updated via itemconfig, never delete-all."""
    TRAIL = (PALE, FG, FG, DIM, DIM, DARK)                # head bright -> tail fading

    def __init__(self, cv):
        self.cv = cv
        r = random.Random(7)
        self.y = [r.uniform(0, RAIN_ROWS) for _ in range(RAIN_COLS)]
        self.v = [r.uniform(0.4, 1.2) for _ in range(RAIN_COLS)]
        self.rng = r
        cw, chh = RAIN_W / RAIN_COLS, RAIN_H / RAIN_ROWS
        self.cell = [[cv.create_text(i * cw + cw / 2, row * chh + chh / 2, font=MONO_S,
                                     fill=BG, text='') for row in range(RAIN_ROWS)]
                     for i in range(RAIN_COLS)]           # created ONCE

    def step(self, arousal):
        cv, r = self.cv, self.rng
        for i in range(RAIN_COLS):
            self.y[i] += self.v[i] * (0.35 + 2.2 * arousal)
            if self.y[i] > RAIN_ROWS + 4:
                self.y[i], self.v[i] = -r.uniform(0, 1.5), r.uniform(0.4, 1.2)
            head = int(self.y[i])
            for row in range(RAIN_ROWS):
                k = head - row                            # 0 = head, grows down the fading trail
                if 0 <= k < len(self.TRAIL):
                    cv.itemconfig(self.cell[i][row], fill=self.TRAIL[k],
                                  text=GLYPHS[r.randrange(len(GLYPHS))])
                else:
                    cv.itemconfig(self.cell[i][row], fill=BG)


class Fly:
    def __init__(self):
        self.brain, self.senses = Brain(), Senses()
        self.learn = Learner(self.brain)
        self.hands = hands_mod.Hands(self.senses)
        # observer summarizes via the LLM synchronously; inject a plain prompt->text call so
        # observer.py stays free of any voice/LLM import of its own.
        self.observer = observer_mod.Observer(
            ask_llm=lambda p: voice._post([{'role': 'user', 'content': p}], max_tokens=120))
        self.labels = all_labels(self.hands.cfg)          # behaviours + every app and task
        self.learn.add_task('commands', self.learn.smell, self.labels)
        self.d = dict(hunger=0.3, fear=0.0, fatigue=0.2, curiosity=0.5)
        self.traits, self.lifetime, self.fed, self.startled = None, 0.0, 0, 0
        self.events = collections.deque(maxlen=50)
        self.want_speak = False
        self.load()
        if not self.traits:                               # a new fly: draw its personality once
            r = random.Random()
            self.traits = {k: round(r.uniform(0.5, 1.5), 2) for k in ('timidity', 'sociability', 'restlessness')}
            self.event('hatched')
        self.sw, self.sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        cx, cy = self.senses.cursor()                       # hatch away from the cursor, not on it
        self.x = self.sw * (0.25 if cx > self.sw / 2 else 0.75)
        self.y, self.heading = self.sh * 0.5, 0.0
        # saccadic motion state: flies glide straight, then turn in quick discrete saccades
        self.heading_target, self.speed, self.speed_target = 0.0, 0.0, 0.0
        self.next_saccade_t, self.pause_until = 0.0, 0.0
        self.state, self.arousal, self.approach = 'idle', 0.0, 0.0
        self.override, self.override_until = None, 0.0
        self.pokes, self.touching, self.feed_since, self.startle_t = [], False, None, 0.0
        self.last_speak, self.last_save = time.time(), time.time()
        self.bubble, self.bubble_until, self.voice_src = '', 0.0, 'template'
        self.acc = 'accuracy: teach at least 2 different commands'
        self.gait, self.airborne, self.flying, self.fly_until, self.fly_since, self.landed_at = 0.0, 0.0, False, 0.0, 0.0, 0.0
        self.autonomy, self.last_urge = False, time.time()
        self.brain.start()

    # ---- memory ------------------------------------------------------------------------
    def load(self):
        if os.path.exists(MEM):
            m = json.load(open(MEM))
            self.d.update(m.get('drives', {}))
            self.traits = m.get('traits')
            self.lifetime, self.fed, self.startled = m.get('lifetime', 0), m.get('fed', 0), m.get('startled', 0)
            self.events.extend(m.get('events', []))
            away = max(0.0, time.time() - m.get('saved_at', time.time()))   # it slept while closed
            if away > 30:
                self.d['fatigue'] = max(0.0, self.d['fatigue'] - away * FATIGUE_DOWN * 0.25)
                self.d['fear'] = 0.0
                self.event(f'woke after {int(away // 60)} min away')
        self.learn.load()

    def save(self):
        json.dump(dict(drives=self.d, traits=self.traits, lifetime=self.lifetime, fed=self.fed,
                       startled=self.startled, saved_at=time.time(), events=list(self.events)),
                  open(MEM, 'w'), indent=1)
        self.learn.save()
        self.last_save = time.time()

    def event(self, msg, speak=False):
        self.events.append(f'{time.strftime("%H:%M")} {msg}')
        self.want_speak |= speak

    # ---- affect ------------------------------------------------------------------------
    def startle(self, why):
        self.startle_t = time.time()
        self.d['fear'] = min(1.0, self.d['fear'] + FEAR_HIT * self.traits['timidity'])
        self.startled += 1
        self.brain.stimulate('MECH_BRISTLE', I_TOUCH * 2)
        self.event(f'startled: {why}', speak=True)

    def eat(self):
        self.d['hunger'] = max(0.0, self.d['hunger'] - 0.3)
        self.brain.stimulate('GUS_GRN_SWEET', I_SWEET)
        self.learn.mb.reward(+1)
        self.fed += 1
        self.feed_since = None
        self.event('fed by cursor', speak=True)

    def set_override(self, action, source):
        if action in (None, 'none'):
            return
        if action == 'come' and self.d['fear'] > 0.6:         # the drives can veto the mind
            self.event(f'{source} said come; too scared')
            return
        self.override, self.override_until = action, time.time() + OVERRIDE_S
        self.event(f'{source} -> {action}')

    # ---- one frame ---------------------------------------------------------------------
    def step(self, dt):
        b, s, d, now = self.brain, self.senses, self.d, time.time()
        self.lifetime += dt
        cx, cy = s.cursor()
        dist = math.hypot(cx - self.x, cy - self.y)
        near = dist < TOUCH_R
        if near:
            b.stimulate('MECH_BRISTLE', I_TOUCH * (1 - dist / TOUCH_R))
            if not self.touching:                                 # a poke = a touch onset
                self.pokes = [t for t in self.pokes if now - t < 4] + [now]
                if len(self.pokes) >= 3:
                    self.pokes.clear()
                    self.startle('poked 3 times fast')
        self.touching = near
        if dist < FEED_R and d['hunger'] > 0.3:
            self.feed_since = self.feed_since or now
            if now - self.feed_since > 1:
                self.eat()
        else:
            self.feed_since = None

        # drives (0-1)
        # Neglect makes it hungrier, but CAPPED: an uncapped 1+idle/60 reaches 30x after half an
        # hour away and saturates hunger in seconds, so the fly is always starving when you return.
        d['hunger'] = min(1.0, d['hunger'] + HUNGER_RATE * dt * min(HUNGER_NEGLECT_MAX, 1 + s.idle / 300))
        # Sleep pressure: discharges while asleep, recovers a little while merely idle, builds
        # otherwise. It must be able to fall on a bright screen, or it pins at 1.0 forever.
        df = (-FATIGUE_DOWN if self.state == 'rest' else
              -FATIGUE_IDLE if self.state == 'idle' else FATIGUE_UP)
        d['fatigue'] = min(1.0, max(0.0, d['fatigue'] + df * dt))
        d['fear'] *= FEAR_DECAY ** dt
        # Curiosity builds whenever the fly is calm and is spent by actually exploring, so it can
        # reach the level autonomy needs instead of draining during ordinary wandering.
        dc = -CURIO_SPEND if self.state in ('phototaxis', 'play') else (CURIO_RATE if d['fear'] < 0.3 else 0.0)
        d['curiosity'] = min(1.0, max(0.0, d['curiosity'] + dc * dt))

        # the machine, into the fly's real sensory neurons
        b.set_sustained('VIS_R1R6', I_LIGHT * s.light)
        b.set_sustained('THERMO_WARM', I_THERMO * max(0.0, (s.cpu - 60) / 40))
        b.set_sustained('THERMO_COOL', I_THERMO * max(0.0, (15 - s.cpu) / 15))
        b.set_sustained('DRIVE_HUNGER', I_DRIVE * d['hunger'])
        b.set_sustained('DRIVE_FATIGUE', I_DRIVE * d['fatigue'])

        # neural readouts
        self.arousal = min(1.0, b.rate('GNG_DESC') / AROUSAL_REF)
        self.approach = min(1.0, b.rate('MB_MBON_APP') / AROUSAL_REF) if self.learn.mb.on else 0.0

        # behavior: priority chain (shape mirrors upstream fly-logic.js)
        ov = self.override if now < self.override_until else None
        center = math.hypot(self.sw / 2 - self.x, self.sh / 2 - self.y)
        # Homeostatic + circadian: a dark screen makes it sleepy early, a bright one holds it up
        # longer, but enough sleep pressure always wins. Hysteresis stops rest/wake flickering.
        sleepy = SLEEP_DARK + (SLEEP_BRIGHT - SLEEP_DARK) * s.light
        resting = d['fatigue'] > sleepy or (self.state == 'rest' and d['fatigue'] > WAKE_AT)
        if now - self.startle_t < 0.5:                        st = 'startle'
        elif d['fear'] > 0.3 and dist < FLEE_R:               st = 'flee'
        elif ov:                                              st = {'sleep': 'rest', 'explore': 'wander'}.get(ov, ov)
        elif self.hands.busy:                                 st = 'work'
        elif self.feed_since:                                 st = 'feed'
        elif resting:                                         st = 'rest'
        elif s.light > 0.5 and d['curiosity'] > 0.6 and center > 150: st = 'phototaxis'
        elif d['hunger'] > 0.5 or self.approach > 0.3:        st = 'seek'
        elif d['curiosity'] > 0.3:                            st = 'wander'
        else:                                                 st = 'idle'
        if st != self.state:
            if st == 'rest':
                self.event('fell asleep', speak=True)
            self.state = st

        # a long task finished: fly to the human and report
        if self.hands.result:
            self.say(self.hands.result, 10)
            self.hands.result = None
            self.event('task done')
            self.set_override('come', 'task')

        # autonomous urge: only when bored, only while you are away, never afraid or busy
        if (self.autonomy and not self.hands.stopped and not self.hands.busy
                and now - self.last_urge > URGE_GAP_S and d['curiosity'] > URGE_CURIOSITY
                and s.idle > URGE_IDLE_S and d['fear'] < 0.3):
            self.last_urge = now
            voice.decide_async(self.state_block(), self.hands.cfg.get('autonomous', []),
                               lambda v, a: self.act(v, a, 'urge'))

        # flight: giant-fibre escape (real fly biology) or a long trip; never while resting
        tgt = center if st == 'phototaxis' else dist
        if not self.flying and now - self.landed_at > 3 and self.arousal > 0.5 and (st in ('startle', 'flee') or (st in ('come', 'seek', 'phototaxis') and tgt > 400)):
            self.flying, self.fly_until, self.fly_since = True, now + 6, now
            self.event('took off')
        elif self.flying and now - self.fly_since > 1.5 and (st not in ('startle', 'flee', 'come', 'seek', 'phototaxis') or tgt < 60 or now > self.fly_until):
            self.flying, self.landed_at = False, now
            self.event('landed')
        self.airborne = min(1.0, max(0.0, self.airborne + (4 * dt if self.flying else -4 * dt)))

        # movement: speed is neural (arousal); heading turns in discrete saccades, both eased.
        # Real flies glide straight then flick a quick ~15-40deg turn, not per-frame wobble.
        # note: brain-only connectome has no lateralized descending neurons, so heading can't
        # be read from spikes. Upgrade path: MaleCNS/BANC include the nerve cord + left/right DNs.
        target_speed = SPEED.get(st, 0) * (0.4 + self.arousal) * self.traits['restlessness'] * 60 * dt
        if st == 'seek':
            target_speed *= self.traits['sociability']
        if self.flying:
            target_speed *= 3
        if now < self.pause_until:                        # stop-and-go: brief natural pauses
            target_speed = 0.0

        away = math.atan2(self.y - cy, self.x - cx)
        if st in ('startle', 'flee'):                     # directed: aim heading_target, no wobble
            self.heading_target = away + random.uniform(-0.3, 0.3)
        elif st in ('come', 'seek'):
            self.heading_target = away + math.pi
        elif st == 'phototaxis':
            self.heading_target = math.atan2(self.sh / 2 - self.y, self.sw / 2 - self.x)
        elif st == 'play':
            self.heading_target += 0.18                   # gentle continuous circling
        elif now >= self.next_saccade_t:                  # wander/idle: occasional quick turn
            self.heading_target = self.heading + math.radians(
                random.choice((-1, 1)) * random.uniform(*SACCADE_DEG))
            self.next_saccade_t = now + random.uniform(SACCADE_MIN, SACCADE_MAX)
            if random.random() < 0.25:                    # sometimes pause after a turn
                self.pause_until = now + random.uniform(0.3, 1.0)

        # ease heading (angular lerp, shortest way round) and speed toward their targets
        dth = (self.heading_target - self.heading + math.pi) % (2 * math.pi) - math.pi
        self.heading += dth * TURN_EASE
        self.speed += (target_speed - self.speed) * SPEED_EASE
        self.x += math.cos(self.heading) * self.speed
        self.y += math.sin(self.heading) * self.speed
        if not 60 < self.x < self.sw - 60:               # bounce = new target, not an instant flip
            self.heading_target = math.pi - self.heading_target
        if not 60 < self.y < self.sh - 60:
            self.heading_target = -self.heading_target
        self.x, self.y = min(max(self.x, 60), self.sw - 60), min(max(self.y, 60), self.sh - 60)
        if not self.flying:
            self.gait += self.speed * 0.25               # legs cycle with distance walked

        if self.want_speak and now - self.last_speak > THOUGHT_GAP_S:   # a thought, rate-limited
            self.want_speak = False
            self.speak()
        if now - self.last_save > SAVE_S:
            self.save()

    # ---- voice, commands, teaching -----------------------------------------------------
    def state_block(self):
        s, d, t = self.senses, self.d, self.learn.tasks['commands']
        return dict(hunger=round(d['hunger'], 2), fear=round(d['fear'], 2), fatigue=round(d['fatigue'], 2),
                    curiosity=round(d['curiosity'], 2), arousal=round(self.arousal, 2), behavior=self.state,
                    light=round(s.light, 2), cpu=int(s.cpu), idle_s=int(s.idle), cursor_near=self.touching,
                    lifetime_min=int(self.lifetime // 60), times_fed=self.fed, times_startled=self.startled,
                    traits=self.traits, learned_commands=dict(collections.Counter(self.labels[i] for i in t['y'])),
                    mb_plasticity_on=self.learn.mb.on, working=self.hands.busy,
                    hands_stopped=self.hands.stopped, recent_events=list(self.events)[-5:],
                    what_user_is_doing=(self.observer.summary or 'not observing') if self.observer.on
                                       else 'not observing')

    def speak(self, msg=None):
        # An exact command is handled instantly and never sent to the LLM: gemma2:2b takes ~11 s
        # to answer, which reads as "nothing happened". Only real conversation goes to the model.
        if msg:
            direct = parse_direct(msg, self.hands.cfg)
            if direct:
                return self.act(*direct, source='you')
        self.last_speak, self.voice_src = time.time(), 'thinking'
        self.say('...', 30)                                  # visible feedback during the wait
        voice.speak_async(self.state_block(), msg or None, self._spoke, self.hands.cfg)

    def say(self, text, secs=8):
        self.bubble, self.bubble_until = text, time.time() + secs

    def _spoke(self, text, verb, arg, src):                # called on the voice thread
        self.say(text)
        self.voice_src = src
        self.event(f'said: {text}')
        self.act(verb, arg, 'voice')

    def act(self, verb, arg='', source='you'):
        """Route one action: movement verbs steer the body, hand verbs touch the machine.
        A taught label like 'open notepad' arrives here already split into verb + arg."""
        if verb in voice.MOVES or verb in BEHAVIORS:
            return self.set_override(verb, source)
        if self.d['fear'] > 0.6 and verb in voice.HANDS + ('draft',):   # a frightened fly will not work
            self.event(f'{source} -> {verb} {arg}; too scared')
            return self.say('Too scared. Not now.', 5)
        if verb == 'draft':
            return self.draft_reply()
        if verb in voice.HANDS:
            reply = self.hands.do(verb, arg)
            self.event(f'{source} -> {verb} {arg}: {reply}')
            if reply:
                self.say(reply, 6)

    def draft_reply(self):
        """Copy a message (Ctrl+C), click into the reply box, then trigger this. Reads the
        clipboard, drafts a reply with the LLM, types it - and stops. It NEVER presses Enter:
        sending a message to a real person can't be undone, so that decision stays yours."""
        if self.hands.stopped:
            return self.say('Stopped.', 5)
        clip = self.hands.read_clipboard().strip()
        if not clip:
            return self.say('Clipboard is empty. Copy the message first (Ctrl+C).', 6)
        self.say('reading message...', 20)

        def done(text, err):
            if err or not text:
                self.say('Could not draft a reply (is Ollama running?).', 6)
                self.event('draft failed')
                return
            result = self.hands.type_text(text)
            if result != 'typed':
                self.say(f'Could not type it: {result}', 6)
                self.event(f'draft typing failed: {result}')
                return
            self.event(f'drafted reply: {text[:60]!r}')
            self.say('Typed a reply below. Review it, then press Enter yourself to send.', 10)

        voice.draft_reply_async(clip, done)

    def command(self, text):
        if not text.strip():
            return
        if not self.learn.ready('commands'):
            self.bubble, self.bubble_until = 'teach me first (Teach box)', time.time() + 5
            return
        label = self.learn.predict('commands', text)
        self.say(f'> {label}', 5)
        verb, _, arg = label.partition(' ')               # 'open notepad' -> ('open', 'notepad')
        self.act(verb, arg, f'smelled "{text}"')

    def teach(self, text, label):
        if not text.strip():
            return
        self.learn.teach('commands', text, label)
        self.event(f'learned "{text}" = {label}')
        acc, n = self.learn.loo('commands'), len(self.learn.tasks['commands']['y'])
        if acc == acc:                                       # not NaN
            self.acc = f'real wiring {acc:.0%} (n={n})  ·  shuffled: press Evaluate'
        self.save()

    def evaluate(self):
        if not self.learn.ready('commands'):
            return
        def run():
            real, shuf, chance = self.learn.evaluate('commands')
            note = '   <- brain not contributing' if abs(real - shuf) < 0.1 else ''
            self.acc = f'real {real:.0%}  ·  shuffled {shuf:.0%}  ·  chance {chance:.0%}{note}'
        self.acc = 'evaluating (fly will stutter a few seconds)...'
        threading.Thread(target=run, daemon=True).start()


def clickthrough(win):
    win.update()
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TRANSPARENT)


class App:
    def __init__(self, fly):
        self.f = fly
        self.root = tk.Tk()
        theme(self.root)                                     # must precede every widget below
        self.root.title('FLYPET // connectome')
        self.root.resizable(False, False)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        if os.path.exists('flypet.ico'):
            try:
                self.root.iconbitmap('flypet.ico')
            except Exception:
                pass
        self.ov = tk.Toplevel(self.root)                     # the fly: transparent, topmost, click-through
        self.ov.overrideredirect(True)
        self.ov.attributes('-topmost', True)
        self.ov.attributes('-transparentcolor', KEY)
        self.cv = tk.Canvas(self.ov, width=SIZE, height=SIZE, bg=KEY, highlightthickness=0)
        self.cv.pack()
        self.ov.geometry(f'{SIZE}x{SIZE}+0+0')
        clickthrough(self.ov)
        self.bb = tk.Toplevel(self.root)                     # speech bubble
        self.bb.overrideredirect(True)
        self.bb.attributes('-topmost', True)
        self.bl = tk.Label(self.bb, text='', bg=BG, fg=FG, font=MONO, wraplength=220,
                           justify='left', padx=6, pady=3, relief='solid', bd=1,
                           highlightthickness=1, highlightbackground=FG)
        self.bl.pack()
        self.bb.withdraw()
        # recording indicator: a small always-on-top red dot, shown only while observing, so the
        # logger is never silently on. Placed top-right of the screen.
        self.rec_dot = tk.Toplevel(self.root)
        self.rec_dot.overrideredirect(True)
        self.rec_dot.attributes('-topmost', True)
        tk.Label(self.rec_dot, text='● REC', bg='#200', fg=HOT, font=('Consolas', 9, 'bold'),
                 padx=6, pady=2).pack()
        self.rec_dot.geometry(f'+{user32.GetSystemMetrics(0) - 90}+8')
        self.rec_dot.withdraw()
        self.build_panel()
        self._hotkey_f = self._hotkey_r = False        # edge-detect state for the global hotkeys
        self.t0 = self.last = time.time()
        self.root.after(20, self.loop)

    def build_panel(self):
        r, f = self.root, self.f
        row = [0]

        def put(w, span=4, col=0, **kw):
            kw.setdefault('padx', 6)
            w.grid(row=row[0], column=col, columnspan=span, sticky='w', **kw)
            return w

        def head(text):
            row[0] += 1
            put(tk.Label(r, text=rule(text), fg=DIM, font=MONO_S), pady=(7, 1))
            row[0] += 1

        self.rain_cv = put(tk.Canvas(r, width=RAIN_W, height=RAIN_H, bg=BG, highlightthickness=0), pady=(6, 2))
        self.rain = Rain(self.rain_cv)
        row[0] += 1
        self.status = put(tk.Label(r, text='', anchor='w', font=MONO_B, fg=PALE))
        row[0] += 1
        self.bars = put(tk.Label(r, text='', anchor='w', justify='left', font=MONO), pady=(3, 0))
        row[0] += 1
        self.mem_lbl = put(tk.Label(r, text='', anchor='w', font=MONO_S, fg=DIM,
                                    wraplength=340, justify='left'))

        head('COMMAND')
        self.cmd = put(tk.Entry(r, width=26), span=2)
        put(tk.Button(r, text='SMELL >', command=lambda: (f.command(self.cmd.get()), self.cmd.delete(0, 'end'))),
            span=2, col=2, padx=0)
        row[0] += 1
        self.chat = put(tk.Entry(r, width=26), span=2, pady=(3, 0))
        put(tk.Button(r, text='ASK LLM >', command=lambda: (f.speak(self.chat.get()), self.chat.delete(0, 'end'))),
            span=2, col=2, padx=0, pady=(3, 0))

        head('TRAINING')
        self.teach_e = put(tk.Entry(r, width=16), span=1)
        self.label_v = tk.StringVar(value=f.labels[0])
        put(tk.OptionMenu(r, self.label_v, *f.labels), span=1, col=1, padx=0)
        put(tk.Button(r, text='TEACH', command=lambda: (f.teach(self.teach_e.get(), self.label_v.get()),
                                                        self.teach_e.delete(0, 'end'))), span=1, col=2, padx=0)
        put(tk.Button(r, text='EVAL', command=f.evaluate), span=1, col=3, padx=0)
        row[0] += 1
        self.acc_lbl = put(tk.Label(r, text='', anchor='w', font=MONO_S), pady=(2, 0))
        row[0] += 1
        self.mb_v = tk.BooleanVar(value=False)
        put(tk.Checkbutton(r, text='PLASTICITY', variable=self.mb_v,
                           command=lambda: f.learn.mb.enable(self.mb_v.get())), span=1)
        put(tk.Button(r, text='GOOD', command=lambda: (f.learn.mb.reward(+1), f.event('rewarded'))), span=1, col=1, padx=0)
        put(tk.Button(r, text='BAD', command=lambda: (f.learn.mb.reward(-1), f.event('punished'))), span=1, col=2, padx=0)
        row[0] += 1
        self.mb_lbl = put(tk.Label(r, text='', anchor='w', font=MONO_S, fg=DIM))

        head('POKE THE REAL BRAIN')
        put(tk.Label(r, text='> LESION scrambles the real wiring; EVAL shows real vs shuffled -- '
                            'proof the connectome matters', fg=DIM, font=MONO_S), span=4)
        row[0] += 1
        self.les_b = put(tk.Button(r, text='LESION', command=self.toggle_lesion), span=1)
        self.watch = tk.BooleanVar(value=False)
        put(tk.Checkbutton(r, text='WATCH', variable=self.watch, command=self.toggle_watch), span=1, col=1, padx=0)
        row[0] += 1
        self.hist_row = row[0]
        self.hist = tk.Canvas(r, width=RAIN_W, height=82, bg=BG, highlightthickness=1,
                              highlightbackground=DARK)

        head('HANDS   [ KILL SWITCH: CTRL+ALT+F ]')
        self.stop_b = put(tk.Button(r, text='STOP', fg=HOT, font=MONO_B, command=self.toggle_stop), span=1)
        self.auto_v = tk.BooleanVar(value=False)
        put(tk.Checkbutton(r, text='AUTO', variable=self.auto_v,
                           command=lambda: setattr(f, 'autonomy', self.auto_v.get())), span=1, col=1, padx=0)
        tasks = list(f.hands.cfg.get('tasks', {})) or ['(none)']
        self.task_v = tk.StringVar(value=tasks[0])
        put(tk.OptionMenu(r, self.task_v, *tasks), span=1, col=2, padx=0)
        put(tk.Button(r, text='RUN', command=lambda: f.act('run', self.task_v.get(), 'panel')), span=1, col=3, padx=0)
        row[0] += 1
        put(tk.Label(r, text='DRAFT REPLY: Ctrl+C the message, click the reply box, press CTRL+ALT+R',
                     fg=PALE, font=MONO_S), span=4, pady=(3, 0))
        put(tk.Label(r, text='> (not a button here on purpose - clicking this window would steal '
                              'focus from the chat)', fg=DIM, font=MONO_S), span=4, pady=(0, 0))
        row[0] += 1
        self.hands_log = put(tk.Label(r, text='', anchor='nw', justify='left', font=MONO_S, fg=DIM,
                                      width=56, height=8, bg=PANEL, relief='solid', bd=1),
                             pady=(4, 8))

        head('OBSERVER   [ learns what you do - OFF by default ]')
        self.obs_b = put(tk.Button(r, text='WATCH ME: OFF', fg=DIM, font=MONO_B,
                                   command=self.toggle_observe), span=2)
        put(tk.Button(r, text='WHAT DID YOU LEARN', command=self.show_summary), span=2, col=2, padx=0)
        row[0] += 1
        put(tk.Button(r, text='WIPE LOG', fg=HOT, command=self.wipe_observe), span=2)
        put(tk.Label(r, text='window titles + clipboard only, encrypted, 3-day auto-delete',
                     fg=DIM, font=MONO_S), span=2, col=2, padx=0)
        row[0] += 1
        self.obs_lbl = put(tk.Label(r, text='> observer off', anchor='nw', justify='left',
                                    font=MONO_S, fg=DIM, width=56, height=4, bg=PANEL,
                                    relief='solid', bd=1), pady=(4, 8))
        for i in range(4):                                 # even columns -> the button grid aligns
            r.grid_columnconfigure(i, uniform='fly', weight=1)
        r.configure(padx=10, pady=6)                       # a little breathing room around it all

    def toggle_observe(self):
        obs = self.f.observer
        if obs.on:
            obs.stop()
            self.obs_b.config(text='WATCH ME: OFF', fg=DIM)
            self.rec_dot.withdraw()
            self.f.event('observer stopped')
        else:
            pw = simpledialog.askstring('Observer passphrase',
                                        'Set a passphrase to encrypt the activity log.\n'
                                        'It is NOT stored; if you forget it the log is unreadable.',
                                        show='*', parent=self.root)
            if not pw:
                return
            obs.start(pw)
            self.obs_b.config(text='WATCH ME: ON', fg=HOT)
            self.rec_dot.deiconify()
            self.f.event('observer started')

    def show_summary(self):
        obs = self.f.observer
        if not obs.on:
            self.obs_lbl.config(text='> observer is off')
            return
        self.obs_lbl.config(text='> summarizing...')
        def run():
            s = obs.summarize()
            if not s:
                s = ('LLM offline - is Ollama running? (start it, then try again)'
                     if not obs.llm_ok else 'nothing notable yet - give it a few minutes')
            self.obs_lbl.config(text='> ' + s)
        threading.Thread(target=run, daemon=True).start()

    def wipe_observe(self):
        if messagebox.askyesno('Wipe activity log', 'Permanently delete the encrypted activity log?'):
            self.f.observer.wipe()
            self.obs_lbl.config(text='> log wiped')
            self.f.event('observer log wiped')

    def toggle_stop(self):
        hd = self.f.hands
        if hd.stopped:
            hd.resume()
        else:
            hd.panic('STOP pressed')
            self.auto_v.set(False)
            self.f.autonomy = False
        self.stop_b.config(text='Resume' if hd.stopped else 'STOP', fg='#070' if hd.stopped else '#b00')

    def toggle_lesion(self):
        b = self.f.brain
        (b.restore if b.lesioned else b.lesion)()
        self.les_b.config(text='Restore' if b.lesioned else 'Lesion')
        self.f.event('wiring scrambled' if b.lesioned else 'wiring restored')

    def toggle_watch(self):
        if self.watch.get():
            self.hist.grid(row=self.hist_row, column=0, columnspan=4, padx=6, pady=(0, 6), sticky='w')
        else:
            self.hist.grid_forget()

    def loop(self):
        now = time.time()
        # Global hotkeys, polled without ever touching the FlyPet window: clicking a panel
        # button steals OS foreground focus away from whatever chat app you were typing into,
        # which is exactly wrong for draft-reply. A hotkey fires with your target still focused.
        # Edge-detected (fires once per press) so holding the keys doesn't repeat the action.
        ctrl_alt = bool(user32.GetAsyncKeyState(0x11) & 0x8000 and user32.GetAsyncKeyState(0x12) & 0x8000)
        f_down = ctrl_alt and bool(user32.GetAsyncKeyState(0x46) & 0x8000)
        r_down = ctrl_alt and bool(user32.GetAsyncKeyState(0x52) & 0x8000)
        if f_down and not self._hotkey_f:                       # Ctrl+Alt+F: kill switch
            self.f.hands.panic('Ctrl+Alt+F')
            self.auto_v.set(False)
            self.f.autonomy = False
            if self.f.observer.on:
                self.f.observer.stop()
                self.obs_b.config(text='WATCH ME: OFF', fg=DIM)
                self.rec_dot.withdraw()
            self.stop_b.config(text='Resume', fg='#070')
            self.f.say('Stopped.', 6)
        if r_down and not self._hotkey_r:                       # Ctrl+Alt+R: draft a reply
            self.f.draft_reply()
        self._hotkey_f, self._hotkey_r = f_down, r_down
        dt, self.last = min(now - self.last, 0.1), now
        f = self.f
        f.step(dt)
        self.ov.geometry(f'+{int(f.x - SIZE / 2)}+{int(f.y - SIZE / 2)}')
        self.draw(now - self.t0)
        if f.bubble and now < f.bubble_until:
            self.bl.config(text=f.bubble)
            self.bb.geometry(f'+{int(f.x - 110)}+{int(f.y - SIZE / 2 - 48)}')
            self.bb.deiconify()
        else:
            self.bb.withdraw()
        if int(now * 50) % 10 == 0:
            self.update_panel()
        self.root.after(20, self.loop)

    def draw(self, t):
        sprite.draw_fly(self.cv, self.f, t)

    def update_panel(self):
        f, hd = self.f, self.f.hands
        self.rain.step(f.arousal)
        self.hands_log.config(text='\n'.join(f'{t} {m}' for t, m in hd.log[-8:]) or '> no actions yet')
        flags = ('  [STOPPED]' if hd.stopped else '') + ('  [LESIONED]' if f.brain.lesioned else '')
        work = f'  WORKING:{hd.busy.upper()}' if hd.busy else ''
        self.status.config(text=f'{f.state.upper():<10} AROUSAL {f.arousal:4.2f}  {f.brain.hz:3.0f}HZ  '
                                f'VOICE:{f.voice_src.upper()}{work}{flags}',
                           fg=HOT if (hd.stopped or f.d['fear'] > 0.5) else PALE)
        top = max(f.d, key=f.d.get)
        self.bars.config(text='\n'.join(f'{k.upper():<10}{bar(v)} {v:4.2f}' for k, v in f.d.items()))
        self.acc_lbl.config(text='> ' + f.acc)
        self.mb_lbl.config(text=f'> synapses {f.learn.mb.delta():.1%}   rewards {f.learn.mb.rewards}   '
                                f'mbon {f.approach:4.2f}   dominant drive: {top}')
        self.mem_lbl.config(text=f'> alive {int(f.lifetime // 60)}m   fed {f.fed}   startled {f.startled}   '
                                 f'{" ".join(f"{k[:4]}:{v}" for k, v in f.traits.items())}')
        if self.watch.get():
            self.hist.delete('all')
            r = f.brain.rates
            m = max(float(r.max()), 1e-6)
            self.hist.create_line(0, 80, RAIN_W, 80, fill=DARK)
            for i, v in enumerate(r):
                h = 76 * v / m
                self.hist.create_rectangle(2 + i * 5, 80 - h, 6 + i * 5, 80,
                                           fill=PALE if h > 60 else FG if h > 4 else DIM, outline='')

    def close(self):
        self.f.hands.panic('quit')
        self.f.observer.stop()
        self.f.save()
        self.f.brain.stop()
        self.root.destroy()


def splash():
    """Shown while the connectome loads. Matters because pythonw.exe (the .lnk launcher) gives
    no console, so without this a double-click looks like nothing happened for several seconds."""
    s = tk.Tk()
    theme(s)
    s.overrideredirect(True)
    s.attributes('-topmost', True)
    w, h = 340, 90
    s.geometry(f'{w}x{h}+{(s.winfo_screenwidth() - w) // 2}+{(s.winfo_screenheight() - h) // 2}')
    tk.Label(s, text='F L Y P E T', font=('Consolas', 15, 'bold'), fg=FG).pack(pady=(16, 2))
    tk.Label(s, text='loading connectome ... 139,255 neurons', font=MONO_S, fg=DIM).pack()
    s.update()
    return s


def show_error(msg, root=None):
    """pythonw discards stdout, so a crash must be shown in a dialog or it vanishes silently.
    messagebox needs a LIVE root: reuse the splash if it still exists, else make a fresh one."""
    from tkinter import messagebox
    try:
        if root is not None and root.winfo_exists():
            root.withdraw()
            root.attributes('-topmost', True)          # else it can hide behind other windows
            messagebox.showerror('FlyPet failed to start', msg, parent=root)
            return
    except Exception:
        pass
    try:
        r = tk.Tk()
        r.withdraw()
        r.attributes('-topmost', True)
        messagebox.showerror('FlyPet failed to start', msg, parent=r)
        r.destroy()
    except Exception:
        pass


if __name__ == '__main__':
    import traceback
    sp = splash()
    try:
        fly = Fly()
        sp.destroy()
        sp = None
        App(fly).root.mainloop()
    except Exception:
        traceback.print_exc()
        show_error(traceback.format_exc()[-1500:], sp)
