"""织云系统 - 同步定时调度器。

单只守护线程按 sync_settings.interval_seconds 轮询：到点且开关打开就自动跑一趟。
- 调度线程不持有 HTTP 会话，触发人为 NULL（留痕显示"系统调度"）；
- 与手动触发共用 sync_service.RUNNING_LOCK，撞车时跳过本轮而不是排队堆积；
- 设置变更（间隔/开关）由下一轮轮询自然生效，无需重启进程。
"""
import threading

from . import sync_service as svc

POLL_SECONDS = 5


class SyncScheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_run_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sync-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        import time
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._tick()
            except Exception:
                # 调度器绝不能因为一趟异常整体死掉
                pass

    def _tick(self) -> None:
        import time
        s = svc.settings_row()
        if not s["enabled"]:
            self._last_run_at = 0.0
            return
        now = time.time()
        if self._last_run_at and now - self._last_run_at < s["interval_seconds"]:
            return
        self._last_run_at = now
        try:
            svc.run_sync("scheduled", None)
        except svc.SyncRunningError:
            # 上一趟（或手动趟）还在跑：本轮跳过，下个间隔再试
            pass


scheduler = SyncScheduler()
