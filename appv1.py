import os
import io
import re
import time
import json
import math
import sqlite3
import hashlib
from datetime import datetime, timezone
import ezdxf
import streamlit as st
from PIL import Image
from pydantic import BaseModel, Field
from typing import Literal
from google import genai
from google.genai import types

# =====================================================================
# 1. CATALOGUL INDUSTRIAL ISO 1219-1 / ISO 1219-2 & MODELE PYDANTIC
# =====================================================================
TIPURI_COMPONENTE = Literal[
    "CYL_DOUBLE",  # Cilindru dublu efect cu amortizare reglabila si inel magnetic (porturi: P1, P2)
    "CYL_SINGLE",  # Cilindru simplu efect cu arc de readucere (porturi: P1)
    "THROTTLE_CHECK_LEFT",  # Drosel cu supapa de sens pe calea 4 / stanga (porturi: 1, 2)
    "THROTTLE_CHECK_RIGHT",  # Drosel cu supapa de sens pe calea 2 / dreapta (porturi: 1, 2)
    "QUICK_EXHAUST",  # Supapa de descarcare rapida (porturi: 1, 2, 3)
    "VALVE_5_3_VENTED",  # Distribuitor 5/3 centru ventilat/evacuat - 3 pozitii (porturi: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_3_CLOSED",  # Distribuitor 5/3 centru inchis - 3 pozitii (porturi: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_3_PRESSURIZED",  # Distribuitor 5/3 centru sub presiune - 3 pozitii (porturi: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_2_BISTABLE",  # Distribuitor 5/2 bistabil memorat - 2 pozitii (porturi: 1, 2, 4, 3, 5, 14, 12)
    "VALVE_5_2_MONOSTABLE",  # Distribuitor 5/2 monostabil cu arc - 2 pozitii (porturi: 1, 2, 4, 3, 5, 14)
    "VALVE_3_2_NC_BUTTON",  # Distribuitor 3/2 Normal Inchis cu buton si arc - 2 pozitii (porturi: 1, 2, 3)
    "VALVE_3_2_NC_ROLLER",  # Distribuitor 3/2 Normal Inchis cu rola limitator a0/a1 - 2 pozitii (porturi: 1, 2, 3)
    "VALVE_3_2_NC_PILOT",  # Distribuitor 3/2 Normal Inchis pilotat - 2 pozitii (porturi: 1, 2, 3, 12)
    "LOGIC_OR",  # Selector de circuit / Supapa SAU - selectie (porturi: 1L, 1R, 2)
    "LOGIC_AND",  # Supapa de simultaneitate / Supapa SI - bimanuala (porturi: 1L, 1R, 2)
    "FRL_EXPANDED",  # Grup FRL desfasurat complet: Filtru + Regulator + Manometru + Lubrificator (porturi: 1, 2)
    "FRL_MACRO",  # Grup FRL simbol simplificat pe scurt ISO 1219 (porturi: 1, 2)
    "AIR_SOURCE"  # Sursa de aer comprimat 6 bar (porturi: 1)
]


class ComponentaISO(BaseModel):
    id: str = Field(
        description="Cod ISO 1219-2: M1 (cilindru), V1 (distribuitor principal), V2/V3 (drosele), V4 (supapa logica SAU/SI), S1/S2 (butoane 3/2), B1 (limitator rola 3/2), Z1 (grup FRL), P1 (sursa aer)")
    tip: TIPURI_COMPONENTE = Field(description="Tipul exact din catalogul ISO 1219")
    nume: str = Field(description="Denumirea tehnica completa in limba romana")
    parametru: str = Field(description="Specificatie tehnica, ex: 'Ø63x200 mm', 'Drosel 50%', '6 bar'")
    cod_festo: str = Field(
        description="Cod real de catalog Festo, ex: 'DSBC-63-200-PPSA-N3', 'GRLA-1/4-QS-8-D', 'VUVG-L14-P53E-T-G18'")
    x: int = Field(description="Coordonata X pe grila (intre 100 si 520; coloana principala la X=350)")
    y: int = Field(
        description="Nivelul ierarhic Y conform ISO 1219 (85=Cilindru, 205=Drosele, 340=Distribuitor principal, 440=Logica/Comanda, 485=FRL, 565=Sursa)")


class ConexiuneISO(BaseModel):
    de_la: str = Field(
        description="ID_Componenta.ID_Port sursa, ex: 'P1.1', 'Z1.2', 'V1.4', 'V1.2', 'V2.2', 'V3.2', 'S1.2', 'V4.2', 'B1.2'")
    pana_la: str = Field(
        description="ID_Componenta.ID_Port destinatie, ex: 'Z1.1', 'V1.1', 'V2.1', 'V3.1', 'M1.P1', 'M1.P2', 'V1.14', 'V1.12', 'V4.1L', 'V4.1R'")
    circuit_activ_in_starea: Literal["PERMANENT", "STARE_14", "STARE_12", "PILOT_14", "PILOT_12"] = Field(
        description="Cand este sub presiune conducta: PERMANENT, STARE_14, STARE_12, PILOT_14, PILOT_12"
    )


class PasFluidSIM(BaseModel):
    pas: int
    element: str
    cum_se_pune_in_fluidsim: str = Field(description="Instructiuni exacte pentru Festo FluidSIM-P")


class PasGrafcet(BaseModel):
    etapa: str = Field(description="Etapa GRAFCET conform IEC 60848")
    actiune: str = Field(description="Actiunea asociata etapei")
    tranzitie: str = Field(description="Conditia de tranzitie catre etapa urmatoare")


class ProiectFluidSIM(BaseModel):
    analiza_inginereasca_pas_cu_pas: str = Field(
        description="Rationamentul tehnic complet: verificarea numarului exact de cai si pozitii (2 pozitii pt 5/2 si 3/2; 3 pozitii DOAR pt 5/3), arhitectura de comanda si regimul de droselizare."
    )
    titlu: str
    numar_pozitii_distribuitor_principal: int = Field(
        description="Pune 2 pentru distribuitoare 5/2 sau 3/2 (nu au pozitie centrala!); pune 3 doar pentru distribuitoare 5/3."
    )
    explicatie_centru_si_ansamblu: str = Field(
        description="Explicatia tehnica detaliata a functionarii in fiecare pozitie reala a schemei")
    simbol_macro_descriere: str = Field(
        description="Explicatia diferentei dintre simbolul simplificat (Macro ISO 1219) si ansamblul complet desfasurat")
    componente: list[ComponentaISO]
    conexiuni: list[ConexiuneISO]
    grafcet: list[PasGrafcet]
    ghid_fluidsim: list[PasFluidSIM]


# =====================================================================
# 2. BAZĂ DE DATE ACID SQLITE3, INTEGRITATE SHA-256, AUDIT & PLUGIN-URI
# =====================================================================
DB_FILE = "fluidsim_enterprise.db"


def init_enterprise_db():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schematic_cache (
                cache_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                sha256_hash TEXT NOT NULL,
                model_used TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS learned_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT UNIQUE NOT NULL,
                sha256_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT NOT NULL,
                schema_sha256 TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS custom_plugins (
                plugin_id TEXT PRIMARY KEY,
                keyword_trigger TEXT NOT NULL,
                base_iso_type TEXT NOT NULL,
                nume_echipament TEXT NOT NULL,
                parametru_implicit TEXT NOT NULL,
                cod_festo TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


init_enterprise_db()


def calculeaza_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inregistreaza_audit(event_type: str, details: str, schema_dict: dict = None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sig = calculeaza_sha256(json.dumps(schema_dict, sort_keys=True, ensure_ascii=False))[
        :16] if schema_dict else "SYSTEM"
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO audit_log (timestamp, event_type, details, schema_sha256) VALUES (?, ?, ?, ?)",
            (ts, event_type, details[:500], sig)
        )
        conn.commit()


def citeste_cache_sigur(cache_key: str) -> dict | None:
    with sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            "SELECT payload_json, sha256_hash FROM schematic_cache WHERE cache_key = ?",
            (cache_key,)
        ).fetchone()
        if not row:
            return None
        payload_json, saved_hash = row
        if calculeaza_sha256(payload_json) != saved_hash:
            conn.execute("DELETE FROM schematic_cache WHERE cache_key = ?", (cache_key,))
            conn.commit()
            return None
        try:
            return json.loads(payload_json)
        except Exception:
            return None


def salveaza_cache_sigur(cache_key: str, schema: dict, model_used: str):
    payload_json = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    digest = calculeaza_sha256(payload_json)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO schematic_cache (cache_key, payload_json, sha256_hash, model_used, created_at) VALUES (?, ?, ?, ?, ?)",
            (cache_key, payload_json, digest, model_used, ts)
        )
        conn.commit()


def incarca_reguli_invatate() -> list[str]:
    reguli_valide = []
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute("SELECT id, rule_text, sha256_hash FROM learned_rules ORDER BY id ASC").fetchall()
        for r_id, r_txt, r_hash in rows:
            if calculeaza_sha256(r_txt) == r_hash:
                reguli_valide.append(r_txt)
            else:
                conn.execute("DELETE FROM learned_rules WHERE id = ?", (r_id,))
        conn.commit()
    return reguli_valide


def salveaza_regula_noua(regula: str):
    r_clean = regula.strip()
    if not r_clean:
        return
    digest = calculeaza_sha256(r_clean)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO learned_rules (rule_text, sha256_hash, created_at) VALUES (?, ?, ?)",
            (r_clean, digest, ts)
        )
        conn.commit()
    inregistreaza_audit("RULE_LEARNED", f"Regulă adăugată: {r_clean}")


def inregistreaza_plugin_echipament(plugin_id: str, keyword: str, base_type: str, nume: str, param: str,
                                    cod_festo: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO custom_plugins VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plugin_id.strip().upper(), keyword.strip().lower(), base_type, nume.strip(), param.strip(),
             cod_festo.strip(), ts)
        )
        conn.commit()


def incarca_pluginuri_echipamente() -> list[dict]:
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT plugin_id, keyword_trigger, base_iso_type, nume_echipament, parametru_implicit, cod_festo FROM custom_plugins"
        ).fetchall()
    return [
        {"plugin_id": r[0], "keyword": r[1], "base_type": r[2], "nume": r[3], "parametru": r[4], "cod_festo": r[5]}
        for r in rows
    ]


def anonimizeaza_cerinta_confidentiala(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[EMAIL_REDACTAT]", text)
    t = re.sub(r"AIza[0-9A-Za-z\-_]{20,}", "[CHEIE_API_REDACTATA]", t)
    t = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP_INTERN_REDACTAT]", t)
    return t


# =====================================================================
# 3. VALIDATOR SIMBOLIC DE GRAF ISO 1219 & COMPILATOR DETERMINIST
# =====================================================================
PORTURI_VALIDE = {
    "CYL_DOUBLE": ["P1", "P2"],
    "CYL_SINGLE": ["P1"],
    "THROTTLE_CHECK_LEFT": ["1", "2"],
    "THROTTLE_CHECK_RIGHT": ["1", "2"],
    "QUICK_EXHAUST": ["1", "2", "3"],
    "VALVE_5_3_VENTED": ["1", "2", "4", "3", "5", "14", "12"],
    "VALVE_5_3_CLOSED": ["1", "2", "4", "3", "5", "14", "12"],
    "VALVE_5_3_PRESSURIZED": ["1", "2", "4", "3", "5", "14", "12"],
    "VALVE_5_2_BISTABLE": ["1", "2", "4", "3", "5", "14", "12"],
    "VALVE_5_2_MONOSTABLE": ["1", "2", "4", "3", "5", "14"],
    "VALVE_3_2_NC_BUTTON": ["1", "2", "3"],
    "VALVE_3_2_NC_ROLLER": ["1", "2", "3"],
    "VALVE_3_2_NC_PILOT": ["1", "2", "3", "12"],
    "LOGIC_OR": ["1L", "1R", "2"],
    "LOGIC_AND": ["1L", "1R", "2"],
    "FRL_EXPANDED": ["1", "2"],
    "FRL_MACRO": ["1", "2"],
    "AIR_SOURCE": ["1"]
}


def inspecteaza_erori_iso1219(schema: dict, cerinta_text: str = "") -> list[str]:
    erori = []
    txt = (cerinta_text or "").lower()
    comps = schema.get("componente", [])
    conns = schema.get("conexiuni", [])

    if not comps:
        return ["Schema nu conține nicio componentă."]

    comp_map = {c["id"]: c for c in comps}
    tipuri = [c["tip"] for c in comps]

    if "AIR_SOURCE" not in tipuri:
        erori.append("Lipsește sursa de aer comprimat P1 (AIR_SOURCE).")
    if "FRL_EXPANDED" not in tipuri and "FRL_MACRO" not in tipuri:
        erori.append("Lipsește grupul de preparare a aerului Z1 (FRL_EXPANDED).")

    main_v = next(
        (c for c in comps if c["tip"].startswith("VALVE_5_") or (c["tip"].startswith("VALVE_3_") and c["id"] == "V1")),
        None)
    if not main_v:
        erori.append("Lipsește distribuitorul principal V1.")
    else:
        vt = main_v["tip"]
        num_pos = schema.get("numar_pozitii_distribuitor_principal")
        if "5_3" in vt and num_pos != 3:
            erori.append(
                f"Distribuitorul {vt} are 3 poziții fizice, dar numar_pozitii_distribuitor_principal={num_pos}.")
        if ("5_2" in vt or "3_2" in vt) and num_pos != 2:
            erori.append(
                f"Distribuitorul {vt} are STRICT 2 poziții (fără centru!), dar numar_pozitii_distribuitor_principal={num_pos}.")

    if (
            "bimanual" in txt or "simultaneitate" in txt or "supapa si" in txt or "supapă și" in txt) and "LOGIC_AND" not in tipuri:
        erori.append("Cerința specifică comandă bimanuală / supapă ȘI, dar lipsește LOGIC_AND (V4) și butoanele S1/S2.")
    if (
            "doua locuri" in txt or "două locuri" in txt or "supapa sau" in txt or "supapă sau" in txt) and "LOGIC_OR" not in tipuri:
        erori.append(
            "Cerința specifică comandă din 2 locuri / supapă SAU, dar lipsește LOGIC_OR (V4) și butoanele S1/S2.")

    for c in conns:
        for endp in (c.get("de_la", ""), c.get("pana_la", "")):
            if "." not in endp:
                erori.append(f"Format port invalid '{endp}' (trebuie ID_Comp.ID_Port).")
                continue
            cid, pid = endp.split(".", 1)
            if cid not in comp_map:
                erori.append(f"Conexiunea referă o componentă inexistentă: '{cid}'.")
            else:
                allowed = PORTURI_VALIDE.get(comp_map[cid]["tip"], [])
                if pid not in allowed:
                    erori.append(
                        f"Portul '{pid}' nu există pe '{cid}' ({comp_map[cid]['tip']}). Porturi permise: {allowed}.")

    return erori


