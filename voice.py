"""Local LLM as the fly's voice and interpreter. The model is frozen; the fly's live state is
its context. It may also emit ONE action, which fly.py routes to movement or to hands.py.
stdlib only. Any failure falls back to templates built from the same state, so the fly always
speaks. Works with Ollama (:11434) or LM Studio (:1234) - both speak OpenAI-compat."""
import json, os, re, threading, urllib.request

BASE_URL, MODEL, TIMEOUT = 'http://localhost:11434/v1', 'gemma2:2b', 25  # optional: talking only
MOVES = ('come', 'flee', 'sleep', 'explore', 'none')
HANDS = ('open', 'close', 'run', 'click', 'doubleclick', 'rightclick', 'move', 'type', 'press')
PERSONA = (
    "You are a fruit fly living on Tanis's Windows desktop. Your brain is a real fly connectome "
    "(FlyWire). You know ONLY what the CURRENT STATE below says - your senses and your memory. "
    "Reply in first person, at most 25 words, plain, a little fly-like, no emojis.")


def vocabulary(cfg):
    """The action menu, built from apps.json so the model can only name things that exist."""
    apps, tasks = ', '.join(cfg.get('apps', {})), ', '.join(cfg.get('tasks', {}))
    return (
        "\n\nYou may end your reply with ONE line, exactly:  ACTION: <verb> <argument>\n"
        "Movement verbs (no argument): come, flee, sleep, explore, none\n"
        f"Apps you can open or close:  ACTION: open <app>  /  ACTION: close <app>   apps: {apps}\n"
        f"Long jobs you can run:  ACTION: run <task>   tasks: {tasks}\n"
        "Mouse and keyboard: ACTION: click <x> <y> | doubleclick <x> <y> | rightclick <x> <y> | "
        "move <x> <y> | type <text> | press <combo, e.g. ctrl+s>\n"
        "Use an app or task name exactly as listed. If you do not want to act, write ACTION: none.")


def _post(msgs, max_tokens=70, temperature=0.8):
    body = json.dumps({'model': MODEL, 'messages': msgs, 'max_tokens': max_tokens,
                       'temperature': temperature}).encode()
    req = urllib.request.Request(f'{BASE_URL}/chat/completions', body, {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=TIMEOUT))['choices'][0]['message']['content'].strip()


def parse_action(text):
    """-> (clean_text, verb, arg). Unknown verbs become 'none'."""
    m = re.search(r'ACTION:\s*([A-Za-z]+)[ \t]*(.*)', text)
    verb, arg = ('none', '')
    if m:
        v, a = m.group(1).lower(), m.group(2).strip().strip('<>"\'').rstrip('.')
        if v in MOVES or v in HANDS:
            verb, arg = v, a
    return re.sub(r'ACTION:.*', '', text, flags=re.S).strip(), verb, arg


def ask(state, user_msg=None, cfg=None):
    """Returns (text, verb, arg). Raises on any failure; caller falls back to template()."""
    ctx = '\n'.join(f'{k}: {v}' for k, v in state.items())
    sysmsg = f'{PERSONA}{vocabulary(cfg) if cfg else ""}\n\nCURRENT STATE\n{ctx}'
    text = _post([{'role': 'system', 'content': sysmsg},
                  {'role': 'user', 'content': user_msg or 'Say what you feel right now.'}])
    text, verb, arg = parse_action(text)
    return (text or template(state)), verb, arg


def decide(state, options):
    """Autonomous urge: pick one action from `options` (lines like 'open notepad') or none.
    Returns (verb, arg) - no speech. Raises on failure; caller treats that as 'none'."""
    if not options:
        return 'none', ''
    ctx = '\n'.join(f'{k}: {v}' for k, v in state.items())
    menu = '\n'.join(f'- {o}' for o in options)
    text = _post([{'role': 'system', 'content':
                   f'{PERSONA}\n\nCURRENT STATE\n{ctx}\n\nYou are bored and Tanis is away. You may do '
                   f'ONE of these, or nothing:\n{menu}\n\nReply with ONLY the line '
                   f'ACTION: <verb> <argument>, or ACTION: none.'},
                  {'role': 'user', 'content': 'Do you want to do something?'}], max_tokens=20, temperature=0.6)
    _, verb, arg = parse_action(text)
    if f'{verb} {arg}'.strip() not in [o.strip() for o in options]:      # never act off-menu
        return 'none', ''
    return verb, arg


REPLY_PERSONA = (
    "You are drafting a short reply for Tanis to send in a chat app. Based on the message "
    "below, write ONE natural, brief reply in Tanis's voice - casual, at most 2 short sentences. "
    "Output ONLY the reply text itself. No quotes, no explanation, no ACTION line.")


def draft_reply(message_text):
    """A reply for the HUMAN to send, not the fly's own voice - a distinct, simpler prompt.
    Raises on failure; caller decides what 'no draft' means."""
    text = _post([{'role': 'system', 'content': REPLY_PERSONA},
                  {'role': 'user', 'content': f'Message received:\n"""{message_text[:600]}"""\n\nWrite the reply.'}],
                 max_tokens=60, temperature=0.7)
    return text.strip().strip('"').strip()


def draft_reply_async(message_text, cb):
    """cb(reply_text_or_None, error_or_None) on a daemon thread."""
    def run():
        try:
            cb(draft_reply(message_text), None)
        except Exception as e:
            cb(None, e)
    threading.Thread(target=run, daemon=True).start()


def template(s):
    """Always-available fallback from the same state block."""
    if s.get('working'):           return f"Busy. {s['working']} is still running."
    if s['fear'] > 0.5:            return "Stay back. That scared me."
    if s['hunger'] > 0.6:          return f"Hungry. You haven't touched me in {int(s['idle_s'] // 60)} min."
    if s['fatigue'] > 0.6 and s['light'] < 0.3: return "Dark and tired. Sleeping."
    if s['cpu'] > 60:              return "Your machine is hot. I don't like it."
    if s['cursor_near']:           return "I feel you there."
    return "Quiet. Watching the light."


def speak_async(state, user_msg, cb, cfg=None):
    """cb(text, verb, arg, source) on a daemon thread; source is 'llm' or 'template'."""
    def run():
        try:
            cb(*ask(state, user_msg, cfg), 'llm')
        except Exception:
            cb(template(state), 'none', '', 'template')       # a template reply never carries an action
    threading.Thread(target=run, daemon=True).start()


def decide_async(state, options, cb):
    """cb(verb, arg) on a daemon thread. Silent on any failure."""
    def run():
        try:
            cb(*decide(state, options))
        except Exception:
            cb('none', '')
    threading.Thread(target=run, daemon=True).start()
