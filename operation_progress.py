"""Small private JSON-lines progress channel; no audit or document parsing."""
import json
from pathlib import Path

class OperationProgress:
    """Private JSON-lines side channel; filenames/tool logs are never commands."""
    def __init__(self, path=None):
        self.stream = Path(path).open('x', encoding='utf-8') if path else None

    def emit(self, stage, state, done, total, filename=''):
        if self.stream is None:
            return
        try:
            self.stream.write(json.dumps({'version': 1, 'stage': stage, 'state': state,
                                         'done': done, 'total': total, 'file': filename},
                                        ensure_ascii=True) + '\n')
            self.stream.flush()
        except OSError:
            self.stream.close()
            self.stream = None
            print('Live progress unavailable; the audit continues. See Run details.', flush=True)

    def close(self):
        if self.stream is not None:
            self.stream.close()