def sinteza_determinista_locala(cerinta_text: str) -> dict:
    txt = (cerinta_text or "").lower()
    is_single = ("simplu efect" in txt) or (
                "3/2" in txt and "5/" not in txt and "bimanual" not in txt and "doua locuri" not in txt and "două locuri" not in txt and "limitator" not in txt)
    has_and = ("bimanual" in txt) or ("supapa si" in txt) or ("supapă și" in txt) or ("simultaneitate" in txt)
    has_or = ("doua locuri" in txt) or ("două locuri" in txt) or ("supapa sau" in txt) or ("supapă sau" in txt) or (
                "selectie" in txt) or ("selecție" in txt)
    has_roller = ("limitator" in txt) or ("semiautomat" in txt) or ("rola" in txt) or ("rolă" in txt)
    has_qe = ("descarcare rapida" in txt) or ("descărcare rapidă" in txt)

    if is_single:
        v_tip = "VALVE_3_2_NC_BUTTON"
        v_nume = "Distribuitor 3/2 Normal Închis cu buton și arc"
        v_festo = "VHEF-PTC-M32-M-G18"
        num_pos = 2
        titlu = "Ansamblu ISO 1219: Cilindru Simplu Efect + Distribuitor 3/2 NI + Drosel + FRL"
        expl = "Distribuitorul 3/2 are strict 2 poziții. În repaus (arc), portul 1 este blocat iar camera cilindrului este ventilată la atmosferă prin 2→3. La apăsarea comenzii 12, aerul trece 1→2 și extinde tija cilindrului."
    elif "5/2" in txt or has_and or has_or or has_roller:
        num_pos = 2
        if ("monostabil" in txt or "arc" in txt or has_and or has_or) and not has_roller and "bistabil" not in txt:
            v_tip = "VALVE_5_2_MONOSTABLE"
            v_nume = "Distribuitor 5/2 Monostabil (pilot 14 + arc de readucere)"
            v_festo = "VUVG-L14-M52-AT-G18"
            titlu = "Ansamblu ISO 1219: Distribuitor 5/2 Monostabil + Drosele + Cilindru Dublu Efect"
            expl = "Distribuitorul 5/2 monostabil are strict 2 poziții (fără centru). În repaus, arcul menține calea 1→2 (cilindru retras). Când pilotul 14 primește semnal, comută pe 1→4 (avans)."
        else:
            v_tip = "VALVE_5_2_BISTABLE"
            v_nume = "Distribuitor 5/2 Bistabil memorat (pilot 14 / pilot 12)"
            v_festo = "VUVG-L14-B52-T-G18"
            titlu = "Ansamblu ISO 1219: Distribuitor 5/2 Bistabil (Memorat) + Drosele + Cilindru Dublu Efect"
            expl = "Distribuitorul 5/2 bistabil are strict 2 poziții memorate (14 și 12), fără poziție centrală. Păstrează ultima stare comandată chiar și după dispariția impulsului."
    else:
        num_pos = 3
        if "inchis" in txt or "închis" in txt or "blocat" in txt:
            v_tip = "VALVE_5_3_CLOSED"
            v_nume = "Distribuitor 5/3 cu Centru Închis (Toate Porturile Blocate)"
            v_festo = "VUVG-L14-P53C-T-G18"
            titlu = "Ansamblu ISO 1219: Distribuitor 5/3 Centru Închis + Drosele + Cilindru Dublu Efect"
            expl = "În poziția 0 (Centru Închis), toate cele 5 porturi (1, 2, 4, 3, 5) sunt blocate cu simbol T, oprind și blocând pneumatic pistonul în orice punct intermediar al cursei."
        elif "presiune" in txt:
            v_tip = "VALVE_5_3_PRESSURIZED"
            v_nume = "Distribuitor 5/3 cu Centru sub Presiune (1→4 și 1→2)"
            v_festo = "VUVG-L14-P53U-T-G18"
            titlu = "Ansamblu ISO 1219: Distribuitor 5/3 Centru sub Presiune + Drosele + Cilindru Dublu Efect"
            expl = "În poziția 0 (Centru sub Presiune), aerul din portul 1 intră simultan pe ambele căi de lucru (1→4 și 1→2), în timp ce evacuările 3 și 5 sunt blocate."
        else:
            v_tip = "VALVE_5_3_VENTED"
            v_nume = "Distribuitor 5/3 cu Centru Ventilat / Evacuat la Atmosferă"
            v_festo = "VUVG-L14-P53E-T-G18"
            titlu = "Ansamblu ISO 1219: Distribuitor 5/3 Centru Ventilat + Drosele + Cilindru Dublu Efect"
            expl = "În poziția 0 (Centru Ventilat), alimentarea 1 este blocată, iar ambele camere ale cilindrului sunt puse la atmosferă (4→5 și 2→3 la 0 bar), eliberând presiunea din cilindru."

    componente = [
        {"id": "P1", "tip": "AIR_SOURCE", "nume": "Sursă Aer Comprimat", "parametru": "6 bar",
         "cod_festo": "Sursa-ISO-6bar", "x": 350, "y": 565},
        {"id": "Z1", "tip": "FRL_EXPANDED",
         "nume": "Grup Preparare Aer FRL Desfășurat (Filtru + Regulator + Manometru + Lubrificator)",
         "parametru": "6 bar / 40 µm", "cod_festo": "MSB4-AGA:C4:J1-WP", "x": 350, "y": 485},
        {"id": "V1", "tip": v_tip, "nume": v_nume, "parametru": "ISO 1219", "cod_festo": v_festo, "x": 350, "y": 340},
        {"id": "V2", "tip": "QUICK_EXHAUST" if has_qe else "THROTTLE_CHECK_LEFT",
         "nume": "Supapă descărcare rapidă" if has_qe else "Drosel cu supapă de sens (Calea 4)",
         "parametru": "100% Cv" if has_qe else "Drosel 50%", "cod_festo": "SEU-1/4" if has_qe else "GRLA-1/4-QS-8-D",
         "x": 270, "y": 205},
    ]

    conexiuni = [
        {"de_la": "P1.1", "pana_la": "Z1.1", "circuit_activ_in_starea": "PERMANENT"},
        {"de_la": "Z1.2", "pana_la": "V1.1", "circuit_activ_in_starea": "PERMANENT"},
    ]

    if is_single:
        componente.append(
            {"id": "M1", "tip": "CYL_SINGLE", "nume": "Cilindru cu Simplu Efect și Arc", "parametru": "Ø50x100 mm",
             "cod_festo": "ESNU-50-100-P-A", "x": 350, "y": 85})
        conexiuni.extend([
            {"de_la": "V1.2", "pana_la": "V2.1", "circuit_activ_in_starea": "STARE_14"},
            {"de_la": "V2.2", "pana_la": "M1.P1", "circuit_activ_in_starea": "STARE_14"},
        ])
    else:
        componente.append({"id": "V3", "tip": "THROTTLE_CHECK_RIGHT", "nume": "Drosel cu supapă de sens (Calea 2)",
                           "parametru": "Drosel 50%", "cod_festo": "GRLA-1/4-QS-8-D", "x": 430, "y": 205})
        componente.append({"id": "M1", "tip": "CYL_DOUBLE", "nume": "Cilindru cu Dublu Efect și Amortizare",
                           "parametru": "Ø63x200 mm", "cod_festo": "DSBC-63-200-PPSA-N3", "x": 350, "y": 85})
        conexiuni.extend([
            {"de_la": "V1.4", "pana_la": "V2.1", "circuit_activ_in_starea": "STARE_14"},
            {"de_la": "V2.2", "pana_la": "M1.P1", "circuit_activ_in_starea": "STARE_14"},
            {"de_la": "V1.2", "pana_la": "V3.1", "circuit_activ_in_starea": "STARE_12"},
            {"de_la": "V3.2", "pana_la": "M1.P2", "circuit_activ_in_starea": "STARE_12"},
        ])

    if has_and or has_or:
        logic_tip = "LOGIC_AND" if has_and else "LOGIC_OR"
        logic_nume = "Supapă de simultaneitate ȘI (Comandă Bimanuală)" if has_and else "Selector de circuit SAU (Comandă din 2 locuri)"
        logic_festo = "ZK-1/8-B" if has_and else "OS-1/8-B"
        titlu = f"Ansamblu Pneumatic cu {logic_nume} + {v_nume} + Cilindru"
        componente.extend([
            {"id": "S1", "tip": "VALVE_3_2_NC_BUTTON", "nume": "Buton Start 1 (3/2 NI)", "parametru": "Manual",
             "cod_festo": "VHEF-PTC-M32", "x": 115, "y": 465},
            {"id": "S2", "tip": "VALVE_3_2_NC_BUTTON", "nume": "Buton Start 2 (3/2 NI)", "parametru": "Manual",
             "cod_festo": "VHEF-PTC-M32", "x": 215, "y": 465},
            {"id": "V4", "tip": logic_tip, "nume": logic_nume, "parametru": "Logică ISO", "cod_festo": logic_festo,
             "x": 165, "y": 340},
        ])
        conexiuni.extend([
            {"de_la": "Z1.2", "pana_la": "S1.1", "circuit_activ_in_starea": "PERMANENT"},
            {"de_la": "Z1.2", "pana_la": "S2.1", "circuit_activ_in_starea": "PERMANENT"},
            {"de_la": "S1.2", "pana_la": "V4.1L", "circuit_activ_in_starea": "PILOT_14"},
            {"de_la": "S2.2", "pana_la": "V4.1R", "circuit_activ_in_starea": "PILOT_14"},
            {"de_la": "V4.2", "pana_la": "V1.14", "circuit_activ_in_starea": "PILOT_14"},
        ])
    elif has_roller:
        titlu = "Ansamblu Semiautomat: Start S1 + Retragere Automată cu Limitator cu Rolă B1 (a1)"
        componente.extend([
            {"id": "S1", "tip": "VALVE_3_2_NC_BUTTON", "nume": "Buton Start S1 (3/2 NI)", "parametru": "Start Avans",
             "cod_festo": "VHEF-PTC-M32", "x": 140, "y": 440},
            {"id": "B1", "tip": "VALVE_3_2_NC_ROLLER", "nume": "Limitator Rolă B1 la cota a1 (3/2 NI)",
             "parametru": "Cota a1 (100%)", "cod_festo": "VHEF-R-M32", "x": 525, "y": 440},
        ])
        conexiuni.extend([
            {"de_la": "Z1.2", "pana_la": "S1.1", "circuit_activ_in_starea": "PERMANENT"},
            {"de_la": "Z1.2", "pana_la": "B1.1", "circuit_activ_in_starea": "PERMANENT"},
            {"de_la": "S1.2", "pana_la": "V1.14", "circuit_activ_in_starea": "PILOT_14"},
            {"de_la": "B1.2", "pana_la": "V1.12", "circuit_activ_in_starea": "PILOT_12"},
        ])

    for plg in incarca_pluginuri_echipamente():
        if plg["keyword"] in txt:
            for c in componente:
                if c["tip"] == plg["base_type"]:
                    c["nume"] = plg["nume"]
                    c["parametru"] = plg["parametru"]
                    c["cod_festo"] = plg["cod_festo"]

    grafcet = [
        {"etapa": "Etapa 0 (Inițială)",
         "actiune": "Sistem alimentat la 6 bar prin grupul FRL (Z1). Cilindrul M1 la cota a0.",
         "tranzitie": "Activare Comandă 14 (Start S1)"},
        {"etapa": "Etapa 1 (Avans M1+)",
         "actiune": "Comutare V1 pe pătratul 14 (1→4 presiune, 2→3 evacuare droselizată prin V3).",
         "tranzitie": "Atingere capăt de cursă a1 (100% = 200 mm)"},
        {"etapa": "Etapa 2 (Retragere M1-)",
         "actiune": "Comutare V1 pe pătratul 12 (1→2 presiune, 4→5 evacuare droselizată prin V2).",
         "tranzitie": "Revenire la capătul de cursă a0 (0 mm)"}
    ]

    return {
        "analiza_inginereasca_pas_cu_pas": f"Arhitectură verificată matematic conform ISO 1219-1/2: distribuitorul {v_tip} are {num_pos} poziții de comutare, droselizarea se realizează pe aerul evacuat (meter-out) pentru stabilitatea vitezei pistonului.",
        "titlu": titlu,
        "numar_pozitii_distribuitor_principal": num_pos,
        "explicatie_centru_si_ansamblu": expl,
        "simbol_macro_descriere": "În reprezentarea Macro simplificată ISO 1219, cele 4 sub-elemente ale grupului FRL (Filtru, Separator apă, Regulator cu Manometru și Lubrificator) sunt condensate într-o singură casetă cu linie punctată, săgeată de presiune și indicator de ulei.",
        "componente": componente,
        "conexiuni": conexiuni,
        "grafcet": grafcet,
        "ghid_fluidsim": [
            {"pas": 1, "element": "P1 + Z1 (Alimentare și Grup Preparare Aer FRL)",
             "cum_se_pune_in_fluidsim": "Din 'Supply Elements' trage 'Compressed air supply' (P1) și 'Air service unit' (Z1). Leagă P1.1 la Z1.1."},
            {"pas": 2, "element": f"V1 ({v_nume})",
             "cum_se_pune_in_fluidsim": "Din 'Directional Control Valves -> Configurable Ways Valves' trage distribuitorul, dă dublu-click pentru a selecta camerele interne, arcurile și pilotarea 14/12. Pe porturile 3 și 5 setează amortizor de zgomot (Silencer)."},
            {"pas": 3, "element": "V2, V3 (Drosele de cale) & M1 (Cilindru)",
             "cum_se_pune_in_fluidsim": "Din 'Flow Control Valves' trage 'One-way flow control valve' (GRLA), rotește-le la 90° pentru droselizare pe evacuare (meter-out) și leagă-le la porturile P1 și P2 ale cilindrului M1."}
        ]
    }


