"""Woningbot: scant huuradvertenties, laat Claude ze beoordelen, stuurt je een Telegram-bericht.
Plak je ook Facebook-berichten of e-mailmeldingen in de Telegram-chat met de bot, dan beoordeelt hij die ook.
Commando's in Telegram: /help /pakket /gereageerd N /lijst /week"""
import os, re, sys, json, time, imaplib, email, base64
from email.header import decode_header, make_header
from email.utils import parseaddr
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import Counter
from urllib.parse import urljoin, urlparse, quote
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
MAX_PER_DAG = 300        # max aantal AI-beoordelingen per dag (mails en advertenties samen)

INSCHRIJFDUUR_JAREN = 0   # hoeveel jaar inschrijfduur (WoningNet/corporatie) jullie nu hebben. Vul dit in!
OPLEIDING_JAREN = 6       # hoe lang je wilt wonen (diergeneeskunde: bachelor + master, controleer dit)
STIL_ONDER_SCORE = 6      # onder dit cijfer komt het bericht zonder geluid/melding binnen
MIN_SCORE = 3            # onder dit cijfer (1-10) krijg je geen bericht
SCORE_REACTIE_VANAF = 7  # vanaf dit cijfer krijg je ook een kant-en-klaar concept-reactiebericht
HERINNERING_NA_DAGEN = 3 # herinnering als je op een woning reageerde en niets hebt gehoord
SKIP_ONHAALBAAR_INKOMEN = True  # sla advertenties over met een inkomenseis waarbij een garantsteller/spaargeld expliciet niet mag
BESTEMMING = "Yalelaan 1, 3584 CL Utrecht, Nederland"   # Diergeneeskunde (Uithof). Controleer dit adres zelf even.
MAX_ROUTES_PER_DAG = 150 # begrenst het aantal aanvragen bij de route-API (gratis tegoed is per maand beperkt)

PROFIEL = ("Twee studenten (vrienden), start diergeneeskunde in Utrecht in 2027, "
           "ouders zijn bereid garant te staan en er is voldoende spaargeld, rustige huurders, zoeken voor de lange termijn "
           "(minimaal de duur van de opleiding). Een inkomenseis van bijv. 3x de huur halen ze op eigen inkomen niet, "
           "dus advertenties zonder inkomenseis, of waar garantsteller/spaargeld volstaat, hebben de voorkeur.")

# Vul per bron in wat voor JOU geldt: "gratis" of "betaald" (reageren kost geld). Leeg = geen label.
BRON_KOSTEN = {
    "Rentola": "",
    "DirectWonen": "",
    "handmatig": "",
}

# Tekst voor het commando /pakket: wat je meestuurt bij een reactie. Pas aan naar jullie situatie.
PAKKET_TEKST = """📎 Meestuurpakket bij een reactie:
• Kopie ID van jullie beiden
• Bewijs van inschrijving opleiding
• Garantverklaring van de ouders + kopie ID garantsteller
• Recente loonstrook of inkomensverklaring van de garantsteller
• Korte, eerlijke introductie van jullie beiden"""

# Websites zelf scannen (Rentola, DirectWonen): aan, na eigen controle van de voorwaarden. Zet op False om te stoppen.
SCAN_SITES = True

# E-mailmeldingen (de eigen alerts van de sites) uitlezen. Zie MAIL_* secrets in GitHub.
MAX_MAILS_PER_RUN = 25

# --- Huurtoeslag-schatting (regels 2026, ruwe schatting; in 2027 veranderen de bedragen!) ---
TOESLAG_AAN = True            # zet op False als jullie door spaargeld of inkomen waarschijnlijk geen recht hebben
ALLE_BEWONERS_ONDER_21 = False# True = voorzichtige schatting (lagere maximale huur); False als iemand 21+ is
TS_BASIS = 202.52             # basishuur per persoon (2026)
TS_KKG = 498.20               # kwaliteitskortingsgrens: tot hier 100% vergoed
TS_AFT = 713.02               # aftoppingsgrens (1-2 personen): tot hier 65%, daarboven 40%
TS_MAX = 932.93               # maximale rekenhuur voor 21+
MAIL_BRONNEN = {   # afzenderdomein -> naam (voor BRON_KOSTEN en in het bericht)
    "funda.nl": "Funda", "pararius.nl": "Pararius", "pararius.com": "Pararius", "huurwoningen.nl": "Huurwoningen.nl",
    "kamernet.nl": "Kamernet", "kamer.nl": "Kamer.nl", "huurstunt.nl": "Huurstunt", "marktplaats.nl": "Marktplaats",
    "directwonen.nl": "DirectWonen", "rentola.nl": "Rentola", "housinganywhere.com": "HousingAnywhere",
    "holland2stay.com": "Holland2Stay", "ssh.nl": "SSH", "duwo.nl": "DUWO", "woningnet.nl": "WoningNet",
    "vesteda.com": "Vesteda", "rotsvast.nl": "Rotsvast",
    "googlealerts-noreply@google.com": "Google Alert",   # een volledig adres mag ook (dan geldt alleen dat adres)
}

# Plak hier je EIGEN zoekresultaat-URL's (alleen sites waarvan je de voorwaarden hebt gelezen!).
SOURCES = [
    {"naam": "Rentola", "url": "https://rentola.nl/huurwoningen?location=utrecht&maxPrice=1400",
     "pattern": r"/(nl/)?(listings|huurwoning)/"},
    {"naam": "DirectWonen", "url": "https://directwonen.nl/huurwoningen-huren/utrecht",
     "pattern": r"/huurwoningen-huren/"},
]
# ===========================================

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Eén of meerdere chat-ID's, gescheiden door een komma: "123456,789012"
CHATS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
GMAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
MAIL_USER = os.environ.get("MAIL_ADDRESS", "")
MAIL_PASS = os.environ.get("MAIL_APP_PASSWORD", "")
MAIL_HOST = os.environ.get("MAIL_IMAP_HOST", "imap.gmail.com")
HEADERS = {"User-Agent": "woningbot-persoonlijk/1.0 (eigen huurwoning-zoektocht, laag volume)",
           "Accept-Language": "nl-NL,nl;q=0.9"}
