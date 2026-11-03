"""Deterministic seed data (SPEC section 6): suppliers, POs, approvers and 200 invoices.

Every inbox invoice is built from a named scenario ("trap"), and the scenario fixes the intended
outcome at construction time: the generator never evaluates the policy. Each scenario gets its own
PO (or a PO with ample headroom) so traps can't interact by accident; the few deliberate
interactions (pairs on one PO, duplicates, resubmissions) are explicit scenarios.

The intended outcome is defined for processing the whole inbox in policy order (received_date,
then ID) while acting as the Teamleiter. It is stored in `invoices.hidden_outcome` and is only
read by tests.

Usage: build_db("/tmp/x.sqlite")  or  python -m ap.seed /tmp/x.sqlite
"""

import hashlib
import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import ledger, money, schema

SEED = 42
TOTAL_INVOICES = 200
TOTAL_POS = 150
FIRST_INVOICE_NO = 301

HISTORY_START, HISTORY_END = date(2026, 8, 3), date(2026, 9, 30)
INBOX_START, INBOX_END = date(2026, 10, 1), date(2026, 10, 23)

# (id, name, role, gross approval limit in cents). IDs are deliberately not in limit order.
APPROVERS = [
    ("AP-203", "Jonas Weber", "Teamleiter", 500_000),
    ("AP-117", "Sabine Krüger", "Abteilungsleiter", 2_500_000),
    ("AP-052", "Dr. Thomas Lindner", "Bereichsleiter", 10_000_000),
    ("AP-008", "Claudia Hofmann", "CFO", None),
]
ACTING_APPROVER_ID = "AP-203"  # intended outcomes assume the agent acts as the Teamleiter
APPROVER_BY_ROLE = {role: aid for aid, _, role, _ in APPROVERS}
TL_LIMIT, AL_LIMIT, BL_LIMIT = 500_000, 2_500_000, 10_000_000

COST_CENTER_NAMES = {
    "2100": "Einkauf", "3100": "Fertigung Zerspanung", "3200": "Montage", "3300": "Instandhaltung",
    "3500": "Qualitätssicherung", "4100": "Logistik/Lager", "5100": "IT", "6100": "Kantine",
    "7100": "Entwicklung/Konstruktion", "8100": "Verwaltung",
}

# (real ZIP codes of the city, city, Bundesland). Larger cities have several delivery areas.
DE_CITIES = [
    (("80331", "80335", "80337", "80339", "80469", "80636", "80807", "81241", "81369", "81671", "81829"),
     "München", "Bayern"),
    (("90402", "90403", "90408", "90411", "90429", "90441", "90451", "90471"), "Nürnberg", "Bayern"),
    (("86150", "86152", "86153", "86154", "86156", "86167", "86199"), "Augsburg", "Bayern"),
    (("93047",), "Regensburg", "Bayern"), (("85049",), "Ingolstadt", "Bayern"), (("97070",), "Würzburg", "Bayern"),
    (("83022",), "Rosenheim", "Bayern"), (("87435",), "Kempten (Allgäu)", "Bayern"), (("94032",), "Passau", "Bayern"),
    (("70173", "70174", "70176", "70178", "70190", "70191", "70372", "70469", "70565"), "Stuttgart",
     "Baden-Württemberg"),
    (("89073",), "Ulm", "Baden-Württemberg"),
    (("76131", "76133", "76135", "76137", "76185", "76187", "76227"), "Karlsruhe", "Baden-Württemberg"),
    (("68159", "68161", "68163", "68165", "68169", "68219", "68309"), "Mannheim", "Baden-Württemberg"),
    (("73728",), "Esslingen am Neckar", "Baden-Württemberg"), (("72764",), "Reutlingen", "Baden-Württemberg"),
    (("74072",), "Heilbronn", "Baden-Württemberg"),
    (("60311", "60313", "60314", "60326", "60386", "60388", "60528", "65929"), "Frankfurt am Main", "Hessen"),
    (("34117",), "Kassel", "Hessen"),
    (("50667", "50668", "50672", "50733", "50825", "50996", "51063", "51105"), "Köln", "Nordrhein-Westfalen"),
    (("44135", "44137", "44139", "44147", "44263", "44309", "44379"), "Dortmund", "Nordrhein-Westfalen"),
    (("40210", "40211", "40213", "40219", "40476", "40549", "40589"), "Düsseldorf", "Nordrhein-Westfalen"),
    (("42103",), "Wuppertal", "Nordrhein-Westfalen"), (("33602",), "Bielefeld", "Nordrhein-Westfalen"),
    (("58095",), "Hagen", "Nordrhein-Westfalen"),
    (("20095", "20097", "21079", "22041", "22083", "22297", "22525", "22761"), "Hamburg", "Hamburg"),
    (("30159", "30161", "30163", "30169", "30419", "30453", "30559"), "Hannover", "Niedersachsen"),
    (("38100",), "Braunschweig", "Niedersachsen"), (("49074",), "Osnabrück", "Niedersachsen"),
    (("01067", "01069", "01097", "01099", "01127", "01159", "01219"), "Dresden", "Sachsen"),
    (("09111",), "Chemnitz", "Sachsen"),
    (("04103", "04109", "04155", "04177", "04229", "04317", "04347"), "Leipzig", "Sachsen"),
    (("99084",), "Erfurt", "Thüringen"), (("07743",), "Jena", "Thüringen"),
    (("55116",), "Mainz", "Rheinland-Pfalz"), (("67059",), "Ludwigshafen am Rhein", "Rheinland-Pfalz"),
    (("66111",), "Saarbrücken", "Saarland"),
    (("28195", "28197", "28199", "28207", "28237", "28309"), "Bremen", "Bremen"),
    (("10115", "10179", "10551", "10785", "12099", "12487", "13355", "13597"), "Berlin", "Berlin"),
    (("39104",), "Magdeburg", "Sachsen-Anhalt"), (("24103",), "Kiel", "Schleswig-Holstein"),
    (("18055",), "Rostock", "Mecklenburg-Vorpommern"), (("14467",), "Potsdam", "Brandenburg"),
]
AT_CITIES = [("4020", "Linz", "Oberösterreich"), ("5020", "Salzburg", "Salzburg"), ("6020", "Innsbruck", "Tirol")]
NL_CITIES = [("5611 AZ", "Eindhoven", "Noord-Brabant"), ("3011 AD", "Rotterdam", "Zuid-Holland")]
# First digit of a German Bankleitzahl = clearing region of the bank (Bundesbank Clearinggebiet).
BLZ_REGION = {
    "Berlin": 1, "Brandenburg": 1, "Mecklenburg-Vorpommern": 1, "Hamburg": 2, "Schleswig-Holstein": 2,
    "Niedersachsen": 2, "Bremen": 2, "Nordrhein-Westfalen": 3, "Hessen": 5, "Rheinland-Pfalz": 5, "Saarland": 5,
    "Baden-Württemberg": 6, "Bayern": 7, "Sachsen": 8, "Thüringen": 8, "Sachsen-Anhalt": 8,
}
WESTFALEN_CITIES = {"Dortmund", "Bielefeld", "Hagen"}  # NRW, but clearing region 4 (Westfalen)

