import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from zhishi.core.observability import SafeJsonFormatter, WorkerMonitor, RequestMetrics, configure_logging


class ObservabilityTests(unittest.TestCase):
    def test_errors_record_location_without_exception_message_or_arguments(self):
        monitor = WorkerMonitor()
        monitor.register('test-worker')
        def fail():
            raise RuntimeError('private-key customer-message account-id')
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(SafeJsonFormatter())
        with self.assertLogs('zhishi.workers', level='INFO') as captured:
            monitor.run('test-worker', fail)
        record = captured.records[0]
        handler.handle(record)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['error_type'], 'RuntimeError')
        self.assertEqual(result['frames'][-1]['function'], 'fail')
        self.assertNotIn('private-key', stream.getvalue())
        self.assertNotIn('customer-message', stream.getvalue())
        self.assertNotIn('account-id', stream.getvalue())
        self.assertNotIn('private-key', '\n'.join(captured.output))
        self.assertIsNone(record.exc_info)

    def test_failures_are_throttled_recovery_is_logged_and_next_tick_runs(self):
        clock = [0.0]
        monitor = WorkerMonitor(clock=lambda: clock[0])
        monitor.register('reminders')
        def fail():
            raise ValueError('private')
        with self.assertLogs('zhishi.workers', level='INFO') as captured:
            monitor.run('reminders', fail)
            clock[0] = 5
            monitor.run('reminders', fail)
            clock[0] = 61
            monitor.run('reminders', fail)
            monitor.run('reminders', lambda: None)
        self.assertEqual([r.msg for r in captured.records], ['worker_failed', 'worker_failed', 'worker_recovered'])
        item = monitor.snapshot()['reminders']
        self.assertEqual((item['calls'], item['errors'], item['consecutive_errors']), (4, 3, 0))
        self.assertIsNotNone(item['last_success'])
        self.assertIsNone(item['error_type'])
        self.assertFalse(item['busy'])

    def test_metrics_are_aggregated_and_snapshot_is_detached(self):
        metrics = RequestMetrics()
        metrics.record('/api/chat', 200, 25)
        metrics.record('/api/chat', 500, 225)
        result = metrics.snapshot()['/api/chat']
        self.assertEqual((result['calls'], result['errors'], result['avg_ms']), (2, 1, 125))
        result['duration_buckets']['lt50'] = 99
        self.assertEqual(metrics.snapshot()['/api/chat']['duration_buckets']['lt50'], 1)

    def test_logging_rotates_and_ignores_unapproved_extra_fields(self):
        logger = logging.getLogger('zhishi')
        previous = (list(logger.handlers), logger.level, logger.propagate)
        # Do not let explicit startup configuration close a host's logging handlers.
        logger.handlers = []
        try:
            with tempfile.TemporaryDirectory() as directory:
                configure_logging(directory)
                handler = logger.handlers[0]
                handler.maxBytes = 300
                for _ in range(12):
                    logger.info('test_event', extra={'api_key': 'must-not-appear', 'body': 'private'})
                for handler in list(logger.handlers):
                    handler.close()
                logger.handlers = []
                files = list(Path(directory).glob('runtime.log*'))
                self.assertGreater(len(files), 1)
                self.assertLessEqual(len(files), 6)
                for file in files:
                    for line in file.read_text('utf-8').splitlines():
                        self.assertEqual(json.loads(line)['event'], 'test_event')
                        self.assertNotIn('must-not-appear', line)
                        self.assertNotIn('private', line)
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers, logger.level, logger.propagate = previous


if __name__ == '__main__':
    unittest.main()