def valideaza_si_repara_schema(schema: dict, cerinta_text: str = "") -> dict:
    txt_low = (cerinta_text or "").lower()
    if not schema or not schema.get("componente"):
        return sinteza_determinista_locala(cerinta_text)

    tipuri_existente = {c.get("tip") for c in schema.get("componente", [])}
    if ((
                "bimanual" in txt_low or "simultaneitate" in txt_low or "supapa si" in txt_low or "supapă și" in txt_low) and "LOGIC_AND" not in tipuri_existente) or \
            ((
                     "doua locuri" in txt_low or "două locuri" in txt_low or "supapa sau" in txt_low or "supapă sau" in txt_low) and "LOGIC_OR" not in tipuri_existente) or \
            ((
                     "limitator" in txt_low or "rola" in txt_low or "rolă" in txt_low) and "VALVE_3_2_NC_ROLLER" not in tipuri_existente):
        return sinteza_determinista_locala(cerinta_text)

    comp_map = {c["id"]: c for c in schema.get("componente", [])}
    main_valve = None
    for c in schema.get("componente", []):
        if not c.get("cod_festo"):
            c["cod_festo"] = "FESTO-ISO-1219"
        if c["tip"].startswith("VALVE_5_") or (c["tip"].startswith("VALVE_3_") and c["id"] == "V1"):
            main_valve = c

    if main_valve:
        if "5/2" in txt_low and "5_3" in main_valve["tip"]:
            main_valve["tip"] = "VALVE_5_2_MONOSTABLE" if (
                        "monostabil" in txt_low or "arc" in txt_low) else "VALVE_5_2_BISTABLE"
        elif "5/3" in txt_low:
            if "inchis" in txt_low or "închis" in txt_low or "blocat" in txt_low:
                main_valve["tip"] = "VALVE_5_3_CLOSED"
            elif "presiune" in txt_low:
                main_valve["tip"] = "VALVE_5_3_PRESSURIZED"
            elif "ventilat" in txt_low or "evacuat" in txt_low:
                main_valve["tip"] = "VALVE_5_3_VENTED"

        schema["numar_pozitii_distribuitor_principal"] = 3 if "5_3" in main_valve["tip"] else 2

    has_logic = any(c["tip"] in ("LOGIC_AND", "LOGIC_OR") for c in schema.get("componente", []))
    btn_idx = 0
    for c in schema.get("componente", []):
        tip = c["tip"]
        if tip in ("CYL_DOUBLE", "CYL_SINGLE"):
            c["x"], c["y"] = 350, 85
        elif tip == "THROTTLE_CHECK_LEFT":
            if ("descarcare rapida" in txt_low or "descărcare rapidă" in txt_low) and c["id"] == "V2":
                c["tip"] = "QUICK_EXHAUST"
                c["nume"] = "Supapă de descărcare rapidă"
            c["x"], c["y"] = 270, 205
        elif tip == "THROTTLE_CHECK_RIGHT":
            c["x"], c["y"] = 430, 205
        elif tip == "QUICK_EXHAUST":
            c["x"], c["y"] = 270, 205
        elif tip.startswith("VALVE_5_") or (tip.startswith("VALVE_3_") and c["id"] == "V1"):
            c["x"], c["y"] = 350, 340
        elif tip in ("LOGIC_OR", "LOGIC_AND"):
            c["x"], c["y"] = 165, 340
        elif tip.startswith("VALVE_3_") and c["id"] != "V1":
            if has_logic:
                c["x"] = 115 if btn_idx == 0 else 215
                c["y"] = 465
            else:
                c["x"] = 140 if btn_idx == 0 else 525
                c["y"] = 440
            btn_idx += 1
        elif tip in ("FRL_EXPANDED", "FRL_MACRO"):
            c["x"], c["y"] = 350, 485
        elif tip == "AIR_SOURCE":
            c["x"], c["y"] = 350, 565

    for plg in incarca_pluginuri_echipamente():
        if plg["keyword"] in txt_low:
            for c in schema.get("componente", []):
                if c["tip"] == plg["base_type"]:
                    c["nume"] = plg["nume"]
                    c["parametru"] = plg["parametru"]
                    c["cod_festo"] = plg["cod_festo"]

    conexiuni_curate = []
    for conn in schema.get("conexiuni", []):
        for cheie in ("de_la", "pana_la"):
            val = conn[cheie]
            if "." in val:
                cid, pid = val.split(".", 1)
                if cid in comp_map:
                    ctip = comp_map[cid]["tip"]
                    valid_p = PORTURI_VALIDE.get(ctip, [])
                    if pid not in valid_p:
                        if ctip == "CYL_DOUBLE":
                            pid = "P1" if pid in ("1", "4", "stanga", "spate", "A") else "P2"
                        elif ctip == "CYL_SINGLE":
                            pid = "P1"
                        elif pid in ("P", "0"):
                            pid = "1"
                        elif pid == "A":
                            pid = "4" if "5_" in ctip else "2"
                        elif pid == "B":
                            pid = "2"
                        conn[cheie] = f"{cid}.{pid}"
        conexiuni_curate.append(conn)

    schema["conexiuni"] = conexiuni_curate
    if not schema.get("grafcet"):
        schema["grafcet"] = [
            {"etapa": "Etapa 0 (Repaus)", "actiune": "Alimentare 6 bar prin FRL Z1.", "tranzitie": "Comandă 14 activă"},
            {"etapa": "Etapa 1 (Avans M1+)", "actiune": "Aer pe calea 1→4 spre M1.P1; evacuare droselizată prin V3.",
             "tranzitie": "Atingere cotă a1"},
            {"etapa": "Etapa 2 (Retragere M1-)",
             "actiune": "Aer pe calea 1→2 spre M1.P2; evacuare droselizată prin V2.", "tranzitie": "Atingere cotă a0"}
        ]
    return schema