STREETS = [
    "Industriestraße", "Gewerbering", "Hauptstraße", "Daimlerstraße", "Robert-Bosch-Straße", "Siemensstraße",
    "Carl-Zeiss-Ring", "Max-Planck-Straße", "Gutenbergstraße", "Lilienthalstraße", "Otto-Hahn-Straße",
    "Benzstraße", "Rudolf-Diesel-Straße", "Im Gewerbepark", "Werkstraße", "Bahnhofstraße", "Mühlweg",
    "Zeppelinstraße", "Am Güterbahnhof", "Ferdinand-Porsche-Straße",
]
NL_STREETS = ["Industrieweg", "Hoofdstraat", "Kanaalweg", "Fabriekstraat", "Havenweg", "Ambachtsweg"]
# Each surname is used at most once across all supplier names (no accidental look-alikes).
# Approver surnames (Weber, Krüger, Lindner, Hofmann) are deliberately absent.
SURNAMES = [
    "Huber", "Wagner", "Schäfer", "Hoffmann", "Krämer", "Brandt", "Lehmann", "Kessler", "Seidel", "Winkler",
    "Lorenz", "Fuchs", "Haas", "Roth", "Berger", "Pfeiffer", "Kaiser", "Sommer", "Ziegler", "Engel", "Jung",
    "Böhm", "Frank", "Graf", "Albrecht", "Hartmann", "Kraus", "Möller", "Schuster", "Brunner", "Eder",
    "Riedl", "Stadler", "Dietz", "Kuhn", "Busch", "Vogt", "Heinrich", "Ludwig", "Sauer", "Baumann", "Walter",
    "Keller", "Thiel", "Arnold", "Lindemann", "Weiß", "Martens", "Petersen", "Schmid", "Beck", "Lang",
    "Werner", "Köhler", "Maier", "Simon", "Franke", "Schulte", "Horn", "Günther", "Peters", "Kühn", "Pohl",
    "Schreiber", "Brinkmann", "Hesse", "Ernst", "Voigt", "Seifert", "Jäger", "Stein", "Otto", "Haase",
    "Friedrich", "Wolff", "Bergmann", "Kurz", "Ulrich", "Gerlach", "Reuter", "Hahn", "Wendt", "Rieger",
    "Brauer", "Wiegand", "Kirchner", "Schubert", "Marx", "Fiedler", "Zimmer", "Lenz", "Ebert", "Kolb",
    "Haug", "Ott", "Grimm", "Fischbach", "Rösler", "Nowak", "Brenner", "Blum", "Gärtner", "Pietsch",
    "Reichert", "Wirth", "Brück", "Strobel", "Hauser", "Kopp", "Lutz", "Kraft", "Mertens", "Funk", "Probst",
    "Heller", "Engelhardt", "Wörner", "Endres", "Schwab", "Rausch", "Hein", "Burkhardt", "Sturm", "Rapp",
    "Diehl", "Stark", "Wenzel", "Bader",
]
AT_SURNAMES = ["Gruber", "Pichler", "Moser", "Leitner", "Wallner"]
NL_SURNAMES = ["Van der Berg", "De Vries", "Bakker", "Visser"]
TRADES = {
    "goods": [
        "Präzisionstechnik", "Industriebedarf", "Metallhandel", "Elektrotechnik", "Hydraulik", "Werkzeugbau",
        "Normteile", "Kunststofftechnik", "Verpackungen", "Arbeitsschutz", "Antriebstechnik", "Schweißtechnik",
        "Bürobedarf", "Lagertechnik", "Stahlhandel", "Drucklufttechnik", "Wälzlager", "Dichtungstechnik",
    ],
    "services": [
        "Logistik", "Spedition", "Gebäudereinigung", "IT-Service", "Ingenieurbüro", "Industrieservice",
        "Instandhaltung", "Sicherheitsdienst", "Personalservice", "Kalibrierdienst",
    ],
}
NAME_PATTERNS = {
    "goods": ["{a} {t} GmbH", "{a} {t} GmbH & Co. KG", "{a} & {b} {t} KG", "{t} {a} GmbH"],
    "services": ["{a} {t} GmbH", "{t} {a} e.K.", "{a} & {b} {t} GmbH"],
    "books": ["Fachbuchhandlung {a} e.K.", "{a} Technische Fachliteratur GmbH", "Buchhandlung {a} & {b} OHG"],
    "food": ["{a} Frischdienst GmbH", "Großbäckerei {a} GmbH", "{a} Lebensmittelgroßhandel KG"],
}
# (PO description, cost center) by supplier trade: each PO fits what its supplier sells and who uses it.
DESCRIPTIONS = {
    # goods
    "Präzisionstechnik": [("Präzisionsdrehteile nach Zeichnung 4711-03", "3200"),
                          ("Passstifte gehärtet ISO 8734", "3200"), ("Führungsbuchsen DIN 179", "3100")],
    "Industriebedarf": [("Putztücher und Reinigungsmittel Werkstatt", "3100"), ("Kabelbinder-Sortiment", "3300"),
                        ("Werkstattbedarf Sammelbestellung Oktober", "3100")],
    "Metallhandel": [("Aluminium-Flachstangen EN AW-6082", "3100"), ("Kupferblech 2 mm, 10 Tafeln", "3100"),
                     ("Messing-Rundstangen CuZn39Pb3", "3100")],
    "Elektrotechnik": [("Schaltschrankkomponenten Linie 2", "3200"), ("NYM-J Kabel 5x2,5, 500 m", "3300"),
                       ("Leitungsschutzschalter B16, 50 Stk.", "3300")],
    "Hydraulik": [("Hydraulikschläuche DN12, 50 m", "3300"), ("Hydraulikzylinder Presse P4", "3300"),
                  ("Hydrauliköl HLP 46, 4 Fässer", "3300")],
    "Werkzeugbau": [("Spritzgießwerkzeug Gehäusedeckel", "3100"), ("Stanzwerkzeug Halter H-12", "3100"),
                    ("Änderung Biegewerkzeug BW-7", "3100")],
    "Normteile": [("Sechskantschrauben M12x40 8.8 verzinkt", "3200"), ("Scheiben DIN 125 A13, 5000 Stk.", "3200"),
                  ("Zylinderschrauben ISO 4762 M8x25", "3200")],
    "Kunststofftechnik": [("POM-Platten 20 mm", "3100"), ("Kunststoff-Gleitlager Ø20, 500 Stk.", "3200"),
                          ("PA6-Rundstäbe Ø50", "3100")],
    "Verpackungen": [("Stretchfolie 23 µm, 48 Rollen", "4100"), ("Faltkartons 600x400x400, 1000 Stk.", "4100"),
                     ("Kantenschutzwinkel, 2000 Stk.", "4100")],
    "Arbeitsschutz": [("Schutzhandschuhe EN 388, 300 Paar", "3100"), ("Sicherheitsschuhe S3, 40 Paar", "3200"),
                      ("Gehörschutzstöpsel, 2000 Paar", "3100")],
    "Antriebstechnik": [("Frequenzumrichter 7,5 kW", "3300"), ("Getriebemotor 1,1 kW", "3200"),
                        ("Zahnriemen HTD 8M, 20 Stk.", "3300")],
    "Schweißtechnik": [("Schweißdraht SG2 1,0 mm", "3200"), ("Schutzgas Argon/CO2, Flaschenmiete", "3200"),
                       ("WIG-Elektroden, 100 Stk.", "3200")],
    "Bürobedarf": [("Kopierpapier A4 80 g", "8100"), ("Druckerpatronen und Toner", "8100"),
                   ("Ordner und Büromaterial Q4", "2100")],
    "Lagertechnik": [("Fachbodenregale Lager Halle 3", "4100"), ("Gitterboxen, 30 Stk.", "4100"),
                     ("Kleinteilemagazin-Regal", "4100")],
    "Stahlhandel": [("Edelstahl-Rundmaterial 1.4301 Ø40", "3100"), ("Flachstahl S235JR 60x10", "3100"),
                    ("Stahlrohre 42,4x2,6", "3100")],
    "Drucklufttechnik": [("Druckluftschläuche und Kupplungen", "3300"), ("Kältetrockner Kompressorstation", "3300"),
                         ("Wartungseinheiten Druckluft", "3300")],
    "Wälzlager": [("Rillenkugellager 6205-2RS, 200 Stk.", "3200"), ("Pendelrollenlager 22210", "3300"),
                  ("Linearführungen Baugröße 25", "3200")],
    "Dichtungstechnik": [("O-Ring-Sortiment NBR 70", "3300"), ("Radialwellendichtringe 40x62x7", "3300"),
                         ("Flachdichtungen nach Zeichnung", "3200")],
    # services
    "Logistik": [("Lagerdienstleistung Außenlager", "4100"), ("Kontraktlogistik Pauschale Oktober", "4100"),
                 ("Inventurunterstützung Lager", "4100")],
    "Spedition": [("Sammelgutfrachten Oktober", "4100"), ("Komplettladung München–Hamburg", "4100"),
                  ("Teilladungen Export KW 40", "4100")],
    "Gebäudereinigung": [("Unterhaltsreinigung Bürogebäude", "8100"), ("Glasreinigung Verwaltungsgebäude", "8100"),
                         ("Grundreinigung Kantine", "6100")],
    "IT-Service": [("IT-Support Pauschale", "5100"), ("Netzwerkwartung Werk", "5100"),
                   ("Einrichtung Arbeitsplätze Konstruktion", "5100")],
    "Ingenieurbüro": [("Konstruktion Vorrichtung V-221", "7100"), ("FEM-Berechnung Maschinenrahmen", "7100"),
                      ("Statik Hallenerweiterung", "8100")],
    "Industrieservice": [("Maschinenumzug Halle 2", "3300"), ("Industriereinigung Fertigung", "3100"),
                         ("Montageunterstützung Linie 3", "3200")],
    "Instandhaltung": [("Wartung CNC-Bearbeitungszentrum", "3300"), ("Reparatur Hallenkran", "3300"),
                       ("Instandsetzung Hydraulikaggregat", "3300")],
    "Sicherheitsdienst": [("Werkschutz Oktober", "8100"), ("Pfortendienst", "8100"), ("Revierfahrten Wochenende", "8100")],
    "Personalservice": [("Arbeitnehmerüberlassung Montage", "3200"), ("Personalgestellung Lager", "4100"),
                        ("Aushilfen Inventur", "4100")],
    "Kalibrierdienst": [("Kalibrierung Messmittel Q3", "3500"), ("DAkkS-Kalibrierung Messschieber", "3500"),
                        ("Prüfung Drehmomentschlüssel", "3500")],
    # books and food
    "Buchhandel": [("Tabellenbuch Metall, 25 Ex.", "7100"), ("DIN-Taschenbücher Schweißtechnik", "3200"),
                   ("Fachliteratur Werkstoffkunde", "7100"), ("Normensammlung Maschinensicherheit", "7100"),
                   ("Fachbücher Maschinenelemente, 10 Ex.", "7100")],
    "Frischdienst": [("Kantinenwaren Frischdienst", "6100"), ("Molkereiprodukte Kantine", "6100"),
                     ("Fleisch- und Wurstwaren Kantine", "6100")],
    "Großbäckerei": [("Backwaren Kantine", "6100"), ("Brötchenlieferung Oktober", "6100"),
                     ("Kuchen und Gebäck Betriebsfeier", "6100")],
    "Lebensmittelgroßhandel": [("Obst und Gemüse Kantine", "6100"), ("Trockenwaren Kantine", "6100"),
                               ("Tiefkühlware Kantine", "6100")],
}
# Trades that plausibly invoice more than EUR 25,000 at once (capital goods, bulk material, big service contracts).
BIG_TICKET_TRADES = {
    "Werkzeugbau", "Antriebstechnik", "Lagertechnik", "Elektrotechnik", "Hydraulik", "Drucklufttechnik",
    "Stahlhandel", "Metallhandel", "Ingenieurbüro", "Industrieservice", "Personalservice", "Spedition", "Logistik",
    "Instandhaltung",
}
# Trades that typically send small invoices without a PO (Kleinbetragsrechnungen).
SMALL_TICKET_TRADES = {"Bürobedarf", "Arbeitsschutz", "Buchhandel", "Frischdienst", "Großbäckerei",
                       "Lebensmittelgroßhandel"}
