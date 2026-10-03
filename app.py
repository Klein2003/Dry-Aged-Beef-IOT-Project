import os
import random
from datetime import datetime
 
import altair as alt
import pandas as pd
import requests
import streamlit as st

from config import (DEFAULTS, HUM_MAX, HUM_MIN, METRICS, SHELVES, SLOTS, TEMP_MAX,
                    TEMP_MIN, issues_of)
from mqtt_bridge import MqttBridge
from storage import Recorder
 
st.set_page_config(page_title="Dry Aged Beef Cabinet", page_icon="🥩", layout="wide")
 
ALERT_REPEAT_SEC = 300             # ส่งเตือนซ้ำทุก 5 นาที ถ้ายังผิดปกติอยู่
STALE_SEC = 60                     # ไม่มีข้อมูลจาก MQTT เกินนี้ = ถือว่าขาดการเชื่อมต่อ
RECORD_LABELS = {"hourly": "รายชั่วโมง", "out_of_range": "ผิดปกติ",
                 "back_to_normal": "กลับสู่ปกติ"}
 
 
# ───────────── LINE OA (Messaging API) ─────────────
def cfg(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, DEFAULTS.get(key))
 
 
def send_line(text):
    token, to = cfg("LINE_CHANNEL_ACCESS_TOKEN"), cfg("LINE_USER_ID")
    if not token or not to:
        return False, "ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID"
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"to": to, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        return r.ok, "ส่งสำเร็จ" if r.ok else f"{r.status_code}: {r.text}"
    except requests.RequestException as e:
        return False, str(e)
 
 
# ───────────── MQTT + SQLite (สร้างครั้งเดียวต่อ process) ─────────────
@st.cache_resource
def get_backend():
    recorder = Recorder(cfg("DB_PATH"), int(cfg("HOURLY_LOG_SEC")), int(cfg("ALARM_LOG_SEC")))
    bridge = MqttBridge(cfg("MQTT_HOST"), cfg("MQTT_PORT"), cfg("MQTT_TOPIC_BASE"), recorder,
                        cfg("MQTT_USERNAME"), cfg("MQTT_PASSWORD")).start()
    return recorder, bridge


# ───────────── State + Simulator ─────────────
def init_state():
    if "slots" in st.session_state:
        return
    st.session_state.slots = {
        (s, c): {"meat": random.random() < 0.75, "temp": 2.0, "hum": 80.0,
                 "fault_t": 0.0, "fault_h": 0.0, "offline": False}
        for s in range(1, SHELVES + 1) for c in range(1, SLOTS + 1)
    }
    st.session_state.history = []   # ค่าย้อนหลัง
    st.session_state.events = []    # ประวัติแจ้งเตือน
    st.session_state.active = {}    # ปัญหาที่ยังเกิดอยู่
 
 
def read_mqtt(bridge):
    """อัปเดตค่าแต่ละช่องจากข้อความ MQTT ล่าสุดที่ ESP32 ส่งมา
    (การบันทึกลง SQLite ทำใน MqttBridge แล้ว ไม่ต้องรอหน้าเว็บ)"""
    data = bridge.snapshot()
    now = datetime.now()
    for key, sl in st.session_state.slots.items():
        d = data.get(key)
        if d is None or (now - d["t"]).total_seconds() > STALE_SEC:
            sl.update(meat=False, offline=True)
            continue
        sl.update(meat=d["meat"], temp=d["temp"], hum=d["hum"], offline=False)


def simulate_sensors():
    for sl in st.session_state.slots.values():
        if sl["meat"]:  # ตู้ปรับให้เข้าช่วงเอง (ดึงกลับเข้าหา 2°C / 80%)
            sl["temp"] += (2 - sl["temp"]) * 0.25 + sl["fault_t"] + random.uniform(-0.08, 0.08)
            sl["hum"] += (80 - sl["hum"]) * 0.25 + sl["fault_h"] + random.uniform(-0.4, 0.4)
        else:
            sl["temp"] += (6 - sl["temp"]) * 0.1 + random.uniform(-0.1, 0.1)
            sl["hum"] += (60 - sl["hum"]) * 0.1 + random.uniform(-0.4, 0.4)
 
 
def slot_issues(sl):
    """คืน dict ของ metric ที่ผิดปกติ (เฉพาะช่องที่มีเนื้อ)"""
    return issues_of(sl["meat"], sl["temp"], sl["hum"])
 
 
