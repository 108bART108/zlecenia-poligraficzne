#!/usr/bin/env python3
"""
Bot śledzenia przesyłek dla aplikacji „Zlecenia poligraficzne” (tabela.html).

Działa w chmurze (GitHub Actions, co ~30 min), więc komputer nie musi być włączony.
1. Czyta zlecenia z Firestore: users/{uid}/app/state → orders[].
   Bierze pod uwagę zlecenia niezamknięte, z wpisanym polem „Nr listu przewozowego / link”.
2. Przewoźnika rozpoznaje z pola „Firma kurierska” albo z adresu linku.
3. Sprawdza status u przewoźnika i przy zmianie etapu wysyła powiadomienie na telefon (ntfy):
      📦 ODEBRANA → 🚚 W DORĘCZENIU → ✅ DORĘCZONA   (+ ⚠️ PROBLEM)
4. Stan zapisuje osobno w users/{uid}/app/sledzenie — nie dotyka listy zleceń,
   więc nie koliduje z zapisami z aplikacji. Aplikacja pokazuje ten stan jako plakietkę.

Zmienne środowiskowe (sekrety GitHub):
  FIREBASE_SA   — cała zawartość pliku klucza konta usługi Firebase (JSON)
  NTFY_TOPIC    — nazwa tematu w aplikacji ntfy
  DHL_API_KEY   — opcjonalnie, darmowy klucz z developer.dhl.com
  GLS_API_KEY   — opcjonalnie, darmowy klucz Track&Trace GLS
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM — wysyłka maili do klientów
  FIREBASE_SA_PANEL — klucz konta usługi Panelu wycen (numer listu wpisany przez drukarnię)

Maile do klientów: tylko etap, tytuł zlecenia i nazwa kuriera — bez numeru listu i linku,
żeby klient nie zobaczył na stronie kuriera, skąd (z której drukarni) nadano paczkę.
"""
import os, re, sys, json, ssl, smtplib, datetime
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlparse, parse_qs
import requests

ETAPY = ["ODEBRANA", "W_DORECZENIU", "DORECZONA"]
OPIS = {
    "ODEBRANA": "📦 Odebrana od nadawcy",
    "W_DORECZENIU": "🚚 W doręczeniu — u kierowcy",
    "DORECZONA": "✅ Doręczona — klient odebrał",
    "PROBLEM": "⚠️ Problem z przesyłką",
}
UA = {"User-Agent": "Mozilla/5.0 (IB Projekt Printing - sledzenie przesylek)"}

# ------------------------------------------------------------ rozpoznawanie

def rozpoznaj_przewoznika(kurier, wpis):
    t = f"{kurier} {wpis}".lower()
    for klucz, wzorce in [
        ("inpost", ["inpost", "paczkomat", "easypack"]),
        ("dhl", ["dhl"]),
        ("dpd", ["dpd"]),
        ("gls", ["gls"]),
    ]:
        if any(w in t for w in wzorce):
            return klucz
    return None


def wyciagnij_numer(wpis):
    """Z numeru albo linku do śledzenia wyciąga sam numer przesyłki."""
    wpis = (wpis or "").strip()
    if not wpis:
        return None
    if re.match(r"^https?://", wpis, re.I):
        u = urlparse(wpis)
        q = parse_qs(u.query)
        for k in ["number", "tracking-id", "trackingNumber", "trackingnumber", "q", "match",
                  "parcelNumber", "p1", "piece", "id", "numer", "nr"]:
            if q.get(k):
                return q[k][0].strip()
        kawalki = [k for k in re.split(r"[/#?=&]", u.path + "#" + u.fragment) if re.fullmatch(r"[A-Za-z0-9]{8,}", k)]
        return kawalki[-1] if kawalki else None
    m = re.search(r"[A-Za-z0-9]{8,}", re.sub(r"(?<=\d)[\s-]+(?=\d)", "", wpis))
    return m.group(0) if m else None

# ------------------------------------------------------------ przewoźnicy
# Każdy zwraca (etap | "PROBLEM" | None, opis statusu).