client = anthropic.Anthropic()

PROMPT = f"""Je beoordeelt huuradvertenties voor deze huurders: {PROFIEL}
Eisen: zelfstandig, kaal, liefst 2 slaapkamers (of 2 aparte kamers), max EUR {MAX_HUUR} totaal per maand inclusief alles,
maximaal {MAX_OV} min OV naar Utrecht Science Park (de Uithof).
Antwoord UITSLUITEND met JSON (geen tekst eromheen) met deze velden (null als onbekend):
is_woning (bool: echte huuradvertentie), titel (kort), plaats, adres (straat + huisnummer als vermeld),
oppervlakte_m2 (int),
huur_totaal (int: per maand all-in; staat er alleen kale huur, tel servicekosten/gwe erbij als bekend),
huur_zeker_inclusief (bool: is duidelijk dat dit all-in is),
kale_huur (int: kale huur per maand zonder servicekosten/gwe, als vermeld),
huurtype ("sociaal" bij sociale huur/corporatiewoning of kale huur t/m ca. EUR 932; "middensegment" bij ca. EUR 932-1228; "vrije sector" erboven; null als onbekend),
toewijzing ("inschrijfduur" als de woning op inschrijfduur/wachttijd wordt toegewezen, "loting" bij verloting, "direct" als je gewoon kunt reageren zonder inschrijfduur, null als onbekend),
inschrijfduur_jaren (getal: vereiste of laatst toegewezen inschrijfduur in jaren, als vermeld), inschrijfduur_tekst (korte zin over inschrijfduur/wachttijd/loting, als vermeld),
contract ("onbepaalde tijd","bepaalde tijd","jongerencontract","campuscontract" of null), contractduur_jaren (getal, bij bepaalde tijd),
beschikbaar_datum (YYYY-MM-DD als te bepalen, anders null),
huurtoeslag (true als de advertentie zegt dat huurtoeslag mogelijk is, false als de advertentie zegt dat het NIET kan, null als het niet vermeld wordt),
slaapkamers (int), zelfstandig (bool), beschikbaar_per (kort),
alleen_stellen (true als de advertentie stellen/partners/duurzame relatie vereist of vrienden/huisgenoten uitsluit; false als
dat niet zo is),
studenten_ok (false als studenten expliciet worden geweigerd, true als ze welkom zijn),
garant_ok (false als een garantsteller expliciet niet wordt geaccepteerd, true als dat kan),
max_personen (int, als er een maximum is), inkomenseis (korte tekst, bv. "3x de huur", anders null),
spaargeld_ok (true als de advertentie spaargeld of een gelijkwaardig alternatief voor inkomen accepteert, false als dat expliciet niet kan),
ov_minuten (int: jouw beste schatting deur-tot-deur met OV naar Utrecht Science Park),
scam_risico ("laag","middel","hoog"): let op vooruitbetaling of borg vooraf zonder bezichtiging, verhuurder in het buitenland,
alleen contact via WhatsApp/buitenlandse nummers, tijdsdruk, prijs ver onder de marktprijs, gestolen-ogende foto's of tekst,
scam_reden (korte tekst of null),
samenvatting (1 korte Nederlandse zin met de belangrijkste haak),
reactie (korte, eerlijke Nederlandse reactie aan de verhuurder namens de huurders; vraag om een bezichtiging en noem dat jullie snel kunnen reageren; verzin GEEN feiten; null als alleen_stellen true is).
"""

PROMPT_MAIL = PROMPT + """
LET OP: de tekst is een e-mailmelding en kan MEERDERE advertenties bevatten. Antwoord met een JSON-LIJST (array), één object per
woning met bovenstaande velden plus link (de URL uit de mail die bij die woning hoort, uit de [..]-delen; anders null).
Is er geen woning in de mail, antwoord dan []."""

def log(*a): print(*a, flush=True)

# ---------- Telegram ----------
def tg(method, **params):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=30)
    return r.json()

def stuur_naar(chat, tekst, stil=False, markup=None):
    if not TG_TOKEN or not chat:
        log("[DRY]", tekst); return
    for i in range(0, len(tekst), 4000):
        extra = {"reply_markup": markup} if markup and i + 4000 >= len(tekst) else {}
        tg("sendMessage", chat_id=chat, text=tekst[i:i+4000], disable_notification=stil, **extra)

def stuur(tekst, stil=False, markup=None):
    """Stuurt naar alle chats in TELEGRAM_CHAT_ID. stil=True: zonder geluid."""
    if not TG_TOKEN or not CHATS:
        log("[DRY]", tekst); return
    for c in CHATS: stuur_naar(c, tekst, stil, markup)

STATUS_CODES = {"g": "gereageerd", "b": "bewaard", "x": "afgewezen"}

def maak_knoppen(nr, status="nieuw"):
    def k(tekst, code, st): return {"text": ("✔ " if status == st else "") + tekst, "callback_data": f"{code}:{nr}"}
    return {"inline_keyboard": [[k("✅ Gereageerd", "g", "gereageerd"), k("⭐ Bewaren", "b", "bewaard"),
                                 k("❌ Niet interessant", "x", "afgewezen")]]}

def zet_status(seen, nr, status):
    it = seen.get("__log__", {}).get("items", {}).get(str(nr))
    if not it: return None
    it["status"] = status
    if status == "gereageerd":
        it["gereageerd_ts"] = int(time.time()); it.pop("herinnerd", None)
    return it

AI_FOUT = getattr(anthropic, "APIError", ())

