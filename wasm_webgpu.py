# -*- python -*-
# ex: set filetype=python:

# Builders that run the unit test suite of https://github.com/juj/wasm_webgpu in web browsers.
# master.cfg imports this file, and calls add_to_config() to add the builders in.

import re
import shlex

from twisted.internet import defer

from buildbot.plugins import changes, schedulers, steps, util
from buildbot.process.results import FAILURE, SKIPPED, SUCCESS

wasm_webgpu_repo = 'https://github.com/juj/wasm_webgpu.git'
wasm_webgpu_branch = 'main'

# (builder name, worker name, os)
builders = [
    ('wasm_webgpu linux x64', 'linux-h12dsi', 'linux'),
    ('wasm_webgpu win11 x64', 'win11-x64-64gb-9800x3d', 'win'),
    ('wasm_webgpu mac27.2 arm64', 'apple-m6-2026-16gb-golden_gate-27', 'mac'),
]

# The Emscripten SDK that the tests are compiled with. 'tot' is the newest precompiled build of the
# Emscripten main branch, so unlike with sdk-main-64bit, LLVM does not get built from source.
emscripten_sdk = 'tot'

# The version of the Python that emsdk installs for its own use on Windows and macOS. Keep this
# the same as the first version that emsdk.bat looks for.
emsdk_python_version = '3.13.3'

# The emsdk packages of the latest stable Firefox, and of the latest stable Chrome for Testing.
emsdk_browsers = ['firefox-latest-64bit', 'chrome-stable-64bit']

# The browsers that come with macOS: (name, path of the .app bundle). A browser that is not
# installed on the worker is skipped.
mac_browsers = [
    ('Safari', '/Applications/Safari.app'),
    ('Safari Technology Preview', '/Applications/Safari Technology Preview.app'),
]

# (name, test.py args). Each of these is one full run of test.py in each browser, i.e. every test
# of the suite built and run in each of the three optimization modes of test.py.
test_variants = [
    ('wasm32', []),
#    ('wasm4gb', ['--wasm4gb']),
#    ('wasm64', ['--wasm64']),
#    ('sanitize', ['--sanitize']),
]

# The whole suite is run in a browser only if these tests pass in it first. The first one ends in
# main(). The second one ends in a callback that is called after main() has returned, like most
# of the tests of the suite do. test.py leaves the browser running when a test fails or times
# out, so if a browser cannot run the tests at all on a worker (e.g. it has no WebGPU support
# there), a run of the whole suite would spend hours in test timeouts, and leave behind a browser
# for each test.
smoke_tests = ['navigator_gpu_available', 'navigator_gpu_request_adapter_async_simple']


def describe_test(cmdline):
    """Shortens the em++ command line of a test to form '<test name> <optimization level>'."""
    name = re.search(r'(?:^|\s)test[/\\](\S+)\.cpp(?:\s|$)', cmdline)
    opt = re.search(r'\s(-O\w)(?:\s|$)', cmdline)
    return ' '.join(m.group(1) for m in (name, opt) if m) or cmdline


class TestPyLineObserver(util.LogLineObserver):
    """Follows the output of test.py. It prints the em++ command line of each test when starting
    it, and after all the tests, a 'FAIL: <em++ command line>' line for each test that failed, and
    a '<passed>/<total> tests passed.' line."""

    def __init__(self):
        super().__init__()
        self.num_started = 0
        self.failed = []
        self.num_passed = None
        self.num_total = None

    def outLineReceived(self, line):
        line = line.strip()
        if line.startswith('em++ '):
            self.num_started += 1
            self.step.setProgress('tests', self.num_started)
        elif line.startswith('FAIL: '):
            self.failed.append(line[len('FAIL: '):])
        else:
            m = re.fullmatch(r'(\d+)/(\d+) tests passed\.', line)
            if m:
                self.num_passed, self.num_total = int(m.group(1)), int(m.group(2))

    errLineReceived = outLineReceived