def build_alert_text(key, m, direction, diff, since, sl):
    name, unit, lo, hi = METRICS[m]
    s, c = key
    action = "ลดอุณหภูมิ" if (m == "temp" and direction == "สูง") else \
             "เพิ่มอุณหภูมิ" if m == "temp" else \
             "ลดความชื้น" if direction == "สูง" else "เพิ่มความชื้น"
    return (
        f"🚨 แจ้งเตือนตู้ Dry Aged\n"
        f"📍 ชั้น {s} ช่อง {c}\n"
        f"{name}{direction}กว่าที่กำหนด {diff:.1f}{unit} (ช่วงที่เหมาะสม {lo:g}-{hi:g}{unit})\n"
        f"🕒 เกิดเมื่อ {since:%d/%m/%Y %H:%M:%S}\n"
        f"สถานะปัจจุบัน: 🌡 {sl['temp']:.1f}°C | 💧 {sl['hum']:.0f}%\n"
        f"⚙️ ตู้กำลังสั่ง{action}"
    )
 
 
def process_alerts():
    now = datetime.now()
    for key, sl in st.session_state.slots.items():
        issues = slot_issues(sl)
        for m in METRICS:
            k = (key, m)
            act = st.session_state.active.get(k)
            if m in issues:
                direction, diff = issues[m]
                if act is None:
                    act = {"since": now, "last_sent": None}
                    st.session_state.active[k] = act
                due = act["last_sent"] is None or \
                    (now - act["last_sent"]).total_seconds() >= ALERT_REPEAT_SEC
                if due:
                    text = build_alert_text(key, m, direction, diff, act["since"], sl)
                    ok, info = send_line(text)
                    act["last_sent"] = now
                    st.session_state.events.insert(0, {
                        "เวลา": now.strftime("%H:%M:%S"),
                        "ตำแหน่ง": f"ชั้น {key[0]} ช่อง {key[1]}",
                        "เหตุการณ์": f"{METRICS[m][0]}{direction}กว่าช่วง {diff:.1f}{METRICS[m][1]}",
                        "LINE": "✅" if ok else f"⚠️ {info}",
                    })
            elif act is not None:
                del st.session_state.active[k]
                st.session_state.events.insert(0, {
                    "เวลา": now.strftime("%H:%M:%S"),
                    "ตำแหน่ง": f"ชั้น {key[0]} ช่อง {key[1]}",
                    "เหตุการณ์": f"{METRICS[m][0]}กลับเข้าช่วงปกติแล้ว",
                    "LINE": "-",
                })
 
 
def record_history():
    now = datetime.now()
    for (s, c), sl in st.session_state.slots.items():
        if sl["meat"]:
            st.session_state.history.append(
                {"t": now, "slot": f"{s}-{c}", "temp": sl["temp"], "hum": sl["hum"]})
    st.session_state.history = st.session_state.history[-3000:]
 
 
# ───────────── UI ─────────────
st.markdown("""
<style>
.block-container {padding-top: 2rem;}
.title {font-size: 2rem; font-weight: 700; margin-bottom: 0;}
.sub {opacity: .65; margin-bottom: 1rem;}
.card {border-radius: 16px; padding: 14px 16px; margin-bottom: 12px;
       border: 1px solid rgba(128,128,128,.25); background: rgba(128,128,128,.07);}
.card.ok {border-left: 6px solid #22c55e;}
.card.bad {border-left: 6px solid #ef4444; background: rgba(239,68,68,.10);}
.card.empty {border-left: 6px solid #9ca3af; opacity: .6;}
.hd {display: flex; justify-content: space-between; align-items: center; font-weight: 600;}
.pill {font-size: .75rem; padding: 2px 10px; border-radius: 999px; color: #fff;}
.pill.ok {background: #22c55e;} .pill.bad {background: #ef4444;} .pill.empty {background: #9ca3af;}
.vals {display: flex; gap: 18px; margin-top: 10px;}
.vals small {display: block; opacity: .65; font-size: .75rem;}
.vals b {font-size: 1.6rem;}
.vals b.bad {color: #ef4444;}
.note {font-size: .78rem; color: #ef4444; margin-top: 6px;}
</style>
""", unsafe_allow_html=True)
 
init_state()
recorder, bridge = get_backend()
 
st.markdown('<div class="title">🥩 Dry Aged Beef Cabinet</div>', unsafe_allow_html=True)
st.markdown(f'<div class="sub">เป้าหมาย: อุณหภูมิ {TEMP_MIN:g}–{TEMP_MAX:g}°C · '
            f'ความชื้น {HUM_MIN:g}–{HUM_MAX:g}%</div>', unsafe_allow_html=True)
 
