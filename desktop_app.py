
import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
from datetime import datetime

DB = 'gym.db'

def get_conn():
    return sqlite3.connect(DB)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gym Fee Desktop")
        self.geometry("700x500")
        self.create_widgets()
        self.refresh_members()

    def create_widgets(self):
        frame = ttk.Frame(self)
        frame.pack(fill='x', padx=10, pady=10)
        ttk.Label(frame, text="Name").grid(row=0,column=0)
        self.name = ttk.Entry(frame); self.name.grid(row=0,column=1)
        ttk.Label(frame, text="Phone").grid(row=0,column=2)
        self.phone = ttk.Entry(frame); self.phone.grid(row=0,column=3)
        ttk.Label(frame, text="Admission (YYYY-MM-DD)").grid(row=1,column=0)
        self.adm = ttk.Entry(frame); self.adm.grid(row=1,column=1)
        ttk.Button(frame, text="Add Member", command=self.add_member).grid(row=1,column=3)

        self.tree = ttk.Treeview(self, columns=('id','name','admission'), show='headings')
        self.tree.heading('id', text='ID'); self.tree.heading('name', text='Name'); self.tree.heading('admission', text='Admission')
        self.tree.pack(fill='both', expand=True, padx=10, pady=10)
        self.tree.bind('<Double-1>', self.on_member_select)

    def add_member(self):
        name = self.name.get().strip()
        phone = self.phone.get().strip()
        adm = self.adm.get().strip()
        try:
            ad = datetime.fromisoformat(adm).date()
        except:
            messagebox.showerror("Error","Invalid date format")
            return
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO member (name, phone, admission_date) VALUES (?, ?, ?)", (name, phone, adm))
        member_id = cur.lastrowid
        # initialize payments
        year = ad.year
        for m in range(1,13):
            status = 'N/A' if datetime(year, m, 1).date() < ad else 'Unpaid'
            cur.execute("INSERT INTO payment (member_id, year, month, status) VALUES (?, ?, ?, ?)", (member_id, year, m, status))
        conn.commit()
        conn.close()
        self.name.delete(0,'end'); self.phone.delete(0,'end'); self.adm.delete(0,'end')
        self.refresh_members()

    def refresh_members(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, name, admission_date FROM member")
        for r in cur.fetchall():
            self.tree.insert('', 'end', values=r)
        conn.close()

    def on_member_select(self, event):
        item = self.tree.selection()[0]
        member_id = self.tree.item(item)['values'][0]
        PaymentsWindow(self, member_id)

class PaymentsWindow(tk.Toplevel):
    def __init__(self, parent, member_id):
        super().__init__(parent)
        self.member_id = member_id
        self.title("Payments for member "+str(member_id))
        self.geometry("500x500")
        self.tree = ttk.Treeview(self, columns=('id','year','month','status'), show='headings')
        for c in ('id','year','month','status'):
            self.tree.heading(c, text=c)
        self.tree.pack(fill='both', expand=True)
        frame = ttk.Frame(self)
        frame.pack(fill='x')
        ttk.Button(frame, text="Refresh", command=self.refresh).pack(side='left')
        ttk.Button(frame, text="Mark Paid", command=lambda:self.set_status('Paid')).pack(side='left')
        ttk.Button(frame, text="Mark Unpaid", command=lambda:self.set_status('Unpaid')).pack(side='left')
        self.refresh()

    def refresh(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id, year, month, status FROM payment WHERE member_id=? ORDER BY year, month", (self.member_id,))
        for r in cur.fetchall():
            self.tree.insert('', 'end', values=r)
        conn.close()

    def set_status(self, status):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Info","Select a row")
            return
        pid = self.tree.item(sel[0])['values'][0]
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE payment SET status=? WHERE id=?", (status, pid))
        conn.commit(); conn.close()
        self.refresh()

if __name__ == '__main__':
    # ensure DB exists and tables present (simple)
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS member (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        admission_date TEXT NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER,
        year INTEGER,
        month INTEGER,
        status TEXT
    )""")
    conn.commit(); conn.close()

    app = App(); app.mainloop()