# § 48b EStG (Bauabzugsteuer) only concerns construction-type services.
CONSTRUCTION_TRADES = {"Industrieservice", "Instandhaltung"}
# Trade of a books/food supplier, from its name pattern; and of a Dutch supplier, from its name.
PATTERN_TRADES = {
    "Frischdienst": "Frischdienst", "Großbäckerei": "Großbäckerei", "Lebensmittelgroßhandel": "Lebensmittelgroßhandel",
}
NL_TRADES = {"Aandrijftechniek": "Antriebstechnik", "Technische Handel": "Industriebedarf",
             "Industrial Supplies": "Industriebedarf"}
BLOCKED_REASONS = [
    "Insolvenzverfahren eröffnet (Amtsgericht {city}), Zahlungen nur nach Freigabe Rechtsabteilung",
    "Bankverbindung ungeklärt – Verdacht auf Rechnungsbetrug, Prüfung läuft",
    "Freistellungsbescheinigung § 48b EStG abgelaufen",
]


# ------------------------------------------------------------------ identifiers


def iban(country: str, bban: str) -> str:
    """Build an IBAN with correct ISO 13616 mod-97 check digits."""
    digits = "".join(str(int(ch, 36)) for ch in bban + country + "00")
    return f"{country}{98 - int(digits) % 97:02d}{bban}"


def de_vat_id(first8: str) -> str:
    """German USt-IdNr: DE + 8 digits + ISO 7064 MOD 11,10 check digit."""
    product = 10
    for ch in first8:
        s = (int(ch) + product) % 10 or 10
        product = (2 * s) % 11
    return f"DE{first8}{(11 - product) % 10}"


def at_vat_id(first7: str) -> str:
    """Austrian UID: ATU + 7 digits + check digit (BMF algorithm)."""
    total = 0
    for i, ch in enumerate(first7):
        d = int(ch)
        total += d if i % 2 == 0 else (2 * d) // 10 + (2 * d) % 10
    return f"ATU{first7}{(10 - (total + 4) % 10) % 10}"


def nl_vat_id(first8: str) -> str | None:
    """Dutch BTW-id: NL + RSIN (9 digits, 11-test) + B01. None if no check digit exists for first8."""
    check = sum(int(ch) * (9 - i) for i, ch in enumerate(first8)) % 11
    return None if check == 10 else f"NL{first8}{check}B01"


def _ascii_initials(name: str) -> str:
    table = str.maketrans("ÄÖÜäöüß", "AOUaous")
    words = [w for w in name.translate(table).replace("&", " ").split() if w[0].isalpha()]
    return "".join(w[0].upper() for w in words[:2]) or "RE"


def _shift_workdays(d: date, n: int) -> date:
    """Move n workdays forward (n > 0) or back (n < 0); n = 0 keeps d."""
    step = 1 if n > 0 else -1
    while n:
        d += timedelta(days=step)
        if d.weekday() < 5:
            n -= step
    return d


def _workdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# ------------------------------------------------------------------ records


@dataclass
class Supplier:
    id: str
    name: str
    street: str
    zip: str
    city: str
    region: str  # Bundesland (DE, AT) or province (NL)
    country: str
    vat_id: str
    iban: str
    payment_terms_days: int
    is_blocked: int
    blocked_reason: str | None
    category: str  # generator-only: what this supplier sells
    trade: str  # generator-only: picks fitting PO descriptions and cost centers
    number_format: str  # generator-only
    next_no: int  # generator-only

    def format_number(self, n: int) -> str:
        return {
            "hyphen": f"{_ascii_initials(self.name)}-2026-{n:04d}",
            "re": f"RE{n:07d}",
            "year": f"2026{n:05d}",
            "slash": f"{n:05d}/26",
        }[self.number_format]