class WasmWebGpuTestStep(steps.ShellCommand):
    """Runs test.py. It exits with return code zero also when tests fail, so the result of the step
    is read from what test.py printed. If passed_property is given, sets that build property to
    tell whether all the tests passed. skip_reason is shown if doStepIf skips the step."""

    def __init__(self, label=None, passed_property=None, skip_reason=None, **kwargs):
        super().__init__(**kwargs)
        self.label = label
        self.passed_property = passed_property
        self.skip_reason = skip_reason
        self.observer = TestPyLineObserver()
        self.addLogObserver('stdio', self.observer)
        self.progressMetrics += ('tests',)

    def finished_all_tests(self):
        return bool(self.observer.num_total)

    @defer.inlineCallbacks
    def run(self):
        cmd = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)
        # Flush the log, so that the observer has received all of the output.
        stdio_log = yield self.getLog('stdio')
        yield stdio_log.finish()
        o = self.observer
        if o.failed:
            yield self.addCompleteLog('failures', ''.join(f'{describe_test(f)}\n' for f in o.failed))
        passed = not cmd.didFail() and self.finished_all_tests() and o.num_passed == o.num_total
        if self.passed_property:
            self.setProperty(self.passed_property, passed, 'WasmWebGpuTestStep')
        return SUCCESS if passed else FAILURE

    def getResultSummary(self):
        o = self.observer
        if self.results == SKIPPED:
            return {'step': f'{self.label}: skipped, since {self.skip_reason}'}
        if not self.finished_all_tests():
            return {'step': f'{self.label}: test.py did not run the tests to the end', 'build': f'FATAL:{self.label}'}
        if o.num_passed == o.num_total and self.results == SUCCESS:
            return {'step': f'{self.label}: OK (passed={o.num_passed})'}
        text = f'FAIL:{self.label}'
        if o.failed:
            text += f' {describe_test(o.failed[0])}'
            if len(o.failed) > 1:
                text += f' (+{len(o.failed) - 1} more)'
        return {'step': f'{text} (failures={o.num_total - o.num_passed}, passed={o.num_passed})', 'build': text}