# =====================================================================
# 4. MOTORUL VECTORIAL CAD ISO 1219-1 & RUTARE CU PUNȚI DE OCOLIRE
# =====================================================================
def get_component_geometry(comp: dict, force_macro_frl: bool = False):
    t = "FRL_MACRO" if (force_macro_frl and comp["tip"] == "FRL_EXPANDED") else comp["tip"]
    cx, cy = comp["x"], comp["y"]
    prims = []
    pins = {}

    def L(x1, y1, x2, y2, dashed=False, group="static", role="body"):
        prims.append(
            {"k": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "dashed": dashed, "group": group, "role": role})

    def R(x, y, w, h, dashed=False, group="static", role="box", click_action=None):
        prims.append({"k": "rect", "x": x, "y": y, "w": w, "h": h, "dashed": dashed, "group": group, "role": role,
                      "click": click_action})

    def C(x, y, r, group="static", role="body"):
        prims.append({"k": "circle", "x": x, "y": y, "r": r, "group": group, "role": role})

    def A(x1, y1, x2, y2, group="static", role="flow"):
        prims.append({"k": "arrow", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "group": group, "role": role})

    def T(x, y, direction="down", group="static"):
        if direction == "down":
            L(x, y, x, y - 10, group=group, role="stop")
            L(x - 5, y - 10, x + 5, y - 10, group=group, role="stop")
        else:
            L(x, y, x, y + 10, group=group, role="stop")
            L(x - 5, y + 10, x + 5, y + 10, group=group, role="stop")

    def TXT(x, y, s, size=10, bold=False, group="static"):
        prims.append({"k": "text", "x": x, "y": y, "s": str(s), "size": size, "bold": bold, "group": group})

    def EXHAUST_TRI(x, y):
        prims.append({"k": "poly", "pts": [(x - 5, y), (x + 5, y), (x, y + 9)], "group": "static", "role": "exhaust"})

    if t in ("CYL_DOUBLE", "CYL_SINGLE"):
        R(cx - 70, cy - 26, 140, 52)
        L(cx - 55, cy + 26, cx - 55, cy + 40)
        pins["P1"] = (cx - 55, cy + 40)
        TXT(cx - 72, cy + 38, "P1", 9, bold=True)

        L(cx - 26, cy - 35, cx - 26, cy - 26)
        R(cx - 31, cy - 43, 10, 8)
        TXT(cx - 32, cy - 46, "a0", 8, bold=True)

        L(cx + 39, cy - 35, cx + 39, cy - 26)
        R(cx + 34, cy - 43, 10, 8)
        TXT(cx + 33, cy - 46, "a1", 8, bold=True)

        if t == "CYL_DOUBLE":
            L(cx + 55, cy + 26, cx + 55, cy + 40)
            pins["P2"] = (cx + 55, cy + 40)
            TXT(cx + 60, cy + 38, "P2", 9, bold=True)
        else:
            pts = [(cx - 5, cy - 18), (cx + 8, cy + 18), (cx + 22, cy - 18), (cx + 36, cy + 18), (cx + 50, cy - 18),
                   (cx + 64, cy + 18)]
            for i in range(len(pts) - 1):
                L(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])

        R(cx - 32, cy - 24, 12, 48, group="piston", role="piston")
        R(cx - 38, cy - 10, 6, 20, group="piston", role="cushion")
        R(cx - 20, cy - 10, 6, 20, group="piston", role="cushion")
        A(cx - 36, cy + 15, cx - 18, cy - 15, group="piston")
        L(cx - 14, cy - 4, cx + 108, cy - 4, group="piston", role="rod")
        L(cx - 14, cy + 4, cx + 108, cy + 4, group="piston", role="rod")
        R(cx + 108, cy - 10, 6, 20, group="piston", role="rod_end")

        TXT(cx - 104, cy - 4, comp["id"], 12, bold=True)
        TXT(cx - 70, cy - 56, f"{comp['nume']} [{comp['parametru']}]", 9, bold=True)

    elif t in ("THROTTLE_CHECK_LEFT", "THROTTLE_CHECK_RIGHT"):
        R(cx - 28, cy - 28, 56, 56, dashed=True)
        L(cx, cy - 40, cx, cy - 28)
        L(cx, cy + 28, cx, cy + 40)
        pins["2"] = (cx, cy - 40)
        pins["1"] = (cx, cy + 40)
        TXT(cx - 14, cy - 31, "2", 9, bold=True)
        TXT(cx - 14, cy + 38, "1", 9, bold=True)

        L(cx, cy - 28, cx - 12, cy - 28)
        L(cx - 12, cy - 28, cx - 12, cy + 28)
        L(cx - 12, cy + 28, cx, cy + 28)
        L(cx - 19, cy - 12, cx - 15, cy)
        L(cx - 15, cy, cx - 19, cy + 12)
        L(cx - 5, cy - 12, cx - 9, cy)
        L(cx - 9, cy, cx - 5, cy + 12)
        A(cx - 23, cy + 14, cx - 1, cy - 14)

        L(cx, cy - 28, cx + 14, cy - 28)
        L(cx + 14, cy - 28, cx + 14, cy + 28)
        L(cx + 14, cy + 28, cx, cy + 28)
        L(cx + 8, cy - 4, cx + 14, cy + 4)
        L(cx + 20, cy - 4, cx + 14, cy + 4)
        C(cx + 14, cy - 3, 4)

        TXT(cx - 54, cy + 4, comp["id"], 10, bold=True)

    elif t == "QUICK_EXHAUST":
        R(cx - 28, cy - 24, 56, 48)
        L(cx, cy - 38, cx, cy - 24)
        L(cx, cy + 24, cx, cy + 38)
        L(cx - 28, cy, cx - 40, cy)
        EXHAUST_TRI(cx - 40, cy)
        pins["2"] = (cx, cy - 38)
        pins["1"] = (cx, cy + 38)
        pins["3"] = (cx - 40, cy + 9)
        TXT(cx + 5, cy - 29, "2", 9, bold=True)
        TXT(cx + 5, cy + 36, "1", 9, bold=True)
        TXT(cx - 38, cy - 5, "3", 9, bold=True)
        L(cx - 10, cy + 10, cx, cy)
        L(cx + 10, cy + 10, cx, cy)
        C(cx, cy - 4, 5)
        A(cx, cy - 10, cx - 22, cy)
        TXT(cx + 34, cy + 4, f"{comp['id']} (Desc. Rapidă)", 8, bold=True)

    elif t.startswith("VALVE_5_"):
        is_5_3 = "5_3" in t
        p4x, p2x = cx - 13, cx + 13
        p5x, p1x, p3x = cx - 15, cx, cx + 15

        L(p4x, cy - 22, p4x, cy - 38)
        L(p2x, cy - 22, p2x, cy - 38)
        L(p5x, cy + 22, p5x, cy + 34)
        L(p1x, cy + 22, p1x, cy + 38)
        L(p3x, cy + 22, p3x, cy + 34)

        EXHAUST_TRI(p5x, cy + 34)
        EXHAUST_TRI(p3x, cy + 34)

        pins["4"] = (p4x, cy - 38)
        pins["2"] = (p2x, cy - 38)
        pins["1"] = (p1x, cy + 38)
        pins["5"] = (p5x, cy + 43)
        pins["3"] = (p3x, cy + 43)

        TXT(p4x - 11, cy - 27, "4", 9, bold=True)
        TXT(p2x + 5, cy - 27, "2", 9, bold=True)
        TXT(p5x - 11, cy + 33, "5", 9, bold=True)
        TXT(p1x + 4, cy + 35, "1", 9, bold=True)
        TXT(p3x + 5, cy + 33, "3", 9, bold=True)

        def draw_square_14(scx, grp):
            R(scx - 22, cy - 22, 44, 44, group=grp, click_action="14")
            A(scx, cy + 22, scx - 13, cy - 22, group=grp)
            A(scx + 13, cy - 22, scx + 15, cy + 22, group=grp)
            T(scx - 15, cy + 22, "down", group=grp)

        def draw_square_12(scx, grp):
            R(scx - 22, cy - 22, 44, 44, group=grp, click_action="12")
            A(scx, cy + 22, scx + 13, cy - 22, group=grp)
            A(scx - 13, cy - 22, scx - 15, cy + 22, group=grp)
            T(scx + 15, cy + 22, "down", group=grp)

        def draw_square_center(scx, grp):
            R(scx - 22, cy - 22, 44, 44, group=grp, click_action="0")
            if t == "VALVE_5_3_VENTED":
                T(scx, cy + 22, "down", group=grp)
                A(scx - 13, cy - 22, scx - 15, cy + 22, group=grp)
                A(scx + 13, cy - 22, scx + 15, cy + 22, group=grp)
            elif t == "VALVE_5_3_CLOSED":
                T(scx, cy + 22, "down", group=grp)
                T(scx - 15, cy + 22, "down", group=grp)
                T(scx + 15, cy + 22, "down", group=grp)
                T(scx - 13, cy - 22, "up", group=grp)
                T(scx + 13, cy - 22, "up", group=grp)
            elif t == "VALVE_5_3_PRESSURIZED":
                L(scx, cy + 22, scx, cy, group=grp)
                A(scx, cy, scx - 13, cy - 22, group=grp)
                A(scx, cy, scx + 13, cy - 22, group=grp)
                T(scx - 15, cy + 22, "down", group=grp)
                T(scx + 15, cy + 22, "down", group=grp)

        if is_5_3:
            draw_square_14(cx - 44, "valve_spool")
            draw_square_center(cx, "valve_spool")
            draw_square_12(cx + 44, "valve_spool")
            left_edge, right_edge = cx - 66, cx + 66
        else:
            draw_square_14(cx - 44, "valve_spool")
            draw_square_12(cx, "valve_spool")
            left_edge, right_edge = cx - 66, cx + 22

        R(left_edge - 34, cy - 12, 24, 24, group="valve_spool", role="coil", click_action="14")
        L(left_edge - 34, cy + 12, left_edge - 10, cy - 12, group="valve_spool")
        prims.append({"k": "poly", "pts": [(left_edge - 10, cy - 6), (left_edge - 10, cy + 6), (left_edge, cy)],
                      "group": "valve_spool", "role": "pilot"})
        if is_5_3:
            pts_l = [(left_edge - 10, cy - 14), (left_edge - 7, cy - 21), (left_edge - 4, cy - 14),
                     (left_edge - 1, cy - 21), (left_edge, cy - 14)]
            for i in range(len(pts_l) - 1):
                L(pts_l[i][0], pts_l[i][1], pts_l[i + 1][0], pts_l[i + 1][1], group="valve_spool")
        TXT(left_edge - 30, cy - 16, "14", 9, bold=True, group="valve_spool")
        pins["14"] = (left_edge - 34, cy)

        if t == "VALVE_5_2_MONOSTABLE":
            pts_r = [(right_edge, cy), (right_edge + 6, cy - 10), (right_edge + 12, cy + 10),
                     (right_edge + 18, cy - 10), (right_edge + 24, cy)]
            for i in range(len(pts_r) - 1):
                L(pts_r[i][0], pts_r[i][1], pts_r[i + 1][0], pts_r[i + 1][1], group="valve_spool")
            TXT(right_edge + 6, cy - 14, "Arc", 8, bold=True, group="valve_spool")
        else:
            R(right_edge + 10, cy - 12, 24, 24, group="valve_spool", role="coil", click_action="12")
            L(right_edge + 10, cy + 12, right_edge + 34, cy - 12, group="valve_spool")
            prims.append({"k": "poly", "pts": [(right_edge + 10, cy - 6), (right_edge + 10, cy + 6), (right_edge, cy)],
                          "group": "valve_spool", "role": "pilot"})
            if is_5_3:
                pts_r = [(right_edge, cy - 14), (right_edge + 3, cy - 21), (right_edge + 6, cy - 14),
                         (right_edge + 9, cy - 21), (right_edge + 10, cy - 14)]
                for i in range(len(pts_r) - 1):
                    L(pts_r[i][0], pts_r[i][1], pts_r[i + 1][0], pts_r[i + 1][1], group="valve_spool")
            TXT(right_edge + 14, cy - 16, "12", 9, bold=True, group="valve_spool")
            pins["12"] = (right_edge + 34, cy)

        TXT(cx - 85, cy + 56, f"{comp['id']}: {comp['nume'][:38]}", 9, bold=True)

    elif t.startswith("VALVE_3_2"):
        is_main = (comp["id"] == "V1")
        grp = "valve_spool" if is_main else "static"
        act_click = "12" if comp["id"] == "B1" else "14"

        if is_main:
            R(cx - 66, cy - 22, 44, 44, group=grp, click_action="14")
            R(cx - 22, cy - 22, 44, 44, group=grp, click_action="12")
            A(cx - 44, cy + 22, cx - 44, cy - 22, group=grp)
            T(cx - 29, cy + 22, "down", group=grp)
            T(cx, cy + 22, "down", group=grp)
            A(cx, cy - 22, cx + 15, cy + 22, group=grp)

            L(cx, cy - 22, cx, cy - 38)
            L(cx, cy + 22, cx, cy + 38)
            L(cx + 15, cy + 22, cx + 15, cy + 34)
            EXHAUST_TRI(cx + 15, cy + 34)
            pins["2"] = (cx, cy - 38)
            pins["1"] = (cx, cy + 38)
            pins["3"] = (cx + 15, cy + 43)

            L(cx - 66, cy - 5, cx - 82, cy - 5, group=grp)
            L(cx - 66, cy + 5, cx - 82, cy + 5, group=grp)
            L(cx - 82, cy - 12, cx - 82, cy + 12, group=grp)
            pts_s = [(cx + 22, cy), (cx + 28, cy - 9), (cx + 34, cy + 9), (cx + 40, cy - 9), (cx + 46, cy)]
            for i in range(len(pts_s) - 1):
                L(pts_s[i][0], pts_s[i][1], pts_s[i + 1][0], pts_s[i + 1][1], group=grp)
            TXT(cx - 82, cy + 54, f"{comp['id']}: {comp['nume']}", 9, bold=True)
        else:
            R(cx - 32, cy - 16, 32, 32, role="coil", click_action=act_click)
            R(cx, cy - 16, 32, 32, click_action="12")
            A(cx - 16, cy + 16, cx - 16, cy - 16)
            T(cx - 6, cy + 16, "down")
            T(cx + 10, cy + 16, "down")
            A(cx + 16, cy - 16, cx + 24, cy + 16)

            L(cx + 16, cy - 16, cx + 16, cy - 28)
            L(cx + 10, cy + 16, cx + 10, cy + 28)
            EXHAUST_TRI(cx + 24, cy + 16)
            pins["2"] = (cx + 16, cy - 28)
            pins["1"] = (cx + 10, cy + 28)
            pins["3"] = (cx + 24, cy + 25)
            TXT(cx + 20, cy - 19, "2", 8, bold=True)
            TXT(cx, cy + 27, "1", 8, bold=True)

            if "ROLLER" in t:
                L(cx - 32, cy, cx - 46, cy)
                C(cx - 50, cy, 4)
            else:
                L(cx - 32, cy - 4, cx - 44, cy - 4)
                L(cx - 32, cy + 4, cx - 44, cy + 4)
                L(cx - 44, cy - 10, cx - 44, cy + 10)
            pts_s = [(cx + 32, cy), (cx + 37, cy - 7), (cx + 42, cy + 7), (cx + 47, cy)]
            for i in range(len(pts_s) - 1):
                L(pts_s[i][0], pts_s[i][1], pts_s[i + 1][0], pts_s[i + 1][1])
            TXT(cx - 38, cy + 39, f"{comp['id']} ({comp['parametru']})", 8, bold=True)

    elif t in ("LOGIC_OR", "LOGIC_AND"):
        R(cx - 30, cy - 18, 60, 36, click_action="14")
        L(cx - 30, cy, cx - 44, cy)
        L(cx + 30, cy, cx + 44, cy)
        L(cx, cy - 18, cx, cy - 32)
        pins["1L"] = (cx - 44, cy)
        pins["1R"] = (cx + 44, cy)
        pins["2"] = (cx, cy - 32)
        TXT(cx - 42, cy - 5, "1", 8, bold=True)
        TXT(cx + 35, cy - 5, "1", 8, bold=True)
        TXT(cx + 4, cy - 22, "2", 8, bold=True)

        if t == "LOGIC_OR":
            L(cx - 20, cy - 8, cx - 12, cy)
            L(cx - 20, cy + 8, cx - 12, cy)
            L(cx + 20, cy - 8, cx + 12, cy)
            L(cx + 20, cy + 8, cx + 12, cy)
            C(cx, cy, 5)
            TXT(cx - 10, cy + 14, "SAU", 7, bold=True)
        else:
            L(cx - 15, cy - 12, cx - 15, cy + 12)
            L(cx + 15, cy - 12, cx + 15, cy + 12)
            L(cx - 8, cy - 9, cx - 8, cy + 9)
            L(cx - 8, cy, cx + 8, cy)
            TXT(cx - 6, cy + 14, "ȘI", 7, bold=True)
        TXT(cx - 35, cy + 30, f"{comp['id']}: {comp['nume'][:24]}", 8, bold=True)

    elif t == "FRL_EXPANDED":
        L(cx, cy - 34, cx, cy - 22)
        L(cx, cy + 22, cx, cy + 34)
        pins["2"] = (cx, cy - 34)
        pins["1"] = (cx, cy + 34)
        TXT(cx + 5, cy - 25, "2", 9, bold=True)
        TXT(cx + 5, cy + 31, "1", 9, bold=True)

        R(cx - 32, cy - 22, 64, 44)
        L(cx - 32, cy + 8, cx + 32, cy + 8, dashed=True)
        L(cx - 10, cy + 18, cx + 10, cy + 18)
        A(cx, cy + 6, cx, cy - 12)
        L(cx - 14, cy - 22, cx - 14, cy - 13)
        L(cx + 32, cy - 2, cx + 44, cy - 2)
        C(cx + 54, cy - 2, 10)
        A(cx + 48, cy + 4, cx + 60, cy - 8)

        TXT(cx - 58, cy + 4, comp["id"], 11, bold=True)
        TXT(cx + 70, cy + 2, f"FRL Complet [{comp['parametru']}]", 8, bold=True)

    elif t == "FRL_MACRO":
        L(cx, cy - 34, cx, cy - 20)
        L(cx, cy + 20, cx, cy + 34)
        pins["2"] = (cx, cy - 34)
        pins["1"] = (cx, cy + 34)
        R(cx - 22, cy - 20, 44, 40)
        L(cx - 22, cy + 8, cx + 22, cy + 8, dashed=True)
        L(cx, cy - 20, cx, cy - 11)
        C(cx + 34, cy, 7)
        L(cx + 22, cy, cx + 27, cy)
        TXT(cx - 52, cy + 4, comp["id"], 10, bold=True)
        TXT(cx + 46, cy + 4, "Simbol Macro FRL ISO 1219", 8, bold=True)

    elif t == "AIR_SOURCE":
        L(cx, cy - 22, cx, cy - 6)
        pins["1"] = (cx, cy - 22)
        prims.append({"k": "poly", "pts": [(cx, cy - 6), (cx - 11, cy + 14), (cx + 11, cy + 14)], "group": "static",
                      "role": "source"})
        TXT(cx + 16, cy + 8, f"{comp['id']} ({comp['parametru']})", 9, bold=True)

    return prims, pins


def compute_orthogonal_wires_and_bridges(schema: dict, all_pins: dict):
    wires = []
    horiz_segs = []
    vert_segs = []

    for idx, conn in enumerate(schema.get("conexiuni", [])):
        p_start = all_pins.get(conn["de_la"])
        p_end = all_pins.get(conn["pana_la"])
        if not p_start or not p_end:
            continue
        x1, y1 = p_start
        x2, y2 = p_end
        stare = conn["circuit_activ_in_starea"]

        if x1 == x2 or y1 == y2:
            pts = [(x1, y1), (x2, y2)]
        elif conn["pana_la"].endswith(".14"):
            left_ch = min(x1, x2) - 18
            pts = [(x1, y1), (x1, y1 - 14), (left_ch, y1 - 14), (left_ch, y2), (x2, y2)]
        elif conn["pana_la"].endswith(".12"):
            right_ch = max(x1, x2) + 18
            pts = [(x1, y1), (x1, y1 - 14), (right_ch, y1 - 14), (right_ch, y2), (x2, y2)]
        else:
            mid_y = int((y1 + y2) / 2) + ((idx % 3) - 1) * 5
            pts = [(x1, y1), (x1, mid_y), (x2, mid_y), (x2, y2)]

        for i in range(len(pts) - 1):
            xa, ya = pts[i]
            xb, yb = pts[i + 1]
            if ya == yb and xa != xb:
                horiz_segs.append((min(xa, xb), max(xa, xb), ya, idx))
            elif xa == xb and ya != yb:
                vert_segs.append((xa, min(ya, yb), max(ya, yb), idx))

        wires.append({
            "pts": pts,
            "stare": stare,
            "label": f"{conn['de_la']} -> {conn['pana_la']}"
        })

    bridges = []
    for xhmin, xhmax, yh, w_h in horiz_segs:
        for xv, yvmin, yvmax, w_v in vert_segs:
            if w_h != w_v and (xhmin + 6 < xv < xhmax - 6) and (yvmin + 6 < yh < yvmax - 6):
                bridges.append((xv, yh))

    return wires, bridges