@dataclass
class PurchaseOrder:
    po_number: str
    supplier_id: str
    cost_center: str
    description: str
    net_cents: int
    net_invoiced_cents: int = 0
    status: str = "open"
    order_date: date | None = None  # set in assign_po_dates(), once all invoices exist


@dataclass
class Invoice:
    supplier_id: str
    supplier_invoice_number: str
    invoice_date: date
    received_date: date
    due_date: date
    po_number: str | None
    cost_center: str
    category: str
    net_cents: int
    vat_rate_pct: int
    vat_cents: int
    gross_cents: int
    status: str
    reason_code: str | None
    comment: str | None
    trap: str
    outcome: tuple[str, str | None, str | None] | None  # (status, reason_code, assigned_to)
    number_group: int = 0  # generator-only: invoices sharing a supplier invoice number
    number_variant: str | None = None  # generator-only: how a resent copy mangles the number
    scan_date: date = field(default=None)  # generator-only: decides the internal ID order
    id: str = ""


# How a resent duplicate's number differs from the original's (SPEC section 6 edge cases).
NUMBER_VARIANTS = {
    "case": str.lower,
    "space": lambda n: n.replace("-", "- ", 1),
    "hyphen": lambda n: n.replace("-", "", 1),
}


APPROVE = ("approved", None, None)


def reject(code: str) -> tuple:
    return ("rejected", code, None)


def hold(code: str) -> tuple:
    return ("on_hold", code, None)


def escalate(role: str) -> tuple:
    return ("escalated", schema.ESCALATION_CODE, APPROVER_BY_ROLE[role])


# ------------------------------------------------------------------ generator


