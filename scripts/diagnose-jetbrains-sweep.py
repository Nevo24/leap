#!/usr/bin/env python3
"""
diagnose-jetbrains-sweep.py — diagnose why stale ``lps <tag>`` tabs
survive in a JetBrains IDE after opening a new Leap session.

============================================================================
HOW TO RUN
============================================================================
1.  Open the JetBrains IDE (GoLand / PyCharm / IntelliJ / WebStorm / etc.)
    where you see stale ``lps <tag>`` tabs.

2.  Open a NEW terminal tab INSIDE that IDE's Terminal tool window.
    (This matters — ``$TERMINAL_EMULATOR`` is only set by JetBrains
    itself, not by iTerm2 / Terminal.app / WezTerm / Warp.)

3.  In that JetBrains terminal, cd into a directory that belongs to
    the project where the stale tabs live (any subdirectory of that
    project's git repo is fine).

4.  Run:

        python3 ~/Downloads/diagnose-jetbrains-sweep.py
        # or wherever you saved the script

5.  Copy the ENTIRE printed output (Cmd+A in the terminal, Cmd+C) and
    send it to Nevo.  Also send the file at /tmp/leap-sweep-diag.txt.

This script is read-only.  It does NOT modify any tabs, sockets,
metadata, or settings — only inspects them.
============================================================================
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import stat as stat_mod
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Optional


# ----------------------------------------------------------------------------
# Inlined Leap helpers (so the script can run standalone without needing
# the leap source tree to be importable)
# ----------------------------------------------------------------------------

# Maps process-name fragment → display name.  Mirrors
# leap.utils.constants.JETBRAINS_IDES.
JETBRAINS_IDES: dict[str, str] = {
    'pycharm': 'PyCharm',
    'goland': 'GoLand',
    'webstorm': 'WebStorm',
    'phpstorm': 'PhpStorm',
    'rubymine': 'RubyMine',
    'clion': 'CLion',
    'datagrip': 'DataGrip',
    'idea': 'IntelliJ IDEA',
    'studio': 'Android Studio',
}

# Maps display name → CLI binary name inside the .app bundle.  Mirrors
# leap.utils.terminal._JETBRAINS_CLI_MAP.
JETBRAINS_CLI_MAP: dict[str, str] = {
    'PyCharm': 'pycharm',
    'IntelliJ IDEA': 'idea',
    'GoLand': 'goland',
    'WebStorm': 'webstorm',
    'PhpStorm': 'phpstorm',
    'Android Studio': 'studio',
    'RubyMine': 'rubymine',
    'CLion': 'clion',
    'DataGrip': 'datagrip',
    'JetBrains IDE': 'idea',
}

JETBRAINS_APP_PATTERNS: list[str] = [
    'IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
    'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
    'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app',
    'Android Studio*.app',
]


def detect_ide() -> str:
    """Same detection logic as leap.utils.ide_detection.detect_ide."""
    term_program = os.environ.get('TERM_PROGRAM', '')
    terminal_emulator = os.environ.get('TERMINAL_EMULATOR', '')

    if term_program == 'vscode':
        bundle_id = os.environ.get('__CFBundleIdentifier', '')
        if 'todesktop' in bundle_id or 'cursor' in bundle_id.lower():
            return 'Cursor'
        return 'VS Code'
    if term_program == 'iTerm.app':
        return 'iTerm2'
    if term_program == 'Apple_Terminal':
        return 'Terminal.app'
    if term_program == 'WarpTerminal':
        return 'Warp'
    if term_program == 'WezTerm':
        return 'WezTerm'

    if 'JetBrains' in terminal_emulator or 'jetbrains' in terminal_emulator.lower():
        # Walk up the process tree to find the specific JetBrains IDE.
        try:
            current_pid = os.getpid()
            for _ in range(10):
                r = subprocess.run(
                    ['ps', '-p', str(current_pid), '-o', 'ppid=,comm='],
                    capture_output=True, text=True, timeout=1,
                )
                if r.returncode != 0:
                    break
                parts = r.stdout.strip().split(None, 1)
                if len(parts) < 2:
                    break
                ppid, name = parts
                name_l = name.lower()
                for key, display in JETBRAINS_IDES.items():
                    if key in name_l:
                        if key == 'idea' and 'pycharm' in name_l:
                            continue
                        return display
                current_pid = int(ppid)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        return 'JetBrains IDE'

    return 'Unknown'


def build_jetbrains_path() -> str:
    """Build a PATH string that lets shutil.which() find the JetBrains CLI."""
    parts: list[str] = []
    for app_dir in ['/Applications', os.path.expanduser('~/Applications')]:
        if not os.path.isdir(app_dir):
            continue
        for pattern in JETBRAINS_APP_PATTERNS:
            for app in glob.glob(f'{app_dir}/{pattern}'):
                parts.append(f'{app}/Contents/MacOS')
            for app in glob.glob(f'{app_dir}/*/{pattern}'):
                parts.append(f'{app}/Contents/MacOS')
    toolbox_scripts = os.path.expanduser(
        '~/Library/Application Support/JetBrains/Toolbox/scripts',
    )
    if os.path.isdir(toolbox_scripts):
        parts.append(toolbox_scripts)
    return ':'.join(parts) + ':' + os.environ.get('PATH', '')


def find_storage_dir() -> Optional[Path]:
    """Find Leap's .storage directory.

    Walks up from the script location and from $HOME looking for a
    directory containing .storage/project-path (the marker file Leap
    installs).  Falls back to a few common spots.
    """
    candidates: list[Path] = []
    script = Path(__file__).resolve()
    # If the script lives inside a leap clone, walk up.
    for p in [script, *script.parents]:
        sd = p / '.storage'
        if (sd / 'project-path').exists():
            candidates.append(sd)
            break

    # Try LEAP_PROJECT_DIR
    env_dir = os.environ.get('LEAP_PROJECT_DIR', '')
    if env_dir:
        sd = Path(env_dir) / '.storage'
        if sd.is_dir():
            candidates.append(sd)

    # Try common install locations
    for raw in [
        str(Path.home() / 'workspace' / 'leap' / '.storage'),
        str(Path.home() / 'leap' / '.storage'),
    ]:
        sd = Path(raw)
        if sd.is_dir():
            candidates.append(sd)

    # Try walking up from CWD (Yuval might be inside the leap repo)
    here = Path.cwd().resolve()
    for p in [here, *here.parents]:
        sd = p / '.storage'
        if sd.is_dir():
            candidates.append(sd)

    return candidates[0] if candidates else None


def server_alive_via_socket(sock_path: Path) -> tuple[bool, str]:
    """Authoritative liveness probe — connect to the Unix socket."""
    try:
        st = sock_path.stat()
    except OSError as e:
        return False, f'stat error: {e}'
    if not stat_mod.S_ISSOCK(st.st_mode):
        return False, f'not a socket (mode={oct(st.st_mode)})'
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(sock_path))
        s.close()
        return True, 'connect ok'
    except OSError as e:
        return False, f'connect error: {e}'


def pid_alive_via_kill(pid: object) -> tuple[bool, str]:
    """Buggy liveness probe — what the current sweep uses."""
    if not isinstance(pid, int) or pid <= 0:
        return True, 'no pid → treated as alive by sweep (default)'
    try:
        os.kill(pid, 0)
        return True, 'os.kill(pid, 0) succeeded'
    except ProcessLookupError:
        return False, 'os.kill(pid, 0) → ProcessLookupError (dead)'
    except PermissionError:
        return True, 'os.kill(pid, 0) → PermissionError (process exists)'
    except OSError as e:
        return False, f'os.kill(pid, 0) → {e}'


# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------

REPORT: list[str] = []
FINDINGS_BUG: list[str] = []
FINDINGS_INFO: list[str] = []


def section(title: str) -> None:
    REPORT.append('')
    REPORT.append('=' * 78)
    REPORT.append(title)
    REPORT.append('=' * 78)


def kv(key: str, value: object) -> None:
    REPORT.append(f'  {key}: {value!r}')


def line(s: str = '') -> None:
    REPORT.append(s if not s else f'  {s}')


def raw(s: str) -> None:
    REPORT.append(s)


def bug(s: str) -> None:
    FINDINGS_BUG.append(s)


def info(s: str) -> None:
    FINDINGS_INFO.append(s)


# ----------------------------------------------------------------------------
# Section A — Environment
# ----------------------------------------------------------------------------

section('A. Environment')
kv('cwd', os.getcwd())
kv('uid', os.getuid())
kv('TERMINAL_EMULATOR', os.environ.get('TERMINAL_EMULATOR', ''))
kv('TERM_PROGRAM', os.environ.get('TERM_PROGRAM', ''))
kv('TERM', os.environ.get('TERM', ''))
kv('LEAP_PROJECT_DIR', os.environ.get('LEAP_PROJECT_DIR', ''))
kv('LEAP_TAG', os.environ.get('LEAP_TAG', ''))
kv('python', sys.version.split()[0])
kv('platform', sys.platform)
try:
    boot = subprocess.check_output(
        ['sysctl', '-n', 'kern.boottime'], text=True, timeout=2,
    ).strip()
    kv('boot time', boot)
except Exception as e:  # noqa: BLE001
    kv('boot time error', str(e))

if 'jetbrains' not in os.environ.get('TERMINAL_EMULATOR', '').lower():
    bug(
        '$TERMINAL_EMULATOR does NOT contain "JetBrains" — you are NOT '
        'running this from inside a JetBrains terminal tab.  The sweep '
        'would never run.  Open a terminal INSIDE GoLand/PyCharm/etc. '
        'and re-run this script.'
    )


# ----------------------------------------------------------------------------
# Section B — detect_ide()
# ----------------------------------------------------------------------------

section('B. detect_ide()')
ide_name = detect_ide()
kv('detect_ide()', ide_name)
kv('CLI command for this IDE',
   JETBRAINS_CLI_MAP.get(ide_name, '<NOT MAPPED>'))

if ide_name not in JETBRAINS_CLI_MAP:
    bug(
        f'detect_ide() returned {ide_name!r} — this is not in the '
        'JETBRAINS_CLI_MAP.  The sweep silently no-ops at the '
        '`cli_name not in map → return ""` branch.  Add the mapping in '
        'src/leap/utils/terminal.py:_JETBRAINS_CLI_MAP.'
    )


# ----------------------------------------------------------------------------
# Section C — JetBrains CLI binary resolution
# ----------------------------------------------------------------------------

section('C. JetBrains CLI binary resolution')

jb_path = build_jetbrains_path()
line('PATH dirs constructed (first 30):')
for d in [d for d in jb_path.split(':') if d][:30]:
    raw(f'    {d}{"  [missing]" if not os.path.isdir(d) else ""}')

# Every JetBrains .app on disk we can find
discovered_apps: list[str] = []
for app_dir in ['/Applications', os.path.expanduser('~/Applications')]:
    if not os.path.isdir(app_dir):
        continue
    for pattern in JETBRAINS_APP_PATTERNS:
        discovered_apps.extend(glob.glob(f'{app_dir}/{pattern}'))
        discovered_apps.extend(glob.glob(f'{app_dir}/*/{pattern}'))
toolbox_apps_root = os.path.expanduser(
    '~/Library/Application Support/JetBrains/Toolbox/apps',
)
if os.path.isdir(toolbox_apps_root):
    for pattern in JETBRAINS_APP_PATTERNS:
        discovered_apps.extend(
            glob.glob(f'{toolbox_apps_root}/**/{pattern}', recursive=True),
        )
discovered_apps = sorted(set(discovered_apps))

line('')
line(f'Discovered {len(discovered_apps)} JetBrains .app bundles:')
for app in discovered_apps:
    macos_dir = f'{app}/Contents/MacOS'
    binaries = (
        sorted(os.listdir(macos_dir))
        if os.path.isdir(macos_dir) else []
    )
    raw(f'    {app}')
    raw(f'      Contents/MacOS contents: {binaries}')

toolbox_scripts = os.path.expanduser(
    '~/Library/Application Support/JetBrains/Toolbox/scripts',
)
line('')
kv('Toolbox scripts dir exists', os.path.isdir(toolbox_scripts))
if os.path.isdir(toolbox_scripts):
    try:
        kv('Toolbox scripts dir contents',
           sorted(os.listdir(toolbox_scripts)))
    except OSError as e:
        kv('Toolbox scripts dir read error', str(e))

cli_name = JETBRAINS_CLI_MAP.get(ide_name, '')
resolved_cli = ''
if cli_name:
    found = shutil.which(cli_name, path=jb_path)
    kv(f'shutil.which({cli_name!r}, jetbrains PATH)', found)
    kv(f'shutil.which({cli_name!r}, default PATH)', shutil.which(cli_name))
    resolved_cli = found or ''

if cli_name and not resolved_cli:
    bug(
        f'shutil.which({cli_name!r}) could NOT find the JetBrains '
        'CLI on the constructed PATH.  This is the most common reason '
        'the sweep never runs.  Check section C above — none of the '
        'PATH dirs contain a binary named ' + cli_name + '.\n'
        '  Likely cause: your JetBrains .app is installed somewhere '
        'OUTSIDE /Applications, ~/Applications, or the Toolbox.  '
        'Drag-to-/Applications or use JetBrains Toolbox.'
    )

kv('CLI resolved (final)', resolved_cli)


# ----------------------------------------------------------------------------
# Section D — cwd → project_path computation
# ----------------------------------------------------------------------------

section('D. cwd → project_path computation')
cwd = os.getcwd()
kv('os.getcwd()', cwd)

git_root: Optional[str] = None
try:
    r = subprocess.run(
        ['git', 'rev-parse', '--show-toplevel'],
        capture_output=True, text=True, cwd=cwd, timeout=2,
    )
    git_root = r.stdout.strip() or None
    if r.returncode != 0:
        kv('git stderr', r.stderr.strip())
except Exception as e:  # noqa: BLE001
    kv('git error', str(e))
kv('git rev-parse --show-toplevel', git_root)

leap_project_path = git_root or cwd
kv('Leap computes project_path as', leap_project_path)
kv('  ends with trailing slash?', leap_project_path.endswith('/'))
kv('  realpath',  os.path.realpath(leap_project_path))


# ----------------------------------------------------------------------------
# Section E — .storage/sockets meta + sock inventory
# ----------------------------------------------------------------------------

section('E. .storage/sockets meta + sock inventory')

storage_dir = find_storage_dir()
kv('storage_dir', str(storage_dir) if storage_dir else None)

if not storage_dir:
    bug(
        'Could not find Leap .storage directory.  Set '
        'LEAP_PROJECT_DIR=/path/to/leap and re-run.'
    )
    meta_files: list[Path] = []
    sock_files: list[Path] = []
else:
    sockets_dir = storage_dir / 'sockets'
    meta_files = sorted(sockets_dir.glob('*.meta')) if sockets_dir.exists() else []
    sock_files = sorted(sockets_dir.glob('*.sock')) if sockets_dir.exists() else []
    kv('sockets_dir', str(sockets_dir))
    kv('sockets_dir exists', sockets_dir.exists())
    kv('meta file count', len(meta_files))
    kv('sock file count', len(sock_files))

# Cache the parsed meta data for later sections
meta_data: dict[str, dict] = {}
sock_alive_cache: dict[str, tuple[bool, str]] = {}
pid_alive_cache: dict[str, tuple[bool, str]] = {}

line('')
for mf in meta_files:
    tag = mf.stem
    try:
        data = json.loads(mf.read_text())
    except Exception as e:  # noqa: BLE001
        data = {'__ERROR__': str(e)}
    meta_data[tag] = data
    sp = mf.parent / f'{tag}.sock'
    sock_alive, sock_reason = server_alive_via_socket(sp)
    pid_alive, pid_reason = pid_alive_via_kill(data.get('pid'))
    sock_alive_cache[tag] = (sock_alive, sock_reason)
    pid_alive_cache[tag] = (pid_alive, pid_reason)
    raw(f'    {mf.name}')
    raw(f'      ide:           {data.get("ide")!r}')
    raw(f'      project_path:  {data.get("project_path")!r}')
    raw(f'      pid:           {data.get("pid")!r}')
    raw(f'      server_start:  {data.get("server_started_at")!r}')
    raw(f'      .sock exists:  {sp.exists()}')
    raw(f'      PID liveness (buggy current code):  {pid_alive} — {pid_reason}')
    raw(f'      Socket liveness (fixed code):       {sock_alive} — {sock_reason}')

# Detect PID/socket mismatches — the post-reboot / PID-reuse pattern
mismatches = [
    t for t in meta_data
    if pid_alive_cache[t][0] and not sock_alive_cache[t][0]
]
if mismatches:
    bug(
        'PID-vs-socket liveness MISMATCH for tags ' + repr(mismatches) +
        '.  The buggy ``os.kill(pid, 0)`` check says they are alive, '
        'but the authoritative socket connect probe says dead.  This '
        'is the classic post-reboot / PID-reuse pattern — the old PID '
        'has been reassigned to an unrelated process.  Affected '
        'sessions are wrongly protected from the sweep on the current '
        'main branch.  My pending fix (socket connect probe) would '
        'reclassify them as dead.'
    )


# ----------------------------------------------------------------------------
# Section F — JetBrains XML config
# ----------------------------------------------------------------------------

section('F. JetBrains XML config (terminal.xml + advancedSettings.xml)')

jb_config_root = Path.home() / 'Library' / 'Application Support' / 'JetBrains'
kv('config root exists', jb_config_root.exists())
if jb_config_root.exists():
    config_dirs = [
        p for p in jb_config_root.iterdir()
        if p.is_dir() and '20' in p.name
    ]
    kv('IDE config dirs', [p.name for p in config_dirs])
    for cd in config_dirs:
        opts = cd / 'options'
        if not opts.exists():
            continue
        line('')
        line(f'  {cd.name}/options/:')
        for fname in ('terminal.xml', 'advancedSettings.xml'):
            f = opts / fname
            if not f.exists():
                raw(f'      {fname}: <missing>')
                continue
            try:
                content = f.read_text()
            except OSError as e:
                raw(f'      {fname}: read error {e}')
                continue
            raw(f'      {fname}:')
            for ln in content.splitlines()[:20]:
                raw(f'        {ln}')

        # Confirm the show.application.title setting is on — this is
        # what enables JetBrains to honour OSC title sequences.
        adv = opts / 'advancedSettings.xml'
        if adv.exists():
            text = adv.read_text()
            if 'terminal.show.application.title' not in text:
                info(
                    f'{cd.name}/advancedSettings.xml does not mention '
                    '``terminal.show.application.title`` — JetBrains '
                    'may be ignoring OSC title sequences entirely.  '
                    'Without it, the cleanup OSC at server-shutdown '
                    'cannot reset a tab name back to bare.  Run '
                    '`make reconfigure` from the leap repo.'
                )


# ----------------------------------------------------------------------------
# Section G — ideScript probe (file-based output)
# ----------------------------------------------------------------------------

section('G. ideScript probe — open projects + every terminal tab')


def run_idescript(groovy_body: str, label: str) -> dict:
    """Run an ideScript that writes output to a temp file.  println()
    from inside ideScript does NOT go to the CLI's stdout — we have to
    use file IO."""
    if not resolved_cli:
        return {'error': 'no CLI resolved', 'output_lines': []}

    tmp_out = tempfile.NamedTemporaryFile(
        mode='w', suffix=f'.diag-{label}.txt', delete=False,
    )
    tmp_out.close()
    out_path = tmp_out.name
    escaped = out_path.replace('\\', '\\\\').replace('"', '\\"')

    wrapper = textwrap.dedent(f'''
        import com.intellij.openapi.project.ProjectManager
        import com.intellij.openapi.wm.ToolWindowManager

        var __DIAG_OUT_PATH = "{escaped}"
        var __DIAG_LINES = new java.util.ArrayList()
        var __diag_println = {{ String s -> __DIAG_LINES.add(s) }}

        IDE.application.invokeAndWait {{
            try {{
                {groovy_body}
            }} catch (Throwable t) {{
                __diag_println("ERROR: " + t.getClass().getName() + ": " + t.getMessage())
                var sw = new java.io.StringWriter()
                t.printStackTrace(new java.io.PrintWriter(sw))
                __diag_println(sw.toString())
            }}
        }}
        new java.io.File(__DIAG_OUT_PATH).text = __DIAG_LINES.join("\\n")
    ''')

    tmp_script = tempfile.NamedTemporaryFile(
        mode='w', suffix='.groovy', delete=False,
    )
    tmp_script.write(wrapper)
    tmp_script.close()

    env = os.environ.copy()
    env['PATH'] = jb_path
    started = time.time()
    error = None
    exit_code = None
    try:
        r = subprocess.run(
            [resolved_cli, 'ideScript', tmp_script.name],
            capture_output=True, text=True, timeout=45, env=env,
        )
        exit_code = r.returncode
        if r.stderr.strip():
            error = f'stderr: {r.stderr.strip()[:500]}'
    except subprocess.TimeoutExpired:
        error = 'ideScript hit 45s timeout'
    except Exception as e:  # noqa: BLE001
        error = f'ideScript raised: {e}'
    finally:
        try:
            os.unlink(tmp_script.name)
        except OSError:
            pass

    # Poll the output file (the CLI returns before the IDE writes it).
    elapsed = time.time() - started
    output_lines: list[str] = []
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            content = Path(out_path).read_text()
        except OSError:
            content = ''
        if content:
            output_lines = content.splitlines()
            break
        time.sleep(0.2)
    try:
        os.unlink(out_path)
    except OSError:
        pass

    return {
        'exit_code': exit_code,
        'elapsed': round(elapsed, 2),
        'error': error,
        'output_lines': output_lines,
    }


jb_basepaths: list[str] = []
# (project_basePath, tab_displayName)
tabs_all: list[tuple[str, str]] = []

if not resolved_cli:
    line('SKIPPED — no JetBrains CLI resolved (see section C).')
else:
    probe = '''
        var projects = ProjectManager.getInstance().getOpenProjects()
        __diag_println("openProjects.length=" + projects.length)
        for (var i = 0; i < projects.length; i++) {
            var p = projects[i]
            __diag_println("PROJECT|" + p.getName() + "|" + p.getBasePath())
        }
        for (var i = 0; i < projects.length; i++) {
            var p = projects[i]
            var tw = ToolWindowManager.getInstance(p).getToolWindow("Terminal")
            if (tw == null) { __diag_println("NO_TW|" + p.getBasePath()); continue }
            var cm = tw.getContentManager()
            if (cm == null) { __diag_println("NO_CM|" + p.getBasePath()); continue }
            var n = cm.getContentCount()
            for (var j = 0; j < n; j++) {
                var c = cm.getContent(j)
                if (c == null) continue
                var name = c.getDisplayName()
                if (name == null) continue
                __diag_println("TAB|" + p.getBasePath() + "|" + name)
            }
        }
    '''
    res = run_idescript(probe, 'probe')
    kv('exit_code', res.get('exit_code'))
    kv('elapsed', res.get('elapsed'))
    if res.get('error'):
        kv('error', res.get('error'))

    out_lines = res.get('output_lines') or []
    line('')
    line(f'ideScript output ({len(out_lines)} lines):')
    for ln in out_lines[:300]:
        raw(f'    {ln}')

    if not out_lines:
        bug(
            'ideScript returned but produced NO output within 10 s.  '
            'The JetBrains script bridge is not delivering our script.  '
            'Possible causes:\n'
            '  - The CLI binary launched a fresh empty IDE instead of '
            'connecting to the running one (would also show '
            'openProjects.length=0 if it did run).\n'
            '  - The IDE detect_ide() identified is NOT the one you '
            'actually have open.\n'
            '  - JetBrains is in the middle of indexing / starting up.'
        )
    else:
        # Parse projects + tabs
        for ln in out_lines:
            if ln.startswith('PROJECT|'):
                try:
                    _, _, base = ln.split('|', 2)
                    jb_basepaths.append(base)
                except ValueError:
                    pass
            elif ln.startswith('TAB|'):
                try:
                    _, base, name = ln.split('|', 2)
                    tabs_all.append((base, name))
                except ValueError:
                    pass

    line('')
    line(f'Parsed JetBrains basePaths: {jb_basepaths!r}')
    line(f'Leap project_path:           {leap_project_path!r}')
    if jb_basepaths and leap_project_path not in jb_basepaths:
        bug(
            f'Leap project_path {leap_project_path!r} does NOT match '
            'any JetBrains project.getBasePath() in this IDE — '
            f'they are {jb_basepaths!r}.  The sweep\'s Groovy '
            'looks up the project via exact-equals on basePath; with '
            'no match it returns without renaming anything.  Common '
            'causes: trailing slash, resolved-vs-unresolved symlink, '
            'alternate git clone of the same project, or you cd\'d '
            'into a sibling repo that JetBrains hasn\'t opened.'
        )


# ----------------------------------------------------------------------------
# Section H — OSC tab-rename probe (visual test for Yuval)
# ----------------------------------------------------------------------------

section('H. OSC tab-rename probe (LOOK AT YOUR TAB TITLE)')

probe_title = f'lps-diag-probe-{os.getpid()}'
sys.stdout.write(f'\x1b]0;{probe_title}\x07')
sys.stdout.flush()
line(f'Just emitted OSC sequence to set this tab title to '
     f'{probe_title!r}.')
line('')
line('LOOK AT YOUR CURRENT TERMINAL TAB AT THE TOP OF JETBRAINS.')
line(f'  - If the tab title CHANGED to "{probe_title}": OSC works.')
line('  - If the tab title is UNCHANGED: OSC is being ignored. ')
line('    Causes: you manually renamed this tab earlier (JetBrains '
     'then ignores OSC for it), or ``terminal.show.application.title``')
line('    is OFF (see section F).')


# ----------------------------------------------------------------------------
# Section I — Simulate the sweep: what would live_tags contain?
# ----------------------------------------------------------------------------

section('I. Simulate the sweep — what live_tags would contain right now')

# This mirrors src/leap/utils/terminal.py:_jetbrains_sweep_stale_tabs
# building live_tags.  Yuval's session might already be running, in
# which case we use that meta to determine ide/proj — but we don't
# know which tag is the "newly starting" one, so simulate without
# exclude_tag.

live_tags_buggy: list[str] = []     # what the current main branch would compute
live_tags_fixed: list[str] = []     # what my pending fix would compute
excluded_other_ide: list[str] = []
excluded_other_proj: list[str] = []
excluded_dead_pid: list[str] = []
excluded_dead_socket: list[str] = []

for tag, data in meta_data.items():
    if not isinstance(data, dict) or '__ERROR__' in data:
        continue
    meta_ide = data.get('ide')
    meta_proj = data.get('project_path')
    if meta_ide != ide_name:
        excluded_other_ide.append(f'{tag} (ide={meta_ide!r})')
        continue
    if meta_proj != leap_project_path:
        excluded_other_proj.append(f'{tag} (project_path={meta_proj!r})')
        continue
    pid_alive, _ = pid_alive_cache[tag]
    sock_alive, _ = sock_alive_cache[tag]
    if pid_alive:
        live_tags_buggy.append(tag)
    else:
        excluded_dead_pid.append(tag)
    if sock_alive:
        live_tags_fixed.append(tag)
    elif pid_alive and not sock_alive:
        excluded_dead_socket.append(tag)

line(f'live_tags (BUGGY — current main, PID check):  {live_tags_buggy!r}')
line(f'live_tags (FIXED — pending socket-probe fix): {live_tags_fixed!r}')
line('')
line(f'Excluded (different IDE):           {excluded_other_ide!r}')
line(f'Excluded (different project_path):  {excluded_other_proj!r}')
line(f'Excluded (PID dead):                {excluded_dead_pid!r}')
line(f'Excluded by socket but kept by PID: {excluded_dead_socket!r}')

if excluded_dead_socket:
    bug(
        'PID-vs-socket mismatch causes these tags to be wrongly kept '
        f'in live_tags by the buggy code: {excluded_dead_socket!r}.  '
        'Tabs for these tags would survive the sweep.'
    )


# ----------------------------------------------------------------------------
# Section J — Per-tab outcome: what the sweep WILL/WOULD do
# ----------------------------------------------------------------------------

section('J. Per-tab outcome — what the sweep WILL DO to each lps/lpc tab')

# Tabs in the JetBrains project matching leap_project_path are the
# ones the sweep CAN touch.  For each, decide whether the sweep would
# rename it (= the lps prefix would be removed).

tabs_in_my_project: list[str] = []
tabs_in_other_projects: list[tuple[str, str]] = []
for base, name in tabs_all:
    if name.startswith('lps ') or name.startswith('lpc '):
        if base == leap_project_path:
            tabs_in_my_project.append(name)
        else:
            tabs_in_other_projects.append((base, name))

line('')
line(f'lps/lpc tabs in YOUR project ({leap_project_path!r}):')
if not tabs_in_my_project:
    line('  (none)')
else:
    line('')
    line(f'  {"TAB":<35} {"EXPECTED (buggy)":<25} {"EXPECTED (fixed)":<25}')
    line(f'  {"-" * 35} {"-" * 25} {"-" * 25}')
    for name in tabs_in_my_project:
        prefix = 'lps ' if name.startswith('lps ') else 'lpc '
        tag = name[len(prefix):]
        v_buggy = (
            'KEEP (in live_tags)' if tag in live_tags_buggy
            else 'RENAME → bare'
        )
        v_fixed = (
            'KEEP (in live_tags)' if tag in live_tags_fixed
            else 'RENAME → bare'
        )
        line(f'  {repr(name):<35} {v_buggy:<25} {v_fixed:<25}')

line('')
line('lps/lpc tabs in OTHER projects (sweep does not touch them here, but '
     'they get cleaned when you next run Leap in those projects):')
for base, name in tabs_in_other_projects:
    raw(f'    [{base}] {name!r}')
if not tabs_in_other_projects:
    raw('    (none)')

if tabs_in_other_projects:
    info(
        f'{len(tabs_in_other_projects)} stale lps/lpc tab(s) live in '
        'OTHER JetBrains projects (not the one you ran this diagnostic '
        'from).  This is BY DESIGN — they get cleaned the next time '
        'you start a Leap session inside each of those projects.'
    )

# Tabs in my project that the buggy sweep KEEPS but a working sweep
# would RENAME — these are the failing-to-clean cases on current main.
bug_survivors_buggy: list[str] = []
bug_survivors_fixed: list[str] = []
for name in tabs_in_my_project:
    prefix = 'lps ' if name.startswith('lps ') else 'lpc '
    tag = name[len(prefix):]
    # Was the sweep "supposed" to rename this tab?
    # It would rename if the tag is NOT in live_tags AND the tag has
    # a meta file matching our ide+project (otherwise the meta wasn't
    # ever considered).  In practice the sweep DOES rename any tab
    # not in live_tags, regardless of whether a matching meta exists.
    # So: the sweep "should" rename if the tag isn't in live_tags.
    if tag in live_tags_buggy:
        # The buggy code is keeping this tab — and the meta file
        # actually says the session is dead.
        sock_alive, _ = sock_alive_cache.get(tag, (False, 'no meta'))
        if not sock_alive:
            bug_survivors_buggy.append(name)
    if tag in live_tags_fixed:
        sock_alive, _ = sock_alive_cache.get(tag, (False, 'no meta'))
        if not sock_alive:
            bug_survivors_fixed.append(name)

if bug_survivors_buggy:
    bug(
        f'BUGGY CODE WILL FAIL to clean these stale in-project tabs: '
        f'{bug_survivors_buggy!r}.  The meta files claim the sessions '
        'are alive (PID check passes — likely PID reuse) but the '
        'sockets are not accepting connections.  My pending socket-probe '
        'fix would reclassify them as dead and the sweep would then '
        'rename them.'
    )

if bug_survivors_fixed:
    bug(
        f'Even the FIXED code would fail to clean these tabs: '
        f'{bug_survivors_fixed!r}.  Something else is keeping their '
        'sockets alive.  Investigate manually.'
    )


# ----------------------------------------------------------------------------
# Section K — Verdict
# ----------------------------------------------------------------------------

section('K. VERDICT — read this first')

if not FINDINGS_BUG and not FINDINGS_INFO:
    line('No anomalies detected.  Diagnostic suggests the sweep '
         'should work normally.  If you\'re still seeing stale tabs, '
         'send the output to Nevo for further investigation.')
else:
    if FINDINGS_BUG:
        line('🐛 BUGS / blocking issues — these are what likely cause '
             'stale tabs to persist on YOUR machine:')
        for f in FINDINGS_BUG:
            REPORT.append('')
            for ln in f.splitlines():
                raw(f'  ✗ {ln}' if ln == f.splitlines()[0] else f'    {ln}')
    if FINDINGS_INFO:
        line('')
        line('ℹ INFO — not bugs, just things you might want to know:')
        for f in FINDINGS_INFO:
            REPORT.append('')
            for ln in f.splitlines():
                raw(f'  • {ln}' if ln == f.splitlines()[0] else f'    {ln}')


# ----------------------------------------------------------------------------
# Print + save
# ----------------------------------------------------------------------------

out_path = Path('/tmp/leap-sweep-diag.txt')
text = '\n'.join(REPORT) + '\n'
out_path.write_text(text)
print(text)
print(f'(report also saved to {out_path})', file=sys.stderr)
