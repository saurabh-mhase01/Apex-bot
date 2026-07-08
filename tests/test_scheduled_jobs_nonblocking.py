import threading
import time
import unittest

from main import Scheduled


class ScheduledJobsTests(unittest.TestCase):
    def test_maybe_run_does_not_block_when_job_is_long_running(self):
        started = []
        completed = []

        def slow_job():
            started.append(True)
            time.sleep(0.2)
            completed.append(True)

        job = Scheduled("slow", ["12:00"], slow_job)
        start = time.time()
        job.maybe_run(
            datetime_obj:=__import__("datetime").datetime(2026, 7, 8, 12, 0),
            market_open=True,
        )
        elapsed = time.time() - start

        self.assertLess(elapsed, 0.05)
        self.assertTrue(started)
        job._thread.join(timeout=1)
        self.assertTrue(completed)


if __name__ == "__main__":
    unittest.main()