def waarschuw(seen, key, tekst, uren=12):
    """Stuurt een foutmelding naar Telegram, maximaal één keer per 'uren' per soort fout."""
    w = seen.setdefault("__waarschuwingen__", {})
    if time.time() - w.get(key, 0) > uren * 3600:
        w[key] = int(time.time()); stuur(tekst)

# ---------- Pagina's ophalen (met robots.txt-controle) ----------
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

# ---------- Beoordeling ----------
def analyseer(tekst, bron):
    msg = client.messages.create(model=MODEL, max_tokens=2000, system=PROMPT,
        messages=[{"role": "user", "content": f"Bron: {bron}\n\n{tekst}"}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)

def aankomsttijd():
    """Eerstvolgende werkdag 09:00 (NL-tijd) als UTC-tijdstip, voor een realistische OV-reistijd."""
    dag = datetime.now(ZoneInfo("Europe/Amsterdam")) + timedelta(days=1)
    while dag.weekday() >= 5: dag += timedelta(days=1)
    t = dag.replace(hour=9, minute=0, second=0, microsecond=0)
    return t.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

def route_minuten(seen, adres, plaats):
    """Echte OV-reistijd (deur-tot-deur) via Google Routes API. None bij fout of limiet."""
    if not GMAPS_KEY: return None
    origin = ", ".join(x for x in [adres, plaats] if x) + ", Nederland"
    cache = seen.setdefault("__route__", {})
    if origin in cache: return cache[origin][0]
    dag = time.strftime("%Y-%m-%d", time.gmtime())
    rt = seen.get("__routes__")
    if not rt or rt.get("dag") != dag: rt = seen["__routes__"] = {"dag": dag, "n": 0}
    if rt["n"] >= MAX_ROUTES_PER_DAG: return None
    rt["n"] += 1
    try:
        r = requests.post("https://routes.googleapis.com/directions/v2:computeRoutes",
            headers={"X-Goog-Api-Key": GMAPS_KEY, "X-Goog-FieldMask": "routes.duration", "Content-Type": "application/json"},
            json={"origin": {"address": origin}, "destination": {"address": BESTEMMING}, "travelMode": "TRANSIT",
                  "arrivalTime": aankomsttijd(), "languageCode": "nl", "regionCode": "NL"}, timeout=30)
        if r.status_code != 200:
            log("Routes API fout:", r.status_code, r.text[:300]); return None
        routes = r.json().get("routes") or []
        if not routes: return None
        minuten = round(int(routes[0]["duration"].rstrip("s")) / 60)
    except Exception as e:
        log("Routes API fout:", e); return None
    cache[origin] = [minuten, int(time.time())]
    return minuten

def verrijk_ov(seen, d):
    """Vervangt de AI-schatting door een echte routetijd als dat lukt. ov_bron: route / plaats / schatting."""
    d["ov_bron"] = "schatting"
    if not GMAPS_KEY or not d.get("plaats"): return
    if d.get("ov_minuten") and d["ov_minuten"] > 100: return
    heeft_adres = bool(d.get("adres") and re.search(r"\d", d["adres"]))
    m = route_minuten(seen, d["adres"] if heeft_adres else None, d["plaats"])
    if m is not None:
        d["ov_minuten"] = m
        d["ov_bron"] = "route" if heeft_adres else "plaats"

def analyseer_mail(tekst, bron, onderwerp):
    msg = client.messages.create(model=MODEL, max_tokens=6000, system=PROMPT_MAIL,
        messages=[{"role": "user", "content": f"Bron: {bron}\nOnderwerp: {onderwerp}\n\n{tekst}"}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    if isinstance(data, dict): data = [data]
    for d in data: d.setdefault("is_woning", True)
    return data

def analyseer_beeld(img_bytes, bijschrift):
    """Beoordeelt een screenshot of foto van een advertentie (bv. een Facebook-bericht)."""
    b64 = base64.b64encode(img_bytes).decode()
    msg = client.messages.create(model=MODEL, max_tokens=2000, system=PROMPT, messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": "Bron: handmatig (screenshot). " + (bijschrift or "")}]}])
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)

def download_foto(m):
    foto = max(m["photo"], key=lambda p: p.get("file_size", 0))
    info = tg("getFile", file_id=foto["file_id"])["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=30)
    r.raise_for_status()
    return r.content

def prijs_per_m2_laag(d):
    h, m2 = d.get("huur_totaal"), d.get("oppervlakte_m2")
    return bool(h and m2 and m2 > 0 and h / m2 < 8)

def score(d):
    """Cijfer 1-10 op basis van vaste regels (transparant, kost geen extra AI)."""
    s = 10
    h = d.get("huur_totaal")
    if h is None: s -= 1
    elif h > MAX_HUUR: s -= min(3, 1 + int(((h / MAX_HUUR - 1) * 100) // 5))
    ov = d.get("ov_minuten")
    if ov is None: s -= 1
    elif ov > MAX_OV: s -= min(3, 1 + (ov - MAX_OV) // 5)
    sk = d.get("slaapkamers")
    if sk is None: s -= 1
    elif sk < 2: s -= 3
    if d.get("zelfstandig") is False: s -= 2
    if d.get("alleen_stellen") is True: s -= 4
    if d.get("studenten_ok") is False: s -= 4
    if d.get("garant_ok") is False: s -= 2
    mp = d.get("max_personen")
    if isinstance(mp, int) and mp < 2: s -= 4
    if d.get("inkomenseis"):
        if d.get("garant_ok") is True or d.get("spaargeld_ok") is True: s -= 1
        elif d.get("garant_ok") is False: s -= 5
        else: s -= 3
    j = d.get("inschrijfduur_jaren")
    if d.get("toewijzing") == "inschrijfduur" and isinstance(j, (int, float)) and j > INSCHRIJFDUUR_JAREN: s -= 4
    if d.get("contract") in ("bepaalde tijd", "jongerencontract", "campuscontract"):
        cd = d.get("contractduur_jaren")
        s -= 2 if isinstance(cd, (int, float)) and cd < OPLEIDING_JAREN else 1
    s -= {"hoog": 5, "middel": 2}.get(d.get("scam_risico"), 0)
    if prijs_per_m2_laag(d): s -= 2
    return max(1, min(10, s))

def labels_en_filter(d, forceer=False):
    """Korte waarschuwingen, of None als de advertentie ruim buiten je grenzen valt."""
    L = []
    if SKIP_ONHAALBAAR_INKOMEN and not forceer and d.get("inkomenseis") \
            and d.get("garant_ok") is False and d.get("spaargeld_ok") is not True:
        return None
    h = d.get("huur_totaal")
    if h:
        if h > MAX_HUUR * RUIM_HUUR and not forceer: return None
        if h > MAX_HUUR: L.append(f"💸 +{round((h / MAX_HUUR - 1) * 100)}% boven budget")
        if not d.get("huur_zeker_inclusief"): L.append("⚠️ all-in onduidelijk")
    ov = d.get("ov_minuten")
    if ov:
        if ov > RUIM_OV and not forceer: return None
        if ov > MAX_OV: L.append("🚆 boven je OV-limiet")
    if d.get("zelfstandig") is False: L.append("🚪 onzelfstandig")
    if d.get("scam_risico") in ("middel", "hoog"):
        L.append(f"🚨 scam-risico {d['scam_risico']}" + (f": {d['scam_reden']}" if d.get("scam_reden") else ""))
    if prijs_per_m2_laag(d): L.append("🚨 opvallend goedkoop per m²")
    return L

def eisen_regel(d):
    d_ = []
    if d.get("studenten_ok") is True: d_.append("studenten ✓")
    if d.get("studenten_ok") is False: d_.append("studenten ✗")
    if d.get("garant_ok") is True: d_.append("garantsteller ✓")
    if d.get("garant_ok") is False: d_.append("garantsteller ✗")
    if d.get("max_personen"): d_.append(f"max {d['max_personen']} pers.")
    if d.get("spaargeld_ok") is True: d_.append("spaargeld ✓")
    if d.get("inkomenseis"):
        alt = d.get("garant_ok") is True or d.get("spaargeld_ok") is True
        d_.append(f"inkomen: {d['inkomenseis']}" + ("" if alt else " ⚠️"))
    return "📋 " + " · ".join(d_) if d_ else ""

def route_link(d):
    """Klikbare Google Maps-link met OV-route (geen API nodig): tik om de reistijd zelf te controleren."""
    if not d.get("plaats"): return None
    adres = d["adres"] if d.get("adres") and re.search(r"\d", d["adres"]) else None
    van = ", ".join(x for x in [adres, d["plaats"]] if x) + ", Nederland"
    return ("https://www.google.com/maps/dir/?api=1&travelmode=transit&origin=" + quote(van) +
            "&destination=" + quote(BESTEMMING))

def woonvorm_regel(d):
    """Huurtype, inschrijfduur/loting, contractvorm en beschikbaarheid in één compacte regel."""
    delen = []
    ht = {"sociaal": "🏘 Sociale huur", "middensegment": "🏘 Middenhuur", "vrije sector": "🏘 Vrije sector"}.get(d.get("huurtype"))
    if ht: delen.append(ht)
    tw, j = d.get("toewijzing"), d.get("inschrijfduur_jaren")
    if tw == "loting":
        delen.append("🎲 loting")
    elif tw == "inschrijfduur" or (d.get("huurtype") == "sociaal" and tw is None):
        t = d.get("inschrijfduur_tekst") or (f"{j} jaar" if j else "niet vermeld")
        warn = f" ⚠️ jullie hebben {INSCHRIJFDUUR_JAREN} jaar" if isinstance(j, (int, float)) and j > INSCHRIJFDUUR_JAREN else ""
        delen.append(f"⏳ inschrijfduur: {t}{warn}")
    elif tw == "direct":
        delen.append("⚡ geen inschrijfduur nodig")
    c = d.get("contract")
    if c == "onbepaalde tijd":
        delen.append("📄 vast contract ✓")
    elif c:
        cd = d.get("contractduur_jaren")
        kort = " ⚠️ korter dan je opleiding" if isinstance(cd, (int, float)) and cd < OPLEIDING_JAREN else ""
        delen.append(f"📄 {c}" + (f" ({cd} jaar)" if cd else "") + kort)
    bd = d.get("beschikbaar_datum")
    if isinstance(bd, str) and re.match(r"\d{4}-\d{2}-\d{2}$", bd):
        if bd > "2027-09-01": delen.append("📅 pas na sept 2027 ⚠️")
        elif bd <= time.strftime("%Y-%m-%d", time.gmtime(time.time() + 30 * 86400)): delen.append("⚡ snel beschikbaar")
    return " · ".join(delen)

SOCIAAL_TEKST = """🏘 Sociale huur en inschrijfduur (Utrecht en omgeving)
• Sociale huurwoningen worden in de regio Utrecht verdeeld via WoningNet (nu DĀK regio Utrecht). Vanaf 18 jaar bouw je daar wachttijd op, in Utrecht kun je volgens de gemeente al vanaf 16 jaar inschrijven.
• De wachttijd is lang. Oudere cijfers voor regio Utrecht liggen rond 8 tot 11 jaar, in Woerden rond 10 jaar. Voor jullie start in 2027 zal sociale huur dus vooral een lange-termijnoptie zijn.
• Schrijf je toch nu in: de inschrijfduur loopt door, ook als je nu nog geen kans maakt. Soms kost inschrijven een bedrag.
• Er is een inkomensgrens voor de meeste sociale huur (ongeveer €48.000 tot €50.000 bruto voor een alleenstaande, controleer de actuele grens) en je hebt een huisvestingsvergunning nodig.
• Sommige woningen zijn gelabeld als jongerenwoning, met voorrang voor een leeftijdsgroep (bijvoorbeeld 18 tot 22 of tot 27 of 29, naar keuze van de verhuurder). Controleer per advertentie of je binnen de leeftijdsgrens valt.
• Let bij jongerencontracten op de looptijd: die zijn vaak tijdelijk, terwijl jullie zo lang mogelijk willen blijven.
• Middenhuur (huur ongeveer tussen €932 en €1.228) heeft meestal geen inschrijfduur en vaak wel huurbescherming. Dat past goed bij jullie budget."""

HUURTOESLAG_TEKST = """🏷 Huurtoeslag voor studenten (regels 2026, controleer ze voor 2027!)
• Alleen een zelfstandige woning (eigen voordeur, keuken, toilet en douche) komt in aanmerking. Een onzelfstandige kamer meestal niet.
• Alleen de kale huur telt, servicekosten tellen in 2026 niet mee.
• Per persoon: tot de basishuur (ca. €203) krijg je niets, daarna 100% tot €498, 65% tot ca. €713 en 40% erboven. Is iedereen jonger dan 21, dan telt de huur maar tot ca. €498.
• Mijn schatting op de kaart gaat uit van: jullie delen de kale huur 50/50 en staan allebei op het contract, je inkomen is laag (student), jullie zitten onder de vermogensgrens en het zijn aparte aanvragers.
• Let op je spaargeld: er geldt een vermogensgrens van ongeveer €38.000 per persoon op 1 januari. Daarboven geen toeslag.
• De schatting is ruw. De echte uitkomst krijg je via de proefberekening op toeslagen.nl (met DigiD).
• De bedragen veranderen elk jaar op 1 januari, kijk dus begin 2027 op toeslagen.nl."""

def toeslag_schatting(rekenhuur_pp):
    """Ruwe maandelijkse huurtoeslag voor één persoon met laag inkomen (regels 2026)."""
    r = min(rekenhuur_pp, TS_KKG if ALLE_BEWONERS_ONDER_21 else TS_MAX)
    a = max(0, min(r, TS_KKG) - TS_BASIS)
    b = max(0, min(r, TS_AFT) - max(TS_BASIS, TS_KKG)) * 0.65
    c = max(0, r - max(TS_BASIS, TS_AFT)) * 0.40
    return round(a + b + c)

def huurtoeslag_regel(d):
    if d.get("huurtoeslag") is False: return "🏷 Huurtoeslag: niet mogelijk volgens de advertentie"
    if d.get("zelfstandig") is False: return "🏷 Huurtoeslag: waarschijnlijk niet (onzelfstandig)"
    tekst = "🏷 Huurtoeslag: mogelijk volgens de advertentie ✓" if d.get("huurtoeslag") is True else "🏷 Huurtoeslag: niet vermeld in advertentie"
    kale, alin = d.get("kale_huur"), d.get("huur_totaal")
    basis = kale or alin
    if TOESLAG_AAN and basis:
        t = toeslag_schatting(basis / 2)
        if t > 0:
            pp = round((alin or basis) / 2)
            tekst += (f"\n💶 Na toeslag ca. €{pp - t} p.p. (€{2 * (pp - t)} totaal) i.p.v. €{pp} p.p. "
                      f"· ruwe schatting{'' if kale else ', kale huur onbekend'}")
    return tekst

def kaart(d, link, bron, nr, sc, labels):
    kleur = "🟢" if sc >= 8 else "🟡" if sc >= 5 else "🔴"
    stel = "💑 ALLEEN STELLEN · " if d.get("alleen_stellen") else ""
    huur = f"€{d['huur_totaal']}" if d.get("huur_totaal") else "€?"
    kost = BRON_KOSTEN.get(bron, "")
    kostlabel = "💳 betaald · " if kost == "betaald" else "🆓 gratis · " if kost == "gratis" else ""
    ovm = d.get("ov_minuten") or "?"
    ov = {"route": f"{ovm} min OV ✓", "plaats": f"~{ovm} min OV (vanaf centrum)"}.get(d.get("ov_bron"), f"~{ovm} min OV (schatting)")
    vuur = "🔥 " if sc >= 9 else ""
    regels = [f"{vuur}{kleur} {sc}/10 · {stel}{d.get('titel') or 'Woning'}",
              f"📍 {d.get('plaats') or '?'} · {huur} · {d.get('slaapkamers') or '?'} slk · {ov}",
              f"{kostlabel}{bron} · #{nr}" + (f" · 📅 {d['beschikbaar_per']}" if d.get("beschikbaar_per") else "")]
    if labels: regels.append(" · ".join(labels))
    e = eisen_regel(d)
    if e: regels.append(e)
    wv = woonvorm_regel(d)
    if wv: regels.append(wv)
    ht = huurtoeslag_regel(d)
    if ht: regels.append(ht)
    if d.get("samenvatting"): regels.append(d["samenvatting"])
    if link: regels.append(link)
    if d.get("ov_bron") != "route" and route_link(d): regels.append("🗺 Check OV-route: " + route_link(d))
    return "\n".join(regels)

# ---------- Opslag (seen.json) ----------
def laad_seen():
    try: return json.load(open("seen.json"))
    except Exception: return {}

def budget_over(seen):
    dag = time.strftime("%Y-%m-%d", time.gmtime())
    b = seen.get("__budget__")
    if not b or b.get("dag") != dag:
        b = seen["__budget__"] = {"dag": dag, "n": 0}
    return MAX_PER_DAG - b["n"]

def dubbel_key(d):
    adres = (d.get("adres") or "").lower()
    plaats = (d.get("plaats") or "").lower()
    if adres and re.search(r"\d", adres) and plaats:
        return re.sub(r"[^a-z0-9]", "", adres) + "|" + re.sub(r"[^a-z0-9]", "", plaats)
    return None

def registreer(seen, d, link, bron="", sc=None):
    lg = seen.setdefault("__log__", {"n": 0, "items": {}})
    lg["n"] += 1
    nr = lg["n"]
    lg["items"][str(nr)] = {"titel": d.get("titel"), "url": link, "ts": int(time.time()), "status": "nieuw",
        "plaats": d.get("plaats"), "huur": d.get("huur_totaal"), "slk": d.get("slaapkamers"),
        "ov": d.get("ov_minuten"), "bron": bron, "score": sc}
    return nr

def voeg_stat_toe(seen, d, link, sc):
    if sc >= 5:
        seen.setdefault("__stats__", []).append({"ts": int(time.time()), "plaats": d.get("plaats") or "?",
            "huur": d.get("huur_totaal"), "score": sc, "titel": d.get("titel"), "url": link})

def verwerk_beoordeling(seen, d, link, bron, forceer=False):
    """Stuurt kaart (+ eventueel reactie). Geeft True als er iets is verstuurd."""
    if not d.get("is_woning") and not forceer: return False
    verrijk_ov(seen, d)
    lab = labels_en_filter(d, forceer=forceer)
    if lab is None: log("overgeslagen (buiten grenzen):", link); return False
    sc = score(d)
    if sc < MIN_SCORE and not forceer: log("score te laag", sc, link); return False
    if not forceer:
        key = dubbel_key(d)
        dupes = seen.setdefault("__dupes__", {})
        if key and key in dupes: log("dubbel, overgeslagen:", link); return False
        if key: dupes[key] = int(time.time())
    nr = registreer(seen, d, link, bron, sc)
    stuur(kaart(d, link, bron, nr, sc, lab), stil=(sc < STIL_ONDER_SCORE and not forceer), markup=maak_knoppen(nr))
    if d.get("reactie") and not d.get("alleen_stellen") and (forceer or sc >= SCORE_REACTIE_VANAF):
        stuur(f"✍️ Concept-reactie #{nr}:\n{d['reactie']}")
    voeg_stat_toe(seen, d, link, sc)
    return True

# ---------- Sites scannen ----------
def scan_sites(seen):
    nieuw = []
    for src in SOURCES:
        try:
            links = vind_links(src)
            log(f"{src['naam']}: {len(links)} links gevonden")
            nieuw += [(src["naam"], u) for u in links if u not in seen]
        except Exception as e:
            log(f"{src['naam']} mislukt: {e}")
            waarschuw(seen, "site-" + src["naam"], f"⚠️ {src['naam']} geeft een fout: {str(e)[:100]}", uren=24)
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
            seen[u] = int(time.time())          # pas na geslaagde beoordeling 'gezien'
            seen["__budget__"]["n"] += 1
            verwerk_beoordeling(seen, d, u, bron)
        except Exception as e:
            log(f"Fout bij {u}: {e}")
            if isinstance(e, AI_FOUT): waarschuw(seen, "ai", f"⚠️ Beoordelen mislukt: {str(e)[:150]}. Is je Anthropic-tegoed op?")

# ---------- E-mailmeldingen uitlezen (alleen lezen, via IMAP) ----------
def mail_tekst(msg):
    html, plain = None, None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/html", "text/plain") and "attachment" not in str(part.get("Content-Disposition", "")):
            try:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            if ct == "text/html" and html is None: html = body
            if ct == "text/plain" and plain is None: plain = body
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "svg", "img"]): t.decompose()
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("http"): a.append(f" [{a['href']}] ")
        txt = soup.get_text(" ")
    else:
        txt = plain or ""
    return re.sub(r"\s+", " ", txt).strip()[:14000]

def scan_mail(seen):
    if not (MAIL_USER and MAIL_PASS): return
    try:
        im = imaplib.IMAP4_SSL(MAIL_HOST)
        im.login(MAIL_USER, MAIL_PASS)
        im.select("INBOX", readonly=True)          # we lezen alleen, er wordt niets gewijzigd of verwijderd
        sinds = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
        ids = im.search(None, f'(SINCE "{sinds}")')[1][0].split()
    except Exception as e:
        log("Mail mislukt:", e)
        waarschuw(seen, "mail", f"⚠️ Ik kan niet in het postvak komen ({str(e)[:100]}). Controleer MAIL_ADDRESS en MAIL_APP_PASSWORD."); return
    verwerkt = 0
    for num in ids:
        if verwerkt >= MAX_MAILS_PER_RUN: break
        try:
            msg = email.message_from_bytes(im.fetch(num, "(BODY.PEEK[])")[1][0][1])
            mid = (msg.get("Message-ID") or "").strip()
            key = "mail:" + mid
            if not mid or key in seen: continue
            onderwerp = str(make_header(decode_header(msg.get("Subject", ""))))
            adres = parseaddr(msg.get("From", ""))[1].lower()
            domein = adres.split("@")[-1]
            bron = next((n for dm, n in MAIL_BRONNEN.items()
                         if (adres == dm) or ("@" not in dm and (domein == dm or domein.endswith("." + dm)))), None)
            if not bron and not re.search(r"huur|woning|appartement|kamer|studio|te huur", onderwerp, re.I):
                seen[key] = int(time.time()); continue   # geen woningmail, negeren
            if budget_over(seen) <= 0: break
            bron = bron or "e-mail"
            tekst = mail_tekst(msg)
            if not tekst:
                seen[key] = int(time.time()); continue
            lijst = analyseer_mail(tekst, bron, onderwerp)
            seen[key] = int(time.time()); seen["__budget__"]["n"] += 1; verwerkt += 1
            for d in lijst: verwerk_beoordeling(seen, d, d.get("link"), bron)
        except Exception as e:
            log("Fout bij e-mail:", e)
            if isinstance(e, AI_FOUT): waarschuw(seen, "ai", f"⚠️ Beoordelen mislukt: {str(e)[:150]}. Is je Anthropic-tegoed op?")
    try: im.logout()
    except Exception: pass

# ---------- Commando's en plak-berichten ----------
HELP = ("🤖 Commando's:\n/gereageerd 12 – markeer woning #12 als 'gereageerd'\n/lijst – woningen waar je op reageerde\n"
        "/pakket – wat je meestuurt bij een reactie\n/huurtoeslag – wat je moet weten over huurtoeslag\n/sociaal – sociale huur en inschrijfduur\n/status – hoe het ervoor staat\n/week – overzicht van de afgelopen 7 dagen\n"
        "Plak een advertentie in de chat, stuur een doorgestuurd bericht of een screenshot, dan beoordeel ik hem.")

def commando(seen, chat, tekst):
    delen = tekst.split()
    cmd = delen[0].lower().split("@")[0]
    items = seen.get("__log__", {}).get("items", {})
    if cmd in ("/start", "/help"):
        stuur_naar(chat, HELP)
    elif cmd == "/pakket":
        stuur_naar(chat, PAKKET_TEKST)
    elif cmd == "/sociaal":
        stuur_naar(chat, SOCIAAL_TEKST)
    elif cmd == "/status":
        b = seen.get("__budget__", {})
        laatst = seen.get("__laatste_run__")
        reageer = sum(1 for v in items.values() if v.get("status") == "gereageerd")
        stuur_naar(chat, "🩺 Status\n"
            f"Laatste run: {time.strftime('%d-%m %H:%M UTC', time.gmtime(laatst)) if laatst else 'onbekend'}\n"
            f"Beoordelingen vandaag: {b.get('n', 0)}/{MAX_PER_DAG}\n"
            f"Mail: {'aan' if (MAIL_USER and MAIL_PASS) else 'uit'} · Sites scannen: {'aan' if SCAN_SITES else 'uit'}\n"
            f"Gereageerd op {reageer} woningen · bijgehouden: {len(items)}")
    elif cmd == "/huurtoeslag":
        stuur_naar(chat, HUURTOESLAG_TEKST)
    elif cmd == "/week":
        stuur_naar(chat, weekoverzicht(seen))
    elif cmd == "/gereageerd":
        nr = delen[1].lstrip("#") if len(delen) > 1 else ""
        if nr in items:
            items[nr]["status"] = "gereageerd"; items[nr]["gereageerd_ts"] = int(time.time())
            stuur_naar(chat, f"✅ #{nr} gemarkeerd als gereageerd: {items[nr].get('titel')}. "
                             f"Ik herinner je over {HERINNERING_NA_DAGEN} dagen.")
        else:
            stuur_naar(chat, "Dat nummer ken ik niet. Gebruik bijvoorbeeld: /gereageerd 12")
    elif cmd == "/lijst":
        rij = [f"#{k} · {v.get('titel')} · {round((time.time() - v['gereageerd_ts']) / 86400)} dgn geleden"
               for k, v in items.items() if v.get("status") == "gereageerd"]
        stuur_naar(chat, "📋 Gereageerd op:\n" + "\n".join(rij) if rij else "Je hebt nog op niets gereageerd.")
    else:
        stuur_naar(chat, HELP)

def telegram_inbox(seen):
    """Verwerkt wat jullie in de chat sturen: commando's, geplakte tekst, doorgestuurde berichten en screenshots."""
    if not TG_TOKEN: return
    ups = tg("getUpdates", timeout=0).get("result", [])
    for u in ups:
        cb = u.get("callback_query")
        if cb:
            cm = cb.get("message") or {}
            cchat = str(cm.get("chat", {}).get("id"))
            code, _, nr = (cb.get("data") or "").partition(":")
            st = STATUS_CODES.get(code)
            if cchat in CHATS and st and zet_status(seen, nr, st):
                tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Gemarkeerd: {st}")
                tg("editMessageReplyMarkup", chat_id=cchat, message_id=cm.get("message_id"), reply_markup=maak_knoppen(nr, st))
            continue
        m = u.get("message") or {}
        chat = str(m.get("chat", {}).get("id"))
        if chat not in CHATS: continue
        tekst = (m.get("text") or m.get("caption") or "").strip()
        if tekst.startswith("/"):
            commando(seen, chat, tekst); continue
        if not tekst and not m.get("photo"): continue
        if tekst and not m.get("photo") and re.fullmatch(r"https?://\S+", tekst):
            stuur_naar(chat, "Ik kan geen links openen (Facebook en andere sites vragen om in te loggen). "
                             "Plak de tekst van het bericht, of stuur een screenshot."); continue
        try:
            d = analyseer_beeld(download_foto(m), tekst) if m.get("photo") else analyseer(tekst[:7000], "handmatig")
            if not d.get("is_woning"):
                stuur_naar(chat, "Ik kon hier geen woningadvertentie in vinden."); continue
            verwerk_beoordeling(seen, d, None, "handmatig", forceer=True)
        except Exception as e:
            stuur_naar(chat, f"Kon dit bericht niet beoordelen: {e}")
    if ups: tg("getUpdates", offset=ups[-1]["update_id"] + 1)

# ---------- Herinneringen en weekoverzicht ----------
def herinneringen(seen):
    nu = time.time()
    for nr, v in seen.get("__log__", {}).get("items", {}).items():
        if v.get("status") == "gereageerd" and not v.get("herinnerd") \
                and nu - v.get("gereageerd_ts", nu) > HERINNERING_NA_DAGEN * 86400:
            stuur(f"⏰ Je reageerde {HERINNERING_NA_DAGEN} dagen geleden op #{nr} ({v.get('titel')}). "
                  f"Nog niets gehoord? Overweeg een korte follow-up of bel de verhuurder.")
            v["herinnerd"] = True

def weekoverzicht(seen, dagen=7):
    nu = time.time()
    st = [x for x in seen.get("__stats__", []) if nu - x["ts"] <= dagen * 86400]
    if not st: return "📊 Weekoverzicht: geen passende woningen de afgelopen week."
    huren = [x["huur"] for x in st if x.get("huur")]
    top = sorted(st, key=lambda x: -x["score"])[:3]
    regels = [f"📊 Afgelopen {dagen} dagen: {len(st)} passende woningen",
              f"💶 Gemiddelde huur: €{round(sum(huren) / len(huren))}" if huren else "",
              "📍 Meeste aanbod: " + ", ".join(f"{p} ({n})" for p, n in Counter(x["plaats"] for x in st).most_common(3)),
              "", "🏆 Beste 3:"]
    regels += [f"{x['score']}/10 · {x['titel']} ({x['plaats']}) {x.get('url') or ''}" for x in top]
    return "\n".join(r for r in regels if r is not None)

def wekelijks(seen):
    t = time.gmtime()
    week = f"{t.tm_year}-{time.strftime('%W', t)}"
    if t.tm_wday == 6 and t.tm_hour >= 17 and seen.get("__week__") != week:
        stuur(weekoverzicht(seen)); seen["__week__"] = week

PAGINA = r"""<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Woningen</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mu:#666;--card:#f4f4f2;--bd:#ddd}
@media(prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--mu:#9a9a9a;--card:#1e1e1e;--bd:#333}}
body{margin:0 auto;padding:16px;max-width:720px;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}
button,select{font:inherit;padding:6px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--card);color:var(--fg)}
button.on{border-color:var(--fg)}.bar{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}
.c{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:12px;margin:10px 0}
.m{color:var(--mu);font-size:14px}a{color:inherit}
</style></head><body><h1>Woningen</h1><div class="bar" id="f"></div>
<div class="bar"><select id="o"><option value="score">Hoogste score</option><option value="ts">Nieuwste</option>
<option value="huur">Laagste huur</option></select></div><div id="l"></div>
<script>
var D=__DATA__,F="open";
var T={open:"Nieuw en bewaard",gereageerd:"Gereageerd",afgewezen:"Afgewezen",alles:"Alles"};
var fb=document.getElementById("f"),ls=document.getElementById("l"),so=document.getElementById("o");
function ok(x){return F=="alles"||(F=="open"?(x.status=="nieuw"||x.status=="bewaard"):x.status==F)}
function draw(){
fb.textContent="";
Object.keys(T).forEach(function(k){var b=document.createElement("button");b.textContent=T[k];if(k==F)b.className="on";b.onclick=function(){F=k;draw()};fb.appendChild(b)});
var r=D.filter(ok),k=so.value;
r.sort(function(a,b){return k=="huur"?(a.huur||1e9)-(b.huur||1e9):(b[k]||0)-(a[k]||0)});
ls.textContent="";
if(!r.length){ls.textContent="Nog niets in deze lijst.";return}
r.forEach(function(x){
var c=document.createElement("div");c.className="c";
var lnk=x.url&&/^https?:\/\//.test(x.url);
var t=document.createElement(lnk?"a":"span");
if(lnk){t.href=x.url;t.target="_blank";t.rel="noopener"}
t.textContent=(x.score>=8?"🟢":x.score>=5?"🟡":"🔴")+" "+(x.score||"?")+"/10 · "+(x.titel||"Woning");
t.style.fontWeight="700";
var m=document.createElement("div");m.className="m";
m.textContent=[x.plaats,x.huur?"€"+x.huur:null,x.slk?x.slk+" slk":null,x.ov?"~"+x.ov+" min OV":null,x.bron,"#"+x.nr,new Date(x.ts*1000).toLocaleDateString("nl-NL")].filter(Boolean).join(" · ");
c.appendChild(t);c.appendChild(m);ls.appendChild(c)})}
so.onchange=draw;draw();
</script></body></html>"""

def schrijf_pagina(seen):
    """Maakt docs/index.html (voor GitHub Pages) met alle woningen, nieuwste eerst."""
    rij = [dict(v, nr=int(k)) for k, v in seen.get("__log__", {}).get("items", {}).items()]
    rij.sort(key=lambda x: -x["ts"])
    velden = ("nr", "titel", "url", "plaats", "huur", "slk", "ov", "bron", "score", "status", "ts")
    data = json.dumps([{k: x.get(k) for k in velden} for x in rij[:300]], ensure_ascii=False).replace("</", "<\\/")
    nieuw = PAGINA.replace("__DATA__", data)
    try: oud = open("docs/index.html", encoding="utf-8").read()
    except Exception: oud = ""
    if nieuw != oud:
        os.makedirs("docs", exist_ok=True)
        open("docs/index.html", "w", encoding="utf-8").write(nieuw)

def opschonen(seen):
    nu = time.time()
    if "__dupes__" in seen:
        seen["__dupes__"] = {k: v for k, v in seen["__dupes__"].items() if nu - v < 60 * 86400}
    if "__route__" in seen:
        seen["__route__"] = {k: v for k, v in seen["__route__"].items() if nu - v[1] < 60 * 86400}
    items = seen.get("__log__", {}).get("items", {})
    for k in [k for k, v in items.items() if nu - v.get("ts", nu) > 120 * 86400 and v.get("status") not in ("gereageerd", "bewaard")]:
        del items[k]
    if "__stats__" in seen:
        seen["__stats__"] = [x for x in seen["__stats__"] if nu - x["ts"] < 35 * 86400]

if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY ontbreekt: controleer de naam en dat het een Repository secret is.")
    seen = laad_seen()
    seen['__laatste_run__'] = int(time.time()) // 3600 * 3600   # per uur afgerond: voorkomt een commit bij elke run
    telegram_inbox(seen)
    scan_mail(seen)
    if SCAN_SITES: scan_sites(seen)
    herinneringen(seen)
    wekelijks(seen)
    opschonen(seen)
    schrijf_pagina(seen)
    json.dump(seen, open("seen.json", "w"))
