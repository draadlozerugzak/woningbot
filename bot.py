"""Woningbot: scant huuradvertenties, laat Claude ze beoordelen, stuurt je een Telegram-bericht.
Plak je ook Facebook-berichten of e-mailmeldingen in de Telegram-chat met de bot, dan beoordeelt hij die ook.
Commando's in Telegram: /help /pakket /gereageerd N /lijst /week"""
import os, re, sys, json, time, imaplib, email, base64, traceback
from statistics import median
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
MAX_PER_DAG = 500        # max aantal AI-beoordelingen per dag (mails en advertenties samen)

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

# Reageren: "gratis", "account" (account of inschrijving nodig) of "betaald". Leeg = onbekend. Controleer dit zelf.
# De advertentietekst gaat voor: staat daar iets over kosten of een account, dan gebruikt de bot dat.
BRON_KOSTEN = {
    "Kamernet": "betaald", "HousingAnywhere": "account", "Holland2Stay": "account", "SSH XL": "account",
    "DAK Utrecht": "account", "DAK Midden-Holland": "account", "WoningNet": "account",
}
# Platforms die aanbod van anderen doorgeven. Alles wat hier NIET in staat (makelaars, corporaties) telt als 'origineel'.
PLATFORMS = {"Pararius", "Funda", "Huurwoningen.nl", "Kamernet", "Kamer.nl", "Huurstunt", "HousingAnywhere", "Rentola",
             "DirectWonen", "Marktplaats", "Stekkies", "Startersroom", "Findify", "Uprent", "Huispedia", "Huislijn",
             "Facebook", "Google Alert", "e-mail", "handmatig", "Nederwoon", "Dutch Student Housing"}
BRON_RISICO = {"Marktplaats": "hoog", "Facebook": "hoog", "Google Alert": "hoog", "e-mail": "hoog", "handmatig": "hoog"}
# Plaatsen rond Utrecht: bronnen met "plaatsen": true worden ook voor deze plaatsen bekeken (elk uur).
OMGEVING = ["zeist", "houten", "nieuwegein", "ijsselstein", "de-bilt", "bunnik", "woerden", "amersfoort",
            "veenendaal", "culemborg", "gouda", "hilversum"]

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

# --- Zoekprofielen: hoofdprofiel (je echte wensen) en een ruimer tweede profiel ---
TIJDELIJK_STRENG = True       # hoofdprofiel: geen tijdelijke huur (antikraak, jongeren- of campuscontract korter dan je opleiding)
RUIM_PROFIEL_AAN = True       # tweede profiel: ruimere grenzen, antikraak en tijdelijke contracten toegestaan
ZP_HOOFD = {"naam": "hoofd", "max_huur": MAX_HUUR, "ruim_huur": RUIM_HUUR, "max_ov": MAX_OV, "ruim_ov": RUIM_OV,
            "min_slk": 2, "tijdelijk": False, "min_score": MIN_SCORE}
ZP_RUIM = {"naam": "ruim", "max_huur": 1500, "ruim_huur": 1.0, "max_ov": 75, "ruim_ov": 90,
           "min_slk": 1, "tijdelijk": True, "min_score": 6}   # meldingen van dit profiel komen stil binnen
MAIL_BRONNEN = {   # afzenderdomein -> naam (voor BRON_KOSTEN en in het bericht)
    "funda.nl": "Funda", "pararius.nl": "Pararius", "pararius.com": "Pararius", "huurwoningen.nl": "Huurwoningen.nl",
    "kamernet.nl": "Kamernet", "kamer.nl": "Kamer.nl", "huurstunt.nl": "Huurstunt", "marktplaats.nl": "Marktplaats",
    "directwonen.nl": "DirectWonen", "rentola.nl": "Rentola", "housinganywhere.com": "HousingAnywhere",
    "holland2stay.com": "Holland2Stay", "ssh.nl": "SSH", "duwo.nl": "DUWO", "woningnet.nl": "WoningNet",
    "vesteda.com": "Vesteda", "rotsvast.nl": "Rotsvast", "huislijn.nl": "Huislijn", "huispedia.nl": "Huispedia",
    "uprent.nl": "Uprent", "stekkies.com": "Stekkies", "iamexpat.nl": "IamExpat", "interhouse.nl": "Interhouse",
    "123wonen.nl": "123Wonen", "houserenting4all.nl": "Houserenting4all",
    "startersroom.nl": "Startersroom", "findify.nl": "Findify",
    "facebookmail.com": "Facebook",   # meldingen van Facebook-groepen (alleen als de mail over woningen gaat)
    "googlealerts-noreply@google.com": "Google Alert",   # een volledig adres mag ook (dan geldt alleen dat adres)
}

# Plak hier je EIGEN zoekresultaat-URL's (alleen sites waarvan je de voorwaarden hebt gelezen!).
SOURCES = [
    {"naam": "Rentola", "url": "https://rentola.nl/huurwoningen?location=utrecht&maxPrice=1400",
     "pattern": r"/(nl/)?(listings|huurwoning)/"},
    {"naam": "DirectWonen", "url": "https://directwonen.nl/huurwoningen-huren/utrecht",
     "pattern": r"/huurwoningen-huren/", "plaatsen": True},
]
# ===========================================

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# Eén of meerdere chat-ID's, gescheiden door een komma: "123456,789012"
CHATS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
GMAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
MAIL_USER = os.environ.get("MAIL_ADDRESS", "")
MAIL_PASS = os.environ.get("MAIL_APP_PASSWORD", "")
MAIL_HOST = os.environ.get("MAIL_IMAP_HOST") or "imap.gmail.com"   # lege secret = Gmail
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
woningruil (true bij woningruil/ruilen), korte_termijn (true als de verhuur korter dan 6 maanden is: short stay, vakantie- of tijdelijke verhuur),
reageren_kosten ("betaald" als reageren/contact geld kost, "account" als een account of inschrijving nodig is, "gratis", null = onbekend),
reageren_via (naam van de site/partij waar je moet reageren als dat niet hier kan, bijv. "MVGM", "WoningNet/DĀK", "corporatie"; anders null),
aangeboden_door (naam van de makelaar of verhuurder als vermeld, anders null),
antikraak (true bij antikraak/tijdelijke bewoning zonder huurbescherming, anders false),
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
    """Telegram-aanroep met één nieuwe poging. Bij een netwerkfout geven we een leeg antwoord terug,
    zodat een trage Telegram-server niet de hele run laat crashen."""
    for poging in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=(10, 40))
            return r.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            log(f"Telegram {method} mislukt (poging {poging + 1}): {e}")
            time.sleep(2)
    return {"ok": False, "result": []}

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

