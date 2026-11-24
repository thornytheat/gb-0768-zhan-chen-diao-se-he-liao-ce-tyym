import os
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'sample_data'
EDITION = 'PALETTE-1.0'
SHEETS = ('weights', 'tares', 'lots', 'claims')
OUTWARD_SECRET = os.getenv('PALETTE_OUTWARD_SECRET', 'palette-outward-dev').encode()