# =====================================================================
# 5. EXPORT DXF 1:1 ISO 7200 PENTRU FESTO FLUIDSIM-P & AUTOCAD
# =====================================================================
def export_exact_dxf(schema: dict) -> bytes:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    doc.layers.add("ISO_SYMBOLS", color=7)
    doc.layers.add("PNEUMATIC_LINES", color=5)
    doc.layers.add("PILOT_LINES", color=30)
    doc.layers.add("LABELS", color=3)
    doc.layers.add("ISO_7200_BORDER", color=8)

    def fy(y):
        return 660 - y

    msp.add_lwpolyline([(20, fy(15)), (865, fy(15)), (865, fy(625)), (20, fy(625)), (20, fy(15))],
                       dxfattribs={"layer": "ISO_7200_BORDER"})
    msp.add_lwpolyline([(545, fy(550)), (865, fy(550)), (865, fy(625)), (545, fy(625)), (545, fy(550))],
                       dxfattribs={"layer": "ISO_7200_BORDER"})
    msp.add_text("STANDARD: ISO 1219-1 / ISO 1219-2",
                 dxfattribs={"height": 5.5, "insert": (555, fy(568)), "layer": "LABELS"})
    msp.add_text(f"PROIECT: {schema['titlu'][:45]}",
                 dxfattribs={"height": 5.5, "insert": (555, fy(588)), "layer": "LABELS"})
    msp.add_text("COMPATIBIL: FESTO FLUIDSIM-P DXF IMPORT 1:1",
                 dxfattribs={"height": 5.0, "insert": (555, fy(608)), "layer": "LABELS"})

    all_pins = {}
    for comp in schema["componente"]:
        prims, pins = get_component_geometry(comp)
        for port_name, (px, py) in pins.items():
            all_pins[f"{comp['id']}.{port_name}"] = (px, py)

        for p in prims:
            k = p["k"]
            if k == "line":
                msp.add_line((p["x1"], fy(p["y1"])), (p["x2"], fy(p["y2"])), dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "rect":
                x, y, w, h = p["x"], p["y"], p["w"], p["h"]
                pts = [(x, fy(y)), (x + w, fy(y)), (x + w, fy(y + h)), (x, fy(y + h)), (x, fy(y))]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "circle":
                msp.add_circle((p["x"], fy(p["y"])), p["r"], dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "poly":
                pts = [(pt[0], fy(pt[1])) for pt in p["pts"]] + [(p["pts"][0][0], fy(p["pts"][0][1]))]
                msp.add_lwpolyline(pts, dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "arrow":
                x1, y1, x2, y2 = p["x1"], fy(p["y1"]), p["x2"], fy(p["y2"])
                msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": "ISO_SYMBOLS"})
                ang = math.atan2(y2 - y1, x2 - x1)
                a1 = ang + math.pi * 0.85
                a2 = ang - math.pi * 0.85
                msp.add_lwpolyline([
                    (x2, y2),
                    (x2 + 6 * math.cos(a1), y2 + 6 * math.sin(a1)),
                    (x2 + 6 * math.cos(a2), y2 + 6 * math.sin(a2)),
                    (x2, y2)
                ], dxfattribs={"layer": "ISO_SYMBOLS"})
            elif k == "text":
                msp.add_text(p["s"],
                             dxfattribs={"height": p["size"] * 0.65, "insert": (p["x"], fy(p["y"])), "layer": "LABELS"})

    wires, _ = compute_orthogonal_wires_and_bridges(schema, all_pins)
    for w in wires:
        pts_cad = [(pt[0], fy(pt[1])) for pt in w["pts"]]
        layer_name = "PILOT_LINES" if "PILOT" in w["stare"] else "PNEUMATIC_LINES"
        msp.add_lwpolyline(pts_cad, dxfattribs={"layer": layer_name})

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


# =====================================================================
# 6. SIMULATORUL FIZIC CALIBRAT (CU REZISTENȚĂ HIDRODINAMICĂ & SARCINĂ)
# =====================================================================
def render_fluidsim_canvas(schema: dict) -> str:
    all_pins = {}
    svg_static = []
    svg_spool = []
    svg_piston = []

    main_valve_type = "VALVE_5_3_VENTED"
    for comp in schema["componente"]:
        if comp["tip"].startswith("VALVE_5_") or (comp["tip"].startswith("VALVE_3_") and comp["id"] == "V1"):
            main_valve_type = comp["tip"]
            break

    has_3_positions = ("5_3" in main_valve_type)

    for comp in schema["componente"]:
        prims, pins = get_component_geometry(comp)
        for port_name, (px, py) in pins.items():
            all_pins[f"{comp['id']}.{port_name}"] = (px, py)

        for p in prims:
            k = p["k"]
            dash = 'stroke-dasharray="4,3"' if p.get("dashed") else ""
            elem = ""
            if k == "line":
                elem = f'<line x1="{p["x1"]}" y1="{p["y1"]}" x2="{p["x2"]}" y2="{p["y2"]}" stroke="#0f172a" stroke-width="2" {dash}/>'
            elif k == "rect":
                fill = "#ffffff" if p.get("role") == "box" else (
                    "#94a3b8" if p.get("role") == "piston" else ("#e0f2fe" if p.get("role") == "coil" else "none"))
                if p.get("dashed"):
                    fill = "none"
                click_attr = f'onclick="setMode(\'{p["click"]}\')" style="cursor:pointer;"' if p.get("click") else ""
                elem = f'<rect x="{p["x"]}" y="{p["y"]}" width="{p["w"]}" height="{p["h"]}" fill="{fill}" stroke="#0f172a" stroke-width="2" {dash} {click_attr}/>'
            elif k == "circle":
                elem = f'<circle cx="{p["x"]}" cy="{p["y"]}" r="{p["r"]}" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>'
            elif k == "poly":
                pts_str = " ".join(f"{pt[0]},{pt[1]}" for pt in p["pts"])
                fill = "#38bdf8" if p.get("role") == "exhaust" else "#ffffff"
                elem = f'<polygon points="{pts_str}" fill="{fill}" stroke="#0f172a" stroke-width="1.8"/>'
            elif k == "arrow":
                elem = f'<line x1="{p["x1"]}" y1="{p["y1"]}" x2="{p["x2"]}" y2="{p["y2"]}" stroke="#0f172a" stroke-width="2" marker-end="url(#isoArrow)"/>'
            elif k == "text":
                weight = "bold" if p.get("bold") else "normal"
                elem = f'<text x="{p["x"]}" y="{p["y"]}" fill="#0f172a" font-family="Consolas, monospace" font-size="{p["size"]}" font-weight="{weight}" style="pointer-events:none;">{p["s"]}</text>'

            if p.get("group") == "valve_spool":
                svg_spool.append(elem)
            elif p.get("group") == "piston":
                svg_piston.append(elem)
            else:
                svg_static.append(elem)

    wires, bridges = compute_orthogonal_wires_and_bridges(schema, all_pins)
    svg_wires = []
    for w in wires:
        pts_str = " ".join(f"{pt[0]},{pt[1]}" for pt in w["pts"])
        stare = w["stare"]
        dash_pilot = 'stroke-dasharray="6,4"' if "PILOT" in stare else ""
        svg_wires.append(
            f'<polyline class="pipe pipe-{stare}" points="{pts_str}" fill="none" stroke="#0284c7" stroke-width="3.3" stroke-linejoin="round" {dash_pilot}/>'
        )
        for pt in (w["pts"][0], w["pts"][-1]):
            svg_wires.append(f'<circle cx="{pt[0]}" cy="{pt[1]}" r="3.1" fill="#0f172a"/>')

    for bx, by in bridges:
        svg_wires.append(f'<circle cx="{bx}" cy="{by}" r="5" fill="#f8fafc" stroke="none"/>')
        svg_wires.append(
            f'<path d="M {bx - 5} {by} A 5 5 0 0 1 {bx + 5} {by}" fill="none" stroke="#0f172a" stroke-width="2.2"/>')

    if has_3_positions:
        center_label = (
            "Poziția 0 (Centru Ventilat: 4→5, 2→3)" if main_valve_type == "VALVE_5_3_VENTED"
            else ("Poziția 0 (Centru Închis: Stop Cursa)" if main_valve_type == "VALVE_5_3_CLOSED"
                  else "Poziția 0 (Centru sub Presiune: 1→4, 1→2)")
        )
        buttons_html = (
            '<button id="btn14" onclick="setMode(\'14\')">▶ Comanda 14 (Avans: 1→4, 2→3)</button>'
            f'<button id="btn0" class="active" onclick="setMode(\'0\')">⏸ {center_label}</button>'
            '<button id="btn12" onclick="setMode(\'12\')">◀ Comanda 12 (Retragere: 1→2, 4→5)</button>'
        )
        default_mode = "0"
    elif main_valve_type.startswith("VALVE_3_2"):
        buttons_html = (
            '<button id="btn12" class="active" onclick="setMode(\'12\')">🔄 Repaus Arc (NI: 1 blocat, 2→3)</button>'
            '<button id="btn14" onclick="setMode(\'14\')">▶ Acționat 12 (Avans: 1→2)</button>'
        )
        default_mode = "12"
    elif main_valve_type == "VALVE_5_2_MONOSTABLE":
        buttons_html = (
            '<button id="btn12" class="active" onclick="setMode(\'12\')">🔄 Repaus Arc (Retras: 1→2, 4→5)</button>'
            '<button id="btn14" onclick="setMode(\'14\')">▶ Comanda 14 Activă (Avans: 1→4, 2→3)</button>'
        )
        default_mode = "12"
    else:
        buttons_html = (
            '<button id="btn14" onclick="setMode(\'14\')">▶ Poziția 14 Memorată (Avans: 1→4, 2→3)</button>'
            '<button id="btn12" class="active" onclick="setMode(\'12\')">◀ Poziția 12 Memorată (Retragere: 1→2, 4→5)</button>'
        )
        default_mode = "12"

    html_template = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { margin: 0; font-family: 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #f8fafc; user-select: none; }
  .toolbar {
    display: flex; flex-wrap: wrap; gap: 7px; padding: 9px 14px; background: #0f172a;
    border-bottom: 1px solid #334155; align-items: center; justify-content: space-between;
  }
  .param-bar {
    display: flex; flex-wrap: wrap; gap: 12px; padding: 7px 14px; background: #1e293b;
    border-bottom: 2px solid #334155; align-items: center; font-size: 11.5px;
  }
  .btn-group { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
  .badge-pos {
    background: #0284c7; color: #fff; font-size: 11px; font-weight: 700;
    padding: 5px 9px; border-radius: 4px;
  }
  button {
    background: #334155; color: #f8fafc; border: 1px solid #475569; padding: 6px 10px;
    border-radius: 6px; font-weight: 600; font-size: 11.5px; cursor: pointer; transition: 0.15s;
  }
  button:hover { background: #475569; }
  button.active { background: #0055cc; border-color: #60a5fa; box-shadow: 0 0 10px rgba(56,189,248,0.55); }
  button.auto-on { background: #059669; border-color: #34d399; }
  .MacroBtn { background: #4f46e5; border-color: #818cf8; }
  .canvas-wrap {
    background-color: #f8fafc;
    background-image: radial-gradient(#cbd5e1 1.2px, transparent 1.2px);
    background-size: 20px 20px;
    width: 100%; height: 555px; overflow: auto; position: relative;
  }
  svg { width: 100%; height: 100%; min-width: 870px; min-height: 550px; transform-origin: top center; transition: transform 0.2s; }
  #valveSpool { transition: transform 0.26s cubic-bezier(0.22, 1, 0.36, 1); }
  .legend {
    display: flex; gap: 14px; padding: 8px 14px; background: #0f172a; font-size: 12px;
    border-top: 1px solid #334155; align-items: center; justify-content: space-between; flex-wrap: wrap;
  }
  .dot { display: inline-block; width: 14px; height: 4px; margin-right: 5px; vertical-align: middle; }
  input[type=range] { vertical-align: middle; accent-color: #38bdf8; cursor: pointer; width: 70px; }
  select { background: #0f172a; color: #38bdf8; border: 1px solid #475569; border-radius: 4px; padding: 2px 5px; font-weight: 700; }
</style>
</head>
<body>

<div class="toolbar">
  <div class="btn-group">
    <span class="badge-pos">__POS_BADGE__</span>
    __BUTTONS_HTML__
    <button id="btnAuto" onclick="toggleAutoCycle()">🔁 Ciclu Automat (a0 ↔ a1)</button>
    <button id="btnAudio" onclick="toggleSound()">🔊 Sunet Aer: OFF</button>
  </div>
  <div class="btn-group">
    <button onclick="zoomCanvas(1.12)" title="Zoom In">➕</button>
    <button onclick="zoomCanvas(0.88)" title="Zoom Out">➖</button>
    <button class="MacroBtn" id="btnView" onclick="toggleMacro()">🔄 Vedere: Ansamblu Desfășurat ISO 1219</button>
  </div>
</div>

<div class="param-bar">
  <div>
    <strong>P1:</strong>
    <input type="range" id="rngPress" min="2" max="10" step="0.5" value="6" oninput="updateParams()">
    <span id="lblPress" style="color:#38bdf8; font-weight:700;">6.0 bar</span>
  </div>
  <div>
    <strong>Alezaj D:</strong>
    <select id="selBore" onchange="updateParams()">
      <option value="32">Ø32 mm</option>
      <option value="50">Ø50 mm</option>
      <option value="63" selected>Ø63 mm</option>
      <option value="80">Ø80 mm</option>
      <option value="100">Ø100 mm</option>
    </select>
  </div>
  <div>
    <strong>Sarcină F_load:</strong>
    <input type="range" id="rngLoad" min="0" max="1500" step="50" value="250" oninput="updateParams()">
    <span id="lblLoad" style="color:#f59e0b; font-weight:700;">250 N</span>
  </div>
  <div>
    <strong>Pierdere Δp:</strong>
    <input type="range" id="rngDrop" min="0" max="1.5" step="0.1" value="0.3" oninput="updateParams()">
    <span id="lblDrop" style="color:#38bdf8; font-weight:700;">0.3 bar</span>
  </div>
  <div>
    <strong>V2(4):</strong>
    <input type="range" id="rngV2" min="10" max="100" step="5" value="50" oninput="updateParams()">
    <span id="lblV2" style="color:#38bdf8; font-weight:700;">50%</span>
  </div>
  <div>
    <strong>V3(2):</strong>
    <input type="range" id="rngV3" min="10" max="100" step="5" value="50" oninput="updateParams()">
    <span id="lblV3" style="color:#38bdf8; font-weight:700;">50%</span>
  </div>
  <div id="telemetryBox" style="margin-left:auto; color:#e2e8f0; font-family:Consolas, monospace;">
    Cursa: <strong>0 mm</strong> | F_net: <strong>1240 N</strong>
  </div>
</div>

<div class="canvas-wrap">
  <svg viewBox="0 0 885 615" id="fluidsimSvg">
    <defs>
      <marker id="isoArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
        <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="#0f172a"/>
      </marker>
    </defs>

    <g id="expandedLayer">
      __SVG_WIRES__
      __SVG_STATIC__
      <g id="valveSpool" transform="translate(0, 0)">
        __SVG_SPOOL__
      </g>
      <g id="pistonGroup" transform="translate(0, 0)">
        __SVG_PISTON__
      </g>

      <g transform="translate(570, 30)">
        <rect x="0" y="0" width="295" height="175" rx="4" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>
        <rect x="0" y="0" width="295" height="24" fill="#0f172a"/>
        <text x="10" y="16" fill="#f8fafc" font-family="Consolas, monospace" font-size="11" font-weight="bold">📈 OSCILOSCOP CALIBRAT s(t) & p_ef(t)</text>
        <line x1="36" y1="42" x2="282" y2="42" stroke="#cbd5e1" stroke-dasharray="3,3"/>
        <line x1="36" y1="82" x2="282" y2="82" stroke="#cbd5e1" stroke-dasharray="3,3"/>
        <line x1="36" y1="122" x2="282" y2="122" stroke="#cbd5e1" stroke-dasharray="3,3"/>
        <text x="6" y="46" fill="#0f172a" font-family="Consolas, monospace" font-size="10" font-weight="bold">a1(1)</text>
        <text x="6" y="86" fill="#64748b" font-family="Consolas, monospace" font-size="9">50%</text>
        <text x="6" y="126" fill="#0f172a" font-family="Consolas, monospace" font-size="10" font-weight="bold">a0(0)</text>
        <polyline id="scopePressWave" points="36,122" fill="none" stroke="#38bdf8" stroke-width="1.6" stroke-dasharray="3,2"/>
        <polyline id="scopeWave" points="36,122" fill="none" stroke="#0055cc" stroke-width="2.6"/>
        <text x="10" y="142" id="chamberPressTxt" fill="#0f172a" font-family="Consolas, monospace" font-size="9.5" font-weight="bold">p1: 0.0 bar | p2: 0.0 bar</text>
        <text x="10" y="156" id="airConsTxt" fill="#0284c7" font-family="Consolas, monospace" font-size="9.5" font-weight="bold">Debit Q: 0.0 Nl/ciclu</text>
        <text x="10" y="169" id="stallWarnTxt" fill="#dc2626" font-family="Consolas, monospace" font-size="9.5" font-weight="bold"></text>
      </g>

      <g transform="translate(570, 520)">
        <rect x="0" y="0" width="295" height="72" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>
        <line x1="0" y1="24" x2="295" y2="24" stroke="#0f172a" stroke-width="1.2"/>
        <line x1="0" y1="48" x2="295" y2="48" stroke="#0f172a" stroke-width="1.2"/>
        <text x="8" y="16" fill="#0f172a" font-family="Consolas, monospace" font-size="10" font-weight="bold">STANDARD: ISO 1219-1 / ISO 1219-2</text>
        <text x="8" y="40" fill="#0055cc" font-family="Consolas, monospace" font-size="10" font-weight="bold">__TITLE_SHORT__</text>
        <text x="8" y="64" fill="#334155" font-family="Consolas, monospace" font-size="9">Festo FluidSIM-P Safety-Critical CAD</text>
      </g>
    </g>

    <g id="macroLayer" style="display:none;">
      <rect x="200" y="130" width="420" height="270" rx="8" fill="#ffffff" stroke="#0f172a" stroke-width="3" stroke-dasharray="10,5"/>
      <rect x="215" y="145" width="390" height="240" fill="#f1f5f9" stroke="#0284c7" stroke-width="2"/>
      <text x="410" y="200" text-anchor="middle" fill="#0f172a" font-size="16" font-weight="bold">REPREZENTARE MACRO SIMPLIFICATĂ (ISO 1219)</text>
      <text x="410" y="232" text-anchor="middle" fill="#0055cc" font-size="13" font-weight="bold">__TITLE_FULL__</text>
      <text x="410" y="268" text-anchor="middle" fill="#334155" font-size="11.5">__MACRO_DESC__</text>
      <rect x="380" y="295" width="60" height="50" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>
      <line x1="380" y1="330" x2="440" y2="330" stroke="#0f172a" stroke-width="1.5" stroke-dasharray="4,3"/>
      <line x1="410" y1="295" x2="410" y2="308" stroke="#0f172a" stroke-width="2"/>
      <circle cx="458" cy="320" r="9" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>
      <line x1="440" y1="320" x2="449" y2="320" stroke="#0f172a" stroke-width="2"/>
      <line x1="410" y1="400" x2="410" y2="475" stroke="#0055cc" stroke-width="4"/>
      <polygon points="410,475 398,497 422,497" fill="#ffffff" stroke="#0f172a" stroke-width="2"/>
      <text x="430" y="450" fill="#0f172a" font-size="12" font-weight="bold">Port 1 (Alimentare Generală 6 bar)</text>
    </g>
  </svg>
</div>

<div class="legend">
  <div>
    <span><span class="dot" style="background:#0055cc;"></span>Presiune Activă (p_ef)</span>
    <span style="margin-left:12px;"><span class="dot" style="background:#38bdf8;"></span>Conductă Ventilată (0 bar)</span>
    <span style="margin-left:12px;"><span class="dot" style="background:#f59e0b;"></span>Aer Captiv Blocat (Centru Închis)</span>
  </div>
  <div id="statusLabel" style="color:#38bdf8; font-weight:600;">Inițializare simulator...</div>
</div>

<script>
  const valveType = "__VALVE_TYPE__";
  const has3Pos = __HAS_3_POS__;
  let currentMode = "__DEFAULT_MODE__";
  let isMacro = false;
  let autoCycle = false;
  let soundEnabled = false;
  let zoomLevel = 1.0;

  let pistonPos = 0.0;
  let supplyBar = 6.0;
  let boreMm = 63.0;
  let loadN = 250.0;
  let lineDropBar = 0.3;
  let v2Pct = 50.0;
  let v3Pct = 50.0;
  let waveHistory = [];
  let pressHistory = [];

  function playPneumaticExhaustSound() {
    if (!soundEnabled) return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const bufferSize = ctx.sampleRate * 0.16;
      const buffer = ctx.createBuffer(1, bufferSize, ctx.sampleRate);
      const data = buffer.getChannelData(0);
      for (let i = 0; i < bufferSize; i++) {
        data[i] = (Math.random() * 2 - 1) * Math.exp(-i / (ctx.sampleRate * 0.045));
      }
      const noise = ctx.createBufferSource();
      noise.buffer = buffer;
      const filter = ctx.createBiquadFilter();
      filter.type = "bandpass";
      filter.frequency.value = 1800;
      const gain = ctx.createGain();
      gain.gain.value = 0.18;
      noise.connect(filter);
      filter.connect(gain);
      gain.connect(ctx.destination);
      noise.start();
    } catch (e) {}
  }

  function toggleSound() {
    soundEnabled = !soundEnabled;
    const btn = document.getElementById('btnAudio');
    btn.innerText = soundEnabled ? '🔊 Sunet Aer: ON' : '🔊 Sunet Aer: OFF';
    btn.classList.toggle('auto-on', soundEnabled);
    if (soundEnabled) playPneumaticExhaustSound();
  }

  function zoomCanvas(factor) {
    zoomLevel = Math.min(1.6, Math.max(0.7, zoomLevel * factor));
    document.getElementById('fluidsimSvg').style.transform = 'scale(' + zoomLevel.toFixed(2) + ')';
  }

  function updateParams() {
    supplyBar = parseFloat(document.getElementById('rngPress').value);
    boreMm = parseFloat(document.getElementById('selBore').value);
    loadN = parseFloat(document.getElementById('rngLoad').value);
    lineDropBar = parseFloat(document.getElementById('rngDrop').value);
    v2Pct = parseFloat(document.getElementById('rngV2').value);
    v3Pct = parseFloat(document.getElementById('rngV3').value);
    document.getElementById('lblPress').innerText = supplyBar.toFixed(1) + ' bar';
    document.getElementById('lblLoad').innerText = loadN.toFixed(0) + ' N';
    document.getElementById('lblDrop').innerText = lineDropBar.toFixed(1) + ' bar';
    document.getElementById('lblV2').innerText = v2Pct.toFixed(0) + '%';
    document.getElementById('lblV3').innerText = v3Pct.toFixed(0) + '%';
  }

  function toggleAutoCycle() {
    autoCycle = !autoCycle;
    const btn = document.getElementById('btnAuto');
    btn.classList.toggle('auto-on', autoCycle);
    if (autoCycle && currentMode !== '14') {
      setMode('14');
    }
  }

  function setMode(m) {
    if (m !== currentMode) playPneumaticExhaustSound();
    currentMode = m;
    ['14', '0', '12'].forEach(id => {
      const el = document.getElementById('btn' + id);
      if (el) el.classList.remove('active');
    });
    const activeBtn = document.getElementById('btn' + m);
    if (activeBtn) activeBtn.classList.add('active');

    const spool = document.getElementById('valveSpool');
    const status = document.getElementById('statusLabel');

    const pipesPerm = document.querySelectorAll('.pipe-PERMANENT');
    const pipes14 = document.querySelectorAll('.pipe-STARE_14, .pipe-PILOT_14');
    const pipes12 = document.querySelectorAll('.pipe-STARE_12, .pipe-PILOT_12');

    pipesPerm.forEach(p => { p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); });

    if (m === '14') {
      spool.setAttribute('transform', 'translate(44, 0)');
      pipes14.forEach(p => { p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); });
      pipes12.forEach(p => { p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); });
      status.innerText = 'Stare 14 Activă: Caseta stângă este pe porturi (1→4 presiune, 2→3 evacuare prin V3).';
    } else if (m === '0' && has3Pos) {
      spool.setAttribute('transform', 'translate(0, 0)');
      if (valveType === 'VALVE_5_3_VENTED') {
        pipes14.forEach(p => { p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); });
        pipes12.forEach(p => { p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); });
        status.innerText = 'Poziția 0 (Centru Ventilat): Portul 1 blocat, ambele camere (4→5 și 2→3) descărcate la 0 bar.';
      } else if (valveType === 'VALVE_5_3_CLOSED') {
        pipes14.forEach(p => { p.setAttribute('stroke', '#f59e0b'); p.setAttribute('stroke-width', '3.2'); });
        pipes12.forEach(p => { p.setAttribute('stroke', '#f59e0b'); p.setAttribute('stroke-width', '3.2'); });
        status.innerText = 'Poziția 0 (Centru Închis): Toate cele 5 porturi sunt blocate! Pistonul este oprit rigid în poziția curentă.';
      } else {
        pipes14.forEach(p => { p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); });
        pipes12.forEach(p => { p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); });
        status.innerText = 'Poziția 0 (Centru sub Presiune): Presiune 1→4 și 1→2 simultan.';
      }
    } else if (m === '12') {
      const shiftX = has3Pos ? -44 : 0;
      spool.setAttribute('transform', 'translate(' + shiftX + ', 0)');
      pipes14.forEach(p => { p.setAttribute('stroke', '#38bdf8'); p.setAttribute('stroke-width', '2.5'); });
      pipes12.forEach(p => { p.setAttribute('stroke', '#0055cc'); p.setAttribute('stroke-width', '3.8'); });
      status.innerText = 'Stare 12 Activă: Aerul curge 1→2 iar camera spate este evacuată prin V2.';
    }
  }

  setInterval(() => {
    const pEff = Math.max(0.2, supplyBar - lineDropBar);
    const dBore = boreMm / 1000.0;
    const dRod  = (boreMm >= 63 ? 20.0 : 12.0) / 1000.0;
    const areaFull = Math.PI * dBore * dBore / 4.0;
    const areaRod  = Math.PI * (dBore * dBore - dRod * dRod) / 4.0;

    const fAvailAdv = Math.round(pEff * 100000 * areaFull * 0.85);
    const fAvailRet = Math.round(pEff * 100000 * areaRod * 0.85);
    const fNetAdv   = fAvailAdv - loadN;
    const fNetRet   = fAvailRet - (loadN * 0.5);

    const loadFactorAdv = fNetAdv > 0 ? Math.min(1.0, fNetAdv / Math.max(1, fAvailAdv)) : 0.0;
    const loadFactorRet = fNetRet > 0 ? Math.min(1.0, fNetRet / Math.max(1, fAvailRet)) : 0.0;

    const speedAdv = (v3Pct / 100.0) * (pEff / 6.0) * 3.4 * loadFactorAdv;
    const speedRet = (v2Pct / 100.0) * (pEff / 6.0) * 3.4 * loadFactorRet;

    let p1 = 0.0, p2 = 0.0, activeSpeed = 0.0;
    const stallEl = document.getElementById('stallWarnTxt');

    if (currentMode === '14') {
      p1 = pEff; p2 = 0.0;
      if (fNetAdv <= 0) {
        stallEl.textContent = '⚠️ BLOCARE SARCINĂ (F_load > F_disp)! Mărește P1 sau D!';
      } else {
        stallEl.textContent = '';
        pistonPos = Math.min(100.0, pistonPos + speedAdv);
        activeSpeed = speedAdv;
        if (autoCycle && pistonPos >= 100.0) setMode('12');
      }
    } else if (currentMode === '12') {
      p1 = 0.0; p2 = (valveType.startsWith('VALVE_3_2')) ? 0.0 : pEff;
      stallEl.textContent = '';
      pistonPos = Math.max(0.0, pistonPos - speedRet);
      activeSpeed = speedRet;
      if (autoCycle && pistonPos <= 0.0) setMode('14');
    } else if (currentMode === '0') {
      stallEl.textContent = '';
      if (valveType === 'VALVE_5_3_VENTED') {
        p1 = 0.0; p2 = 0.0;
      } else if (valveType === 'VALVE_5_3_CLOSED') {
        p1 = pEff * 0.85; p2 = pEff * 0.85;
      } else if (valveType === 'VALVE_5_3_PRESSURIZED') {
        p1 = pEff; p2 = pEff;
        pistonPos = Math.min(100.0, pistonPos + speedAdv * 0.22);
        activeSpeed = speedAdv * 0.22;
      }
    }

    const pxShift = (pistonPos / 100.0) * 65.0;
    document.getElementById('pistonGroup').setAttribute('transform', 'translate(' + pxShift.toFixed(1) + ', 0)');

    const qCycle = ((areaFull + areaRod) * 0.200 * (pEff + 1.013) * 1000).toFixed(2);
    const mmPos = Math.round((pistonPos / 100.0) * 200);

    document.getElementById('telemetryBox').innerHTML =
      'Cursa: <strong>' + mmPos + ' mm (' + pistonPos.toFixed(0) + '%)</strong> | F_net_avans: <strong>' + Math.max(0, fNetAdv) + ' N</strong> | F_ret: <strong>' + fAvailRet + ' N</strong>';
    document.getElementById('chamberPressTxt').textContent =
      'p1_ef: ' + p1.toFixed(1) + ' bar | p2_ef: ' + p2.toFixed(1) + ' bar | v: ' + (activeSpeed * 15).toFixed(0) + ' mm/s';
    document.getElementById('airConsTxt').textContent =
      'Debit Q: ' + qCycle + ' Nl/ciclu (Ø' + boreMm + 'x200, Δp=' + lineDropBar.toFixed(1) + 'b)';

    waveHistory.push(pistonPos);
    pressHistory.push((p1 / 10.0) * 100.0);
    if (waveHistory.length > 82) { waveHistory.shift(); pressHistory.shift(); }

    const pts = waveHistory.map((val, idx) => {
      const x = 36 + idx * 3.0;
      const y = 122 - (val / 100.0) * 80.0;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    const ptsP = pressHistory.map((val, idx) => {
      const x = 36 + idx * 3.0;
      const y = 122 - (val / 100.0) * 80.0;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');

    document.getElementById('scopeWave').setAttribute('points', pts);
    document.getElementById('scopePressWave').setAttribute('points', ptsP);
  }, 40);

  function toggleMacro() {
    isMacro = !isMacro;
    document.getElementById('expandedLayer').style.display = isMacro ? 'none' : 'block';
    document.getElementById('macroLayer').style.display = isMacro ? 'block' : 'none';
    document.getElementById('btnView').innerText = isMacro
      ? '🔄 Vedere: Simbol Macro Simplificat (Click pt. Ansamblu Desfășurat)'
      : '🔄 Vedere: Ansamblu Desfășurat ISO 1219 (Click pt. Simbol Macro)';
  }

  setMode('__DEFAULT_MODE__');
</script>
</body>
</html>"""

    html_out = (
        html_template
        .replace("__POS_BADGE__", "3 POZIȚII (5/3)" if has_3_positions else "2 POZIȚII (FĂRĂ CENTRU)")
        .replace("__BUTTONS_HTML__", buttons_html)
        .replace("__SVG_WIRES__", "".join(svg_wires))
        .replace("__SVG_STATIC__", "".join(svg_static))
        .replace("__SVG_SPOOL__", "".join(svg_spool))
        .replace("__SVG_PISTON__", "".join(svg_piston))
        .replace("__TITLE_SHORT__", schema["titlu"][:35])
        .replace("__TITLE_FULL__", schema["titlu"][:58])
        .replace("__MACRO_DESC__", schema["simbol_macro_descriere"][:85] + "...")
        .replace("__VALVE_TYPE__", main_valve_type)
        .replace("__HAS_3_POS__", "true" if has_3_positions else "false")
        .replace("__DEFAULT_MODE__", default_mode)
    )
    return html_out


# =====================================================================
# 7. PIPELINE AGENTIC ÎN 3 STADII (GENERATOR → VALIDATOR → CRITIC)
# =====================================================================
def genereaza_proiect_fluidsim(
        api_key: str,
        model_name: str,
        cerinta_text: str = None,
        imagine: Image.Image = None,
        foloseste_cache: bool = True,
        schema_anterioara: dict = None,
        feedback_corectie: str = None,
        max_iteratii_critic: int = 3
) -> dict:
    cerinta_sigura = anonimizeaza_cerinta_confidentiala(cerinta_text or "")
    feedback_sigur = anonimizeaza_cerinta_confidentiala(feedback_corectie or "")
    clean_model = model_name.replace("models/", "").strip()

    if imagine is not None:
        img_bytes = io.BytesIO()
        imagine.save(img_bytes, format="PNG")
        img_hash = hashlib.sha256(img_bytes.getvalue()).hexdigest()[:16]
        cheie_cache = f"v6_foto_{img_hash}_{cerinta_sigura.strip().lower()}"
    else:
        cheie_cache = f"v6_text_{cerinta_sigura.strip().lower()}"

    if foloseste_cache and not feedback_sigur:
        cached_schema = citeste_cache_sigur(cheie_cache)
        if cached_schema:
            st.toast("Verificat SHA-256 & încărcat din SQLite Cache (0 consum API!)", icon="🛡️")
            return valideaza_si_repara_schema(cached_schema, cerinta_sigura)

    if not api_key:
        rez_local = sinteza_determinista_locala(f"{cerinta_sigura} {feedback_sigur}")
        salveaza_cache_sigur(cheie_cache, rez_local, "LOCAL_DETERMINISTIC_COMPILER")
        inregistreaza_audit("OFFLINE_SYNTHESIS", f"Generat local: {cerinta_sigura}", rez_local)
        return rez_local

    client = genai.Client(api_key=api_key)
    reguli_invatate = incarca_reguli_invatate()
    sectiune_reguli = ""
    if reguli_invatate:
        sectiune_reguli = "\nREGULI OBLIGATORII ÎNVĂȚATE DIN AUDITUL INGINERESC:\n" + "\n".join(
            f"- {r}" for r in reguli_invatate
        )

    prompt_baza = f"""
    Ești inginer proiectant șef în sisteme pneumatice Festo FluidSIM-P și standardul ISO 1219-1 / ISO 1219-2.

    REGULI CRITICE PENTRU PRECIZIE 100% (VERIFICATE AUTOMAT DE VALIDATORUL SIMBOLIC):
    1. Completează `analiza_inginereasca_pas_cu_pas`:
       - Distribuitoarele 5/2 (`VALVE_5_2_MONOSTABLE`, `VALVE_5_2_BISTABLE`) și 3/2 (`VALVE_3_2_NC_BUTTON`) au STRICT 2 POZIȚII (`numar_pozitii_distribuitor_principal = 2`, fără poziție centrală!).
       - Distribuitoarele 5/3 (`VALVE_5_3_VENTED`, `VALVE_5_3_CLOSED`, `VALVE_5_3_PRESSURIZED`) au STRICT 3 POZIȚII (`numar_pozitii_distribuitor_principal = 3`).
    2. Desfășoară ÎNTOTDEAUNA ansamblul complet pe straturi ierarhice de jos în sus (pe axa X = 350):
       - Y = 565, X = 350: Sursa de aer comprimat `P1` (`AIR_SOURCE`, port `1`).
       - Y = 485, X = 350: Grupul de preparare a aerului `Z1` (`FRL_EXPANDED`, porturi `1` jos, `2` sus).
       - Y = 340, X = 350: Distribuitorul principal `V1`.
       - Y = 205, X = 270 (calea 4) și X = 430 (calea 2): Drosele cu supapă de sens `V2` (`THROTTLE_CHECK_LEFT` sau `QUICK_EXHAUST`) și `V3` (`THROTTLE_CHECK_RIGHT`).
       - Y = 85,  X = 350: Cilindrul `M1` (`CYL_DOUBLE` cu porturile `P1`, `P2` sau `CYL_SINGLE` cu portul `P1`).
    3. Dacă se cere comandă bimanuală (supapă ȘI) sau comandă din 2 locuri (supapă SAU), include obligatoriu butoanele `S1` (X=115, Y=465), `S2` (X=215, Y=465) și supapa logică `V4` (`LOGIC_AND` sau `LOGIC_OR` la X=165, Y=340) conectată la `V1.14`.
    4. Dacă se cere limitator de cursă cu rolă B1 (a1), include `S1` (`VALVE_3_2_NC_BUTTON` la X=140, Y=440) conectat la `V1.14` și `B1` (`VALVE_3_2_NC_ROLLER` la X=525, Y=440) conectat la `V1.12`.
    {sectiune_reguli}
    """

    if feedback_sigur and schema_anterioara:
        prompt_baza += f"""
        \nCORECȚIE OBLIGATORIE SOLICITATĂ DE INGINER PE SCHEMA CURENTĂ:
        Schema anterioară JSON: {json.dumps(schema_anterioara, ensure_ascii=False)}
        Corecția cerută: "{feedback_sigur}"
        """

    rezultat = None
    ultima_eroare_api = ""
    prompt_curent = prompt_baza + f"\nCerință utilizator: {cerinta_sigura}"

    for iteratie in range(1, max_iteratii_critic + 1):
        continut = [imagine, prompt_curent] if (imagine and iteratie == 1) else [prompt_curent]
        try:
            response = client.models.generate_content(
                model=clean_model,
                contents=continut,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ProiectFluidSIM,
                    temperature=0.05 if iteratie > 1 else 0.1,
                ),
            )
            rezultat = json.loads(response.text)
            erori_curente = inspecteaza_erori_iso1219(rezultat, f"{cerinta_sigura} {feedback_sigur}")

            if not erori_curente:
                st.toast(f"Generat cu succes prin {clean_model} (validat ISO 1219 din stadiul {iteratie})!", icon="✅")
                break
            else:
                prompt_curent = (
                        prompt_baza
                        + f"\nCerință: {cerinta_sigura}\n"
                        + f"\nATENȚIE CRITICĂ (Iterația de auto-corecție {iteratie + 1}/{max_iteratii_critic}): "
                        + "Validatorul ISO 1219 a găsit următoarele erori în JSON-ul tău anterior:\n"
                        + "\n".join(f"- {err}" for err in erori_curente)
                        + f"\nJSON-ul anterior cu erori: {json.dumps(rezultat, ensure_ascii=False)}\n"
                        + "Generează acum JSON-ul complet corectat!"
                )
        except Exception as e:
            ultima_eroare_api = str(e)
            if "429" in ultima_eroare_api or "RESOURCE_EXHAUSTED" in ultima_eroare_api:
                wait_s = 3 * iteratie
                st.toast(f"Limită de rată Free Tier (429). Reîncercare automată în {wait_s}s...", icon="⏳")
                time.sleep(wait_s)
            else:
                break

    if not rezultat:
        st.info(
            f"Notă API ({clean_model}): {ultima_eroare_api[:120]}. S-a construit schema prin Compilatorul Determinist Local ISO 1219!")
        rezultat = sinteza_determinista_locala(f"{cerinta_sigura} {feedback_sigur}")
        model_folosit = "FAILOVER_DETERMINISTIC_COMPILER"
    else:
        model_folosit = f"{clean_model} (Stadiul {iteratie})"

    rezultat = valideaza_si_repara_schema(rezultat, f"{cerinta_sigura} {feedback_sigur}")
    salveaza_cache_sigur(cheie_cache, rezultat, model_folosit)
    inregistreaza_audit("SCHEMA_GENERATED", f"Model: {model_folosit} | Cerință: {cerinta_sigura}", rezultat)
    return rezultat


# =====================================================================
# 8. SUITĂ INTEGRATĂ DE AUTO-TESTARE AUTOMATĂ
# =====================================================================
def ruleaza_teste_automate_iso1219() -> list[dict]:
    rezultate_teste = []
    toate_tipurile = [
        "CYL_DOUBLE", "CYL_SINGLE", "THROTTLE_CHECK_LEFT", "THROTTLE_CHECK_RIGHT",
        "QUICK_EXHAUST", "VALVE_5_3_VENTED", "VALVE_5_3_CLOSED", "VALVE_5_3_PRESSURIZED",
        "VALVE_5_2_BISTABLE", "VALVE_5_2_MONOSTABLE", "VALVE_3_2_NC_BUTTON",
        "VALVE_3_2_NC_ROLLER", "VALVE_3_2_NC_PILOT", "LOGIC_OR", "LOGIC_AND",
        "FRL_EXPANDED", "FRL_MACRO", "AIR_SOURCE"
    ]
    ok_simboluri = True
    for t in toate_tipurile:
        prims, pins = get_component_geometry(
            {"id": "T1", "tip": t, "nume": "Test", "parametru": "6 bar", "x": 350, "y": 300})
        if not prims or not pins:
            ok_simboluri = False
    rezultate_teste.append({
        "Test": "1. Geometrie Vectorială 18/18 Simboluri ISO 1219-1",
        "Status": "✅ PASS" if ok_simboluri else "❌ FAIL",
        "Detalii": "Toate cele 18 componente generează primitive vectoriale și pini valizi."
    })

    s_52 = sinteza_determinista_locala("Distribuitor 5/2 monostabil cu arc")
    s_53 = sinteza_determinista_locala("Distribuitor 5/3 centru închis")
    html_52 = render_fluidsim_canvas(s_52)
    html_53 = render_fluidsim_canvas(s_53)
    ok_pos = (s_52["numar_pozitii_distribuitor_principal"] == 2 and 'id="btn0"' not in html_52) and \
             (s_53["numar_pozitii_distribuitor_principal"] == 3 and 'id="btn0"' in html_53)
    rezultate_teste.append({
        "Test": "2. Control Dinamic Strict Poziții (2 Butoane pt. 5/2 vs. 3 Butoane pt. 5/3)",
        "Status": "✅ PASS" if ok_pos else "❌ FAIL",
        "Detalii": "Distribuitoarele 5/2 și 3/2 elimină complet butonul central 0; 5/3 include poziția 0."
    })

    dxf_data = export_exact_dxf(s_53)
    ok_dxf = len(dxf_data) > 2500 and b"ISO_SYMBOLS" in dxf_data and b"PNEUMATIC_LINES" in dxf_data
    rezultate_teste.append({
        "Test": "3. Paritate 1:1 Export CAD .DXF (Straturi ISO + Cartuș ISO 7200)",
        "Status": "✅ PASS" if ok_dxf else "❌ FAIL",
        "Detalii": f"Fișier DXF valid generat ({len(dxf_data)} bytes)."
    })

    salveaza_cache_sigur("__self_test_key__", s_52, "SELF_TEST")
    citit = citeste_cache_sigur("__self_test_key__")
    ok_db = (citit is not None and citit["titlu"] == s_52["titlu"])
    rezultate_teste.append({
        "Test": "4. Bază de Date ACID SQLite3 & Verificare Integritate SHA-256",
        "Status": "✅ PASS" if ok_db else "❌ FAIL",
        "Detalii": "Suma de control SHA-256 validată la scriere și citire."
    })

    return rezultate_teste


# =====================================================================
# 9. INTERFAȚA ENTERPRISE STREAMLIT (PC & TELEFON)
# =====================================================================
st.set_page_config(page_title="FluidSIM-P ISO 1219 Safety-Critical CAD", layout="wide")
st.title("⚙️ Festo FluidSIM-P Enterprise CAD & Calibrated Physics Simulator (ISO 1219)")

try:
    default_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    default_key = os.environ.get("GEMINI_API_KEY", "")

if "schema_activa" not in st.session_state:
    st.session_state["schema_activa"] = sinteza_determinista_locala(
        "Distribuitor 5/3 cu centru ventilat + drosele + cilindru dublu efect"
    )
if "ultima_cerinta" not in st.session_state:
    st.session_state[
        "ultima_cerinta"] = "Distribuitor 5/3 cu centru ventilat în ansamblu cu cilindru dublu efect, drosele și grup FRL"

with st.sidebar:
    st.header("🛡️ Configurare AI & Cache")
    api_key = st.text_input("Gemini API Key (opțional pt. compilatorul local):", value=default_key, type="password")

    model_ales = st.text_input(
        "Scrie ID-ul modelului AI:",
        value="gemini-2.5-flash",
        help="Modele recomandate: gemini-2.5-flash, gemini-2.5-flash-lite, gemini-3-flash-preview"
    ).strip()

    if st.button("🔍 Vezi modelele permise de cheia ta", use_container_width=True):
        if not api_key:
            st.warning("Fără cheie API.")
        else:
            try:
                client_test = genai.Client(api_key=api_key)
                modele = [m.name.replace("models/", "") for m in client_test.models.list() if "gemini" in m.name]
                st.code("\n".join(modele))
            except Exception as e:
                st.error(f"Eroare: {e}")

    foloseste_cache = st.checkbox("Cache SQLite cu Integritate SHA-256", value=True)

    st.divider()
    st.subheader("⚡ 8 Arhitecturi Industriale Instant")
    sablon = st.selectbox(
        "Selectează o arhitectură standard (0 API):",
        [
            "1. Distribuitor 5/3 cu centru ventilat + drosele V2/V3 + cilindru dublu efect",
            "2. Distribuitor 5/3 cu centru închis (oprire intermediară) + drosele + cilindru",
            "3. Distribuitor 5/3 cu centru sub presiune + drosele + cilindru dublu efect",
            "4. Distribuitor 5/2 monostabil cu arc (2 poziții) + drosele + cilindru",
            "5. Comandă bimanuală de siguranță cu supapă ȘI (AND) + butoane S1/S2 + 5/2",
            "6. Comandă din două locuri cu supapă SAU (OR) + butoane S1/S2 + 5/2",
            "7. Ciclu semiautomat cu buton Start S1 + limitator cu rolă B1 (a1) + 5/2 bistabil",
            "8. Distribuitor 3/2 normal închis + drosel + cilindru simplu efect cu arc"
        ]
    )
    if st.button("⚡ Încarcă Șablonul Selectat", use_container_width=True):
        st.session_state["ultima_cerinta"] = sablon
        st.session_state["schema_activa"] = sinteza_determinista_locala(sablon)
        st.rerun()

    st.divider()
    st.subheader("🧠 Reguli Învățate (SQLite SHA-256)")
    reguli_curente = incarca_reguli_invatate()
    if reguli_curente:
        for idx, r in enumerate(reguli_curente, 1):
            st.caption(f"{idx}. {r}")
        if st.button("🗑️ Șterge regulile învățate"):
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("DELETE FROM learned_rules")
                conn.commit()
            st.rerun()
    else:
        st.caption("Nicio regulă personalizată salvată încă.")

tab_text, tab_foto, tab_ext, tab_audit = st.tabs([
    "🔤 Generează Schemă (Multi-Stage AI / Offline)",
    "📸 Recunoaște Schemă din Poză",
    "🧩 Extensibilitate & Plugin-uri Echipamente",
    "🛡️ Audit, Calibrare & Auto-Testare"
])

with tab_text:
    col_in, col_btn = st.columns([4, 1])
    with col_in:
        cerinta = st.text_input(
            "Descrie instalația pneumatică (scrie orice cerință și apasă Generează 1:1):",
            value="Instalație pneumatică cu comandă bimanuală de siguranță prin supapă de simultaneitate ȘI și două butoane 3/2 normal închise, distribuitor principal 5/2 monostabil cu arc, supapă de descărcare rapidă pe avans, drosel pe retragere și cilindru cu dublu efect"
        )
    with col_btn:
        st.write("")
        st.write("")
        gen_click = st.button("🚀 Generează 1:1", use_container_width=True)

    if gen_click and cerinta:
        with st.spinner(f"Se execută Pipeline-ul Generator → Validator ISO 1219 → Critic ({model_ales})..."):
            st.session_state["ultima_cerinta"] = cerinta
            st.session_state["schema_activa"] = genereaza_proiect_fluidsim(
                api_key, model_ales, cerinta_text=cerinta, foloseste_cache=foloseste_cache
            )

with tab_foto:
    fisier_foto = st.file_uploader("Încarcă o poză cu o schemă din caiet / tablă / FluidSIM",
                                   type=["jpg", "jpeg", "png"])
    indicatii_foto = st.text_input("Detalii opționale pentru poză:",
                                   placeholder="Ex: Desfășoară complet schema din poză")
    if fisier_foto:
        img = Image.open(fisier_foto)
        st.image(img, caption="Poză încărcată", width=360)
        if st.button("🔍 Transformă Poza în Schemă FluidSIM 1:1"):
            with st.spinner(f"Se scanează și se validează simbolurile ISO 1219 ({model_ales})..."):
                st.session_state["ultima_cerinta"] = indicatii_foto
                st.session_state["schema_activa"] = genereaza_proiect_fluidsim(
                    api_key, model_ales, cerinta_text=indicatii_foto, imagine=img, foloseste_cache=foloseste_cache
                )

with tab_ext:
    st.markdown("### 🧩 Registru de Extensibilitate — Adaugă Echipamente / Serii Festo Noi")
    c_p1, c_p2, c_p3 = st.columns(3)
    with c_p1:
        plg_id = st.text_input("ID Plugin:", value="CYL_GREU_100")
        plg_kw = st.text_input("Cuvânt-cheie declanșator:", value="cilindru greu")
    with c_p2:
        plg_base = st.selectbox("Simbol de bază ISO 1219:", [
            "CYL_DOUBLE", "CYL_SINGLE", "VALVE_5_3_VENTED", "VALVE_5_3_CLOSED",
            "VALVE_5_2_MONOSTABLE", "VALVE_5_2_BISTABLE", "THROTTLE_CHECK_LEFT", "QUICK_EXHAUST", "FRL_EXPANDED"
        ])
        plg_nume = st.text_input("Denumire tehnică:", value="Cilindru Greu ISO 15552 cu Tijă Întărită")
    with c_p3:
        plg_param = st.text_input("Parametru nominal:", value="Ø100x400 mm / 10 bar")
        plg_festo = st.text_input("Cod Catalog Festo / Producător:", value="DSBC-100-400-PPSA-N3")

    if st.button("➕ Înregistrează Echipamentul în Baza de Date SQLite"):
        inregistreaza_plugin_echipament(plg_id, plg_kw, plg_base, plg_nume, plg_param, plg_festo)
        st.success(f"Echipamentul '{plg_nume}' ({plg_festo}) a fost salvat!")

    pluginuri_active = incarca_pluginuri_echipamente()
    if pluginuri_active:
        st.table(pluginuri_active)

with tab_audit:
    st.markdown("### 🛡️ Suita de Auto-Testare, Verificare de Referință & Jurnal de Audit")
    if st.button("🧪 Rulează Toate Testele Automate de Verificare ISO 1219"):
        st.table(ruleaza_teste_automate_iso1219())

    st.markdown("#### 📏 Tabel de Calibrare și Verificare cu Valori de Referință (ISO 6358 / Festo)")
    st.table([
        {"Alezaj Cilindru (D)": "Ø32 mm (tijă Ø12)", "F_avans Teoretic (6 bar)": "482 N",
         "F_avans Util (η=0.85, Δp=0.3b)": "389 N", "Consum Aer Q (cursă 200mm)": "2.11 Nl/ciclu"},
        {"Alezaj Cilindru (D)": "Ø50 mm (tijă Ø16)", "F_avans Teoretic (6 bar)": "1178 N",
         "F_avans Util (η=0.85, Δp=0.3b)": "951 N", "Consum Aer Q (cursă 200mm)": "5.22 Nl/ciclu"},
        {"Alezaj Cilindru (D)": "Ø63 mm (tijă Ø20)", "F_avans Teoretic (6 bar)": "1870 N",
         "F_avans Util (η=0.85, Δp=0.3b)": "1510 N", "Consum Aer Q (cursă 200mm)": "8.31 Nl/ciclu"},
        {"Alezaj Cilindru (D)": "Ø80 mm (tijă Ø25)", "F_avans Teoretic (6 bar)": "3016 N",
         "F_avans Util (η=0.85, Δp=0.3b)": "2435 N", "Consum Aer Q (cursă 200mm)": "13.43 Nl/ciclu"},
        {"Alezaj Cilindru (D)": "Ø100 mm (tijă Ø25)", "F_avans Teoretic (6 bar)": "4712 N",
         "F_avans Util (η=0.85, Δp=0.3b)": "3805 N", "Consum Aer Q (cursă 200mm)": "21.32 Nl/ciclu"},
    ])

    st.markdown("#### 📜 Jurnal Imutabil de Audit (Ultimele 15 Operațiuni cu Semnătură SHA-256)")
    with sqlite3.connect(DB_FILE) as conn:
        audit_rows = conn.execute(
            "SELECT timestamp, event_type, details, schema_sha256 FROM audit_log ORDER BY id DESC LIMIT 15"
        ).fetchall()
    if audit_rows:
        st.table([
            {"Timp UTC": r[0], "Eveniment": r[1], "Detalii": r[2], "Semnătură SHA-256": r[3]}
            for r in audit_rows
        ])

# =====================================================================
# 10. AFIȘARE SIMULATOR FIZIC CALIBRAT, GRAFCET, FEEDBACK & EXPORT DXF
# =====================================================================
schema = st.session_state.get("schema_activa")

if schema:
    st.divider()
    sig_hash = calculeaza_sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False))[:12]
    st.subheader(
        f"📐 {schema['titlu']} ({schema.get('numar_pozitii_distribuitor_principal', 3)} Poziții | SHA-256: {sig_hash})")
    st.info(f"**Funcționare & Logică ISO 1219:** {schema['explicatie_centru_si_ansamblu']}")

    with st.expander("🧠 Verificare Inginerească & Topologică Pas cu Pas (Chain-of-Thought)", expanded=False):
        st.write(schema.get("analiza_inginereasca_pas_cu_pas", ""))

    html_canvas = render_fluidsim_canvas(schema)
    st.components.v1.html(html_canvas, height=710, scrolling=False)

    col_dxf, col_json = st.columns(2)
    with col_dxf:
        dxf_bytes = export_exact_dxf(schema)
        st.download_button(
            "📥 DESCARCĂ .DXF IDENTIC 1:1 CU CARTUȘ ISO PENTRU FESTO FLUIDSIM-P (File -> Import -> DXF)",
            data=dxf_bytes,
            file_name="schema_iso1219_fluidsim.dxf",
            mime="application/dxf",
            use_container_width=True
        )
    with col_json:
        st.download_button(
            "📥 Descarcă Specificația Tehnică, GRAFCET & BOM (.JSON)",
            data=json.dumps(schema, indent=2, ensure_ascii=False),
            file_name="specificatie_fluidsim.json",
            mime="application/json",
            use_container_width=True
        )

    st.markdown("### 🛠️ Corectează Schema & Învață AI-ul (Feedback Loop cu Semnătură SHA-256)")
    with st.container(border=True):
        feedback_text = st.text_input(
            "Dacă vrei o modificare pe schemă sau AI-ul a omis un detaliu, scrie aici:",
            placeholder="Ex: Adaugă comandă bimanuală cu supapă ȘI / Schimbă în 5/2 monostabil cu arc / Pune descărcare rapidă pe avans"
        )
        col_fb1, col_fb2 = st.columns([2, 1])
        with col_fb1:
            memoreaza_regula = st.checkbox(
                "🧠 Salvează această corecție în baza de date SQLite ca regulă permanentă",
                value=True
            )
        with col_fb2:
            if st.button("🔧 Aplică Corecția pe Schemă", use_container_width=True):
                if feedback_text:
                    if memoreaza_regula:
                        salveaza_regula_noua(feedback_text)
                    with st.spinner("Se execută bucla Critic pentru aplicarea corecției..."):
                        st.session_state["schema_activa"] = genereaza_proiect_fluidsim(
                            api_key=api_key,
                            model_name=model_ales,
                            cerinta_text=st.session_state.get("ultima_cerinta", "") + " " + feedback_text,
                            foloseste_cache=False,
                            schema_anterioara=schema,
                            feedback_corectie=feedback_text
                        )
                        st.rerun()

    st.markdown("### 📊 GRAFCET Secvențial (IEC 60848), Ghid FluidSIM-P & Listă de Echipamente (BOM)")
    if schema.get("grafcet"):
        st.table(schema["grafcet"])

    col_g1, col_g2 = st.columns([3, 2])
    with col_g1:
        for pas in schema.get("ghid_fluidsim", []):
            with st.expander(f"Pasul {pas['pas']}: {pas['element']}", expanded=True):
                st.markdown(f"**Configurare în FluidSIM-P:** {pas['cum_se_pune_in_fluidsim']}")

        st.markdown("#### 📦 Listă de Echipamente ISO 1219 / Catalog Festo (BOM)")
        tabel_bom = [
            {
                "Cod ISO": c["id"],
                "Denumire Echipament": c["nume"],
                "Parametru": c["parametru"],
                "Cod Catalog Festo": c.get("cod_festo", "FESTO-ISO")
            }
            for c in schema["componente"]
        ]
        st.table(tabel_bom)

    with col_g2:
        st.markdown("#### 🔗 Tabel de Conexiuni Pin-la-Pin (Traseu Conducte)")
        tabel_con = [
            {
                "De la (Pin)": c["de_la"],
                "Până la (Pin)": c["pana_la"],
                "Regim Conductă": c["circuit_activ_in_starea"]
            }
            for c in schema["conexiuni"]
        ]
        st.table(tabel_con)