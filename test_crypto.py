import sys
import logging
import sqlite3

from config import DB_PATH
import dashboard_app

try:
    print(dashboard_app.get_crypto_trades())
except Exception as e:
    import traceback
    traceback.print_exc()