INPOST = {
    "collected_from_sender": "ODEBRANA", "taken_by_courier": "ODEBRANA",
    "adopted_at_source_branch": "ODEBRANA", "sent_from_source_branch": "ODEBRANA",
    "adopted_at_sorting_center": "ODEBRANA", "sent_from_sorting_center": "ODEBRANA",
    "adopted_at_target_branch": "ODEBRANA", "dispatched_by_sender": "ODEBRANA",
    "taken_by_courier_from_pok": "ODEBRANA",
    "out_for_delivery": "W_DORECZENIU", "out_for_delivery_to_address": "W_DORECZENIU",
    "ready_to_pickup": "W_DORECZENIU",
    "delivered": "DORECZONA",
    "undelivered": "PROBLEM", "returned_to_sender": "PROBLEM", "avizo": "PROBLEM",
    "rejected_by_receiver": "PROBLEM", "undelivered_wrong_address": "PROBLEM",
    "undelivered_incomplete_address": "PROBLEM", "undelivered_unknown_receiver": "PROBLEM",
    "missing": "PROBLEM",
}


def inpost(nr):
    r = requests.get(f"https://api-shipx-pl.easypack24.net/v1/tracking/{nr}", headers=UA, timeout=20)
    if r.status_code == 404:
        return None, "numer jeszcze nieznany w InPost"
    r.raise_for_status()
    st = r.json().get("status", "")
    return INPOST.get(st), st


def dhl(nr):
    klucz = os.environ.get("DHL_API_KEY")
    if not klucz:
        return None, "DHL: brak klucza API (sekret DHL_API_KEY)"
    r = requests.get("https://api-eu.dhl.com/track/shipments",
                     params={"trackingNumber": nr, "language": "pl"},
                     headers={"DHL-API-Key": klucz}, timeout=20)
    if r.status_code == 404:
        return None, "numer jeszcze nieznany w DHL"
    r.raise_for_status()
    s = (r.json().get("shipments") or [{}])[0].get("status", {})
    kod = (s.get("statusCode") or "").lower()
    opis = s.get("description") or s.get("status") or kod
    if kod == "delivered":
        return "DORECZONA", opis
    if kod == "failure":
        return "PROBLEM", opis
    e = klasyfikuj_tekst(opis)
    if e:
        return e, opis
    return ("ODEBRANA" if kod == "transit" else None), opis


# Dla przewoźników bez otwartego API: rozpoznanie po słowach w opisie statusu.
SLOWA = [  # od najbardziej zaawansowanego etapu
    ("PROBLEM", ["zwrot do nadawcy", "nieudane doręczenie", "nie doręczono", "odmowa przyjęcia",
                 "błędny adres", "returned to sender", "delivery failed"]),
    ("DORECZONA", ["przesyłka doręczona", "doręczono", "doręczona", "delivered", "odebrana przez odbiorcę",
                   "odebrana przez adresata"]),
    ("W_DORECZENIU", ["wydanie przesyłki do doręczenia", "wydana do doręczenia", "w doręczeniu",
                      "przekazana kurierowi do doręczenia", "out for delivery", "outfordelivery"]),
    ("ODEBRANA", ["odebrana przez kuriera", "odebrana od nadawcy", "przyjęcie przesyłki", "przyjęta w oddziale",
                  "w drodze", "w transporcie", "sortowni", "in transit", "intransit", "inwarehouse",
                  "picked up", "przesyłka nadana"]),
]


def klasyfikuj_tekst(t):
    t = (t or "").lower()
    for etap, slowa in SLOWA:
        if any(s in t for s in slowa):
            return etap
    return None


def dpd(nr):
    """DPD Polska — publiczna strona śledzenia (bez API; tryb eksperymentalny)."""
    r = requests.post("https://tracktrace.dpd.com.pl/findPackage",
                      data={"q": nr, "typ": "1"}, headers=UA, timeout=25)
    r.raise_for_status()
    tekst = re.sub(r"<[^>]+>", " ", r.text)
    tekst = re.sub(r"\s+", " ", tekst)
    etap = klasyfikuj_tekst(tekst)
    return etap, "DPD: status ze strony śledzenia" if etap else "DPD: nie odczytano statusu"


GLS_MAPA = {"DELIVERED": "DORECZONA", "DELIVEREDPS": "DORECZONA", "INDELIVERY": "W_DORECZENIU",
            "INTRANSIT": "ODEBRANA", "INWAREHOUSE": "ODEBRANA", "NOTDELIVERED": "PROBLEM"}


