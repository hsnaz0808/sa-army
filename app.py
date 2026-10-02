import os, re, json, random, sqlite3, urllib.parse
from contextlib import closing
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY"))
MODEL = "openrouter/free"
DB = "sa_army_v2.db"
COMMISSION = 200
EE_WHATSAPP = os.getenv("EE_WHATSAPP", "60123456789")   # demo number
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")        # empty = no admin login (demo)
LEAD_STATUSES = ["New", "Contacted", "Quoted", "Sold", "Lost"]
now = lambda: datetime.now().isoformat(timespec="seconds")

# ---------------- DB ----------------
def q(sql, p=()):
    with closing(sqlite3.connect(DB)) as c:
        return pd.read_sql(sql, c, params=p)

def ex(sql, p=()):
    with closing(sqlite3.connect(DB)) as c:
        cur = c.execute(sql, p)
        c.commit()
        return cur.lastrowid

def log(kind, desc):
    ex("INSERT INTO activity_log (event_type, description, created_at) VALUES (?,?,?)", (kind, desc, now()))

def init_db():
    with closing(sqlite3.connect(DB)) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dealerships (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, brand TEXT, area TEXT, phone TEXT, status TEXT DEFAULT 'Not contacted');
        CREATE TABLE IF NOT EXISTS sas (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, dealership_id INTEGER, phone TEXT UNIQUE, pin TEXT, referral_code TEXT UNIQUE, status TEXT DEFAULT 'Signed up', joined_at TEXT);
        CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, sa_id INTEGER, customer_name TEXT, customer_phone TEXT, car_model TEXT, area TEXT, status TEXT DEFAULT 'New', created_at TEXT);
        CREATE TABLE IF NOT EXISTS commissions (id INTEGER PRIMARY KEY AUTOINCREMENT, sa_id INTEGER, lead_id INTEGER UNIQUE, amount REAL, status TEXT DEFAULT 'Payable', created_at TEXT, paid_at TEXT);
        CREATE TABLE IF NOT EXISTS outreach (id INTEGER PRIMARY KEY AUTOINCREMENT, dealership_id INTEGER, message TEXT, status TEXT DEFAULT 'Approved', created_at TEXT, recipient TEXT, recipient_phone TEXT);
        CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, description TEXT, created_at TEXT);
        """)
        have = [r[1] for r in c.execute("PRAGMA table_info(outreach)")]   # migrate older DB files
        for col in ("recipient", "recipient_phone"):
            if col not in have: c.execute(f"ALTER TABLE outreach ADD COLUMN {col} TEXT")

def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if d.startswith("0"): d = "6" + d
    return d

# ---------------- AI ----------------
def llm(prompt, temp=0.7):
    try:
        r = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": prompt}], temperature=temp)
        return r.choices[0].message.content.strip()
    except Exception as e:
        st.error(f"AI error: {e}")
        return None

def gen_outreach(dealer, brand, area, sa_name=None):
    who = f"Sales Advisor {sa_name} at {dealer} ({brand}, {area})" if sa_name else f"a Sales Advisor at {dealer} ({brand}, {area})"
    return llm(f"""Write a short WhatsApp message from Energi Elite (Malaysian EV home charger company) to {who}.
Goal: invite them to our referral programme: RM200 for every home charger sold through their referral.
Facts you may use (do not invent anything else): our E1 home charger is 7kW / 32A, TUV CE certified, with free charger relocation within 5km, and works for every EV brand and model.
Rules: casual Malaysian English (a little Bahasa ok), max 4 sentences, warm not spammy, mention RM200, max 1 emoji, end with a soft question.
Return ONLY the message.""")

def gen_nudge(name):
    return llm(f"""Write a 2-sentence friendly WhatsApp nudge from Energi Elite to SA {name} who hasn't sent a lead in a while.
Remind them RM200 per charger, and that sending a lead takes 30 seconds. Casual Malaysian English, max 1 emoji. Return ONLY the message.""")

def regex_lead(msg):
    msg = re.sub(r"(?im)^ref:.*$", "", msg)
    ph = re.search(r"\+?\d[\d\- ]{8,12}\d", msg)
    body = re.sub(r"(?i)^\s*new lead\s*:?", "", msg)
    parts = [x.strip() for x in re.split(r"[,\n]", body) if x.strip()]
    rest = [x for x in parts if not (ph and ph.group(0) in x)]
    g = lambda i: rest[i] if len(rest) > i else None
    return {"customer_name": g(0), "customer_phone": ph.group(0) if ph else None, "car_model": g(1), "area": g(2)}

def parse_lead(msg):
    text = llm(f"""Extract lead info from this WhatsApp message from a car Sales Advisor.
