"""Woningbot: scant huuradvertenties, laat Claude ze beoordelen, stuurt je een Telegram-bericht.
Plak je ook Facebook-berichten in de Telegram-chat met de bot, dan beoordeelt hij die ook."""
import os, re, sys, json, time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup
import anthropic

# ====== INSTELLINGEN (pas gerust aan) ======
MODEL = "claude-haiku-5-5"
MAX_HUUR = 1250          # totaal per maand, alles inclusief
RUIM_HUUR = 1.15         # tot 15% erboven tonen we nog, met label
MAX_OV = 60              # minuten OV naar Utrecht Science Park (Uithof)
RUIM_OV = 75             # tot 75 min tonen we nog, met label
MAX_NIEUW_PER_RUN = 20   # begrenst de AI-kosten per run
MAX_PER_DAG = 150        # max aantal AI-beoordelingen per dag (ca. EUR 0,30/dag, dus ruim onder EUR 10 per maand)

PROFIEL = ("Twee studenten (vrienden), start diergeneeskunde in Utrecht in 2027, "
           "ouders zijn bereid garant te staan, rustige huurders, zoeken voor de lange termijn "
           "(minimaal de duur van de opleiding).")

# Plak hier je EIGEN zoekresultaat-URL's: stel de filters in op de site (max huur, regio) en kopieer de link.
# 'pattern' = herkenbaar stuk van de link naar een losse advertentie. Test met: workflow handmatig starten.
SOURCES = [
    {"naam": "Rentola", "url": "https://rentola.nl/huurwoningen?location=utrecht&maxPrice=1400",
     "pattern": r"/(nl/)?(listings|huurwoning)/"},
    {"naam": "DirectWonen", "url": "https://directwonen.nl/huurwoningen-huren/utrecht",
     "pattern": r"/huurwoningen-huren/"},
]
# ===========================================

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
HEADERS = {"User-Agent": "woningbot-persoonlijk/1.0 (eigen huurwoning-zoektocht, laag volume)",
           "Accept-Language": "nl-NL,nl;q=0.9"}
client = anthropic.Anthropic()

PROMPT = f"""Je beoordeelt huuradvertenties voor deze huurders: {PROFIEL}
Eisen: zelfstandig, kaal, liefst 2 slaapkamers (of 2 aparte kamers), max EUR {MAX_HUUR} totaal per maand inclusief alles,
maximaal {MAX_OV} min OV naar Utrecht Science Park (de Uithof).
Antwoord UITSLUITEND met JSON (geen tekst eromheen) met deze velden:
is_woning (bool: echte huuradvertentie), titel, plaats,
huur_totaal (int of null: per maand all-in; staat er alleen kale huur, tel servicekosten/gwe erbij als bekend),
huur_zeker_inclusief (bool: is duidelijk dat dit all-in is),
slaapkamers (int of null), zelfstandig (bool of null), beschikbaar_per (tekst of null),
alleen_stellen (true als de advertentie stellen/partners/duurzame relatie vereist of vrienden/huisgenoten uitsluit; false als
dat niet zo is; null als onbekend),
ov_minuten (int: jouw beste schatting deur-tot-deur met OV naar Utrecht Science Park),
scam_risico ("laag","middel","hoog"), scam_reden (tekst of null),
samenvatting (max 2 korte Nederlandse zinnen, noem eventuele haken zoals max aantal personen of inkomenseis),
reactie (korte, eerlijke Nederlandse reactie aan de verhuurder namens de huurders; verzin GEEN feiten; null als alleen_stellen true is).
"""

def log(*a): print(*a, flush=True)

def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=30)
    return r.json()

def stuur(tekst):
    if not TG_TOKEN:
        log("[DRY]", tekst); return
    for i in range(0, len(tekst), 4000):
        tg("sendMessage", chat_id=TG_CHAT, text=tekst[i:i+4000])

def pagina_tekst(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "svg"]): t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:7000]

_robots = {}
def toegestaan(url):
    """Respecteer robots.txt. Bij twijfel (fout bij ophalen) niet ophalen."""
    host = "{0.scheme}://{0.netloc}".format(urlparse(url))
    if host not in _robots:
        rp = RobotFileParser()
        try:
            r = requests.get(host + "/robots.txt", headers=HEADERS, timeout=15)
            if r.status_code == 200: rp.parse(r.text.splitlines())
            elif r.status_code == 404: rp.parse([])
            else: rp.parse(["User-agent: *", "Disallow: /"])
        except Exception:
            rp.parse(["User-agent: *", "Disallow: /"])
        _robots[host] = rp
    return _robots[host].can_fetch(HEADERS["User-Agent"], url)