# ── Sidebar ──
with st.sidebar:
    st.header("⚙️ ตั้งค่า")
    source = st.radio("แหล่งข้อมูล", ["MQTT (ESP32)", "จำลอง"], horizontal=True)
    use_mqtt = source.startswith("MQTT")
    live = st.toggle("อัปเดตอัตโนมัติ", value=True)
    every = st.select_slider("รีเฟรชทุก (วินาที)", [1, 2, 3, 5, 10], value=2)

    st.subheader("📡 MQTT")
    st.caption(f"`{cfg('MQTT_HOST')}:{cfg('MQTT_PORT')}`  \n"
               f"topic: `{cfg('MQTT_TOPIC_BASE')}/slot/+`")
    if bridge.connected:
        st.success(f"เชื่อมต่อ broker แล้ว · ESP32: {bridge.device_status or 'ไม่ทราบสถานะ'}")
    else:
        st.warning("ยังไม่ได้เชื่อมต่อ broker")
    if bridge.last_error:
        st.caption(f"⚠️ {bridge.last_error}")
    st.caption(f"🗄️ SQLite: `{cfg('DB_PATH')}`")
 
    st.subheader("📲 LINE OA")
    if st.button("ส่งข้อความทดสอบ"):
        ok, info = send_line("✅ ทดสอบการแจ้งเตือนจากตู้ Dry Aged")
        (st.success if ok else st.error)(info)
 
log_sim = False
if not use_mqtt:
    with st.sidebar:
        st.subheader("🧪 โหมดจำลอง")
        log_sim = st.checkbox("บันทึกข้อมูลจำลองลง SQLite ด้วย (ไว้ทดสอบ)", value=False)
        pick = st.selectbox("เลือกช่อง", [f"{s}-{c}" for s in range(1, 4) for c in range(1, 4)])
        key = tuple(int(x) for x in pick.split("-"))
        sl = st.session_state.slots[key]
        sl["meat"] = st.checkbox("มีเนื้ออยู่ในช่อง", value=sl["meat"], key=f"meat_{pick}")
        a, b = st.columns(2)
        if a.button("🔥 อุณหภูมิสูง"):
            sl["fault_t"] = 0.6
        if b.button("💧 ชื้นต่ำ"):
            sl["fault_h"] = -2.0
        if st.button("ล้างความผิดปกติ"):
            sl["fault_t"] = sl["fault_h"] = 0.0
 
 