def gls(nr):
    """GLS — Track&Trace. Od 2025/26 GLS wymaga klucza API (sekret GLS_API_KEY)."""
    klucz = os.environ.get("GLS_API_KEY")
    if klucz:
        r = requests.get("https://api-tt.de.gls-group.com/app/service/open/rest/PL/pl/rstt001",
                         params={"match": nr}, headers={**UA, "x-api-key": klucz, "Accept": "application/json"}, timeout=25)
    else:
        r = requests.get("https://gls-group.com/app/service/open/rest/PL/pl/rstt001",
                         params={"match": nr}, headers={**UA, "Accept": "application/json"}, timeout=25)
    if r.status_code in (401, 403):
        return None, "GLS: klucz API nieprawidłowy lub nieaktywny"
    if r.status_code == 404:
        return None, "numer jeszcze nieznany w GLS"
    try:
        dane = r.json()
    except ValueError:
        return None, "GLS: potrzebny klucz API (sekret GLS_API_KEY)" if not klucz else "GLS: nieczytelna odpowiedź"
    tu = (dane.get("tuStatus") or [{}])[0]
    info = ((tu.get("progressBar") or {}).get("statusInfo") or "").upper()
    hist = tu.get("history") or []
    opis = (hist[0].get("evtDscr") if hist and isinstance(hist[0], dict) else "") or info or "GLS"
    if info in GLS_MAPA:
        etap = GLS_MAPA[info]
        if etap != "DORECZONA" and klasyfikuj_tekst(opis) == "W_DORECZENIU":
            etap = "W_DORECZENIU"
        return etap, opis
    return klasyfikuj_tekst(opis), opis


PRZEWOZNICY = {"inpost": inpost, "dhl": dhl, "dpd": dpd, "gls": gls}

# ------------------------------------------------------------ logika etapów

def teraz():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=1))).strftime("%Y-%m-%d %H:%M")


def aktualizuj(wpis, nowy, opis):
    """Zmienia stan przesyłki; zwraca tekst powiadomienia albo None."""
    wpis["status"] = opis
    wpis["sprawdzono"] = teraz()
    if nowy is None:
        return None
    if nowy == "PROBLEM":
        if wpis.get("problem") == opis:
            return None
        wpis["problem"] = opis
        return "PROBLEM"
    wpis.pop("problem", None)
    stary = wpis.get("etap")
    if stary in ETAPY and ETAPY.index(nowy) <= ETAPY.index(stary):
        return None
    wpis["etap"] = nowy
    wpis.setdefault("historia", []).append({"etap": nowy, "kiedy": teraz(), "status": opis})
    return nowy


def powiadom(tytul, tresc, link=""):
    temat = os.environ.get("NTFY_TOPIC")
    print("  → wysłano powiadomienie")  # repo jest publiczne: w logach bez nazw zleceń i klientów
    if not temat:
        return
    h = {"Title": tytul.encode("utf-8"), "Tags": "package"}
    if link:
        h["Click"] = link
    try:
        requests.post(f"https://ntfy.sh/{temat}", data=tresc.encode("utf-8"), headers=h, timeout=15)
    except Exception as e:
        print("  ! ntfy:", e)


