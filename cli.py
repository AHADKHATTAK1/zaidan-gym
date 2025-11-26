
import sqlite3
import argparse
from datetime import datetime
import pandas as pd

DB = 'gym.db'

def conn():
    return sqlite3.connect(DB)

def add_member(name, phone, admission):
    c = conn(); cur = c.cursor()
    cur.execute("INSERT INTO member (name, phone, admission_date) VALUES (?, ?, ?)", (name, phone, admission))
    member_id = cur.lastrowid
    year = datetime.fromisoformat(admission).year
    for m in range(1,13):
        status = 'N/A' if datetime(year,m,1).date() < datetime.fromisoformat(admission).date() else 'Unpaid'
        cur.execute("INSERT INTO payment (member_id, year, month, status) VALUES (?, ?, ?, ?)", (member_id, year, m, status))
    c.commit(); c.close()
    print('Added', name, 'id=', member_id)

def export_member(member_id, out):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT year, month, status FROM payment WHERE member_id=? ORDER BY year, month", (member_id,))
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=['Year','Month','Status'])
    df.to_excel(out, index=False)
    print('Exported to', out)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd')
    a = sub.add_parser('add'); a.add_argument('--name'); a.add_argument('--phone', default=''); a.add_argument('--admission')
    e = sub.add_parser('export'); e.add_argument('--id', type=int); e.add_argument('--out', default='member.xlsx')
    args = parser.parse_args()
    if args.cmd=='add':
        add_member(args.name, args.phone, args.admission)
    elif args.cmd=='export':
        export_member(args.id, args.out)
    else:
        parser.print_help()
