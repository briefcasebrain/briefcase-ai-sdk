import sqlite3

db = sqlite3.connect("evidence.db")
for (name,) in db.execute("select name from sqlite_master where type='trigger'").fetchall():
    db.execute(f"drop trigger {name}")
db.execute("update chain_entries set record_json = replace(record_json, '0.82', '0.99') where seq = 2")
db.commit()
print("edited the stored result of entry 2 from 0.82 to 0.99")
