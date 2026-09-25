from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
UPDATER = ROOT / "updater.py"
BASE_COMMIT = "8efa8819f87eb0ec7026be27c6ad420667e53625"

def fetch_base():
    subprocess.run(
        ["git", "fetch", "origin", BASE_COMMIT, "--depth=1"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    r = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:updater.py"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return r.stdout

def must_replace(s, old, new, label):
    if old not in s:
        raise RuntimeError(f"{label}: old block not found")
    return s.replace(old, new, 1)

def patch(s):
    # Add a dedicated structured consensus fallback from MarketBeat.
    anchor = "def _earnings_dates_one(ticker,name,symbol,group):"
    if anchor not in s:
        raise RuntimeError("earnings anchor not found")

    helper = r