def create_build_factory(os, emsdk_branch):
    win = os == 'win'
    emsdk_cmd = 'emsdk' if win else './emsdk'

    def python_cmd(args, cwd=None):
        """Returns the command to run 'python <args>' in the Python and environment of emsdk."""
        if win:
            # %EMSDK_PYTHON% cannot be used here: cmd.exe expands it when it parses the command
            # line, which is before emsdk_env has set it. The same goes for %EMTEST_BROWSER%.
            return 'emsdk_env && ' + (f'cd {cwd} && ' if cwd else '') + f'cmd /v:on /c call "!EMSDK_PYTHON!" {args}'
        script = 'source ./emsdk_env.sh; '
        if os == 'linux':
            script += 'export EMSDK_PYTHON=~/.pyenv/versions/3.13.3/bin/python3; '
        if cwd:
            script += f'cd {cwd}; '
        return ['bash', '-c', script + f'"$EMSDK_PYTHON" {args}']

    def add_test_step(name, label, browser_arg, test_py_args, **kwargs):
        factory.addStep(WasmWebGpuTestStep(name=name, label=label,
                                           command=python_cmd(' '.join(['test.py', '--browser', browser_arg] + test_py_args), cwd='wasm_webgpu'),
                                           haltOnFailure=False,
                                           env={'PYTHONUNBUFFERED': '1', 'PYTHONUTF8': '1'},
                                           **kwargs))
        # Kill the browsers that the failed tests left behind. On macOS a browser also keeps
        # running after its last window has closed. Kill only the browsers that this builder
        # runs: the ones that emsdk installed into this build directory, and Safari.
        add_kill_browsers_step()

    def add_kill_browsers_step():
        if win:
            # On Windows also kill the emrun web servers of this build directory. When a test
            # times out, test.py kills emrun.exe, but that is only a launcher: the Python process
            # that it started keeps running, and keeps serving in the port that the next tests
            # use. They would then all time out, since the test pages would talk to the old server.
            cmd = ('powershell -NoProfile -Command "$d = (Get-Location).Path; Get-Process | Where-Object '
                   "{ $_.Path -like ($d + '\\firefox\\*') -or $_.Path -like ($d + '\\chrome\\*') } | Stop-Process -Force; "
                   "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $d + '\\*emrun.py*') } "
                   '| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"')
        else:
            script = 'pkill -9 -f "$PWD/firefox/"; pkill -9 -f "$PWD/chrome/"; '
            if os == 'mac':
                # The [S] is there so that the pattern does not match the command line of this script itself.
                script += 'pkill -9 -f "/Applications/[S]afari( Technology Preview)?\\.app/"; '
            cmd = ['bash', '-c', script + 'true']
        factory.addStep(steps.ShellCommand(name='kill leftover browsers', command=cmd, alwaysRun=True,
                                           warnOnFailure=False, flunkOnFailure=False,
                                           hideStepIf=lambda results, s: results == SUCCESS))

    factory = util.BuildFactory()
    factory.addStep(steps.Git(name='git emsdk', repourl='https://github.com/emscripten-core/emsdk.git', branch=emsdk_branch,
                              mode='incremental', haltOnFailure=True, alwaysUseLatest=True))
    factory.addStep(steps.Git(name='git wasm_webgpu', repourl=wasm_webgpu_repo, branch=wasm_webgpu_branch,
                              workdir='build/wasm_webgpu', mode='incremental', haltOnFailure=True))

    if win:
        # emsdk.bat runs emsdk.py in the Python that emsdk has installed for itself. A new build
        # directory does not have that Python yet, and emsdk.bat then uses the 'python' of PATH,
        # which on the Windows worker is a dummy that does nothing. So install the Python of emsdk
        # first, with the first python.exe of PATH that manages to do it.
        emsdk_python = f'python\\{emsdk_python_version}_64bit\\python.exe'
        cmd = (f'(for /f "delims=" %%p in (\'where python.exe\') do if not exist {emsdk_python} "%%p" emsdk.py install python-{emsdk_python_version}-64bit)'
               f' & if not exist {emsdk_python} (echo No python.exe of PATH was able to install {emsdk_python} & exit /b 1)')
        factory.addStep(steps.ShellCommand(name='install emsdk python', command=cmd, haltOnFailure=True,
                                           hideStepIf=lambda results, s: results == SUCCESS))

    factory.addStep(steps.ShellCommand(name='emsdk install', command=[emsdk_cmd, 'install', emscripten_sdk] + emsdk_browsers,
                                       haltOnFailure=True, timeout=60*60))
    factory.addStep(steps.ShellCommand(name='emsdk activate', command=[emsdk_cmd, 'activate', emscripten_sdk], haltOnFailure=True))

    # Without psutil, emrun cannot detect that the browser closed after a test called window.close(),
    # and every test would run into the test timeout.
    factory.addStep(steps.ShellCommand(name='check that emrun has psutil', command=python_cmd('-c "import psutil"'),
                                       haltOnFailure=True, hideStepIf=lambda results, s: results == SUCCESS))

    add_kill_browsers_step()

    # (name, the emsdk package to activate or None, the path of the .app bundle or None)
    browsers = [(b.replace('-64bit', ''), b, None) for b in emsdk_browsers]
    if os == 'mac':
        browsers += [(name, None, app) for name, app in mac_browsers]

    i = 1
    num_test_runs = len(browsers) * len(test_variants)
    for browser_name, emsdk_package, app in browsers:
        if emsdk_package:
            # Activating the browser makes emsdk_env set the path of its executable to EMTEST_BROWSER.
            factory.addStep(steps.ShellCommand(name=f'emsdk activate {emsdk_package}', command=[emsdk_cmd, 'activate', emsdk_package],
                                               haltOnFailure=True, hideStepIf=lambda results, s: results == SUCCESS))
            browser_arg = '"!EMTEST_BROWSER!"' if win else '"$EMTEST_BROWSER"'
            is_installed = True
        else:
            # Find out if the browser is installed on the worker. 'test' exits with 1 if it is not.
            installed = f'{browser_name} is installed'
            factory.addStep(steps.SetPropertyFromCommand(name=f'check for {browser_name}', command=['test', '-d', app],
                                                         extract_fn=lambda rc, stdout, stderr, prop=installed: {prop: rc == 0},
                                                         decodeRC={0: SUCCESS, 1: SUCCESS},
                                                         hideStepIf=lambda results, s: results == SUCCESS))
            # emrun splits the path of an .app bundle at spaces, so the spaces in it need to be escaped with backslashes.
            browser_arg = shlex.quote(app.replace(' ', '\\ '))
            is_installed = lambda step, prop=installed: bool(step.getProperty(prop))

        for variant_name, variant_args in test_variants:
            label = f'{browser_name}/{variant_name}'
            smoke_test_passed = f'{label} smoke test passed'
            add_test_step(f'Smoke test {label}', f'{label} smoke test', browser_arg, variant_args + smoke_tests,
                          passed_property=smoke_test_passed,
                          doStepIf=is_installed, skip_reason=f'{browser_name} is not installed')
            add_test_step(f'Test {i}/{num_test_runs} {label}', label, browser_arg, variant_args,
                          doStepIf=lambda step, prop=smoke_test_passed: bool(step.getProperty(prop)),
                          skip_reason='the smoke test did not pass')
            i += 1

        if emsdk_package:
            factory.addStep(steps.ShellCommand(name=f'emsdk deactivate {emsdk_package}', command=[emsdk_cmd, 'deactivate', emsdk_package],
                                               warnOnFailure=True, flunkOnFailure=False, hideStepIf=lambda results, s: results == SUCCESS))

    return factory


def add_to_config(c, emsdk_branch):
    builder_names = [name for name, worker, os in builders]

    c['change_source'].append(changes.GitPoller(wasm_webgpu_repo, workdir='gitpoller-workdir-wasm_webgpu',
                                                branch=wasm_webgpu_branch, pollInterval=30*60))
    c['schedulers'].append(schedulers.SingleBranchScheduler(
                                name='wasm_webgpu',
                                change_filter=util.ChangeFilter(repository=wasm_webgpu_repo, branch=wasm_webgpu_branch),
                                treeStableTimer=None,
                                builderNames=builder_names))
    c['schedulers'].append(schedulers.ForceScheduler(
                                name='force_wasm_webgpu',
                                builderNames=builder_names))

    for name, worker, os in builders:
        c['builders'].append(util.BuilderConfig(name=name, workernames=[worker], factory=create_build_factory(os, emsdk_branch)))