class _Generator:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.suppliers: list[Supplier] = []
        self.pos: list[PurchaseOrder] = []
        self.invoices: list[Invoice] = []
        self.used_po_numbers: set[str] = set()
        self.number_groups = 0
        self.same_date_pairs: list[tuple[Invoice, Invoice]] = []  # (original, duplicate): ID must decide
        self.id_inversions: list[tuple[Invoice, Invoice]] = []  # (received earlier, received later): later gets the lower ID
        self.inbox_days = _workdays(INBOX_START, INBOX_END)
        self.history_days = _workdays(HISTORY_START, HISTORY_END)

    # ---------- master data

    def make_suppliers(self) -> None:
        rng = self.rng
        plan = (
            [("goods", "DE")] * 35 + [("services", "DE")] * 14 + [("books", "DE")] * 3 + [("food", "DE")] * 3
            + [("goods", "AT")] * 3 + [("goods", "NL")] * 2
        )
        # Trades the scenarios rely on are guaranteed, twice each; the rest are random.
        forced = {0: "Bürobedarf", 1: "Bürobedarf", 2: "Arbeitsschutz", 3: "Arbeitsschutz",
                  35: "Industrieservice", 36: "Instandhaltung", 37: "Industrieservice", 38: "Instandhaltung"}
        pools = {"DE": SURNAMES[:], "AT": AT_SURNAMES[:], "NL": NL_SURNAMES[:]}
        for pool in pools.values():
            rng.shuffle(pool)
        specs = []
        for n, (category, country) in enumerate(plan):
            surnames = pools[country]  # popped, so no surname is used twice
            if country == "AT":
                trade = rng.choice(TRADES["goods"])
                name = f"{surnames.pop()} {trade} GmbH"
            elif country == "NL":
                nl_trade = rng.choice(list(NL_TRADES))
                trade = NL_TRADES[nl_trade]
                name = f"{surnames.pop()} {nl_trade} B.V."
            else:
                pattern = rng.choice(NAME_PATTERNS[category])
                a = surnames.pop()
                b = surnames.pop() if "{b}" in pattern else ""
                if n in forced:
                    trade = forced[n]
                elif category in TRADES:
                    trade = rng.choice(TRADES[category])
                elif category == "books":
                    trade = "Buchhandel"
                else:
                    trade = next(t for word, t in PATTERN_TRADES.items() if word in pattern)
                name = pattern.format(a=a, b=b, t=trade)
            specs.append((category, country, name, trade))
        rng.shuffle(specs)

        de_index = {c: [i for i, (cat, country, _, _) in enumerate(specs) if cat == c and country == "DE"]
                    for c in ("goods", "services")}
        construction = [i for i in de_index["services"] if specs[i][3] in CONSTRUCTION_TRADES]
        blocked = dict(zip(rng.sample(de_index["goods"], 2) + rng.sample(construction, 1), BLOCKED_REASONS))
        for i, (category, country, name, trade) in enumerate(specs):
            if country == "DE":
                zips, city, region = rng.choice(DE_CITIES)
                zip_ = rng.choice(zips)
                region_digit = 4 if city in WESTFALEN_CITIES else BLZ_REGION[region]
                bban = f"{region_digit}{rng.randint(0, 9_999_999):07d}{rng.randint(0, 9_999_999_999):010d}"
                vat = de_vat_id(f"{rng.randint(1, 9)}{rng.randint(0, 9_999_999):07d}")
            elif country == "AT":
                zip_, city, region = rng.choice(AT_CITIES)
                bban = f"{rng.randint(10_000, 99_999)}{rng.randint(0, 99_999_999_999):011d}"
                vat = at_vat_id(f"{rng.randint(1_000_000, 9_999_999)}")
            else:
                zip_, city, region = rng.choice(NL_CITIES)
                bban = f"{rng.choice(['ABNA', 'INGB', 'RABO', 'SNSB'])}{rng.randint(0, 9_999_999_999):010d}"
                vat = None
                while vat is None:
                    vat = nl_vat_id(f"{rng.randint(0, 99_999_999):08d}")
            self.suppliers.append(
                Supplier(
                    id=f"K-{10001 + i}",
                    name=name,
                    street=f"{rng.choice(NL_STREETS if country == 'NL' else STREETS)} {rng.randint(1, 140)}"
                           f"{rng.choice(['', '', '', 'a', 'b'])}",
                    zip=zip_,
                    city=city,
                    region=region,
                    country=country,
                    vat_id=vat,
                    iban=iban(country, bban),
                    payment_terms_days=rng.choice([14, 30, 30, 30, 45, 60]),
                    is_blocked=int(i in blocked),
                    blocked_reason=blocked[i].format(city=city) if i in blocked else None,
                    category=category,
                    trade=trade,
                    number_format=rng.choice(["hyphen", "hyphen", "re", "year", "slash"]),
                    next_no=rng.randint(100, 4000),
                )
            )

    def pick(self, category: str | None = None, *, fmt: str | None = None, blocked: bool = False,
             exclude: Supplier | None = None, big: bool = False, trades: set[str] | None = None) -> Supplier:
        """Random DE supplier; category None means goods or services (any category if `trades` is given);
        big: trades that bill > EUR 25,000."""
        cats = {category} if category else (set(schema.CATEGORIES) if trades else {"goods", "services"})
        pool = [
            s for s in self.suppliers
            if s.country == "DE" and s.category in cats and bool(s.is_blocked) == blocked
            and (fmt is None or s.number_format == fmt) and s is not exclude
            and (not big or s.trade in BIG_TICKET_TRADES) and (trades is None or s.trade in trades)
        ]
        return self.rng.choice(pool)

    def any_category(self) -> str:
        return self.rng.choice(["goods"] * 5 + ["services"] * 3)

    # ---------- amounts and dates

    def net_in_gross_band(self, lo: int, hi: int, rate: int) -> int:
        """A net amount whose correct gross lies in [lo, hi]."""
        net = self.rng.randint(lo, hi) * 100 // (100 + rate)
        while net + money.expected_vat_cents(net, rate) > hi:
            net -= 1
        while net + money.expected_vat_cents(net, rate) < lo:
            net += 1
        return net

    @staticmethod
    def net_for_exact_gross(gross: int, rate: int) -> int:
        base = gross * 100 // (100 + rate)
        for net in range(base - 3, base + 4):
            if net + money.expected_vat_cents(net, rate) == gross:
                return net
        raise ValueError(f"no net amount gives gross {gross} at {rate}%")

    def half_cent_net(self, lo: int, hi: int) -> int:
        """A net ending in 50 cents: net * 19% and net * 7% both land exactly on half a cent."""
        return self.rng.randint(lo // 100, hi // 100) * 100 + 50

    def day(self) -> date:
        return self.rng.choice(self.inbox_days)

    def days(self, n: int) -> list[date]:
        return sorted(self.rng.sample(self.inbox_days, n))

    # ---------- POs and invoices

    def po(self, supplier: Supplier, net: int, *, status: str = "open") -> PurchaseOrder:
        while True:
            number = f"45000{self.rng.randint(10_000, 99_999)}"
            if number not in self.used_po_numbers:
                break
        self.used_po_numbers.add(number)
        description, cost_center = self.rng.choice(DESCRIPTIONS[supplier.trade])
        po = PurchaseOrder(
            po_number=number,
            supplier_id=supplier.id,
            cost_center=cost_center,
            description=description,
            net_cents=net,
            status=status,
        )
        self.pos.append(po)
        return po

    def po_with_headroom(self, supplier: Supplier, net: int) -> PurchaseOrder:
        po_net = -(-(net + self.rng.randint(0, net // 2)) // 100) * 100
        if supplier.trade not in BIG_TICKET_TRADES:  # small trades don't place orders above EUR 25,000
            po_net = max(min(po_net, AL_LIMIT), net)
        return self.po(supplier, po_net)

    def invoice(self, supplier: Supplier, net: int, *, trap: str, outcome: tuple | None,
                po: PurchaseOrder | None = None, po_number: str | None = None, rate: int | None = None,
                vat: int | None = None, gross: int | None = None, same_number_as: "Invoice | None" = None,
                received: date | None = None, invoice_date: date | None = None, status: str = "received",
                reason_code: str | None = None, comment: str | None = None) -> Invoice:
        rate = money.expected_vat_rate_pct(supplier.category) if rate is None else rate
        vat = money.expected_vat_cents(net, rate) if vat is None else vat
        gross = net + vat if gross is None else gross
        received = received or self.day()
        invoice_date = invoice_date or _shift_workdays(received, -self.rng.randint(1, 6))
        if same_number_as is None:
            self.number_groups += 1
        inv = Invoice(
            supplier_id=supplier.id,
            supplier_invoice_number="",  # assigned in assign_numbers(), in invoice-date order
            invoice_date=invoice_date,
            received_date=received,
            due_date=invoice_date + timedelta(days=supplier.payment_terms_days),
            po_number=po.po_number if po else po_number,
            cost_center=po.cost_center if po else self.rng.choice(DESCRIPTIONS[supplier.trade])[1],
            category=supplier.category,
            net_cents=net,
            vat_rate_pct=rate,
            vat_cents=vat,
            gross_cents=gross,
            status=status,
            reason_code=reason_code,
            comment=comment,
            trap=trap,
            outcome=outcome,
            number_group=same_number_as.number_group if same_number_as else self.number_groups,
        )
        self.invoices.append(inv)
        return inv

    def history_approved(self, supplier: Supplier, po: PurchaseOrder, net: int, **kw) -> Invoice:
        po.net_invoiced_cents += net
        return self.invoice(supplier, net, po=po, trap="history", outcome=None, status="approved",
                            received=self.rng.choice(self.history_days), **kw)

    def copy(self, orig: Invoice, *, trap: str, outcome: tuple, received: date, variant: str | None = None,
             **changes) -> Invoice:
        """A resent invoice: same supplier, number (optionally mangled), PO and dates unless changed."""
        fields = dict(
            supplier_id=orig.supplier_id, supplier_invoice_number="",
            number_group=orig.number_group, number_variant=variant,
            invoice_date=orig.invoice_date, received_date=received, due_date=orig.due_date,
            po_number=orig.po_number, cost_center=orig.cost_center, category=orig.category,
            net_cents=orig.net_cents, vat_rate_pct=orig.vat_rate_pct, vat_cents=orig.vat_cents,
            gross_cents=orig.gross_cents, status="received", reason_code=None, comment=None,
            trap=trap, outcome=outcome,
        )
        fields.update(changes)
        inv = Invoice(**fields)
        self.invoices.append(inv)
        return inv

    def clean_with_po(self, supplier: Supplier | None = None, *, lo: int = 30_000, hi: int = TL_LIMIT,
                      trap: str = "clean", outcome: tuple = APPROVE, received: date | None = None) -> Invoice:
        supplier = supplier or self.pick(self.any_category())
        net = self.net_in_gross_band(lo, hi, money.expected_vat_rate_pct(supplier.category))
        return self.invoice(supplier, net, po=self.po_with_headroom(supplier, net), trap=trap,
                            outcome=outcome, received=received)

    # ---------- scenarios

    def duplicates(self) -> None:
        dup = reject("DUPLICATE")
        for variant in NUMBER_VARIANTS:
            d1, d2 = self.days(2)
            orig = self.clean_with_po(self.pick("goods", fmt="hyphen"), trap="dup_original", received=d1)
            self.copy(orig, trap=f"dup_{variant}", outcome=dup, received=d2, variant=variant)

        d1, d2 = self.days(2)
        orig = self.clean_with_po(trap="dup_original", received=d1)
        net = orig.net_cents + self.rng.randint(1_000, 20_000)
        vat = money.expected_vat_cents(net, orig.vat_rate_pct)
        self.copy(orig, trap="dup_different_amount", outcome=dup, received=d2,
                  net_cents=net, vat_cents=vat, gross_cents=net + vat)

        for _ in range(4):  # resent invoice that was already approved (paid) in September
            s = self.pick()
            net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
            orig = self.history_approved(s, self.po_with_headroom(s, net), net)
            self.copy(orig, trap="dup_of_paid", outcome=dup, received=self.day())

        d = self.day()  # same received date: the internal ID decides which is the original
        orig = self.clean_with_po(trap="dup_original", received=d)
        self.same_date_pairs.append((orig, self.copy(orig, trap="dup_same_day", outcome=dup, received=d)))

        d1, d2 = self.days(2)
        s = self.pick("food")
        net = self.net_in_gross_band(3_000, 20_000, 7)
        orig = self.invoice(s, net, trap="dup_original_kleinbetrag", outcome=APPROVE, received=d1)
        self.copy(orig, trap="dup_kleinbetrag", outcome=dup, received=d2)

        d1, d2 = self.days(2)
        orig = self.clean_with_po(trap="dup_original", received=d1)
        bad_vat = orig.vat_cents + self.rng.randint(1, 400)
        self.copy(orig, trap="dup_with_vat_error", outcome=dup, received=d2,
                  vat_cents=bad_vat, gross_cents=orig.net_cents + bad_vat)

        d1, d2, d3 = self.days(3)
        orig = self.clean_with_po(trap="dup_original", received=d1)
        self.copy(orig, trap="dup_triple", outcome=dup, received=d2)
        self.copy(orig, trap="dup_triple", outcome=dup, received=d3)

        for _ in range(2):
            d1, d2 = self.days(2)
            orig = self.clean_with_po(trap="dup_original", received=d1)
            self.copy(orig, trap="dup_exact", outcome=dup, received=d2)

    def resubmissions(self) -> None:
        # Rejected in September for the wrong rate; the corrected invoice keeps its number.
        s = self.pick("goods")
        net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
        po = self.po_with_headroom(s, net)
        bad = self.invoice(s, net, po=po, rate=7, trap="history", outcome=None, status="rejected",
                           received=self.rng.choice(self.history_days), reason_code="VAT_ERROR",
                           comment="Falscher Steuersatz (7 % statt 19 %), bitte korrigierte Rechnung senden.")
        self.invoice(s, net, po=po, same_number_as=bad, trap="resubmission_after_reject",
                     outcome=APPROVE)

        # Both in the inbox: the faulty original is rejected first, so the resubmission is no duplicate.
        d1, d2 = self.days(2)
        s = self.pick("services")
        net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
        po = self.po_with_headroom(s, net)
        wrong_vat = money.expected_vat_cents(net, 19) - self.rng.randint(5, 900)
        bad = self.invoice(s, net, po=po, vat=wrong_vat, trap="vat_error_resubmitted_later",
                           outcome=reject("VAT_ERROR"), received=d1)
        self.invoice(s, net, po=po, same_number_as=bad, trap="resubmission_in_batch",
                     outcome=APPROVE, received=d2)

        # The same, but the corrected copy got the lower ID: processed by ID, it would look like a duplicate.
        d1, d2 = self.days(2)
        s = self.pick("goods")
        net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
        po = self.po_with_headroom(s, net)
        bad = self.invoice(s, net, po=po, rate=7, trap="vat_error_resubmitted_lower_id",
                           outcome=reject("VAT_ERROR"), received=d1)
        fixed = self.invoice(s, net, po=po, same_number_as=bad, trap="resubmission_lower_id",
                             outcome=APPROVE, received=d2)
        self.id_inversions.append((bad, fixed))

    def po_chains(self) -> None:
        """Three POs with four received invoices each, where processing order (and approver) decide what fits."""
        exceeded, dup = hold("PO_EXCEEDED"), reject("DUPLICATE")

        # A: 40 % + 35 % fit, 30 % exceeds and is held, 25 % still fits (exactly 100 %) after the hold.
        #    The 2nd and 3rd have inverted IDs: processed by ID, the 2nd would be held instead of the 3rd.
        s = self.pick("goods")
        po_net = self.rng.randint(60, 100) * 10_000
        po = self.po(s, po_net)
        d = self.days(4)
        a = [self.invoice(s, po_net * pct // 100, po=po, received=day, trap=trap, outcome=outcome)
             for pct, day, trap, outcome in [(40, d[0], "po_chain_fits", APPROVE), (35, d[1], "po_chain_fits", APPROVE),
                                            (30, d[2], "po_chain_held", exceeded),
                                            (25, d[3], "po_chain_fits_after_hold", APPROVE)]]
        self.id_inversions.append((a[1], a[2]))

        # B: the first invoice is above the Teamleiter limit. Escalated, it consumes no budget and all others fit;
        #    an Abteilungsleiter approves it, and then only the last one fits.
        s = self.pick()
        f = self.rng.randint(85, 100)
        po = self.po(s, 1_200_000 * f // 100)
        d = self.days(4)
        self.invoice(s, 900_000 * f // 100, po=po, received=d[0], trap="po_chain_escalated",
                     outcome=escalate("Abteilungsleiter"))
        for net, day in [(400_000, d[1]), (350_000, d[2]), (200_000, d[3])]:
            self.invoice(s, net * f // 100, po=po, received=day, trap="po_chain_after_escalation", outcome=APPROVE)

        # C: a duplicate inside the chain (its original has the higher ID); approving it would push
        #    the last two over the PO.
        s = self.pick("goods")
        f = self.rng.randint(85, 100)
        po = self.po(s, 700_000 * f // 100)
        d = self.days(4)
        orig = self.invoice(s, 300_000 * f // 100, po=po, received=d[0], trap="po_chain_dup_original", outcome=APPROVE)
        copy = self.copy(orig, trap="po_chain_duplicate", outcome=dup, received=d[1])
        self.id_inversions.append((orig, copy))
        for net, day in [(250_000, d[2]), (160_000, d[3])]:
            self.invoice(s, net * f // 100, po=po, received=day, trap="po_chain_after_duplicate", outcome=APPROVE)

    def po_budget(self) -> None:
        exceeded = hold("PO_EXCEEDED")
        for _ in range(7):  # most of the PO was already invoiced in August/September
            s = self.pick()
            po_net = self.rng.randint(3_000, 30_000 if s.trade in BIG_TICKET_TRADES else 12_000) * 100
            already = po_net * self.rng.randint(70, 95) // 100
            po = self.po(s, po_net)
            self.history_approved(s, po, already)
            net = po_net * 102 // 100 - already + 1 + self.rng.randint(0, po_net // 4)
            assert not money.within_po_tolerance(po_net, already, net)
            self.invoice(s, net, po=po, trap="po_exceeded", outcome=exceeded)

        # Exactly at +2% (passes) and one cent above (holds).
        s = self.pick("goods")
        po = self.po(s, 300_000)
        self.history_approved(s, po, 100_000)
        self.invoice(s, 206_000, po=po, trap="po_exact_tolerance", outcome=APPROVE)
        s = self.pick("goods")
        po = self.po(s, 500_000)
        self.history_approved(s, po, 200_000)
        self.invoice(s, 310_001, po=po, trap="po_one_cent_over", outcome=exceeded)

        # Two invoices that only exceed the PO together: order decides.
        d1, d2 = self.days(2)
        s = self.pick()
        po = self.po(s, 400_000)
        self.invoice(s, 250_000, po=po, trap="po_pair_first", outcome=APPROVE, received=d1)
        self.invoice(s, 200_000, po=po, trap="po_pair_second", outcome=exceeded, received=d2)

        # A Kleinbetrag with a PO counts toward it and pushes a later invoice over the limit.
        d1, d2 = self.days(2)
        s = self.pick("goods")
        po = self.po(s, 100_000)
        self.invoice(s, 15_000, po=po, trap="kleinbetrag_pair_first", outcome=APPROVE, received=d1)
        self.invoice(s, 90_000, po=po, trap="kleinbetrag_pair_second", outcome=exceeded, received=d2)

        # PO exceeded and wrong VAT: the PO amount check comes first.
        s = self.pick("goods")
        po = self.po(s, 200_000)
        self.history_approved(s, po, 150_000)
        self.invoice(s, 120_000, po=po, rate=7, trap="po_exceeded_and_vat_error", outcome=exceeded)

    def vat_errors(self) -> None:
        err = reject("VAT_ERROR")
        for category in ("goods", "goods", "services"):
            s = self.pick(category)
            net = self.net_in_gross_band(30_000, TL_LIMIT, 7)
            self.invoice(s, net, po=self.po_with_headroom(s, net), rate=7, trap="vat_wrong_rate", outcome=err)
        for category in ("books", "food"):
            s = self.pick(category)
            net = self.net_in_gross_band(30_000, 300_000, 19)
            self.invoice(s, net, po=self.po_with_headroom(s, net), rate=19, trap="vat_wrong_rate", outcome=err)
        for i in range(3):
            s = self.pick(trades={"Bürobedarf", "Arbeitsschutz"}) if i == 0 else self.pick()
            off = self.rng.choice([-1, 1]) * self.rng.randint(1, 300)
            if i == 0:  # a Kleinbetrag without PO can still have wrong VAT
                net = self.net_in_gross_band(2_000, 20_000, 19)
                self.invoice(s, net, vat=money.expected_vat_cents(net, 19) + off, trap="vat_amount_wrong_kleinbetrag",
                             outcome=err)
            else:
                net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
                self.invoice(s, net, po=self.po_with_headroom(s, net), vat=money.expected_vat_cents(net, 19) + off,
                             trap="vat_amount_wrong", outcome=err)
        for _ in range(3):
            s = self.pick()
            net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
            vat = money.expected_vat_cents(net, 19)
            off = self.rng.choice([-1, 1]) * self.rng.randint(1, 1_000)
            self.invoice(s, net, po=self.po_with_headroom(s, net), gross=net + vat + off,
                         trap="vat_gross_mismatch", outcome=err)
        for category in ("goods", "books"):  # half a cent rounded down instead of up
            s = self.pick(category)
            net = self.half_cent_net(30_000, 300_000)
            rate = money.expected_vat_rate_pct(category)
            self.invoice(s, net, po=self.po_with_headroom(s, net), vat=money.expected_vat_cents(net, rate) - 1,
                         trap="vat_half_cent_rounded_down", outcome=err)
        s = self.pick("goods")  # wrong VAT beats the approval limit
        net = self.net_in_gross_band(800_000, 2_000_000, 7)
        self.invoice(s, net, po=self.po_with_headroom(s, net), rate=7, trap="vat_error_above_limit", outcome=err)

    def purchase_order_rules(self) -> None:
        missing, invalid = reject("MISSING_PO"), reject("INVALID_PO")
        for _ in range(12):
            s = self.pick()
            self.invoice(s, self.net_in_gross_band(30_000, TL_LIMIT, 19), trap="missing_po", outcome=missing)
        s = self.pick("goods")
        self.invoice(s, self.net_for_exact_gross(25_001, 19), trap="missing_po_one_cent_over_kleinbetrag",
                     outcome=missing)
        s = self.pick()
        self.invoice(s, self.net_in_gross_band(800_000, 2_000_000, 19), trap="missing_po_above_limit",
                     outcome=missing)
        s = self.pick("books")
        self.invoice(s, self.net_in_gross_band(30_000, 150_000, 7), trap="missing_po", outcome=missing)

        for kleinbetrag in (False, False, False, True):
            s = self.pick()
            other = self.pick(exclude=s)
            lo, hi = (2_000, 24_000) if kleinbetrag else (30_000, TL_LIMIT)
            net = self.net_in_gross_band(lo, hi, money.expected_vat_rate_pct(s.category))
            foreign_po = self.po_with_headroom(other, net)
            self.invoice(s, net, po=foreign_po, trap="kleinbetrag_other_supplier_po" if kleinbetrag
                         else "po_of_other_supplier", outcome=invalid)
        for _ in range(3):  # PO closed after its final invoice; a late follow-up invoice cites it
            s = self.pick()
            po_net = self.rng.randint(1_000, 8_000) * 100
            po = self.po(s, po_net, status="closed")
            self.history_approved(s, po, po_net)
            net = self.net_in_gross_band(30_000, TL_LIMIT, money.expected_vat_rate_pct(s.category))
            self.invoice(s, net, po=po, trap="po_closed", outcome=invalid)
        for _ in range(2):  # typo: two digits of the supplier's real PO swapped
            s = self.pick()
            net = self.net_in_gross_band(30_000, TL_LIMIT, money.expected_vat_rate_pct(s.category))
            real = self.po_with_headroom(s, net)
            digits = list(real.po_number)
            while "".join(digits) in self.used_po_numbers:
                i = self.rng.randint(5, 8)
                digits = list(real.po_number)
                digits[i], digits[i + 1] = digits[i + 1], digits[i]
                if digits == list(real.po_number):
                    digits[9] = str((int(digits[9]) + 1) % 10)
            typo = "".join(digits)
            self.used_po_numbers.add(typo)  # reserve it so no PO with this number is ever created
            self.invoice(s, net, po=real, trap="po_does_not_exist", outcome=invalid)
            self.invoices[-1].po_number = typo

    def blocked_suppliers(self) -> None:
        blocked = hold("SUPPLIER_BLOCKED")
        for _ in range(4):
            self.clean_with_po(self.pick(blocked=True), trap="supplier_blocked", outcome=blocked)
        s = self.pick(blocked=True)
        self.invoice(s, self.net_in_gross_band(30_000, TL_LIMIT, 19), trap="supplier_blocked_and_missing_po",
                     outcome=blocked)
        s = self.pick(blocked=True)
        net = self.net_in_gross_band(30_000, TL_LIMIT, 19)
        self.invoice(s, net, po=self.po_with_headroom(s, net), rate=7, trap="supplier_blocked_and_vat_error",
                     outcome=blocked)
        self.clean_with_po(self.pick(blocked=True), lo=TL_LIMIT + 1, hi=AL_LIMIT,
                           trap="supplier_blocked_above_limit", outcome=blocked)

    def escalations(self) -> None:
        bands = [
            ("Abteilungsleiter", TL_LIMIT + 1, AL_LIMIT, 15, [TL_LIMIT + 1, AL_LIMIT]),
            ("Bereichsleiter", AL_LIMIT + 1, BL_LIMIT, 5, [AL_LIMIT + 1, BL_LIMIT]),
            ("CFO", BL_LIMIT + 1, 3 * BL_LIMIT, 2, [BL_LIMIT + 1]),
        ]
        for role, lo, hi, n_random, exact in bands:
            for _ in range(n_random):
                s = self.pick(self.any_category(), big=lo > AL_LIMIT)
                self.clean_with_po(s, lo=lo, hi=hi, trap="above_limit", outcome=escalate(role))
            for gross in exact:
                s = self.pick("goods", big=gross > AL_LIMIT)
                net = self.net_for_exact_gross(gross, 19)
                self.invoice(s, net, po=self.po_with_headroom(s, net), trap="above_limit_boundary",
                             outcome=escalate(role))
        s = self.pick("goods")  # exactly at the Teamleiter's own limit: approve
        net = self.net_for_exact_gross(TL_LIMIT, 19)
        self.invoice(s, net, po=self.po_with_headroom(s, net), trap="at_own_limit", outcome=APPROVE)

    def kleinbetraege(self) -> None:
        s = self.pick(trades={"Bürobedarf", "Arbeitsschutz"})
        self.invoice(s, self.net_for_exact_gross(money.KLEINBETRAG_LIMIT_CENTS, 19),
                     trap="kleinbetrag_exact_limit", outcome=APPROVE)
        for i in range(17):  # mostly office supplies, safety equipment, books and food; a few from anyone
            s = self.pick(trades=SMALL_TICKET_TRADES) if i < 14 else self.pick()
            net = self.net_in_gross_band(1_500, 24_900, money.expected_vat_rate_pct(s.category))
            self.invoice(s, net, trap="kleinbetrag_no_po", outcome=APPROVE)
        s = self.pick("books")
        po = self.po(s, 50_000)
        self.invoice(s, 12_000, po=po, trap="kleinbetrag_with_po", outcome=APPROVE)

    def clean(self) -> None:
        for category in ("goods", "books"):  # half-cent VAT, correctly rounded up
            s = self.pick(category)
            net = self.half_cent_net(30_000, 300_000)
            self.invoice(s, net, po=self.po_with_headroom(s, net), trap="vat_half_cent_correct", outcome=APPROVE)
        for _ in range(3):  # framework PO (Rahmenbestellung) with three invoices and ample room
            s = self.pick()
            nets = [self.net_in_gross_band(30_000, 300_000, 19) for _ in range(3)]
            po = self.po(s, -(-sum(nets) * 13 // 10 // 100) * 100)
            for net in nets:
                self.invoice(s, net, po=po, trap="clean_framework_po", outcome=APPROVE)
        for category in ("books", "books", "food", "food"):
            self.clean_with_po(self.pick(category), hi=300_000)
        while len(self.invoices) < TOTAL_INVOICES:
            self.clean_with_po()

    def spare_pos(self) -> None:
        """Open POs without invoices, so the PO table looks like a real one."""
        active = [s for s in self.suppliers if not s.is_blocked]
        while len(self.pos) < TOTAL_POS:
            s = self.rng.choice(active)
            self.po(s, self.rng.randint(500, 60_000 if s.trade in BIG_TICKET_TRADES else 15_000) * 100)

    # ---------- ids

    def assign_numbers(self) -> None:
        """Each supplier numbers its invoices sequentially in invoice-date order; resent copies reuse the number."""
        groups: dict[int, list[Invoice]] = {}
        for inv in self.invoices:
            groups.setdefault(inv.number_group, []).append(inv)
        for supplier in self.suppliers:
            own = [g for g, invs in groups.items() if invs[0].supplier_id == supplier.id]
            for g in sorted(own, key=lambda g: (min(i.invoice_date for i in groups[g]), g)):
                supplier.next_no += self.rng.randint(1, 37)
                number = supplier.format_number(supplier.next_no)
                for inv in groups[g]:
                    inv.supplier_invoice_number = (
                        NUMBER_VARIANTS[inv.number_variant](number) if inv.number_variant else number)

    def assign_po_dates(self) -> None:
        """A PO is ordered 3-30 workdays before the earliest invoice citing it; unused POs any time since June."""
        earliest: dict[str, date] = {}
        for inv in self.invoices:
            if inv.po_number:
                earliest[inv.po_number] = min(inv.invoice_date, earliest.get(inv.po_number, inv.invoice_date))
        order_days = _workdays(date(2026, 6, 1), INBOX_END)
        for po in self.pos:
            if po.po_number in earliest:
                po.order_date = _shift_workdays(earliest[po.po_number], -self.rng.randint(3, 30))
            else:
                po.order_date = self.rng.choice(order_days)

    def assign_ids(self) -> None:
        """Internal IDs follow scanning order, which lags the received date by 0-3 workdays."""
        for inv in self.invoices:
            inv.scan_date = _shift_workdays(inv.received_date, self.rng.choice([0, 0, 0, 0, 1, 1, 2, 3]))
        keyed = [(inv.scan_date, self.rng.random(), inv) for inv in self.invoices]
        keyed.sort(key=lambda k: (k[0], k[1]))
        for n, (_, _, inv) in enumerate(keyed, start=FIRST_INVOICE_NO):
            inv.id = f"ER-2026-{n:05d}"
        for orig, dup in self.same_date_pairs:
            if orig.id > dup.id:
                orig.id, dup.id = dup.id, orig.id
        for earlier, later in self.id_inversions:
            if earlier.id < later.id:
                earlier.id, later.id = later.id, earlier.id

    def build(self) -> "_Generator":
        self.make_suppliers()
        self.duplicates()
        self.resubmissions()
        self.po_budget()
        self.po_chains()
        self.vat_errors()
        self.purchase_order_rules()
        self.blocked_suppliers()
        self.escalations()
        self.kleinbetraege()
        self.clean()
        self.spare_pos()
        self.assign_numbers()
        self.assign_po_dates()
        self.assign_ids()
        assert len(self.invoices) == TOTAL_INVOICES, len(self.invoices)
        return self


def generate(seed: int = SEED) -> _Generator:
    """Generate all records in memory (deterministic for a given seed)."""
    return _Generator(seed).build()


def build_db(path: str | Path, seed: int = SEED) -> str:
    """Create a fresh SQLite database at `path` filled with the seed data. Returns the path."""
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        raise FileExistsError(f"{path} already exists and is not empty")
    gen = generate(seed)
    conn = sqlite3.connect(path)
    try:
        schema.create_schema(conn)
        with conn:
            conn.executemany(
                "INSERT INTO approvers (id, name, role, approval_limit_gross_cents) VALUES (?, ?, ?, ?)", APPROVERS
            )
            conn.executemany(
                "INSERT INTO suppliers (id, name, street, zip, city, region, country, vat_id, iban, "
                "payment_terms_days, is_blocked, blocked_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(s.id, s.name, s.street, s.zip, s.city, s.region, s.country, s.vat_id, s.iban,
                  s.payment_terms_days, s.is_blocked, s.blocked_reason) for s in gen.suppliers],
            )
            conn.executemany(
                "INSERT INTO purchase_orders (po_number, supplier_id, order_date, cost_center, description, "
                "net_cents, net_invoiced_cents, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(p.po_number, p.supplier_id, p.order_date.isoformat(), p.cost_center, p.description, p.net_cents,
                  p.net_invoiced_cents, p.status) for p in sorted(gen.pos, key=lambda p: p.po_number)],
            )
            invoices = sorted(gen.invoices, key=lambda i: i.id)
            conn.executemany(
                "INSERT INTO invoices (id, supplier_id, supplier_invoice_number, invoice_date, received_date, "
                "due_date, po_number, cost_center, category, net_cents, vat_rate_pct, vat_cents, gross_cents, "
                "currency, status, reason_code, assigned_to, comment, hidden_trap, hidden_outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EUR', ?, ?, NULL, ?, ?, ?)",
                [(i.id, i.supplier_id, i.supplier_invoice_number, i.invoice_date.isoformat(),
                  i.received_date.isoformat(), i.due_date.isoformat(), i.po_number, i.cost_center, i.category,
                  i.net_cents, i.vat_rate_pct, i.vat_cents, i.gross_cents, i.status, i.reason_code, i.comment,
                  i.trap, _outcome_json(i.outcome)) for i in invoices],
            )
            names = {s.id: s.name for s in gen.suppliers}
            for i in invoices:
                if i.status == "approved":  # history: post the same ledger lines approval would have
                    text = f"{names[i.supplier_id]} RE {i.supplier_invoice_number}"
                    lines = ledger.ledger_lines(i.category, i.vat_rate_pct, i.net_cents, i.vat_cents,
                                                i.gross_cents, text)
                    conn.executemany(
                        "INSERT INTO ledger_entries (invoice_id, account, debit_cents, credit_cents, text) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(i.id, *line) for line in lines],
                    )
            conn.execute("INSERT INTO app_context (key, value) VALUES ('acting_approver_id', ?)",
                         (ACTING_APPROVER_ID,))
    finally:
        conn.close()
    return str(path)


def fingerprint(db: str | Path) -> str:
    """SHA-256 over the schema and every row of every table, in a stable order.

    Tasks (gold actions, scopes) are generated from the seed database, so any change to this value
    means every task must be regenerated.
    """
    conn = sqlite3.connect(db)
    try:
        h = hashlib.sha256()
        tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name").fetchall()
        for name, sql in tables:
            h.update(sql.encode())
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
                h.update(json.dumps([name, *row], ensure_ascii=False).encode())
        return h.hexdigest()
    finally:
        conn.close()


def _outcome_json(outcome: tuple | None) -> str | None:
    if outcome is None:
        return None
    status, reason_code, assigned_to = outcome
    return json.dumps({"status": status, "reason_code": reason_code, "assigned_to": assigned_to})


if __name__ == "__main__":
    print(build_db(sys.argv[1] if len(sys.argv) > 1 else "invoice_approval.sqlite"))
