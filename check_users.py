import sys
sys.path.insert(0, 'database')
from db import get_db_connection

conn = get_db_connection()
cur = conn.cursor()
cur.execute('SELECT name, role FROM "user"')
for row in cur.fetchall():
    print(row['name'], '-', row['role'])