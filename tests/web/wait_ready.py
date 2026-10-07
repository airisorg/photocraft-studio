"""Wait for the disposable CI server's database migration, reading JSON only."""
import json
import time
from urllib.error import URLError
from urllib.request import urlopen

deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        with urlopen('http://127.0.0.1:8876/api/config', timeout=2) as response:
            if json.load(response).get('cloud') is True:
                print('Local PhotoCraft cloud is ready')
                break
    except (URLError, TimeoutError, ValueError):
        pass
    time.sleep(1)
else:
    raise SystemExit('Local PhotoCraft cloud did not become ready within 30 seconds')