def geldige_url(u):
    return bool(u) and len(u) < 1000 and re.fullmatch(r"https?://[^\s]+", u) is not None

def maak_knoppen(nr, status="nieuw", url=None, route=None):
    def k(tekst, code, st): return {"text": ("✔ " if status == st else "") + tekst, "callback_data": f"{code}:{nr}"}
    rij = ([{"text": "🔗 Advertentie", "url": url}] if geldige_url(url) else []) + \
          ([{"text": "🗺 OV-route", "url": route}] if geldige_url(route) else [])
    knoppen = [k("✅ Gereageerd", "g", "gereageerd"), k("⭐ Bewaren", "b", "bewaard"), k("❌ Niet interessant", "x", "afgewezen")]
    return {"inline_keyboard": ([rij] if rij else []) + [knoppen]}

def zet_status(seen, nr, status):
    it = seen.get("__log__", {}).get("items", {}).get(str(nr))
    if not it: return None
    it["status"] = status
    if status == "gereageerd":
        it["gereageerd_ts"] = int(time.time()); it.pop("herinnerd", None)
    return it

AI_FOUT = getattr(anthropic, "APIError", ())

def ai_fout(seen, e):
    """Meldt AI-fouten in Telegram. Geeft 'credits' terug als het tegoed op is, 'overig' bij andere API-fouten, anders None."""
    if not isinstance(e, AI_FOUT): return None
    if "credit balance" in str(e).lower():
        waarschuw(seen, "credits", "💳 Je Anthropic-tegoed is op, dus ik kan geen woningen meer beoordelen.\n"
                  "Stort tegoed via console.anthropic.com > Billing. Daarna gaat de bot vanzelf verder met wat hij nog niet had beoordeeld "
                  "(mails van de laatste 3 dagen worden dan nog meegenomen).", uren=6)
        return "credits"
    waarschuw(seen, "ai", f"⚠️ Beoordelen mislukt: {str(e)[:150]}")
    return "overig"

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

SCAM_TEKST = """🚨 Scam-check bij huurwoningen
• Betaal nooit borg of huur voordat je de woning hebt gezien en een contract hebt getekend. Bij een echte verhuurder kan een bezichtiging altijd.
• De waarborgsom mag maximaal 2 maanden kale huur zijn (Wet goed verhuurderschap). Vraagt iemand meer, dan klopt er iets niet.
• Wees extra voorzichtig bij Marktplaats, Facebook en losse e-mails. Verhuurders in het buitenland, alleen contact via WhatsApp, haast of een prijs ver onder de markt zijn rode vlaggen.
• Staat de woning ook bij een makelaar of corporatie, dan is dat een goed teken. De bot meldt dat als hij het ziet.
• Controleer de makelaar op kvk.nl en zoek het adres online op.
• Twijfel je? Meld het bij het Meldpunt huurmisstanden van de gemeente Utrecht of bij de Fraudehelpdesk."""

def laad_extra_bronnen():
    """Voegt sites toe uit bronnen.json (zo hoef je bot.py niet aan te passen). Zet \"aan\": false om een site uit te zetten."""
    try:
        for b in json.load(open("bronnen.json", encoding="utf-8")):
            if b.get("aan", True) and b.get("naam") and b.get("url"):
                SOURCES.append({"naam": b["naam"], "url": b["url"], "pattern": b.get("pattern"), "plaatsen": b.get("plaatsen", False)})
    except FileNotFoundError:
        pass
    except Exception as e:
        log("bronnen.json fout:", e)

def site_check(url):
    """Hulp om een nieuwe site te beoordelen: staat robots.txt het toe en welk link-patroon past?"""
    if not toegestaan(url):
        return "🚫 De robots.txt van die site staat het ophalen van deze pagina niet toe (of is niet te lezen). Ik laat deze site links liggen."
    try:
        r = requests.get(url, headers=HEADERS, timeout=30); r.raise_for_status()
    except Exception as e:
        return f"Ophalen mislukt: {e}"
    host, c, vb = urlparse(url).netloc, Counter(), {}
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0].split("?")[0]
        seg = [x for x in urlparse(u).path.split("/") if x]
        if urlparse(u).netloc != host or len(seg) < 2: continue
        for pre in {"/" + seg[0] + "/", "/" + "/".join(seg[:-1]) + "/"}:   # eerste deel en volledig pad zonder laatste deel
            c[pre] += 1; vb.setdefault(pre, u)
    top = [(p, n) for p, n in c.most_common(3) if n >= 3]
    if not top:
        return "robots.txt staat het toe, maar ik vond geen herkenbare advertentielinks (de site laadt misschien alles met JavaScript)."
    return ("✅ robots.txt staat het ophalen toe. Mogelijke patronen:\n" +
            "\n".join(f"• {re.escape(p)} ({n} links), bv. {vb[p]}" for p, n in top) +
            "\nLees ook de voorwaarden van de site. Mag het, zet dan in bronnen.json: "
            '{"naam": "...", "url": "<jouw zoek-URL>", "pattern": "<patroon>"}')

def haal_op(url, pogingen=2):
    """Haalt een pagina op met een ruimere time-out en één nieuwe poging bij een tijdelijke netwerkfout (niet bij 403/404)."""
    for i in range(pogingen):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(10, 45))
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError:
            raise
        except requests.exceptions.RequestException:
            if i == pogingen - 1: raise
            time.sleep(3)

class GeenAanbod(Exception): pass
class Geweigerd(Exception): pass

