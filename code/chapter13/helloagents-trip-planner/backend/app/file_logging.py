"""Mirror backend output to a local file for debugging."""
import logging
import os
import sys
from pathlib import Path


class _Tee:
    def __init__(self, stream, file):
        self.stream, self.file = stream, file

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        self.file.flush()
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def configure_file_logging(path=None):
    if getattr(sys.stdout, '_trip_log_tee', False):
        return
    path = Path(path) if path else Path(__file__).resolve().parents[1] / 'logs' / 'backend.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    file = os.fdopen(fd, 'a', encoding='utf-8', buffering=1)
    for name in ('stdout', 'stderr'):
        stream = _Tee(getattr(sys, name), file)
        stream._trip_log_tee = True
        setattr(sys, name, stream)
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(handler)
    for name in ('uvicorn.error', 'uvicorn.access'):
        logging.getLogger(name).addHandler(handler)
