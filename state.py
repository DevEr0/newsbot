"""Tiny JSON state store (what we've already sent, Telegram offset, digest dates...)."""
import json
import os
import tempfile

DEFAULT = {
    "seeded": False,
    "seen": [],            # uids of every feed item already examined
    "titles": [],          # normalised titles of items we actually sent (fuzzy dedupe)
    "sent_log": [],        # last sent items, used by the digests
    "update_offset": 0,    # Telegram getUpdates offset
    "watch_add": [],       # tickers added with /add
    "watch_remove": [],    # config tickers hidden with /remove
    "digests": {},         # digest name -> ISO date last sent
    "price_alerts": {},    # alert key -> unix time
    "last_price_check": 0,
}


class State:
    def __init__(self, path):
        self.path = path
        self.d = json.loads(json.dumps(DEFAULT))
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.d.update(json.load(f))
            except (OSError, ValueError):
                pass  # corrupt state -> start fresh, bot re-seeds instead of flooding

    def save(self):
        folder = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=folder)
        with os.fdopen(fd, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)  # atomic: never leaves half-written state
