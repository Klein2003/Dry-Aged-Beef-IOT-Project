import os
import random
from datetime import datetime
 
import altair as alt
import pandas as pd
import requests
import streamlit as st
 
st.set_page_config(page_title="Dry Aged Beef Cabinet", page_icon="🥩", layout="wide")
 
# ───────────── ค่าที่เหมาะสม ─────────────
TEMP_MIN, TEMP_MAX = 1.0, 3.0      # °C
HUM_MIN, HUM_MAX = 75.0, 85.0      # %RH
SHELVES, SLOTS = 3, 3
ALERT_REPEAT_SEC = 300             # ส่งเตือนซ้ำทุก 5 นาที ถ้ายังผิดปกติอยู่
METRICS = {
    "temp": ("อุณหภูมิ", "°C", TEMP_MIN, TEMP_MAX),
    "hum": ("ความชื้น", "%", HUM_MIN, HUM_MAX),
}
 
 
# ───────────── LINE OA (Messaging API) ─────────────
def cfg(key):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key)
 
 
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
 
 
# ───────────── State + Simulator ─────────────
def init_state():
    if "slots" in st.session_state:
        return
    st.session_state.slots = {
        (s, c): {"meat": random.random() < 0.75, "temp": 2.0, "hum": 80.0,
                 "fault_t": 0.0, "fault_h": 0.0}
        for s in range(1, SHELVES + 1) for c in range(1, SLOTS + 1)
    }
    st.session_state.history = []   # ค่าย้อนหลัง
    st.session_state.events = []    # ประวัติแจ้งเตือน
    st.session_state.active = {}    # ปัญหาที่ยังเกิดอยู่
 
 
def read_sensors():
    """จุดนี้คือที่ต้องเปลี่ยนเป็นการอ่านค่าจริง (MQTT / HTTP / DB / Serial)
    ให้อัปเดต slot["meat"], slot["temp"], slot["hum"] ของแต่ละช่อง"""
    for sl in st.session_state.slots.values():
        if sl["meat"]:  # ตู้ปรับให้เข้าช่วงเอง (ดึงกลับเข้าหา 2°C / 80%)
            sl["temp"] += (2 - sl["temp"]) * 0.25 + sl["fault_t"] + random.uniform(-0.08, 0.08)
            sl["hum"] += (80 - sl["hum"]) * 0.25 + sl["fault_h"] + random.uniform(-0.4, 0.4)
        else:
            sl["temp"] += (6 - sl["temp"]) * 0.1 + random.uniform(-0.1, 0.1)
            sl["hum"] += (60 - sl["hum"]) * 0.1 + random.uniform(-0.4, 0.4)
 
 
def deviation(value, lo, hi):
    if value > hi:
        return "สูง", value - hi
    if value < lo:
        return "ต่ำ", lo - value
    return None, 0.0
 
 
def slot_issues(sl):
    """คืน dict ของ metric ที่ผิดปกติ (เฉพาะช่องที่มีเนื้อ)"""
    if not sl["meat"]:
        return {}
    out = {}
    for m, (_, _, lo, hi) in METRICS.items():
        direction, diff = deviation(sl[m], lo, hi)
        if direction:
            out[m] = (direction, diff)
    return out
 
 
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
 
st.markdown('<div class="title">🥩 Dry Aged Beef Cabinet</div>', unsafe_allow_html=True)
st.markdown(f'<div class="sub">เป้าหมาย: อุณหภูมิ {TEMP_MIN:g}–{TEMP_MAX:g}°C · '
            f'ความชื้น {HUM_MIN:g}–{HUM_MAX:g}%</div>', unsafe_allow_html=True)
 
# ── Sidebar ──
with st.sidebar:
    st.header("⚙️ ตั้งค่า")
    live = st.toggle("อัปเดตอัตโนมัติ", value=True)
    every = st.select_slider("รีเฟรชทุก (วินาที)", [1, 2, 3, 5, 10], value=2)
 
    st.subheader("📲 LINE OA")
    if st.button("ส่งข้อความทดสอบ"):
        ok, info = send_line("✅ ทดสอบการแจ้งเตือนจากตู้ Dry Aged")
        (st.success if ok else st.error)(info)
 
    st.subheader("🧪 โหมดจำลอง (ลบออกเมื่อใช้ sensor จริง)")
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
    if live:
        read_sensors()
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
 
    tab1, tab2, tab3 = st.tabs(["📦 ภาพรวมตู้", "📈 กราฟย้อนหลัง", "🔔 ประวัติแจ้งเตือน"])
 
    with tab1:
        for s in range(1, SHELVES + 1):
            st.markdown(f"**ชั้น {s}**")
            cols = st.columns(SLOTS)
            for c, col in zip(range(1, SLOTS + 1), cols):
                sl = slots[(s, c)]
                issues = slot_issues(sl)
                if not sl["meat"]:
                    cls, label = "empty", "ว่าง"
                elif issues:
                    cls, label = "bad", "ผิดปกติ"
                else:
                    cls, label = "ok", "ปกติ"
                tcls = "bad" if "temp" in issues else ""
                hcls = "bad" if "hum" in issues else ""
                notes = "".join(
                    f'<div class="note">{METRICS[m][0]}{d}กว่าช่วง {v:.1f}{METRICS[m][1]}</div>'
                    for m, (d, v) in issues.items())
                col.markdown(
                    f'<div class="card {cls}"><div class="hd"><span>ช่อง {s}-{c}</span>'
                    f'<span class="pill {cls}">{label}</span></div>'
                    f'<div class="vals"><div><small>🌡 อุณหภูมิ</small><b class="{tcls}">{sl["temp"]:.1f}°C</b></div>'
                    f'<div><small>💧 ความชื้น</small><b class="{hcls}">{sl["hum"]:.0f}%</b></div></div>'
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
 
 
dashboard()