Message: "{msg}"
Return ONLY JSON with keys customer_name, customer_phone, car_model, area (null if missing).""", temp=0)
    if text:
        try:
            return json.loads(text.replace("```json", "").replace("```", "").strip())
        except Exception:
            pass
    return regex_lead(msg)   # fallback if the AI fails / the free model returns garbage

def save_lead(sa_id, p):
    if not any(p.get(k) for k in ("customer_name", "customer_phone", "car_model", "area")):
        return False, "No lead info found in the message."
    ph = norm_phone(p.get("customer_phone"))
    if ph and len(q("SELECT id FROM leads WHERE customer_phone=?", (ph,))):
        return False, "This lead already exists (same customer phone). Duplicate avoided."
    ex("INSERT INTO leads (sa_id, customer_name, customer_phone, car_model, area, created_at) VALUES (?,?,?,?,?,?)",
       (sa_id, p.get("customer_name"), ph or None, p.get("car_model"), p.get("area"), now()))
    ex("UPDATE sas SET status='Activated' WHERE id=?", (sa_id,))
    log("lead", f"New lead from SA #{sa_id}: {p.get('customer_name')} ({p.get('car_model')})")
    return True, "Lead received!"

def set_lead_status(lead_id, status):
    ex("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
    if status == "Sold":   # commission is created ONLY when the charger is actually sold
        sa = q("SELECT sa_id FROM leads WHERE id=?", (lead_id,)).iloc[0]["sa_id"]
        if not len(q("SELECT id FROM commissions WHERE lead_id=?", (lead_id,))):
            ex("INSERT INTO commissions (sa_id, lead_id, amount, created_at) VALUES (?,?,?,?)", (int(sa), lead_id, COMMISSION, now()))
            log("sale", f"Lead #{lead_id} SOLD, RM{COMMISSION} commission for SA #{sa}")

def seed_demo():
    r = random.Random(7)
    brands = [("BYD", "Petaling Jaya"), ("BYD", "Cheras"), ("Volvo", "Shah Alam"), ("Volvo", "Mont Kiara"), ("BMW", "Jalan Tun Razak"),
              ("BMW", "Subang Jaya"), ("Mercedes-Benz", "Petaling Jaya"), ("Mercedes-Benz", "Klang"), ("XPeng", "Puchong"),
              ("XPeng", "Bangsar"), ("Zeekr", "Damansara"), ("Proton eMas", "Shah Alam")]
    models = {"BYD": ["Atto 3", "Seal", "Dolphin", "Sealion 7"], "Volvo": ["EX30", "XC40 Recharge", "EX90"], "BMW": ["iX1", "i4", "iX3"],
              "Mercedes-Benz": ["EQA", "EQB", "EQE"], "XPeng": ["G6", "G9", "P7+"], "Zeekr": ["001", "7X", "X"], "Proton eMas": ["eMas 7", "eMas 5"]}
    areas = ["Petaling Jaya", "Shah Alam", "Subang Jaya", "Cheras", "Bangsar", "Puchong", "Klang", "Kajang", "Damansara", "Cyberjaya", "Mont Kiara", "Ampang"]
    cust = ["Ali", "Mei Ling", "Kumar", "Nurul", "Daniel", "Farah", "Wong", "Hakim", "Priya", "Azlan", "Chong", "Sarah", "Imran", "Jessica", "Zul", "Aisha"]
    ago = lambda d: (datetime.now() - timedelta(days=d)).isoformat(timespec="seconds")
    sas = [("Ahmad Faizal", 0, "active", [(2, "Sold"), (6, "Quoted"), (12, "Sold"), (1, "New")]),
           ("Siti Nurhaliza", 1, "active", [(3, "Contacted"), (9, "Sold"), (20, "Lost")]),
           ("Raj Kumar", 2, "active", [(19, "Sold"), (26, "Lost")]),            # dormant
           ("Nurul Aina", 4, "active", [(4, "Quoted"), (8, "New")]),
           ("Jason Tan", 6, "active", [(5, "Sold"), (11, "Contacted"), (15, "Quoted")]),
           ("Farid Hakim", 8, "active", [(22, "Contacted"), (28, "Lost")]),      # dormant
           ("Priya Devi", 9, "active", [(1, "New"), (7, "Sold")]),
           ("Amirul Hafiz", 3, "signed", []), ("Lim Wei Jie", 10, "signed", []),
           ("Syafiqah Ismail", 5, "invited", []), ("Kevin Ong", 7, "invited", []), ("Aisyah Rahman", 11, "invited", []),
           ("Muthu Samy", 0, "invited", []), ("Zulkifli Aziz", 2, "invited", [])]
    with closing(sqlite3.connect(DB)) as c:
        for t in ("commissions", "leads", "outreach", "sas", "dealerships", "activity_log"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM sqlite_sequence")
        dids = []
        for i, (b, a) in enumerate(brands):
            dids.append(c.execute("INSERT INTO dealerships (name, brand, area, phone, status) VALUES (?,?,?,?,?)",
                (f"[DEMO] {b} {a}", b, a, f"6011000{i:04d}", "Contacted" if i < 8 else "Not contacted")).lastrowid)
            if i < 8:
                c.execute("INSERT INTO outreach (dealership_id, message, status, created_at, recipient, recipient_phone) VALUES (?,?,?,?,?,?)",
                    (dids[-1], f"Hi, we are from Energi Elite. For every E1 charger sold through your referral at {b} {a}, you earn RM200. Interested to know more?", "Sent", ago(20 - i), f"[DEMO] {b} {a}", f"6011000{i:04d}"))
        for n, (name, di, state, plan) in enumerate(sas, 1):
            code = f"EE-{name.split()[0].upper()[:6]}{n:02d}" if state != "invited" else None
            joined = ago(max([d for d, _ in plan], default=5) + 3) if state == "active" else ago(5 if state == "signed" else 2)
            sid = c.execute("INSERT INTO sas (name, dealership_id, phone, pin, referral_code, status, joined_at) VALUES (?,?,?,?,?,?,?)",
                (name, dids[di], f"6011001{n:04d}", None if state == "invited" else "1234", code,
                 {"active": "Activated", "signed": "Signed up", "invited": "Invited"}[state], joined)).lastrowid
            if state != "invited":
                c.execute("INSERT INTO activity_log (event_type, description, created_at) VALUES (?,?,?)", ("signup", f"New SA: {name} ({code})", joined))
            for d, stat in plan:
                model = r.choice(models[brands[di][0]])
                cname = r.choice(cust)
                lid = c.execute("INSERT INTO leads (sa_id, customer_name, customer_phone, car_model, area, status, created_at) VALUES (?,?,?,?,?,?,?)",
                    (sid, cname, f"6012{r.randint(1000000, 9999999)}", f"{brands[di][0]} {model}", r.choice(areas), stat, ago(d))).lastrowid
                c.execute("INSERT INTO activity_log (event_type, description, created_at) VALUES (?,?,?)", ("lead", f"New lead from {name}: {cname} ({model})", ago(d)))
                if stat == "Sold":
                    paid = d >= 10
                    c.execute("INSERT INTO commissions (sa_id, lead_id, amount, status, created_at, paid_at) VALUES (?,?,?,?,?,?)",
                        (sid, lid, COMMISSION, "Paid" if paid else "Payable", ago(d), ago(max(d - 3, 0)) if paid else None))
        c.commit()

# ---------------- UI helpers ----------------
st.set_page_config(page_title="SA Army | Energi Elite", page_icon="⚡", layout="wide")
init_db()
st.markdown("""<style>
#MainMenu, footer {visibility:hidden;}
.block-container {padding-top:1.5rem; max-width:1150px;}
.hero {background:linear-gradient(135deg,#0f766e,#14b8a6); color:white; padding:22px 28px; border-radius:16px; margin-bottom:18px;}
.hero h1 {margin:0; font-size:1.7rem;} .hero p {margin:4px 0 0; opacity:.9;}
[data-testid="stMetric"] {background:#f8fafc; border:1px solid #e2e8f0; padding:14px 16px; border-radius:12px;}
[data-testid="stMetricValue"] {color:#0f766e;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {color:#334155 !important; font-weight:600;}
.stButton>button, .stLinkButton>a {border-radius:10px; font-weight:600;}
</style>""", unsafe_allow_html=True)

def hero(title, sub):
    st.markdown(f'<div class="hero"><h1>{title}</h1><p>{sub}</p></div>', unsafe_allow_html=True)

def flash(msg):
    st.session_state["_flash"] = msg

def target(phone):
    """Test mode ON (default): every WhatsApp link goes to the test number, never a real person."""
    if st.session_state.get("test_mode", True):
        return norm_phone(st.session_state.get("test_number", "60143912303"))
    return norm_phone(phone)

def wa_link(phone, text):
    return f"https://wa.me/{phone}?text={urllib.parse.quote(text)}"

def sa_login():
    if "sa_id" in st.session_state:
        return st.session_state["sa_id"]
    st.info("Log in with your WhatsApp number + 4-digit PIN.")
    with st.form("login"):
        ph = st.text_input("WhatsApp number")
        pin = st.text_input("PIN", type="password", max_chars=4)
        if st.form_submit_button("Log in", type="primary"):
            r = q("SELECT id FROM sas WHERE phone=? AND pin=?", (norm_phone(ph), pin))
            if len(r):
                st.session_state["sa_id"] = int(r.iloc[0]["id"]); st.rerun()
            else:
                st.error("Wrong number or PIN.")
    return None

# ---------------- Sidebar ----------------
st.sidebar.title("⚡ SA Army")
role = st.sidebar.radio("Mod", ["🛠 Admin", "📱 SA Portal"], horizontal=True)
if role == "🛠 Admin":
    if ADMIN_PASSWORD and st.sidebar.text_input("Admin password", type="password") != ADMIN_PASSWORD:
        st.warning("Enter the admin password in the sidebar."); st.stop()
    page = st.sidebar.radio("Menu", ["📊 Dashboard", "🏢 Dealerships", "✉️ Outreach", "👥 SAs", "📋 Leads & Commissions", "📜 Activity"])
    with st.sidebar.expander("🧪 Demo data"):
        st.caption("Deletes ALL data and loads demo data.")
        if st.checkbox("I understand") and st.button("Load demo data"):
            seed_demo(); st.session_state.pop("sa_id", None)
            flash("Demo data loaded. Demo SA login: 0110010001 / PIN 1234 (Ahmad Faizal)."); st.rerun()
    st.sidebar.checkbox("🔒 Test mode (WhatsApp goes to the test number only)", value=True, key="test_mode")
    st.sidebar.text_input("Test number", "60143912303", key="test_number")
else:
    page = st.sidebar.radio("Menu", ["📝 Sign Up", "💬 Send Lead", "💰 My Dashboard", "🏆 Leaderboard"])
    if "sa_id" in st.session_state and st.sidebar.button("Log out"):
        del st.session_state["sa_id"]; st.rerun()

if "_flash" in st.session_state:
    st.success(st.session_state.pop("_flash"))

# ================= ADMIN =================
if page == "📊 Dashboard":
    hero("Dashboard", "Where does the SA journey leak the most? See the funnel below.")
    d, s, l = q("SELECT * FROM dealerships"), q("SELECT * FROM sas"), q("SELECT * FROM leads")
    if d.empty and s.empty:
        st.info("Database is empty. Open the sidebar → 🧪 Demo data, or import a CSV in the Dealerships menu.")
    paid = q("SELECT * FROM commissions WHERE status='Paid'")
    reg = s[s.status != "Invited"]
    vals = [len(d), int((d.status != "Not contacted").sum()), len(s), len(reg), int(l.sa_id.nunique()),
            int(l[l.status == "Sold"].sa_id.nunique()), int(paid.sa_id.nunique())]
    names = ["1. Dealerships", "2. Contacted", "3. SAs invited", "4. SAs signed up", "5. SAs sent a lead", "6. Has a sale", "7. Paid"]
    due = q("SELECT COALESCE(SUM(amount),0) a FROM commissions WHERE status='Payable'").iloc[0]["a"]
    c = st.columns(5)
    c[0].metric("SAs invited", len(s), help="All SAs in our list, including those who have not signed up yet."); c[1].metric("SAs signed up", len(reg), help="SAs who created their own account (status is not Invited)."); c[2].metric("Total leads", len(l), help="Buyers referred by SAs so far.")
    c[3].metric("Sales (Sold)", int((l.status == "Sold").sum()), help="Leads that became a charger sale. Each one earns the SA RM200."); c[4].metric("Unpaid commission", f"RM{due:.0f}", help="RM200 commissions for sold chargers that we have not paid yet.")
    st.subheader("Funnel")
    st.bar_chart(pd.DataFrame({"Stage": names, "Count": vals}).set_index("Stage"), color="#14b8a6")
    drops = [(1 - vals[i + 1] / vals[i], i) for i in range(2, 6) if vals[i]]
    if drops and max(drops)[0] > 0:
        pct, i = max(drops)
        st.error(f"🔻 Biggest leak: {names[i][3:]} → {names[i + 1][3:]} (lost {pct:.0%}). Fix this first.")
    if vals[3] > vals[4]:
        st.warning(f"{vals[3] - vals[4]} SAs have signed up but not yet sent their first lead (activation problem).")

elif page == "🏢 Dealerships":
    hero("Dealerships", "EV dealership list for KL & Selangor.")
    d = q("SELECT * FROM dealerships"); st.dataframe(d, use_container_width=True, hide_index=True)
    t1, t2 = st.tabs(["Import CSV", "Add manually"])
    with t1:
        st.caption("Columns: name, brand, area, phone. Export from Google Maps / Places API / your own list.")
        up = st.file_uploader("CSV", type="csv")
        if up and st.button("Import"):
            df = pd.read_csv(up).fillna("")
            for _, r in df.iterrows():
                if not len(q("SELECT id FROM dealerships WHERE name=? AND area=?", (r["name"], r["area"]))):
                    ex("INSERT INTO dealerships (name, brand, area, phone) VALUES (?,?,?,?)", (r["name"], r["brand"], r["area"], norm_phone(str(r["phone"]))))
            log("dealer", f"Import {len(df)} dealership"); st.rerun()
    with t2:
        with st.form("adddealer", clear_on_submit=True):
            a, b, c2, e = st.columns(4)
            n, br, ar, ph = a.text_input("Name"), b.text_input("Brand"), c2.text_input("Area"), e.text_input("Phone")
            if st.form_submit_button("Add") and n:
                ex("INSERT INTO dealerships (name, brand, area, phone) VALUES (?,?,?,?)", (n, br, ar, norm_phone(ph))); st.rerun()

elif page == "✉️ Outreach":
    hero("Outreach", "AI drafts, a human approves, then it is sent. Demo: test numbers only.")
    d = q("SELECT * FROM dealerships")
    opts = {f"{r['name']} ({r['brand']}, {r['area']})": r["id"] for _, r in d.iterrows()}
    pick = st.selectbox("Dealership", list(opts))
    row = d[d.id == opts[pick]].iloc[0]
    if st.button("✨ Generate message"):
        with st.spinner("AI is writing..."):
            m = gen_outreach(row["name"], row["brand"], row["area"])
            if m: st.session_state["draft_box"] = m
    if "draft_box" in st.session_state:
        st.text_area("Draft (editable before approval)", key="draft_box", height=130)
        if st.button("✅ Approve & save", type="primary"):
            ex("INSERT INTO outreach (dealership_id, message, created_at, recipient, recipient_phone) VALUES (?,?,?,?,?)",
               (int(row["id"]), st.session_state["draft_box"], now(), row["name"], row["phone"]))
            log("outreach", f"Approved outreach for {row['name']}")
            del st.session_state["draft_box"]
            flash("✅ Saved to 'Approved messages' below. Open WhatsApp, then press 'Mark as sent' so the dealership counts as Contacted."); st.rerun()
        st.divider(); st.subheader("Approved messages")
    st.caption("Status flow: Approved → (you send it on WhatsApp) → Sent. Sent messages disappear from this list.")
    msgs = q("""SELECT o.*, COALESCE(o.recipient, d.name) who, COALESCE(o.recipient_phone, d.phone) ph
                FROM outreach o JOIN dealerships d ON d.id=o.dealership_id
                WHERE o.status != 'Sent' ORDER BY o.id DESC""")
    if msgs.empty:
        st.success("No messages waiting. Everything has been sent 🎉")
    for n, o in msgs.iterrows():
        with st.expander(f"{o['who']}  ·  {o['status']}  ·  {o['created_at']}", expanded=(n == 0)):
            st.write(o["message"])
            a, b = st.columns(2)
            a.link_button("Open WhatsApp", wa_link(target(o["ph"]), o["message"]), use_container_width=True)
            if b.button("📤 Mark as sent", key=f"sent{o['id']}", use_container_width=True):
                ex("UPDATE outreach SET status='Sent' WHERE id=?", (int(o["id"]),))
                ex("UPDATE dealerships SET status='Contacted' WHERE id=?", (int(o["dealership_id"]),))
                log("outreach", f"Marked as sent: {o['who']}"); st.rerun()

    with st.expander("📂 Sent message history (last 10)"):
        done = q("""SELECT COALESCE(o.recipient, d.name) recipient, o.created_at, o.message
                    FROM outreach o JOIN dealerships d ON d.id=o.dealership_id
                    WHERE o.status='Sent' ORDER BY o.id DESC LIMIT 10""")
        st.dataframe(done, use_container_width=True, hide_index=True)

elif page == "👥 SAs":
    hero("Sales Advisors", "Import a target list, personal outreach, keep SAs active.")
    with st.expander("📥 Import SA list (CSV)"):
        st.caption("Columns: name, phone, dealership. Status stays 'Invited' until the SA signs up themselves (SA must consent, PDPA).")
        up = st.file_uploader("SA CSV", type="csv", key="sacsv")
        if up and st.button("Import SA"):
            df, n = pd.read_csv(up, dtype=str).fillna(""), 0
            for _, r in df.iterrows():
                ph = norm_phone(r["phone"])
                if len(ph) < 10 or len(q("SELECT id FROM sas WHERE phone=?", (ph,))): continue
                dm = q("SELECT id FROM dealerships WHERE LOWER(name)=LOWER(?)", (r["dealership"],))
                did = int(dm.iloc[0]["id"]) if len(dm) else ex("INSERT INTO dealerships (name) VALUES (?)", (r["dealership"],))
                ex("INSERT INTO sas (name, dealership_id, phone, status, joined_at) VALUES (?,?,?,'Invited',?)", (r["name"], did, ph, now()))
                n += 1
            log("import", f"Import {n} SA (Invited)"); flash(f"{n} SAs imported (duplicates/invalid numbers skipped)."); st.rerun()
    s = q("""SELECT s.id, s.name, s.dealership_id, d.name dealership, s.phone, s.referral_code, s.status, s.joined_at,
             (SELECT MAX(created_at) FROM leads WHERE sa_id=s.id) last_lead FROM sas s LEFT JOIN dealerships d ON d.id=s.dealership_id""")
    st.dataframe(s.drop(columns=["dealership_id"]), use_container_width=True, hide_index=True)

    st.subheader("✉️ Personal outreach to SAs (Invited)")
    inv = s[s.status == "Invited"]
    if inv.empty:
        st.info("No SAs with Invited status.")
    else:
        info = inv.set_index("id")
        pick = int(st.selectbox("SA", info.index, format_func=lambda i: f"{info.loc[i, 'name']} · {info.loc[i, 'dealership']}"))
        dl = q("SELECT d.id, d.name, d.brand, d.area FROM sas s JOIN dealerships d ON d.id=s.dealership_id WHERE s.id=?", (pick,)).iloc[0]
        if st.button("✨ Generate personal message"):
            with st.spinner("AI is writing..."):
                m = gen_outreach(dl["name"], dl["brand"] or "EV", dl["area"] or "KL/Selangor", info.loc[pick, "name"])
                if m: st.session_state["sa_draft"] = m
        if "sa_draft" in st.session_state:
            st.text_area("Draft (edit first, human approves)", key="sa_draft", height=120)
            a, b = st.columns(2)
            a.link_button("Open WhatsApp", wa_link(target(info.loc[pick, "phone"]), st.session_state["sa_draft"]), use_container_width=True)
            if b.button("✅ Approve & save", type="primary", use_container_width=True):
                ex("INSERT INTO outreach (dealership_id, message, created_at, recipient, recipient_phone) VALUES (?,?,?,?,?)",
                   (int(dl["id"]), st.session_state["sa_draft"], now(), info.loc[pick, "name"], info.loc[pick, "phone"]))
                log("outreach", f"Approved personal outreach for {info.loc[pick, 'name']}")
                del st.session_state["sa_draft"]
                flash("✅ Saved. Find it in Outreach → Approved messages, and mark it as sent after WhatsApp."); st.rerun()

    st.subheader("😴 Dormant SAs (no lead in 14 days)")
    cut = (datetime.now() - timedelta(days=14)).isoformat()
    ll = s.last_lead.fillna("")
    dorm = s[(s.status != "Invited") & (((ll == "") & (s.joined_at < cut)) | ((ll != "") & (ll < cut)))]
    if dorm.empty: st.success("All SAs are still active.")
    for _, r in dorm.iterrows():
        k = f"nudge_{r['id']}"
        if st.button(f"Draft nudge for {r['name']}", key=f"n{r['id']}"):
            m = gen_nudge(r["name"])
            if m: st.session_state[k] = m
        if k in st.session_state:
            st.text_area("Nudge (review first)", key=k, height=90)
            a, b = st.columns(2)
            a.link_button(f"Open WhatsApp to {r['name'].split()[0]}", wa_link(target(r["phone"]), st.session_state[k]), use_container_width=True)
            if b.button("✅ Approve & save", key=f"na{r['id']}", type="primary", use_container_width=True):
                ex("INSERT INTO outreach (dealership_id, message, created_at, recipient, recipient_phone) VALUES (?,?,?,?,?)",
                   (int(r["dealership_id"]), st.session_state[k], now(), r["name"], r["phone"]))
                log("outreach", f"Approved nudge for {r['name']}")
                del st.session_state[k]
                flash("✅ Nudge saved. Find it in Outreach → Approved messages."); st.rerun()

elif page == "📋 Leads & Commissions":
    hero("Leads & Commissions", "RM200 commission is only created when lead status = Sold.")
    sa_list = q("SELECT id, name FROM sas WHERE status != 'Invited' ORDER BY name")
    sa_opts = {"All SAs": None, **{f"{r['name']} (#{r['id']})": int(r['id']) for _, r in sa_list.iterrows()}}
    f1, f2 = st.columns(2)
    sa_f = sa_opts[f1.selectbox("🔎 Select SA", list(sa_opts))]
    st_f = f2.selectbox("Lead status", ["All"] + LEAD_STATUSES)

    t1, t2 = st.tabs(["Lead pipeline", "Commission payouts"])
    with t1:
        l = q("""SELECT l.id, s.name sa, l.customer_name, l.car_model, l.area, l.status, l.created_at
                 FROM leads l JOIN sas s ON s.id=l.sa_id
                 WHERE (? IS NULL OR l.sa_id=?) AND (?='All' OR l.status=?)
                 ORDER BY l.id DESC""", (sa_f, sa_f, st_f, st_f))
        st.caption(f"{len(l)} lead(s)")
        st.dataframe(l, use_container_width=True, hide_index=True)
        if len(l):
            a, b, c = st.columns([2, 2, 1])
            lid = a.selectbox("Lead", l.id, format_func=lambda i: f"#{i} {l[l.id == i].customer_name.iloc[0]} ({l[l.id == i].status.iloc[0]})")
            ns = b.selectbox("New status", LEAD_STATUSES)
            if c.button("Update", use_container_width=True): set_lead_status(int(lid), ns); st.rerun()
    with t2:
        c = q("""SELECT c.id, s.name sa, c.lead_id, c.amount, c.status, c.paid_at
                 FROM commissions c JOIN sas s ON s.id=c.sa_id
                 WHERE (? IS NULL OR c.sa_id=?) ORDER BY c.status='Paid', c.id DESC""", (sa_f, sa_f))
        m1, m2 = st.columns(2)
        m1.metric("Payable", f"RM{c[c.status == 'Payable'].amount.sum():.0f}")
        m2.metric("Paid", f"RM{c[c.status == 'Paid'].amount.sum():.0f}")
        st.dataframe(c, use_container_width=True, hide_index=True)
        due = c[c.status == "Payable"]
        if len(due):
            cid = st.selectbox("Mark as paid", due.id)
            if st.button("💸 Mark Paid"):
                ex("UPDATE commissions SET status='Paid', paid_at=? WHERE id=?", (now(), int(cid))); log("paid", f"Commission #{cid} paid"); st.rerun()

elif page == "📜 Activity":
    hero("Activity Log", "Everything that happens in the system.")
    cats = {"All": None, "📥 Lead": ["lead"], "📝 Signup": ["signup"], "🏆 Sale": ["sale"],
            "💸 Paid": ["paid"], "✉️ Outreach": ["outreach"], "⚙️ Other": ["dealer", "import"]}
    for tab, (label, types) in zip(st.tabs(list(cats)), cats.items()):
        with tab:
            if types:
                ph = ",".join("?" * len(types))
                df = q(f"SELECT created_at, event_type, description FROM activity_log WHERE event_type IN ({ph}) ORDER BY id DESC LIMIT 50", tuple(types))
            else:
                df = q("SELECT created_at, event_type, description FROM activity_log ORDER BY id DESC LIMIT 50")
            st.caption(f"Latest {len(df)} record(s) (max 50)")
            st.dataframe(df, use_container_width=True, hide_index=True)

# ================= SA PORTAL =================
elif page == "📝 Sign Up":
    hero("Join Energi Elite as a partner", f"RM{COMMISSION} for every home charger sold through your referral.")
    d = q("SELECT id, name, brand, area FROM dealerships")
    opts = {f"{r['name']} ({r['brand']})": r["id"] for _, r in d.iterrows()}
    with st.form("signup"):
        name, phone = st.text_input("Full name"), st.text_input("WhatsApp number")
        dealer, pin = st.selectbox("Dealership", list(opts)), st.text_input("Choose a 4-digit PIN", type="password", max_chars=4)
        if st.form_submit_button("✅ Sign up", type="primary"):
            ph = norm_phone(phone)
            prev = q("SELECT id, status FROM sas WHERE phone=?", (ph,))
            if not name.strip() or len(ph) < 10 or not pin.isdigit() or len(pin) != 4:
                st.error("Enter your name, a valid WhatsApp number, and a 4-digit PIN.")
            elif len(prev) and prev.iloc[0]["status"] != "Invited":
                st.error("This number is already registered.")
            else:
                while True:
                    code = f"EE-{re.sub('[^A-Z]', '', name.split()[0].upper())[:6]}{random.randint(10, 99)}"
                    if not len(q("SELECT id FROM sas WHERE referral_code=?", (code,))): break
                if len(prev):   # this SA was already invited (imported), so upgrade the account
                    ex("UPDATE sas SET name=?, dealership_id=?, pin=?, referral_code=?, status='Signed up', joined_at=? WHERE id=?",
                       (name.strip(), opts[dealer], pin, code, now(), int(prev.iloc[0]["id"])))
                else:
                    ex("INSERT INTO sas (name, dealership_id, phone, pin, referral_code, joined_at) VALUES (?,?,?,?,?,?)", (name.strip(), opts[dealer], ph, pin, code, now()))
                log("signup", f"New SA: {name.strip()} ({code})")
                st.success(f"Welcome {name}! Your referral code: **{code}**")
                st.info("Next: send your first lead, it only takes 30 seconds.")

elif page == "💬 Send Lead":
    hero("Send a lead in 30 seconds", "Type one WhatsApp message. We handle the rest.")
    sid = sa_login()
    if sid:
        sa = q("SELECT * FROM sas WHERE id=?", (sid,)).iloc[0]
        tpl = f"New lead: Name, 012xxxxxxx, Car model, Area\nRef: {sa['referral_code']}"
        st.link_button("💬 Open WhatsApp with template", wa_link(EE_WHATSAPP, tpl), use_container_width=True)
        st.divider()
        st.caption("Demo: simulates a WhatsApp message arriving in the Energi Elite inbox.")
        st.caption(f"Logged in as **{sa['name']}** (code {sa['referral_code']}). In the sample message, **Ali is the customer (buyer)** being referred. "
                   "The SA is identified by the **Ref code**, just like WhatsApp would identify the sender.")
        msg = st.text_area("Message received", f"New lead: Ali, 0123456789, BYD Atto 3, PJ\nRef: {sa['referral_code']}", height=100)
        if st.button("📥 Process message", type="primary"):
            code = re.search(r"EE-[A-Z0-9]+", msg.upper())
            owner = q("SELECT id, name FROM sas WHERE referral_code=?", (code.group(0),)) if code else pd.DataFrame()
            if code and owner.empty:
                st.error(f"Referral code {code.group(0)} was not found.")
            else:
                owner_id, owner_name = (int(owner.iloc[0]["id"]), owner.iloc[0]["name"]) if len(owner) else (sid, sa["name"])
                with st.spinner("AI is processing..."):
                    ok, info = save_lead(owner_id, parse_lead(msg))
                (st.success if ok else st.warning)(f"{info} Credited to {owner_name}." if ok else info)

elif page == "💰 My Dashboard":
    hero("My dashboard", "Check your leads and commissions anytime.")
    sid = sa_login()
    if sid:
        sa = q("SELECT * FROM sas WHERE id=?", (sid,)).iloc[0]
        l = q("SELECT customer_name, car_model, area, status, created_at FROM leads WHERE sa_id=? ORDER BY id DESC", (sid,))
        c = q("SELECT amount, status, paid_at FROM commissions WHERE sa_id=?", (sid,))
        open_n = int(l.status.isin(["New", "Contacted", "Quoted"]).sum())
        m = st.columns(4)
        m[0].metric("Leads sent", len(l), help="Buyers you have referred so far."); m[1].metric("Potential (open leads)", f"RM{open_n * COMMISSION}", help=f"Open leads x RM{COMMISSION}. What you earn if they all become sales.")
        m[2].metric("Payable", f"RM{c[c.status == 'Payable'].amount.sum():.0f}", help="Charger sold. Energi Elite owes you this and will pay soon."); m[3].metric("Already paid", f"RM{c[c.status == 'Paid'].amount.sum():.0f}", help="Money already paid to you.")
        st.caption(f"Referral code: **{sa['referral_code']}**")
        st.caption(f"**Potential** = open leads × RM{COMMISSION} (if they become sales) · **Payable** = sold, waiting for payment · **Already paid** = money you received.")
        st.dataframe(l, use_container_width=True, hide_index=True)
        sold = int((l.status == "Sold").sum())
        st.success(f"🏅 {sold} sale{'s' if sold != 1 else ''} so far. " + ("Send your first lead!" if not len(l) else "Keep going!"))
        st.link_button("📣 Share with fellow SAs", wa_link("", f"I earn RM{COMMISSION} for every charger sold with Energi Elite. Sign up with my code {sa['referral_code']}!"))

elif page == "🏆 Leaderboard":
    hero("Leaderboard SA", "Top SAs this month.")
    lb = q("""SELECT s.name, d.brand, COUNT(l.id) leads, SUM(l.status='Sold') sold FROM sas s
              LEFT JOIN dealerships d ON d.id=s.dealership_id LEFT JOIN leads l ON l.sa_id=s.id GROUP BY s.id ORDER BY sold DESC, leads DESC""")
    lb["name"] = lb["name"].str.split().str[0]
    medals = ["🥇", "🥈", "🥉"]
    lb.insert(0, "Rank", [medals[i] if i < 3 else str(i + 1) for i in range(len(lb))])
    st.dataframe(lb, use_container_width=True, hide_index=True)