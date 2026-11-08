"""Run-owned bounded JSONL output using standard library logging handlers.

Callers provide encoded records; this module does not serialize simulator data.
"""
from __future__ import annotations

import copy
import hashlib
import logging
from logging.handlers import QueueHandler, QueueListener
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time


class _ResultQueueHandler(QueueHandler):
    def prepare(self, record):
        return record  # Owned local queue; immutable, already encoded C++ JSON.

    def handleError(self, record):
        raise  # QueueHandler.emit otherwise silently swallows queue.Full.


class _ResultFileHandler(logging.FileHandler):
    def __init__(self, path, owner):
        self.owner, self.io_failed, self.current_failed = owner, False, False
        super().__init__(path, mode='x', encoding='utf-8')

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors, newline='\n')

    def handleError(self, record):
        self.io_failed = self.current_failed = True
        self.owner._unwritten.append(record.msg)
        self.owner._fail('file_write_or_flush', sys.exc_info()[1])

    def emit(self, record):
        if self.io_failed:
            self.owner._unwritten.append(record.msg)
            return
        started, cpu = time.monotonic_ns(), time.thread_time_ns()
        self.current_failed = False
        self.owner._in_progress = record.msg
        try:
            super().emit(record)
        except BaseException:
            self.handleError(record)
        finally:
            wall, cpu = time.monotonic_ns()-started, time.thread_time_ns()-cpu
            self.owner._maximum_write_wall_ns = max(self.owner._maximum_write_wall_ns or 0, wall)
            self.owner._maximum_write_thread_cpu_ns = max(self.owner._maximum_write_thread_cpu_ns or 0, cpu)
            self.owner._in_progress = None
        if not self.current_failed:
            self.owner._written_digest.update((record.msg+'\n').encode('utf-8'))
            self.owner._written += 1
            self.owner._written_bytes += record.encoded_bytes


class _ResultQueueListener(QueueListener):
    def __init__(self, queue, handler, owner):
        self.owner, self.file_handler, self.sentinel_sent = owner, handler, False
        super().__init__(queue, handler)

    def _monitor(self):
        self.owner._writer = dict(pid=os.getpid(), thread_id=threading.get_ident(),
                                  native_thread_id=threading.get_native_id())
        try:
            super()._monitor()
        except BaseException as error:
            self.owner._fail('listener', error)
        finally:
            try:
                self.file_handler.close()  # Any blocking final flush stays on the owned writer thread.
            except BaseException as error:
                self.owner._fail('file_close', error)

    def stop(self, timeout_s):
        if self._thread is None:
            return
        deadline = time.monotonic()+timeout_s
        if not self.sentinel_sent:
            try:
                self.queue.put(self._sentinel, timeout=max(0, deadline-time.monotonic()))
            except queue.Full as error:
                raise TimeoutError('native result logger sentinel deadline exceeded') from error
            self.sentinel_sent = True
        self._thread.join(max(0, deadline-time.monotonic()))
        if self._thread.is_alive():
            raise TimeoutError('native result logger writer did not stop before deadline')
        self._thread = None