def ontdek_patroon(html, url):
    """Zoekt zelf welk deel van het adres bij advertentielinks hoort (links die op een losse woning lijken)."""
    host, c = urlparse(url).netloc, Counter()
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0].split("?")[0]
        seg = [x for x in urlparse(u).path.split("/") if x]
        if urlparse(u).netloc != host or len(seg) < 2: continue
        if not (re.search(r"\d", seg[-1]) or seg[-1].count("-") >= 2): continue
        for d_ in range(1, len(seg)): c["/" + "/".join(seg[:d_]) + "/"] += 1
    if not c: return None
    top = c.most_common(1)[0][1]
    if top < 4: return None
    return re.escape(max((p for p, n in c.items() if n >= 0.7 * top), key=len))

def vind_links(src, seen=None):
    if not toegestaan(src["url"]):
        raise Geweigerd("robots.txt staat dit niet toe")
    r = haal_op(src["url"])
    pat = src.get("pattern")
    if not pat:
        cache = (seen if seen is not None else {}).setdefault("__patronen__", {})
        if src["url"] not in cache: cache[src["url"]] = ontdek_patroon(r.text, src["url"]) or ""
        pat = cache[src["url"]]
        if not pat: raise GeenAanbod("geen herkenbare advertentielinks, waarschijnlijk JavaScript")
    links = set()
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        u = urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
        if re.search(pat, u) and u.rstrip("/") != src["url"].rstrip("/") and len(u) > len(src["url"]) + 5:
            links.add(u)
    return links

