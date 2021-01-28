import mozprocess
import subprocess

from .base import Browser, ExecutorBrowser, require_arg
from .base import get_timeout_multiplier   # noqa: F401
from .chrome import executor_kwargs as chrome_executor_kwargs
from ..webdriver_server import ChromeDriverServer
from ..executors.executorwebdriver import (WebDriverTestharnessExecutor,  # noqa: F401
                                           WebDriverRefTestExecutor)  # noqa: F401
from ..executors.executorchrome import ChromeDriverWdspecExecutor  # noqa: F401


__wptrunner__ = {"product": "chrome_android",
                 "check_args": "check_args",
                 "browser": "ChromeAndroidBrowser",
                 "executor": {"testharness": "WebDriverTestharnessExecutor",
                              "reftest": "WebDriverRefTestExecutor",
                              "wdspec": "ChromeDriverWdspecExecutor"},
                 "browser_kwargs": "browser_kwargs",
                 "executor_kwargs": "executor_kwargs",
                 "env_extras": "env_extras",
                 "env_options": "env_options",
                 "timeout_multiplier": "get_timeout_multiplier"}

_wptserve_ports = set()


def check_args(**kwargs):
    require_arg(kwargs, "package_name")
    require_arg(kwargs, "webdriver_binary")


def browser_kwargs(logger, test_type, run_info_data, config, **kwargs):
    return {"package_name": kwargs["package_name"],
            "device_serial": kwargs["device_serial"],
            "webdriver_binary": kwargs["webdriver_binary"],
            "webdriver_args": kwargs.get("webdriver_args")}


def executor_kwargs(logger, test_type, server_config, cache_manager, run_info_data,
                    **kwargs):
    # Use update() to modify the global list in place.
    _wptserve_ports.update(set(
        server_config['ports']['http'] + server_config['ports']['https'] +
        server_config['ports']['ws'] + server_config['ports']['wss']
    ))

    executor_kwargs = chrome_executor_kwargs(logger, test_type, server_config,
                                             cache_manager, run_info_data,
                                             **kwargs)
    # Remove unsupported options on mobile.
    del executor_kwargs["capabilities"]["goog:chromeOptions"]["prefs"]

    assert kwargs["package_name"], "missing --package-name"
    executor_kwargs["capabilities"]["goog:chromeOptions"]["androidPackage"] = \
        kwargs["package_name"]
    if kwargs.get("device_serial"):
        executor_kwargs["capabilities"]["goog:chromeOptions"]["androidDeviceSerial"] = \
            kwargs["device_serial"]

    return executor_kwargs


def env_extras(**kwargs):
    return []


def env_options():
    # allow the use of host-resolver-rules in lieu of modifying /etc/hosts file
    return {"server_host": "127.0.0.1"}

class LogcatRunner(object):
    def __init__(self, logger, browser, remote_queue):
        self.logger = logger
        self.browser = browser
        self.remote_queue = remote_queue

    def start(self):
        try:
            self._run()
        except KeyboardInterrupt:
            self.stop()

    def _run(self):
        try:
            self.browser.clear_log()
            self._cmd = self.browser.logcat_cmd()
            self._proc = mozprocess.ProcessHandler(
                self._cmd,
                processOutputLine=self.on_output,
                storeOutput=False)
            self._proc.run()
        except (OSError, subprocess.CalledProcessError):
            self.logger.error("Failed to start adb logcat")

    def _send_message(self, command, *args):
        self.remote_queue.put((command, args))

    def stop(self, force=False):
        if self.is_alive():
            kill_result = self._proc.kill()
            if force and kill_result != 0:
                self._proc.kill(9)

    def is_alive(self):
        return hasattr(self._proc, "proc") and self._proc.poll() is None

    def on_output(self, line):
        try:
            data = {
                "process": "LOGCAT",
                "command": "logcat",
                "data": line
            }
            self._send_message("log", "process_output", data)
        except:
            pass

class AndroidBrowser(Browser):
    def __init__(self, logger,
                 webdriver_binary="chromedriver",
                 remote_queue = None,
                 device_serial=None, webdriver_args=None):
        super(AndroidBrowser, self).__init__(logger)
        self.device_serial = device_serial
        self.remote_queue = remote_queue
        self.server = ChromeDriverServer(self.logger,
                                         binary=webdriver_binary,
                                         args=webdriver_args)
        self.setup_adb_reverse()
        if self.remote_queue is not None:
            self.logcat_runner = LogcatRunner(self.logger,
                                          self, self.remote_queue)

    def setup(self):
        if self.remote_queue is not None:
            self.logcat_runner.start()

    def _adb_run(self, args):
        cmd = ['adb']
        if self.device_serial:
            cmd.extend(['-s', self.device_serial])
        cmd.extend(args)
        self.logger.info(' '.join(cmd))
        subprocess.check_call(cmd)

    def setup_adb_reverse(self):
        pass

    def start(self, **kwargs):
        self.server.start(block=False)

    def stop(self, force=False):
        self.server.stop(force=force)

    def pid(self):
        return self.server.pid

    def is_alive(self):
        # TODO(ato): This only indicates the driver is alive,
        # and doesn't say anything about whether a browser session
        # is active.
        return self.server.is_alive()

    def cleanup(self):
        self.stop()
        self._adb_run(['forward', '--remove-all'])
        self._adb_run(['reverse', '--remove-all'])
        if self.remote_queue is not None:
            self.logcat_runner.stop(force=True)

    def executor_browser(self):
        return ExecutorBrowser, {"webdriver_url": self.server.url}

    def clear_log(self):
        self._adb_run(['logcat', '-c'])

    def logcat_cmd(self):
        cmd = ['adb']
        if self.device_serial:
            cmd.extend(['-s', self.device_serial])
        cmd.extend(['logcat', '*:D'])
        return cmd

class ChromeAndroidBrowser(AndroidBrowser):
    """Chrome is backed by chromedriver, which is supplied through
    ``wptrunner.webdriver.ChromeDriverServer``.
    """

    def __init__(self, logger, package_name,
                 webdriver_binary="chromedriver",
                 remote_queue = None,
                 device_serial=None, webdriver_args=None):
        super(ChromeAndroidBrowser, self).__init__(logger,
                webdriver_binary, remote_queue, device_serial, webdriver_args)
        self.package_name = package_name

    def setup_adb_reverse(self):
        self._adb_run(['wait-for-device'])
        self._adb_run(['forward', '--remove-all'])
        self._adb_run(['reverse', '--remove-all'])
        # "adb reverse" forwards network connection from device to host.
        for port in _wptserve_ports:
            self._adb_run(['reverse', 'tcp:%d' % port, 'tcp:%d' % port])
