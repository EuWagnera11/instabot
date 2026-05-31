import sqlite3
conn = sqlite3.connect("data/instabot.db")
cur = conn.cursor()

# Fix UTC dates -> BRT (subtract 3 hours)
# Posts with 23:35 UTC should be 20:35 BRT
cur.execute("""
    UPDATE scheduled_posts 
    SET scheduled_at = REPLACE(scheduled_at, 'T23:35:00', 'T20:35:00') 
    WHERE scheduled_at LIKE '%T23:35:00' AND status='pending'
""")
print(f"Fixed {cur.rowcount} posts (23:35 UTC -> 20:35 BRT)")

conn.commit()

# Verify
cur.execute("SELECT id, scheduled_at, status FROM scheduled_posts ORDER BY id")
for r in cur.fetchall():
    print(f"  ID={r[0]} | {r[1]} | {r[2]}")

conn.close()