def przetworz(orders, sledzenie, sprawdz=None):
    """Główna pętla na danych (bez Firestore — łatwa do testów). Zwraca liczbę zmian."""
    sprawdz = sprawdz or (lambda przew, nr: PRZEWOZNICY[przew](nr))
    zmiany = 0
    for o in orders:
        wpis_txt = (o.get("listPrzewozowy") or "").strip()
        if o.get("zamkniete") or not wpis_txt:
            continue
        nr = wyciagnij_numer(wpis_txt)
        przew = rozpoznaj_przewoznika(o.get("kurier", ""), wpis_txt)
        w = sledzenie.get(o["id"]) or {}
        if w.get("numer") != nr:          # nowy lub zmieniony numer → zaczynamy od zera
            w = {"numer": nr, "przewoznik": przew}
        w["przewoznik"] = przew
        sledzenie[o["id"]] = w
        if w.get("etap") == "DORECZONA":
            continue
        if not nr or not przew:
            w["status"] = "nie rozpoznano przewoźnika lub numeru" if nr else "nie rozpoznano numeru"
            w["sprawdzono"] = teraz()
            zmiany += 1
            continue
        try:
            nowy, opis = sprawdz(przew, nr)
        except Exception as e:
            print(f"  ! {o['id']} [{przew}]: błąd zapytania ({type(e).__name__})")
            w["status"] = f"błąd odczytu u przewoźnika ({type(e).__name__})"
            w["blad"] = True
            w["sprawdzono"] = teraz()
            zmiany += 1
            continue
        w["blad"] = nowy is None and str(opis).startswith(("GLS:", "DHL:", "DPD: nie"))
        zdarzenie = aktualizuj(w, nowy, opis)
        zmiany += 1
        print(f"  {o['id']} [{przew}]: {w.get('etap') or 'oczekuje'}")
        if zdarzenie:
            tytul = f"{OPIS[zdarzenie]} — {o.get('tytul') or 'zlecenie'}"
            tresc = (f"{o.get('klient') or ''}\n{przew.upper()} {nr}\nStatus przewoźnika: {opis}").strip()
            link = wpis_txt if wpis_txt.lower().startswith("http") else ""
            powiadom(tytul, tresc, link)
            if zdarzenie in ETAPY and o.get("mailKlientWysylka") and (o.get("emailKlienta") or "").strip():
                if zdarzenie not in w.get("maile", []):
                    if mail_do_klienta(o, przew, zdarzenie, opis):
                        w.setdefault("maile", []).append(zdarzenie)
    return zmiany

# ------------------------------------------------------------ maile do klientów

NAZWY_KURIEROW = {"inpost": "InPost", "dhl": "DHL", "dpd": "DPD", "gls": "GLS"}
PODPIS = ("Pozdrawiam serdecznie\nBartosz Zając\nIB Projekt Printing\n"
          "tel. 792 331 442 · bz@ibprojekt.eu")


def tresc_maila(o, przew, etap, status):
    t = (o.get("tytul") or "zamówienie").strip()
    k = NAZWY_KURIEROW.get(przew, przew.upper())
    if etap == "ODEBRANA":
        temat = f"„{t}” — przesyłka jest w drodze"
        txt = f"Dzień dobry,\n\nuprzejmie informuję, że Państwa zamówienie „{t}” zostało przekazane firmie kurierskiej {k} i jest w drodze."
    elif etap == "W_DORECZENIU" and status == "ready_to_pickup":
        temat = f"„{t}” — przesyłka czeka na odbiór"
        txt = f"Dzień dobry,\n\nzamówienie „{t}” czeka na odbiór w wybranym punkcie {k}. Kod odbioru przyjdzie od {k} SMS-em lub e-mailem."
    elif etap == "W_DORECZENIU":
        temat = f"„{t}” — kurier jest w drodze do Państwa"
        txt = f"Dzień dobry,\n\nprzesyłka z zamówieniem „{t}” została wydana kurierowi {k} do doręczenia. Dostawa spodziewana jest dziś."
    else:
        temat = f"„{t}” — przesyłka doręczona"
        txt = (f"Dzień dobry,\n\nwedług informacji {k} przesyłka z zamówieniem „{t}” została doręczona.\n"
               "Jeśli coś się nie zgadza albo paczka nie dotarła w komplecie, proszę o wiadomość — zajmę się tym od razu.")
    return temat, txt + "\n\n" + PODPIS


def mail_do_klienta(o, przew, etap, status):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("  · mail do klienta pominięty: brak SMTP_HOST")
        return False
    port = int(os.environ.get("SMTP_PORT") or 465)
    user, haslo = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", "")
    nadawca = os.environ.get("MAIL_FROM") or user
    temat, tresc = tresc_maila(o, przew, etap, status)
    m = EmailMessage()
    m["Subject"] = temat
    m["From"] = formataddr(("IB Projekt Printing", nadawca))
    m["To"] = ", ".join(a.strip() for a in re.split(r"[;,]", o["emailKlienta"]) if a.strip())
    m["Reply-To"] = nadawca
    m.set_content(tresc)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(user, haslo); s.send_message(m)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context()); s.login(user, haslo); s.send_message(m)
        print("  ✉ wysłano mail do klienta")
        return True
    except Exception as e:
        print(f"  ! mail do klienta: {type(e).__name__}")
        return False

