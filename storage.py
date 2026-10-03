"""บันทึกข้อมูลลง SQLite

กติกา (เฉพาะช่องที่ตรวจพบเนื้อเท่านั้น):
- hourly          : เก็บค่าทุกๆ 1 ชั่วโมง
- out_of_range    : เก็บทันทีที่อุณหภูมิ/ความชื้นออกนอกเกณฑ์ และเก็บซ้ำทุก ALARM_LOG_SEC
                    ระหว่างที่ยังผิดปกติ (หรือเมื่อปัญหาเปลี่ยน เช่น จากอุณหภูมิสูง → ความชื้นต่ำ)
- back_to_normal  : เก็บตอนที่ค่ากลับเข้าเกณฑ์

แต่ละช่องเก็บแยกตารางกัน: slot_1_1, slot_1_2, ... slot_3_3
"""
import sqlite3
import threading
from datetime import datetime, timedelta

from config import SHELVES, SLOTS, describe_issues, issues_of

TS_FMT = "%Y-%m-%d %H:%M:%S"
RECORD_TYPES = ("hourly", "out_of_range", "back_to_normal")


def table_name(shelf, slot):
    return f"slot_{int(shelf)}_{int(slot)}"


class Recorder:
    def __init__(self, path="dryaged.db", hourly_sec=3600, alarm_sec=60):
        self.path = path
        self.hourly = timedelta(seconds=hourly_sec)
        self.alarm = timedelta(seconds=alarm_sec)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # สถานะต่อช่อง: last_hourly, issues (set ของ metric ที่ผิดปกติ), last_alarm
        self.state = {}
        with self.lock:
            for s in range(1, SHELVES + 1):
                for c in range(1, SLOTS + 1):
                    t = table_name(s, c)
                    self.conn.execute(f"""
                        CREATE TABLE IF NOT EXISTS {t} (
                            id          INTEGER PRIMARY KEY AUTOINCREMENT,
                            ts          TEXT NOT NULL,
                            temp        REAL NOT NULL,
                            hum         REAL NOT NULL,
                            record_type TEXT NOT NULL,
                            detail      TEXT
                        )""")
                    self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{t}_ts ON {t}(ts)")
                    row = self.conn.execute(
                        f"SELECT MAX(ts) FROM {t} WHERE record_type = 'hourly'").fetchone()
                    last = datetime.strptime(row[0], TS_FMT) if row[0] else None
                    self.state[(s, c)] = {"last_hourly": last, "issues": frozenset(),
                                          "last_alarm": None}
            self.conn.commit()

    def _insert(self, key, now, temp, hum, record_type, detail=""):
        self.conn.execute(
            f"INSERT INTO {table_name(*key)} (ts, temp, hum, record_type, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (now.strftime(TS_FMT), round(temp, 2), round(hum, 2), record_type, detail))

    def process(self, key, meat, temp, hum, now=None):
        """เรียกทุกครั้งที่ได้ค่าใหม่จาก sensor ของช่อง key=(ชั้น, ช่อง)
        คืน list ของ record_type ที่ถูกบันทึกในรอบนี้"""
        if key not in self.state:
            return []
        now = now or datetime.now()
        st = self.state[key]
        saved = []
        with self.lock:
            if not meat:
                # ไม่มีเนื้อ → ไม่เก็บอะไร และล้างสถานะผิดปกติ
                st["issues"], st["last_alarm"] = frozenset(), None
                return saved

            if st["last_hourly"] is None or now - st["last_hourly"] >= self.hourly:
                self._insert(key, now, temp, hum, "hourly")
                st["last_hourly"] = now
                saved.append("hourly")

            issues = issues_of(meat, temp, hum)
            keys = frozenset(issues)
            if issues:
                if keys != st["issues"] or st["last_alarm"] is None or \
                        now - st["last_alarm"] >= self.alarm:
                    self._insert(key, now, temp, hum, "out_of_range", describe_issues(issues))
                    st["last_alarm"] = now
                    saved.append("out_of_range")
            elif st["issues"]:
                self._insert(key, now, temp, hum, "back_to_normal", "กลับเข้าช่วงปกติแล้ว")
                st["last_alarm"] = None
                saved.append("back_to_normal")
            st["issues"] = keys

            if saved:
                self.conn.commit()
        return saved

    def fetch(self, shelf, slot, record_types=None, limit=1000):
        sql = f"SELECT ts, temp, hum, record_type, detail FROM {table_name(shelf, slot)}"
        args = []
        if record_types:
            sql += f" WHERE record_type IN ({','.join('?' * len(record_types))})"
            args += list(record_types)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(zip(("ts", "temp", "hum", "record_type", "detail"), r)) for r in rows]

    def counts(self):
        out = {}
        with self.lock:
            for key in self.state:
                out[key] = dict(self.conn.execute(
                    f"SELECT record_type, COUNT(*) FROM {table_name(*key)} "
                    "GROUP BY record_type").fetchall())
        return out