def analyseer(tekst, bron):
    msg = client.messages.create(model=MODEL, max_tokens=8000, system=PROMPT,
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
    msg = client.messages.create(model=MODEL, max_tokens=16000, system=PROMPT_MAIL,
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
    msg = client.messages.create(model=MODEL, max_tokens=8000, system=PROMPT, messages=[{"role": "user", "content": [
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

def score(d, p=ZP_HOOFD):
    """Cijfer 1-10 op basis van vaste regels (transparant, kost geen extra AI)."""
    s = 10
    h = d.get("huur_totaal")
    if h is None: s -= 1
    elif h > p['max_huur']: s -= min(3, 1 + int(((h / p['max_huur'] - 1) * 100) // 5))
    ov = d.get("ov_minuten")
    if ov is None: s -= 1
    elif ov > p['max_ov']: s -= min(3, 1 + (ov - p['max_ov']) // 5)
    sk = d.get("slaapkamers")
    if sk is None: s -= 1
    elif sk < p['min_slk']: s -= 3
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
    if d.get("antikraak"): s -= 1 if p["tijdelijk"] else 4
    if not p["tijdelijk"] and d.get("contract") in ("bepaalde tijd", "jongerencontract", "campuscontract"):
        cd = d.get("contractduur_jaren")
        s -= 2 if isinstance(cd, (int, float)) and cd < OPLEIDING_JAREN else 1
    s -= {"hoog": 5, "middel": 2}.get(d.get("scam_risico"), 0)
    if BRON_RISICO.get(d.get("bron")) == "hoog": s -= 1
    if prijs_per_m2_laag(d): s -= 2
    return max(1, min(10, s))

def labels_en_filter(d, forceer=False, p=ZP_HOOFD):
    """Korte waarschuwingen, of None als de advertentie ruim buiten je grenzen valt."""
    L = []
    if SKIP_ONHAALBAAR_INKOMEN and not forceer and d.get("inkomenseis") \
            and d.get("garant_ok") is False and d.get("spaargeld_ok") is not True:
        return None
    h = d.get("huur_totaal")
    if h:
        if h > p['max_huur'] * p['ruim_huur'] and not forceer: return None
        if h > MAX_HUUR: L.append(f"💸 +{round((h / MAX_HUUR - 1) * 100)}% boven budget")
        if not d.get("huur_zeker_inclusief"): L.append("⚠️ all-in onduidelijk")
    ov = d.get("ov_minuten")
    if ov:
        if ov > p['ruim_ov'] and not forceer: return None
        if ov > MAX_OV: L.append("🚆 boven je OV-limiet")
    if d.get("zelfstandig") is False: L.append("🚪 onzelfstandig")
    if d.get("antikraak"): L.append("⚠️ antikraak: weinig huurbescherming")
    if d.get("scam_risico") in ("middel", "hoog"):
        L.append(f"🚨 scam-risico {d['scam_risico']}" + (f": {d['scam_reden']}" if d.get("scam_reden") else ""))
    if prijs_per_m2_laag(d): L.append("🚨 opvallend goedkoop per m²")
    if BRON_RISICO.get(d.get("bron")) == "hoog": L.append("🚨 open platform: extra opletten op scam")
    return L

def is_tijdelijk(d):
    """Tijdelijke huur: antikraak, of een contract voor bepaalde tijd dat korter is dan je opleiding (of de duur is onbekend)."""
    if d.get("antikraak"): return True
    if d.get("contract") in ("bepaalde tijd", "jongerencontract", "campuscontract"):
        cd = d.get("contractduur_jaren")
        return not (isinstance(cd, (int, float)) and cd >= OPLEIDING_JAREN)
    return False

def prijs_ppm(d):
    h, m2 = d.get("huur_totaal"), d.get("oppervlakte_m2")
    return h / m2 if h and m2 and m2 >= 15 else None

def prijs_label(seen, d):
    """Vergelijkt de prijs per m² met wat de bot eerder zag (zelfde plaats, anders de hele regio)."""
    ppm, pr = prijs_ppm(d), seen.get("__prijzen__", {})
    if not ppm: return None
    plaats = (d.get("plaats") or "").lower()
    if len(pr.get(plaats, [])) >= 5: basis, naam = median(pr[plaats]), d.get("plaats")
    elif len(pr.get("__alle__", [])) >= 10: basis, naam = median(pr["__alle__"]), "de regio"
    else: return None
    diff = ppm / basis - 1
    if diff >= 0.25: return f"💶 €{ppm:.0f}/m²: {diff:.0%} duurder dan gemiddeld in {naam}"
    if diff <= -0.25: return f"💶 €{ppm:.0f}/m²: {-diff:.0%} goedkoper dan gemiddeld in {naam}, check all-in en scam"
    return None

def prijs_registreer(seen, d):
    ppm = prijs_ppm(d)
    if not ppm: return
    pr = seen.setdefault("__prijzen__", {})
    for k in ((d.get("plaats") or "").lower(), "__alle__"):
        if k:
            pr.setdefault(k, []).append(round(ppm, 1))
            pr[k] = pr[k][-(150 if k == "__alle__" else 40):]

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
• Er is een inkomensgrens voor de meeste sociale huur (in 2026 ongeveer €51.500 voor een alleenstaande en €56.900 voor meer personen, controleer de actuele grens) en je hebt een huisvestingsvergunning nodig.
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
    if d.get("huurtoeslag") is False: return "🏷 geen toeslag (volgens advertentie)"
    if d.get("zelfstandig") is False: return "🏷 geen toeslag (onzelfstandig)"
    tekst = "🏷 toeslag mogelijk ✓" if d.get("huurtoeslag") is True else "🏷 toeslag niet vermeld"
    kale, alin = d.get("kale_huur"), d.get("huur_totaal")
    basis = kale or alin
    if TOESLAG_AAN and basis:
        t = toeslag_schatting(basis / 2)
        if t > 0:
            tekst += f" · na toeslag ca. €{round((alin or basis) / 2) - t} p.p." + ("" if kale else " (kale huur onbekend)")
    return tekst

def kaart(d, link, bron, nr, sc, labels):
    kleur = "🟢" if sc >= 8 else "🟡" if sc >= 5 else "🔴"
    stel = "💑 ALLEEN STELLEN · " if d.get("alleen_stellen") else ""
    vuur = "🔥 " if sc >= 9 else ""
    huur = f"€{d['huur_totaal']}" if d.get("huur_totaal") else "€?"
    m2 = f" · {d['oppervlakte_m2']} m²" if d.get("oppervlakte_m2") else ""
    ovm = d.get("ov_minuten") or "?"
    ov = {"route": f"{ovm} min OV ✓", "plaats": f"~{ovm} min OV (centrum)"}.get(d.get("ov_bron"), f"~{ovm} min OV")
    kost = {"betaald": "💳 betaald reageren", "account": "🔑 account nodig", "gratis": "🆓 gratis reageren"}.get(
        d.get("reageren_kosten") or BRON_KOSTEN.get(bron, ""))
    door = d.get("aangeboden_door")
    r3 = [f"{bron} #{nr}", f"door {door}" if door and door.lower() not in bron.lower() else None, kost,
          f"➡️ reageren via {d['reageren_via']}" if d.get("reageren_via") else None,
          f"📅 {d['beschikbaar_per']}" if d.get("beschikbaar_per") else None]
    regels = [f"{vuur}{kleur} {sc}/10 · {stel}{d.get('titel') or 'Woning'}",
              f"📍 {d.get('plaats') or '?'} · {huur} · {d.get('slaapkamers') or '?'} slk{m2} · {ov}",
              " · ".join(x for x in r3 if x)]
    for extra in (" · ".join(labels) if labels else "", woonvorm_regel(d), eisen_regel(d), huurtoeslag_regel(d)):
        if extra: regels.append(extra)
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

def toeslag_netto(d):
    """Geschatte huur per persoon na huurtoeslag, of None."""
    if not TOESLAG_AAN or d.get("huurtoeslag") is False or d.get("zelfstandig") is False: return None
    basis = d.get("kale_huur") or d.get("huur_totaal")
    if not basis: return None
    t = toeslag_schatting(basis / 2)
    return round((d.get("huur_totaal") or basis) / 2 - t) if t > 0 else None

def registreer(seen, d, link, bron="", sc=None, vlaggen=None):
    lg = seen.setdefault("__log__", {"n": 0, "items": {}})
    lg["n"] += 1
    nr = lg["n"]
    lg["items"][str(nr)] = {"titel": d.get("titel"), "url": link, "ts": int(time.time()), "status": "nieuw",
        "plaats": d.get("plaats"), "huur": d.get("huur_totaal"), "slk": d.get("slaapkamers"),
        "ov": d.get("ov_minuten"), "bron": bron, "score": sc, "opp": d.get("oppervlakte_m2"),
        "netto": toeslag_netto(d), "vlaggen": vlaggen or [], "wv": woonvorm_regel(d), "sam": d.get("samenvatting"),
        "route": route_link(d), "kosten": d.get("reageren_kosten") or BRON_KOSTEN.get(bron, ""),
        "via": d.get("reageren_via"), "door": d.get("aangeboden_door")}
    return nr

def voeg_stat_toe(seen, d, link, sc):
    if sc >= 5:
        seen.setdefault("__stats__", []).append({"ts": int(time.time()), "plaats": d.get("plaats") or "?",
            "huur": d.get("huur_totaal"), "score": sc, "titel": d.get("titel"), "url": link})

def verwerk_beoordeling(seen, d, link, bron, forceer=False):
    """Stuurt kaart (+ eventueel reactie). Geeft True als er iets is verstuurd."""
    if not d.get("is_woning") and not forceer: return False
    d["bron"] = bron
    if not forceer and (d.get("woningruil") or d.get("korte_termijn")):
        log("woningruil of short stay, overgeslagen:", link); return False
    verrijk_ov(seen, d)
    prijslabel = prijs_label(seen, d)
    prijs_registreer(seen, d)
    kandidaten = [ZP_HOOFD] if forceer else ([ZP_HOOFD, ZP_RUIM] if RUIM_PROFIEL_AAN else [ZP_HOOFD])
    gekozen = None
    for pf in kandidaten:
        if pf is ZP_HOOFD and TIJDELIJK_STRENG and not forceer and is_tijdelijk(d): continue
        lab = labels_en_filter(d, forceer=forceer, p=pf)
        if lab is None: continue
        sc = score(d, pf)
        if forceer or sc >= pf["min_score"]: gekozen = (pf, lab, sc); break
    if not gekozen: log("overgeslagen (buiten profiel of score te laag):", link); return False
    pf, lab, sc = gekozen
    if pf is ZP_RUIM: lab = ["🔎 ruim profiel: buiten je normale eisen"] + lab
    if prijslabel: lab.append(prijslabel)
    key, zk = (dubbel_key(d), None) if not forceer else (None, None)
    if key:
        oud = seen.setdefault("__dupes__", {}).get(key)
        if oud is not None:
            nr0 = oud.get("nr") if isinstance(oud, dict) else None
            item = seen.get("__log__", {}).get("items", {}).get(str(nr0)) if nr0 else None
            if item is not None:
                item.setdefault("ook", []).append({"bron": bron, "url": link})
                if bron not in PLATFORMS and item.get("bron") in PLATFORMS and geldige_url(link):
                    item["origineel"] = link
                    stuur(f"✅ #{nr0} staat ook bij de verhuurder ({bron}). Reageren daar is vaak directer en goedkoper.", stil=True,
                          markup={"inline_keyboard": [[{"text": "🔗 Bij de verhuurder", "url": link}]]})
            log("dubbel, overgeslagen:", link); return False
    elif not forceer and d.get("huur_totaal") and d.get("oppervlakte_m2") and d.get("plaats"):
        zk = f"{d['plaats'].lower()}|{d['huur_totaal']}|{d['oppervlakte_m2']}|{d.get('slaapkamers')}"
        if zk in seen.setdefault("__zacht__", {}): lab.append(f"🔁 lijkt op #{seen['__zacht__'][zk]} (mogelijk dubbel)")
    nr = registreer(seen, d, link, bron, sc, lab)
    if key: seen["__dupes__"][key] = {"ts": int(time.time()), "nr": nr}
    if zk: seen["__zacht__"][zk] = nr
    stuur(kaart(d, link, bron, nr, sc, lab), stil=((sc < STIL_ONDER_SCORE or pf is ZP_RUIM) and not forceer),
          markup=maak_knoppen(nr, "nieuw", link, route_link(d)))
    if d.get("reactie") and not d.get("alleen_stellen") and (forceer or sc >= SCORE_REACTIE_VANAF):
        stuur(f"✍️ Concept-reactie #{nr}:\n{d['reactie']}")
    voeg_stat_toe(seen, d, link, sc)
    return True

# ---------- Sites scannen ----------
def scan_sites(seen):
    nu = time.time()
    uit, bf = seen.setdefault("__uit__", {}), seen.setdefault("__bronfouten__", {})
    bronnen = []
    for src in SOURCES:
        bronnen.append(src)
        if src.get("plaatsen") and re.search("utrecht", src["url"], re.I):
            for t in OMGEVING:       # omliggende plaatsen: elk uur, om de sites niet te belasten
                k = src["naam"] + "|" + t
                if nu - seen.setdefault("__variant__", {}).get(k, 0) >= 3600:
                    bronnen.append({**src, "naam": f"{src['naam']} ({t.title()})", "basis": src["naam"], "vkey": k,
                                    "url": re.sub("utrecht", t, src["url"], flags=re.I)})
    nieuw, nieuw_uit = [], []
    for src in bronnen:
        naam, basis = src["naam"], src.get("basis", src["naam"])
        if nu - uit.get(basis, 0) < 7 * 86400: continue     # een week uitgezet, daarna proberen we het opnieuw
        try:
            links = vind_links(src, seen)
            log(f"{naam}: {len(links)} links gevonden")
            bf[naam] = 0
            nieuw += [(basis, u) for u in links if u not in seen]
        except (GeenAanbod, Geweigerd) as e:
            log(f"{naam}: {e}")
            if "vkey" not in src: uit[basis] = int(nu); nieuw_uit.append(f"{naam} ({e})")
        except Exception as e:
            log(f"{naam} mislukt: {e}")
            if "vkey" not in src:
                bf[naam] = bf.get(naam, 0) + 1
                # een losse time-out is normaal; pas melden als het al ongeveer een uur achter elkaar misgaat
                if bf[naam] >= 12:
                    waarschuw(seen, "site-" + naam, f"⚠️ {naam} geeft al ongeveer een uur een fout: {str(e)[:100]}. "
                              f"Blijft dit dagen zo, dan blokkeert de site de bot. Haal hem dan uit bronnen.json.", uren=24)
        finally:
            if "vkey" in src: seen["__variant__"][src["vkey"]] = int(nu)
    if nieuw_uit:
        stuur("⏹ Deze bronnen kan ik niet scannen. Zet daar een e-mailalert aan:\n" + "\n".join("• " + x for x in nieuw_uit), stil=True)
    bs = seen.setdefault("__bronstats__", {})
    for bron, u in nieuw[:MAX_NIEUW_PER_RUN]:
        if budget_over(seen) <= 0:
            if not seen["__budget__"].get("gemeld"):
                stuur(f"⏸ Dagplafond van {MAX_PER_DAG} beoordelingen bereikt. Morgen (UTC) ga ik door.")
                seen["__budget__"]["gemeld"] = True
            break
        try:
            if not toegestaan(u):
                log("robots.txt staat niet toe:", u); continue
            r = haal_op(u)
            d = analyseer(pagina_tekst(r.text), bron)
            seen[u] = int(time.time())          # pas na geslaagde beoordeling 'gezien'
            seen["__budget__"]["n"] += 1
            st = bs.setdefault(bron, {"gezien": 0, "woningen": 0})
            st["gezien"] += 1; st["woningen"] += 1 if d.get("is_woning") else 0
            verwerk_beoordeling(seen, d, u, bron)
            if st["gezien"] >= 6 and st["woningen"] == 0 and bron not in uit:
                uit[bron] = int(time.time())
                stuur(f"⏹ {bron}: 6 pagina's bekeken, geen enkele woning gevonden. Ik zet deze bron uit. Gebruik daar een e-mailalert.", stil=True)
        except Exception as e:
            log(f"Fout bij {u}: {e}")
            if ai_fout(seen, e) == "credits": break    # geen zin om de rest nu nog te proberen

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

def spam_map():
    """Naam van de spammap bij je provider (overschrijfbaar met de optionele secret MAIL_SPAMMAP)."""
    h = MAIL_HOST.lower()
    return os.environ.get("MAIL_SPAMMAP") or ("[Gmail]/Spam" if "gmail" in h else "Bulk" if "yahoo" in h else "Junk")

def vind_spammap(im):
    """Zoekt de spammap via de \\Junk-markering van de mailserver (werkt ongeacht de taal van je postvak)."""
    try:
        for r in im.list()[1] or []:
            regel = r.decode(errors="replace") if isinstance(r, bytes) else str(r)
            if "\\Junk" in regel:
                m = re.search(r'"([^"]+)"\s*$', regel) or re.search(r'(\S+)\s*$', regel)
                if m: return m.group(1)
    except Exception as e:
        log("Spammap zoeken mislukt:", e)
    return spam_map()

def scan_mail(seen):
    if not (MAIL_USER and MAIL_PASS): return
    try:
        im = imaplib.IMAP4_SSL(MAIL_HOST)
        im.login(MAIL_USER, MAIL_PASS)
        sinds = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
    except Exception as e:
        log("Mail mislukt:", e)
        waarschuw(seen, "mail", f"⚠️ Ik kan niet in het postvak komen ({str(e)[:100]}). Controleer MAIL_ADDRESS en MAIL_APP_PASSWORD."); return
    verwerkt = [0]
    # Inbox: alle woningmails. Spammap: alleen mails van bekende woningsites (alerts die onterecht als spam zijn gemarkeerd).
    for mapnaam, strikt in (("INBOX", False), (vind_spammap(im), True)):
        try:
            if im.select(f'"{mapnaam}"', readonly=True)[0] != "OK":    # alleen lezen: er wordt niets gewijzigd of verwijderd
                continue
            ids = im.search(None, f'(SINCE "{sinds}")')[1][0].split()
        except Exception as e:
            log(f"Map {mapnaam} overgeslagen:", e); continue
        if scan_map(seen, im, ids, strikt, verwerkt) == "stop": break
    try: im.logout()
    except Exception: pass

def scan_map(seen, im, ids, strikt, verwerkt):
    for num in ids:
        if verwerkt[0] >= MAX_MAILS_PER_RUN: return "stop"
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
            if not bron and (strikt or not re.search(r"huur|woning|appartement|kamer|studio|te huur", onderwerp, re.I)):
                seen[key] = int(time.time()); continue   # geen woningmail, negeren
            if budget_over(seen) <= 0: return "stop"
            bron = bron or "e-mail"
            tekst = mail_tekst(msg)
            if not tekst or (bron == "Facebook" and not re.search(r"huur|woning|appartement|slaapkamer|studio|€", tekst, re.I)):
                seen[key] = int(time.time()); continue    # leeg, of een Facebook-melding die niet over woningen gaat
            lijst = analyseer_mail(tekst, bron, onderwerp)
            seen[key] = int(time.time()); seen["__budget__"]["n"] += 1; verwerkt[0] += 1
            for d in lijst: verwerk_beoordeling(seen, d, d.get("link"), bron)
        except Exception as e:
            log("Fout bij e-mail:", e)
            if ai_fout(seen, e) == "credits": return "stop"    # geen zin om de rest nu nog te proberen

# ---------- Commando's en plak-berichten ----------
HELP = ("🤖 Commando's:\n/gereageerd 12 – markeer woning #12 als 'gereageerd'\n/lijst – woningen waar je op reageerde\n"
        "/pakket – wat je meestuurt bij een reactie\n/huurtoeslag – wat je moet weten over huurtoeslag\n/sociaal – sociale huur en inschrijfduur\n/prijzen – gemiddelde prijs per m² per plaats\n/schoon – schone start (wist overzicht en statistieken)\n/scam – tips tegen oplichting\n/site <url> – check of een nieuwe site gescand mag worden\n/status – hoe het ervoor staat\n/week – overzicht van de afgelopen 7 dagen\n"
        "Plak een advertentie in de chat, stuur een doorgestuurd bericht of een screenshot, dan beoordeel ik hem.")

def commando(seen, chat, tekst):
    delen = tekst.split()
    cmd = delen[0].lower().split("@")[0]
    items = seen.get("__log__", {}).get("items", {})
    if cmd in ("/start", "/help"):
        stuur_naar(chat, HELP)
    elif cmd == "/pakket":
        stuur_naar(chat, PAKKET_TEKST)
    elif cmd == "/scam":
        stuur_naar(chat, SCAM_TEKST)
    elif cmd == "/site":
        u = delen[1] if len(delen) > 1 else ""
        stuur_naar(chat, site_check(u) if re.match(r"https?://", u) else "Gebruik: /site https://voorbeeld.nl/huurwoningen")
    elif cmd == "/schoon":
        if len(delen) > 1 and delen[1].lower() == "ja":
            for k in ("__log__", "__stats__", "__dupes__", "__prijzen__", "__waarschuwingen__"): seen.pop(k, None)
            stuur_naar(chat, "🧹 Schone start: overzicht, statistieken, nummering, prijzen en gereageerd-status zijn gewist. "
                             "Wat ik al had gezien blijft onthouden, dus je krijgt geen oude woningen opnieuw.")
        else:
            stuur_naar(chat, "Dit wist je woningoverzicht (ook op de website), de statistieken, de nummering en wat je als gereageerd hebt "
                             "gemarkeerd. Weet je het zeker? Stuur dan: /schoon ja")
    elif cmd == "/prijzen":
        pr = seen.get("__prijzen__", {})
        rij = sorted(((k, v) for k, v in pr.items() if k != "__alle__" and len(v) >= 3), key=lambda x: -len(x[1]))[:15]
        alle = pr.get("__alle__", [])
        stuur_naar(chat, "💶 Gemiddelde prijs per m² (wat ik tot nu toe zag)\n" +
            ("\n".join(f"{k.title()}: €{median(v):.0f} ({len(v)} woningen)" for k, v in rij) or "Nog te weinig data per plaats.") +
            (f"\nRegio totaal: €{median(alle):.0f} ({len(alle)} woningen)" if alle else ""))
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
            it = zet_status(seen, nr, st) if (cchat in CHATS and st) else None
            if it:
                tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Gemarkeerd: {st}")
                tg("editMessageReplyMarkup", chat_id=cchat, message_id=cm.get("message_id"),
                   reply_markup=maak_knoppen(nr, st, it.get("url"), it.get("route")))
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
            if ai_fout(seen, e) == "credits":
                stuur_naar(chat, "💳 Je Anthropic-tegoed is op, dus ik kan dit nu niet beoordelen. Stort tegoed via console.anthropic.com > Billing.")
            else:
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
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Woningzoeker</title>
<style>
:root{--bg:#f3f5f7;--card:#fff;--fg:#14202b;--mu:#5d6b78;--bd:#dde3e8;--ac:#0b7a75;--acf:#fff;--gr:#1f9d55;--ge:#d99a00;--ro:#d64545;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1519;--card:#18212a;--fg:#e8eef3;--mu:#98a6b3;--bd:#2a3642;--ac:#3cc3bb;--acf:#06201e}}
:root[data-theme="dark"]{--bg:#0f1519;--card:#18212a;--fg:#e8eef3;--mu:#98a6b3;--bd:#2a3642;--ac:#3cc3bb;--acf:#06201e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:var(--ac);color:var(--acf);padding:18px 16px}.w{max-width:1000px;margin:0 auto;padding:0 14px}header .w{padding:0}
h1{margin:0;font-size:22px}header p{margin:4px 0 0;opacity:.85}
.st{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}.st div{flex:1 1 120px;background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:10px 12px}
.st b{display:block;font-size:20px}.st span{color:var(--mu);font-size:13px}
.g{display:grid;gap:16px;grid-template-columns:260px 1fr;align-items:start}@media(max-width:800px){.g{grid-template-columns:1fr}}
.f{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px}.f summary{font-weight:700;cursor:pointer}
.f label{display:block;font-size:13px;color:var(--mu);margin:12px 0 4px}
select,input[type=range]{width:100%}select{padding:8px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
.ch{display:flex;flex-wrap:wrap;gap:6px}.ch button,.b{font:inherit;border:1px solid var(--bd);background:var(--bg);color:var(--fg);border-radius:20px;padding:5px 12px;cursor:pointer;text-decoration:none;display:inline-block}
.ch button.on{background:var(--ac);color:var(--acf);border-color:var(--ac)}
.c{display:flex;gap:14px;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px;margin-bottom:12px}
.sc{flex:none;width:52px;height:52px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;font-size:18px}
.m{flex:1;min-width:0}.t{font-weight:700;font-size:17px;color:var(--fg);text-decoration:none}.t:hover{text-decoration:underline}
.p{font-size:20px;font-weight:700;margin:2px 0}.p small{font-weight:400;color:var(--mu);font-size:13px}.i{color:var(--mu);font-size:14px;margin-top:2px}
.fl{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.fl span{font-size:12.5px;padding:3px 9px;border-radius:12px;background:#fff3d6;color:#7a5200}.fl .ok{background:#e0f4e8;color:#14532d}
.tg{float:right;font-size:12px;padding:2px 9px;border-radius:10px;border:1px solid var(--bd);color:var(--mu)}
.a{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}.a .pr{background:var(--ac);color:var(--acf);border-color:var(--ac)}
</style></head><body>
<header><div class="w"><h1>🏠 Woningzoeker</h1><p>Twee studenten · Utrecht en omgeving · alles op één plek</p></div></header>
<div class="w"><div class="st" id="s"></div>
<div class="g"><details class="f" id="fw"><summary>Filters</summary><div id="f"></div></details><main id="l"></main></div>
<p class="i" style="margin:16px 0">Scores, reistijden en toeslagen zijn schattingen. Controleer alles in de advertentie zelf.</p></div>
<script>
var D=__DATA__,S={st:"open",pl:"",br:"",mh:2000,ms:0,so:"score"};
var T={open:"Nieuw en bewaard",bewaard:"Bewaard",gereageerd:"Gereageerd",afgewezen:"Afgewezen",alles:"Alles"};
function $(i){return document.getElementById(i)}
function e(t,c,x){var n=document.createElement(t);if(c)n.className=c;if(x!=null)n.textContent=x;return n}
function eur(n){return "€"+Number(n).toLocaleString("nl-NL")}
function uniq(k){var o={};D.forEach(function(x){if(x[k])o[x[k]]=1});return Object.keys(o).sort()}
function ok(x){var s=x.status||"nieuw";
if(S.st=="open"?(s!="nieuw"&&s!="bewaard"):(S.st!="alles"&&s!=S.st))return false;
if(S.pl&&x.plaats!=S.pl)return false;if(S.br&&x.bron!=S.br)return false;
if(x.huur&&x.huur>S.mh)return false;return(x.score||0)>=S.ms}
function sel(f,lbl,k){f.appendChild(e("label",0,lbl));var s=e("select");s.appendChild(new Option("Alle",""));uniq(k=="pl"?"plaats":"bron").forEach(function(o){s.appendChild(new Option(o,o))});s.value=S[k];s.onchange=function(){S[k]=s.value;list()};f.appendChild(s)}
function rng(f,lbl,k,mn,mx,st,fm){var l=e("label",0,lbl+": "+fm(S[k])),r=e("input");r.type="range";r.min=mn;r.max=mx;r.step=st;r.value=S[k];r.oninput=function(){S[k]=+r.value;l.textContent=lbl+": "+fm(S[k]);list()};f.appendChild(l);f.appendChild(r)}
function filters(){var f=$("f");f.textContent="";f.appendChild(e("label",0,"Status"));var c=e("div","ch");
Object.keys(T).forEach(function(k){var b=e("button",k==S.st?"on":"",T[k]);b.onclick=function(){S.st=k;filters();list()};c.appendChild(b)});f.appendChild(c);
sel(f,"Plaats","pl");sel(f,"Website","br");
rng(f,"Maximale huur","mh",800,2000,50,eur);rng(f,"Minimale score","ms",0,10,1,function(v){return v+"/10"});
f.appendChild(e("label",0,"Sorteren op"));var so=e("select");[["score","Hoogste score"],["ts","Nieuwste"],["huur","Laagste huur"]].forEach(function(o){so.appendChild(new Option(o[1],o[0]))});
so.value=S.so;so.onchange=function(){S.so=so.value;list()};f.appendChild(so)}
function stats(){var o=D.filter(function(x){var s=x.status||"nieuw";return s=="nieuw"||s=="bewaard"}),h=o.filter(function(x){return x.huur}).map(function(x){return x.huur});
var bs=Math.max.apply(null,[0].concat(o.map(function(x){return x.score||0}))),s=$("s");s.textContent="";
[[o.length,"open woningen"],[bs?bs+"/10":"-","beste score"],[h.length?eur(Math.round(h.reduce(function(a,b){return a+b})/h.length)):"-","gemiddelde huur"],[D.filter(function(x){return x.status=="gereageerd"}).length,"gereageerd"]].forEach(function(v){var d=e("div");d.appendChild(e("b",0,v[0]));d.appendChild(e("span",0,v[1]));s.appendChild(d)})}
function lk(t,c,u){var a=e("a",c,t);a.href=u;a.target="_blank";a.rel="noopener";return a}
function card(x){var c=e("div","c"),sc=x.score||0,b=e("div","sc",sc||"?");b.style.background=sc>=8?"var(--gr)":sc>=5?"var(--ge)":"var(--ro)";c.appendChild(b);
var m=e("div","m"),st=x.status||"nieuw",u=x.url&&/^https?:\/\//.test(x.url);if(st!="nieuw")m.appendChild(e("span","tg",T[st]||st));
m.appendChild(u?lk(x.titel||"Woning","t",x.url):e("span","t",x.titel||"Woning"));
var p=e("div","p",x.huur?eur(x.huur):"Prijs onbekend");p.appendChild(e("small",0," per maand"));m.appendChild(p);
m.appendChild(e("div","i",[x.plaats&&"📍 "+x.plaats,x.slk&&"🛏 "+x.slk+" slk",x.opp&&"📐 "+x.opp+" m²",x.ov&&"🚆 ~"+x.ov+" min OV"].filter(Boolean).join("  ·  ")));
if(x.netto)m.appendChild(e("div","i","💶 Na huurtoeslag ca. "+eur(x.netto)+" p.p. (schatting)"));
var fl=e("div","fl");(x.vlaggen||[]).forEach(function(v){fl.appendChild(e("span",0,v))});if(x.wv)fl.appendChild(e("span","ok",x.wv));if(fl.childNodes.length)m.appendChild(fl);
if(x.sam)m.appendChild(e("div","i",x.sam));
m.appendChild(e("div","i",[x.bron,"#"+x.nr,new Date(x.ts*1000).toLocaleDateString("nl-NL")].filter(Boolean).join(" · ")));
var a=e("div","a");if(u)a.appendChild(lk("Bekijk advertentie ↗","b pr",x.url));if(x.route)a.appendChild(lk("🗺 OV-route","b",x.route));m.appendChild(a);c.appendChild(m);return c}
function list(){var r=D.filter(ok),k=S.so;r.sort(function(a,b){return k=="huur"?(a.huur||1e9)-(b.huur||1e9):(b[k]||0)-(a[k]||0)});
var L=$("l");L.textContent="";L.appendChild(e("p","i",r.length+" woningen"));if(!r.length)L.appendChild(e("p","i","Geen woningen met deze filters."));
r.forEach(function(x){L.appendChild(card(x))})}
if(innerWidth>800)$("fw").open=true;stats();filters();list();
</script></body></html>"""

def schrijf_pagina(seen):
    """Maakt docs/index.html (voor GitHub Pages) met alle woningen, nieuwste eerst."""
    rij = [dict(v, nr=int(k)) for k, v in seen.get("__log__", {}).get("items", {}).items()]
    rij.sort(key=lambda x: -x["ts"])
    velden = ("nr", "titel", "url", "plaats", "huur", "slk", "opp", "ov", "bron", "score", "status", "ts", "netto", "vlaggen", "wv", "sam", "route")
    data = json.dumps([{k: x.get(k) for k in velden} for x in rij[:300]], ensure_ascii=False).replace("</", "<\\/")
    nieuw = PAGINA.replace("__DATA__", data)
    try: oud = open("docs/index.html", encoding="utf-8").read()
    except Exception: oud = ""
    if nieuw != oud:
        os.makedirs("docs", exist_ok=True)
        open("docs/index.html", "w", encoding="utf-8").write(nieuw)

def site_url():
    """Adres van je GitHub Pages-site (automatisch uit de repository, of zelf instellen met PAGINA_URL)."""
    u = os.environ.get("PAGINA_URL")
    if u: return u
    r = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in r:
        eigenaar, naam = r.split("/", 1)
        return f"https://{eigenaar.lower()}.github.io/{naam}/"
    return None

def dagbericht(seen):
    """Elke dag vanaf 21:00 (Nederlandse tijd) één stil bericht met een verwijzing naar de website."""
    nu = datetime.now(ZoneInfo("Europe/Amsterdam"))
    dag = nu.strftime("%Y-%m-%d")
    if nu.hour < 21 or seen.get("__dagbericht__") == dag: return
    start = nu.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    items = seen.get("__log__", {}).get("items", {}).values()
    vandaag = [v for v in items if v.get("ts", 0) >= start]
    goed = [v for v in vandaag if (v.get("score") or 0) >= 7]
    open_ = [v for v in items if v.get("status", "nieuw") in ("nieuw", "bewaard")]
    url = site_url()
    stuur("🌙 Dagoverzicht\n"
          f"Vandaag {len(vandaag)} nieuwe woningen ({len(goed)} met score 7 of hoger). Nog open: {len(open_)}.\n"
          + (f"Bekijk alles op de website: {url}" if url else "Bekijk alles op je woningen-website."), stil=True)
    seen["__dagbericht__"] = dag

def opschonen(seen):
    nu = time.time()
    if "__dupes__" in seen:
        seen["__dupes__"] = {k: v for k, v in seen["__dupes__"].items() if nu - (v["ts"] if isinstance(v, dict) else v) < 60 * 86400}
    if "__zacht__" in seen: seen["__zacht__"] = dict(list(seen["__zacht__"].items())[-500:])
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
    stappen = [("Telegram", telegram_inbox), ("mail", scan_mail)]
    if SCAN_SITES:
        laad_extra_bronnen()
        stappen.append(("sites", scan_sites))
    stappen += [("herinneringen", herinneringen), ("weekoverzicht", wekelijks), ("dagbericht", dagbericht),
                ("opschonen", opschonen), ("pagina", schrijf_pagina)]
    for naam, stap in stappen:        # een fout in één onderdeel laat de rest en het opslaan van de status niet mislukken
        try:
            stap(seen)
        except Exception as e:
            log(f"Stap {naam} mislukt: {e!r}")
            log(traceback.format_exc())
            if naam not in ("Telegram", "pagina"):
                waarschuw(seen, "stap-" + naam, f"⚠️ Onderdeel '{naam}' mislukte: {str(e)[:120]}")
    json.dump(seen, open("seen.json", "w"))