# ------------------------------------------------------------ Panel wycen (drukarnie)

def synchronizuj_panel(db, panel, uid_ref):
    """1) Wysyła do panelu listę otwartych zleceń (do wyboru przy przekazaniu zlecenia drukarni).
       2) Numer listu wpisany przez drukarnię przepisuje do odpowiedniego zlecenia w tabeli."""
    from google.cloud import firestore as gfs
    state_ref = uid_ref.collection("app").document("state")
    orders = (state_ref.get().to_dict() or {}).get("orders", [])
    lista = [{"id": o["id"], "tytul": o.get("tytul", ""), "klient": o.get("klient", "")}
             for o in orders if not o.get("zamkniete")]
    panel.collection("prywatne").document("_tabela").set({"lista": lista, "aktualizacja": teraz()})
    for z in panel.collection("zapytania").stream():
        zd = z.to_dict() or {}
        wyk = zd.get("wykonawca")
        if not wyk:
            continue
        ws = panel.collection("zapytania").document(z.id).collection("wysylka").document(wyk).get()
        if not ws.exists:
            continue
        wd = ws.to_dict() or {}
        kurier, numer = (wd.get("kurier") or "").strip(), (wd.get("numer") or "").strip()
        pryw_ref = panel.collection("prywatne").document(z.id)
        pryw = pryw_ref.get().to_dict() or {}
        zid = pryw.get("zlecenieTabela")
        if not (zid and numer) or pryw.get("wysylkaPrzepisana") == f"{kurier}|{numer}|{zid}":
            continue

        @gfs.transactional
        def wpisz(tr):
            snap = state_ref.get(transaction=tr)
            lst = (snap.to_dict() or {}).get("orders", [])
            for o in lst:
                if o.get("id") == zid:
                    o["kurier"], o["listPrzewozowy"] = kurier, numer
                    tr.set(state_ref, {"orders": lst}, merge=True)
                    return o
            return None

        o = wpisz(db.transaction())
        if o is None:
            print(f"  ! panel {z.id}: nie znaleziono zlecenia {zid} w tabeli")
            continue
        pryw_ref.set({"wysylkaPrzepisana": f"{kurier}|{numer}|{zid}"}, merge=True)
        print(f"  panel {z.id}: przepisano numer listu do zlecenia {zid}")
        powiadom(f"🏭 Drukarnia podała numer listu — {o.get('tytul') or 'zlecenie'}",
                 f"{kurier} {numer}\nWpisane do tabeli zleceń — śledzenie rusza automatycznie.")

# ------------------------------------------------------------ Firestore

def main():
    import firebase_admin
    from firebase_admin import credentials, firestore
    sa = os.environ.get("FIREBASE_SA")
    if not sa:
        sys.exit("Brak sekretu FIREBASE_SA")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    db = firestore.client()
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        powiadom("Test — IB Projekt Printing", "Bot śledzenia przesyłek działa w chmurze.")
        return
    panel = None
    if os.environ.get("FIREBASE_SA_PANEL"):
        papp = firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_SA_PANEL"])), name="panel")
        panel = firestore.client(app=papp)
    for u in db.collection("users").list_documents():
        if panel is not None and u.collection("app").document("state").get().exists:
            try:
                synchronizuj_panel(db, panel, u)
            except Exception as e:
                print(f"  ! panel: {type(e).__name__}: {str(e)[:120]}")
        state = u.collection("app").document("state").get()
        if not state.exists:
            continue
        orders = (state.to_dict() or {}).get("orders", [])
        ref = u.collection("app").document("sledzenie")
        snap = ref.get()
        sledzenie = (snap.to_dict() or {}).get("przesylki", {}) if snap.exists else {}
        print(f"[{teraz()}] sprawdzam {len(orders)} zleceń")
        if przetworz(orders, sledzenie):
            ref.set({"przesylki": sledzenie, "aktualizacja": teraz()})


if __name__ == "__main__":
    main()