@st.fragment(run_every=every if live else None)
def dashboard():
    if use_mqtt:
        read_mqtt(bridge)
    elif live:
        for sl in st.session_state.slots.values():
            sl["offline"] = False
        simulate_sensors()
        if log_sim:
            for key, sl in st.session_state.slots.items():
                recorder.process(key, sl["meat"], sl["temp"], sl["hum"])
    process_alerts()
    record_history()
 
    slots = st.session_state.slots
    monitored = [k for k, v in slots.items() if v["meat"]]
    bad = [k for k in monitored if slot_issues(slots[k])]
 
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("ช่องที่มีเนื้อ", f"{len(monitored)} / {SHELVES * SLOTS}")
    k2.metric("ช่องปกติ", len(monitored) - len(bad))
    k3.metric("ช่องผิดปกติ", len(bad))
    if monitored:
        k4.metric("ค่าเฉลี่ย", f"{sum(slots[k]['temp'] for k in monitored) / len(monitored):.1f}°C · "
                              f"{sum(slots[k]['hum'] for k in monitored) / len(monitored):.0f}%")
    else:
        k4.metric("ค่าเฉลี่ย", "-")
 
    if bad:
        st.error("⚠️ ผิดปกติ: " + ", ".join(f"ชั้น {s} ช่อง {c}" for s, c in bad))
    else:
        st.success("ทุกช่องอยู่ในเกณฑ์ปกติ")
 
    tab1, tab2, tab3, tab4 = st.tabs(["📦 ภาพรวมตู้", "📈 กราฟย้อนหลัง", "🔔 ประวัติแจ้งเตือน",
                                      "🗄️ ข้อมูลที่บันทึก (SQLite)"])
 
    with tab1:
        for s in range(1, SHELVES + 1):
            st.markdown(f"**ชั้น {s}**")
            cols = st.columns(SLOTS)
            for c, col in zip(range(1, SLOTS + 1), cols):
                sl = slots[(s, c)]
                issues = slot_issues(sl)
                if sl.get("offline"):
                    cls, label = "empty", "ไม่มีข้อมูล"
                elif not sl["meat"]:
                    cls, label = "empty", "ว่าง"
                elif issues:
                    cls, label = "bad", "ผิดปกติ"
                else:
                    cls, label = "ok", "ปกติ"
                tv, hv = ("-", "-") if sl.get("offline") else \
                    (f'{sl["temp"]:.1f}°C', f'{sl["hum"]:.0f}%')
                tcls = "bad" if "temp" in issues else ""
                hcls = "bad" if "hum" in issues else ""
                notes = "".join(
                    f'<div class="note">{METRICS[m][0]}{d}กว่าช่วง {v:.1f}{METRICS[m][1]}</div>'
                    for m, (d, v) in issues.items())
                col.markdown(
                    f'<div class="card {cls}"><div class="hd"><span>ช่อง {s}-{c}</span>'
                    f'<span class="pill {cls}">{label}</span></div>'
                    f'<div class="vals"><div><small>🌡 อุณหภูมิ</small><b class="{tcls}">{tv}</b></div>'
                    f'<div><small>💧 ความชื้น</small><b class="{hcls}">{hv}</b></div></div>'
                    f'{notes}</div>',
                    unsafe_allow_html=True)
 
    with tab2:
        df = pd.DataFrame(st.session_state.history)
        if df.empty:
            st.info("ยังไม่มีข้อมูล")
        else:
            opts = sorted(df["slot"].unique())
            sel = st.selectbox("เลือกช่อง", opts, key="chart_slot")
            d = df[df["slot"] == sel]
            for col, name, lo, hi, color in [
                ("temp", "อุณหภูมิ (°C)", TEMP_MIN, TEMP_MAX, "#3b82f6"),
                ("hum", "ความชื้น (%)", HUM_MIN, HUM_MAX, "#06b6d4"),
            ]:
                band = alt.Chart(pd.DataFrame({"lo": [lo], "hi": [hi]})).mark_rect(
                    opacity=0.15, color="#22c55e").encode(y="lo:Q", y2="hi:Q")
                line = alt.Chart(d).mark_line(color=color).encode(
                    x=alt.X("t:T", title=None),
                    y=alt.Y(f"{col}:Q", title=name, scale=alt.Scale(zero=False)))
                st.altair_chart((band + line).properties(height=230), use_container_width=True)
            st.caption("แถบสีเขียว = ช่วงที่เหมาะสม")
 
    with tab3:
        if st.session_state.events:
            st.dataframe(pd.DataFrame(st.session_state.events),
                         use_container_width=True, hide_index=True)
        else:
            st.info("ยังไม่มีการแจ้งเตือน")

    with tab4:
        db_records()


def db_records():
    st.caption("บันทึกเฉพาะช่องที่มีเนื้อ: ทุก 1 ชั่วโมง + ตอนที่อุณหภูมิ/ความชื้นออกนอกเกณฑ์ "
               "(แต่ละช่องแยกตาราง slot_<ชั้น>_<ช่อง>)")
    counts = recorder.counts()
    st.dataframe(pd.DataFrame([
        {"ช่อง": f"{s}-{c}", "ตาราง": f"slot_{s}_{c}",
         **{RECORD_LABELS[t]: counts[(s, c)].get(t, 0) for t in RECORD_LABELS}}
        for s, c in sorted(counts)]), use_container_width=True, hide_index=True)

    a, b = st.columns([1, 2])
    pick = a.selectbox("ช่อง", [f"{s}-{c}" for s in range(1, SHELVES + 1)
                                for c in range(1, SLOTS + 1)], key="db_slot")
    types = b.multiselect("ประเภท", list(RECORD_LABELS), default=list(RECORD_LABELS),
                          format_func=RECORD_LABELS.get, key="db_types")
    s, c = (int(x) for x in pick.split("-"))
    df = pd.DataFrame(recorder.fetch(s, c, types), columns=["ts", "temp", "hum", "record_type", "detail"])
    if df.empty:
        st.info("ยังไม่มีข้อมูลที่บันทึกสำหรับช่องนี้")
        return
    df["record_type"] = df["record_type"].map(RECORD_LABELS)
    df.columns = ["เวลา", "อุณหภูมิ (°C)", "ความชื้น (%)", "ประเภท", "รายละเอียด"]
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button("⬇️ ดาวน์โหลด CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"slot_{s}_{c}.csv", mime="text/csv")
 
 
dashboard()