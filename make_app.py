"""Turn FlyPet into a double-clickable app.

    python make_app.py              -> flypet.ico + FlyPet.lnk in this folder
    python make_app.py --desktop    -> also copies the shortcut to your Desktop

The shortcut runs pythonw.exe, so there is NO console window - it launches like an app.
Re-run this any time (e.g. after moving the folder or upgrading Python).
"""
import os, subprocess, sys
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ICO, LNK = os.path.join(HERE, 'flypet.ico'), os.path.join(HERE, 'FlyPet.lnk')
BG, GREEN, DIM, EYE = (0, 0, 0, 255), (0, 255, 65, 255), (10, 122, 36, 255), (255, 49, 49, 255)


def draw_icon(path, px=256):
    """A Matrix-green wireframe fly on black, drawn at 256 and downsampled by Pillow."""
    img = Image.new('RGBA', (px, px), BG)
    d, u = ImageDraw.Draw(img), px / 256
    def S(*v): return [x * u for x in v]
    d.rounded_rectangle(S(6, 6, 250, 250), radius=38 * u, outline=DIM, width=int(3 * u))
    for k in (-1, 1):                                                   # wings
        d.polygon(S(128, 118 + k * 6, 196, 60 + k * 74, 226, 104 + k * 44), outline=GREEN, width=int(3 * u))
    for i, (x0, y0) in enumerate(((104, 96), (100, 128), (106, 160))):  # legs, tripod splay
        for k in (-1, 1):
            d.line(S(x0, 128 + k * 14, x0 - 34 - i * 6, 128 + k * (54 + i * 8)), fill=GREEN, width=int(4 * u))
            d.line(S(x0 - 34 - i * 6, 128 + k * (54 + i * 8), x0 - 58 - i * 10, 128 + k * (72 + i * 10)),
                   fill=DIM, width=int(3 * u))
    d.ellipse(S(40, 100, 132, 156), outline=GREEN, width=int(4 * u))    # abdomen
    for x in (58, 78, 98):
        d.line(S(x, 104, x, 152), fill=DIM, width=int(2 * u))           # segment seams
    d.ellipse(S(112, 92, 190, 164), outline=GREEN, width=int(5 * u))    # thorax
    d.ellipse(S(176, 100, 224, 156), outline=GREEN, width=int(4 * u))   # head
    for k in (-1, 1):                                                   # LED eyes
        d.ellipse(S(196, 128 + k * 24 - 15, 226, 128 + k * 24 + 15), fill=EYE)
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    img.save(path, sizes=sizes)
    return path


def make_shortcut(path, target, args, workdir, icon):
    ps = (f'$s=(New-Object -COM WScript.Shell).CreateShortcut("{path}");'
          f'$s.TargetPath="{target}";$s.Arguments="{args}";$s.WorkingDirectory="{workdir}";'
          f'$s.IconLocation="{icon}";$s.Description="FlyPet - connectome fly";$s.Save()')
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or 'CreateShortcut failed')
    return path


if __name__ == '__main__':
    pyw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.exists(pyw):
        pyw = sys.executable
        print('! pythonw.exe not found - the app will show a console window')
    print('icon    ', draw_icon(ICO))
    print('shortcut', make_shortcut(LNK, pyw, 'fly.py', HERE, ICO))

    bat = os.path.join(HERE, 'FlyPet.bat')                    # fallback if .lnk files are blocked
    with open(bat, 'w') as fh:
        fh.write(f'@echo off\r\ncd /d "%~dp0"\r\nstart "" "{pyw}" fly.py\r\n')
    print('fallback', bat)

    if '--desktop' in sys.argv:
        desk = os.path.join(os.path.expanduser('~'), 'Desktop', 'FlyPet.lnk')
        print('desktop ', make_shortcut(desk, pyw, 'fly.py', HERE, ICO))
    else:
        print('\n(run with --desktop to also put a shortcut on your Desktop)')
    print('\nDouble-click FlyPet.lnk to run.')