def vind_links(src):
    if not toegestaan(src["url"]):
        raise RuntimeError("robots.txt staat dit niet toe, overgeslagen")
    r = requests.get(src["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    links = set()
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        u = urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
        if re.search(src["pattern"], u) and u.rstrip("/") != src["url"].rstrip("/") and len(u) > len(src["url"]) + 5:
            links.add(u)
    return links

def analyseer(tekst, bron):
    msg = client.messages.create(model=MODEL, max_tokens=2000, system=PROMPT,
        messages=[{"role": "user", "content": f"Bron: {bron}\n\n{tekst}"}])
    # het antwoord kan ook 'thinking'-blokken bevatten: neem alleen de tekstblokken
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)

def labels_en_filter(d, forceer=False):
    """Geeft lijst labels terug, of None als de advertentie wordt overgeslagen."""
    L = []
    h = d.get("huur_totaal")
    if h:
        if h > MAX_HUUR * RUIM_HUUR and not forceer: return None
        if h > MAX_HUUR: L.append(f"💸 €{h}, {round((h / MAX_HUUR - 1) * 100)}% boven budget")
        if not d.get("huur_zeker_inclusief"): L.append("⚠️ onduidelijk of dit all-in is")
    else:
        L.append("❓ huur onbekend")
    ov = d.get("ov_minuten")
    if ov:
        if ov > RUIM_OV and not forceer: return None
        if ov > MAX_OV: L.append(f"🚆 {ov} min, boven je limiet")
    sk = d.get("slaapkamers")
    if sk is not None and sk < 2: L.append(f"🛏 maar {sk} slaapkamer(s)")
    if d.get("zelfstandig") is False: L.append("🚪 onzelfstandig")
    if d.get("scam_risico") in ("middel", "hoog"): L.append(f"🚨 scam-risico {d['scam_risico']}: {d.get('scam_reden')}")
    return L

def opmaak(d, link, bron, labels):
    kop = "💑 ALLEEN VOOR STELLEN\n" if d.get("alleen_stellen") else ""
    regels = [f"{kop}🏠 {d.get('titel')} ({bron})",
              f"📍 {d.get('plaats')} · 💶 {('€' + str(d['huur_totaal'])) if d.get('huur_totaal') else '?'} · "
              f"🛏 {d.get('slaapkamers', '?')} · 🚆 ~{d.get('ov_minuten', '?')} min OV (geschat)"]
    if d.get("beschikbaar_per"): regels.append(f"📅 {d['beschikbaar_per']}")
    regels += labels
    regels += ["", d.get("samenvatting") or "", link or ""]
    if d.get("reactie") and not d.get("alleen_stellen"):
        regels += ["", "✍️ Concept-reactie:", d["reactie"]]
    return "\n".join(regels)

def laad_seen():
    try: return json.load(open("seen.json"))
    except Exception: return {}

def budget_over(seen):
    """Hoeveel beoordelingen mogen er vandaag nog? (teller zit in seen.json)"""
    dag = time.strftime("%Y-%m-%d", time.gmtime())
    b = seen.get("__budget__")
    if not b or b.get("dag") != dag:
        b = seen["__budget__"] = {"dag": dag, "n": 0}
    return MAX_PER_DAG - b["n"]

def scan_sites(seen, dry):
    nieuw = []
    for src in SOURCES:
        try:
            links = vind_links(src)
            log(f"{src['naam']}: {len(links)} links gevonden")
            nieuw += [(src["naam"], u) for u in links if u not in seen]
        except Exception as e:
            log(f"{src['naam']} mislukt: {e}")
    for bron, u in nieuw[:MAX_NIEUW_PER_RUN]:
        if budget_over(seen) <= 0:
            if not seen["__budget__"].get("gemeld"):
                stuur(f"⏸ Dagplafond van {MAX_PER_DAG} beoordelingen bereikt. Morgen (UTC) ga ik door.")
                seen["__budget__"]["gemeld"] = True
            break
        try:
            if not toegestaan(u):
                log("robots.txt staat niet toe:", u); continue
            r = requests.get(u, headers=HEADERS, timeout=30); r.raise_for_status()
            d = analyseer(pagina_tekst(r.text), bron)
            # pas na een geslaagde beoordeling als 'gezien' markeren
            seen[u] = int(time.time())
            seen["__budget__"]["n"] += 1
            if not d.get("is_woning"): continue
            lab = labels_en_filter(d)
            if lab is None: log("overgeslagen:", u); continue
            stuur(opmaak(d, u, bron, lab))
        except Exception as e:
            log(f"Fout bij {u}: {e}")

def telegram_inbox():
    """Alles wat jij in de chat plakt (bv. Facebook-berichten) wordt beoordeeld."""
    if not TG_TOKEN: return
    ups = tg("getUpdates", timeout=0).get("result", [])
    for u in ups:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TG_CHAT) or not m.get("text"): continue
        if m["text"].startswith("/"): continue
        try:
            d = analyseer(m["text"][:7000], "handmatig (bv. Facebook)")
            stuur(opmaak(d, None, "handmatig", labels_en_filter(d, forceer=True) or []))
        except Exception as e:
            stuur(f"Kon dit bericht niet beoordelen: {e}")
    if ups: tg("getUpdates", offset=ups[-1]["update_id"] + 1)

if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY ontbreekt: controleer de naam en dat het een Repository secret is.")
    seen = laad_seen()
    telegram_inbox()
    scan_sites(seen, dry=not TG_TOKEN)
    json.dump(seen, open("seen.json", "w"))
