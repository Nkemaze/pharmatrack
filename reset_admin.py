import sys
sys.path.insert(0, 'database')
from db import get_db_connection
from werkzeug.security import generate_password_hash

ADMIN_NAME = "Admin"
NEW_PASSWORD = "admin1234"

conn = get_db_connection()
cur = conn.cursor()
cur.execute(
    "UPDATE user SET password_hash = ? WHERE name = ?",
    (generate_password_hash(NEW_PASSWORD), ADMIN_NAME)
)
conn.commit()
print(f"Rows updated: {cur.rowcount}")
conn.close()