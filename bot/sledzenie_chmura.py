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
"""
import os, re, sys, json, datetime
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


def gls(nr):
    """GLS Polska — publiczne zapytanie strony śledzenia (tryb eksperymentalny)."""
    r = requests.get("https://gls-group.com/app/service/open/rest/PL/pl/rstt001",
                     params={"match": nr}, headers=UA, timeout=25)
    r.raise_for_status()
    dane = r.json()
    try:
        info = dane["tuStatus"][0]["progressBar"]["statusInfo"]
    except Exception:
        info = ""
    mapa = {"DELIVERED": "DORECZONA", "INDELIVERY": "W_DORECZENIU", "INTRANSIT": "ODEBRANA",
            "INWAREHOUSE": "ODEBRANA", "DELIVEREDPS": "DORECZONA", "NOTDELIVERED": "PROBLEM"}
    if info.upper() in mapa:
        return mapa[info.upper()], info
    return klasyfikuj_tekst(json.dumps(dane, ensure_ascii=False)), info or "GLS"


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
            continue
        zdarzenie = aktualizuj(w, nowy, opis)
        zmiany += 1
        print(f"  {o['id']} [{przew}]: {w.get('etap') or 'oczekuje'}")
        if zdarzenie:
            tytul = f"{OPIS[zdarzenie]} — {o.get('tytul') or 'zlecenie'}"
            tresc = (f"{o.get('klient') or ''}\n{przew.upper()} {nr}\nStatus przewoźnika: {opis}").strip()
            link = wpis_txt if wpis_txt.lower().startswith("http") else ""
            powiadom(tytul, tresc, link)
    return zmiany

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
    for u in db.collection("users").list_documents():
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
