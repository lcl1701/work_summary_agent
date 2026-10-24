"""Small in-process task runner for the single-user local workbench."""

from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock, Thread
import uuid


class OperationBusy(ValueError):
    def __init__(self, job_id=None):
        super().__init__("已有同步、生成或数据维护操作正在进行，请先查看进度后重试。")
        self.job_id = job_id


class JobManager:
    def __init__(self, operation_lock):
        self.operation_lock = operation_lock
        self.lock = Lock()
        self.jobs = {}
        self.active_id = None

    def current(self):
        return self.get(self.active_id) if self.active_id else None

    def clear_finished(self):
        with self.lock:
            self.jobs = {key: job for key, job in self.jobs.items() if job["status"] == "running"}

    def get(self, job_id):
        with self.lock:
            value = self.jobs.get(job_id)
            return deepcopy(value) if value else None

    def start(self, action, task):
        if not self.operation_lock.acquire(blocking=False):
            raise OperationBusy(self.active_id)
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "action": action, "status": "running", "stage": "queued",
               "message": "任务已开始，正在准备所选范围。", "completed": 0, "total": 0,
               "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self.lock:
            # Keep a bounded number of completed jobs; active tasks are never evicted.
            while len(self.jobs) >= 30:
                self.jobs.pop(next(iter(self.jobs)))
            self.jobs[job_id] = job
            self.active_id = job_id

        def update(**changes):
            allowed = {key: value for key, value in changes.items()
                       if key in {"stage", "message", "completed", "total", "page", "attempt", "wait"}}
            with self.lock:
                self.jobs[job_id].update(allowed)

        def run():
            try:
                result = task(update)
                with self.lock:
                    completed = self.jobs[job_id].get("total", 0)
                    self.jobs[job_id].update(status="succeeded", stage="complete", message="操作已完成。",
                                             completed=completed, result=result)
            except Exception as error:
                # Only explicit, safe application errors are returned to the page.
                from .feishu import FeishuError
                from .reporting import ModelError
                message = str(error) if isinstance(error, (ValueError, FeishuError, ModelError)) else "服务未完成本次操作，请检查本地服务后重试。"
                with self.lock:
                    self.jobs[job_id].update(status="failed", stage="failed", message=message, error=message)
            finally:
                with self.lock:
                    self.jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    if self.active_id == job_id:
                        self.active_id = None
                self.operation_lock.release()

        try:
            Thread(target=run, name="work-summary-job", daemon=True).start()
        except Exception:
            with self.lock:
                self.jobs.pop(job_id, None)
                self.active_id = None
            self.operation_lock.release()
            raise
        return self.get(job_id)