class AsyncJsonlWriter:
    """Bounded stdlib logging adapter for already encoded, immutable JSONL records.

    Only a successful FileHandler write+flush counts as written, without fsync
    durability claims. A blocked writer cannot be cancelled by Python: close
    reports its owned live thread and the parent process must reclaim it.
    """
    def __init__(self, path, capacity=512, max_record_bytes=65536):
        if type(capacity) is not int or not 1 <= capacity <= 512:
            raise ValueError('result log capacity must be 1..512')
        if type(max_record_bytes) is not int or not 1 <= max_record_bytes <= 65536:
            raise ValueError('result log max_record_bytes must be 1..65536')
        self.path, self.capacity, self.max_record_bytes = str(Path(path).absolute()), capacity, max_record_bytes
        self._accepted = self._written = self._rejected = self._max_queue = 0
        self._accepted_bytes = self._written_bytes = self._error_count = 0
        self._written_digest = hashlib.sha256()
        self._closed = self._closing = self._drained = False
        self._first_exception = self._last_exception = None
        self._maximum_write_wall_ns = self._maximum_write_thread_cpu_ns = None
        self._unwritten, self._in_progress, self._rejected_record = [], None, None
        self._producer = dict(pid=os.getpid(), thread_id=threading.get_ident(), native_thread_id=threading.get_native_id())
        self._writer = None
        self._offer_lock, self._queue = threading.Lock(), queue.Queue(maxsize=capacity)
        self._file_handler = _ResultFileHandler(self.path, self)
        self._file_handler.setFormatter(logging.Formatter('%(message)s'))
        self._queue_handler = _ResultQueueHandler(self._queue)
        self._listener = _ResultQueueListener(self._queue, self._file_handler, self)
        try:
            self._listener.start()
        except BaseException:
            self._file_handler.close(); self._queue_handler.close()
            raise

    def _fail(self, stage, error):
        detail = dict(stage=stage, type=type(error).__name__, message=str(error),
            host_monotonic_ns=time.monotonic_ns(), thread_id=threading.get_ident(),
            native_thread_id=threading.get_native_id())
        self._error_count += 1
        if self._first_exception is None:
            self._first_exception = detail
        self._last_exception = detail

    def check(self):
        if not self._closed and not self._closing and not self._listener._thread.is_alive():
            self._fail('listener', RuntimeError('native result logger stopped unexpectedly'))
        if self._first_exception is not None:
            raise RuntimeError('native result log failed: '+str(self._first_exception))

    def write(self, encoded_str):
        with self._offer_lock:
            if self._closed or self._closing:
                raise RuntimeError('native result log is closing or closed')
            self.check()
            try:
                if type(encoded_str) is not str or not encoded_str or '\n' in encoded_str or '\r' in encoded_str:
                    raise ValueError('one already encoded JSON string without literal line breaks is required')
                size = len(encoded_str.encode('utf-8'))+1  # Includes the unchanged LF terminator.
                if size > self.max_record_bytes:
                    raise ValueError('native result record exceeds the encoded byte limit')
                record = logging.LogRecord('bas.native_results', logging.INFO, '', 0, encoded_str, (), None)
                record.encoded_bytes = size
                self._queue_handler.emit(record)  # Standard put_nowait, never file I/O.
            except Exception as error:
                self._rejected += 1
                if isinstance(error, queue.Full):
                    self._rejected_record = encoded_str
                self._fail('queue_full' if isinstance(error, queue.Full) else 'enqueue', error)
                raise RuntimeError('native result record was not accepted: '+str(error)) from error
            self._accepted += 1
            self._accepted_bytes += size
            self._max_queue = max(self._max_queue, self._queue.qsize())

    def snapshot(self):
        thread = self._listener._thread
        return dict(path=self.path, capacity=self.capacity, max_record_bytes=self.max_record_bytes,
            accepted=self._accepted, written=self._written, failed=len(self._unwritten)+self._rejected,
            rejected=self._rejected, accepted_bytes=self._accepted_bytes, written_bytes=self._written_bytes,
            sha256=self._written_digest.hexdigest(),
            max_queue=self._max_queue, queue_depth=self._queue.qsize(), closed=self._closed,
            drained=self._drained, listener_alive=thread is not None and thread.is_alive(),
            error_count=self._error_count, first_exception=copy.deepcopy(self._first_exception),
            last_exception=copy.deepcopy(self._last_exception), producer=dict(self._producer),
            writer=copy.deepcopy(self._writer), maximum_write_wall_ns=self._maximum_write_wall_ns,
            maximum_write_thread_cpu_ns=self._maximum_write_thread_cpu_ns,
            unwritten_records=list(self._unwritten), in_progress_record=self._in_progress,
            rejected_record=self._rejected_record,
            scope='owned QueueHandler/QueueListener/FileHandler; max_queue is observed after enqueue; write+flush is not fsync')

    def close(self, timeout_s=5):
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError('result log close timeout must be finite and positive')
        with self._offer_lock:
            if self._closed:
                self.check(); return
            self._closing = True
        try:
            self._listener.stop(timeout_s)
        except Exception as error:
            self._fail('close', error)
            raise RuntimeError('native result logger did not close: '+str(error)) from error
        self._queue_handler.close()
        self._closed, self._drained = True, self._queue.empty()
        if not self._drained or self._accepted != self._written:
            if self._first_exception is None:
                self._fail('drain', RuntimeError('accepted records did not all complete write+flush'))
        self.check()
