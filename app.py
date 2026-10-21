import os
import io
import json
import hashlib
import ezdxf
import streamlit as st
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# =====================================================================
# 1. DATA STRUCTURE FOR GEMINI AI (ISO 1219 & FLUIDSIM)
# =====================================================================
class ComponentaFluidSIM(BaseModel):
    id: str = Field(description="ISO identifier, e.g., V1, M1, Z1, S1")
    nume: str = Field(description="Full technical name in Romanian/English, e.g., Distribuitor 5/3 centru ventilat")
    porturi: list[str] = Field(description="Ports list, e.g., ['1', '2', '4', '3', '5']")
    x: int = Field(description="X coordinate on schematic (50 to 500)")
    y: int = Field(description="Y coordinate on schematic (50 to 500)")


class Conexiune(BaseModel):
    de_la: str = Field(description="Source port, e.g., Z1.2 or V1.4")
    pana_la: str = Field(description="Target port, e.g., V1.1 or M1.spate")


class SchemaPneumatica(BaseModel):
    titlu: str = Field(description="Title of the component or assembly")
    simbol_pe_scurt: str = Field(description="Short macro symbol description vs full assembly explanation")
    explicatie_functionare: str = Field(description="Clear explanation of how the air flows in each state")
    componente_desfasurate: list[ComponentaFluidSIM]
    conexiuni: list[Conexiune]
    cod_html_svg_interactiv: str = Field(
        description=(
            "Complete self-contained HTML + SVG + JS interactive simulator optimized for mobile screen. "
            "Must include clickable buttons to switch valve states (e.g., 14, 0, 12) or toggle between "
            "Simplified Macro Symbol and Full Expanded Assembly, highlighting pressurized lines in dark blue "
            "and exhausted/vented lines in light blue."
        )
    )


# =====================================================================
# 2. FLUIDSIM DXF EXPORT GENERATOR
# =====================================================================
def genereaza_fisier_dxf(schema: dict) -> bytes:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    coords = {}

    for comp in schema["componente_desfasurate"]:
        x, y = comp["x"], comp["y"]
        coords[comp["id"]] = (x, y)
        msp.add_lwpolyline([(x - 30, y - 20), (x + 30, y - 20), (x + 30, y + 20), (x - 30, y + 20), (x - 30, y - 20)])
        msp.add_text(f"{comp['id']}: {comp['nume']}", dxfattribs={"height": 5, "insert": (x - 28, y + 24)})
        msp.add_text(f"Ports: {','.join(comp['porturi'])}", dxfattribs={"height": 3.5, "insert": (x - 28, y - 15)})

    for conn in schema["conexiuni"]:
        src_id = conn["de_la"].split(".")[0]
        dst_id = conn["pana_la"].split(".")[0]
        if src_id in coords and dst_id in coords:
            msp.add_line(coords[src_id], coords[dst_id])

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


# =====================================================================
# 3. AI ENGINE (WITH QUOTA-SAVING CACHE FOR TEXT & PHOTOS)
# =====================================================================
CACHE_FILE = "scheme_salvate.json"


def apeleaza_gemini(api_key: str, model_name: str, cerinta_text: str = None, imagine: Image.Image = None,
                    foloseste_cache: bool = True) -> dict:
    cache = json.load(open(CACHE_FILE, "r", encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}

    if imagine is not None:
        img_bytes = io.BytesIO()
        imagine.save(img_bytes, format="PNG")
        img_hash = hashlib.md5(img_bytes.getvalue()).hexdigest()[:12]
        cheie_cache = f"foto_{img_hash}_{(cerinta_text or '').strip().lower()}"
    else:
        cheie_cache = f"text_{(cerinta_text or '').strip().lower()}"

    if foloseste_cache and cheie_cache in cache:
        st.toast("Încărcat instant din Cache (0 consum API!)", icon="⚡")
        return cache[cheie_cache]

    client = genai.Client(api_key=api_key)
    prompt_baza = """
    Ești inginer automatist expert în Festo FluidSIM-P și standardul ISO 1219.
    1. Identifică piesa sau ansamblul cerut (sau recunoaște toate simbolurile, denumirile și porturile din poză).
    2. Dacă este un ansamblu simbolizat pe scurt (ex: grup FRL, comandă bimanuală, distribuitor 5/3 centru ventilat cu cilindru),
       desfășoară toate subcomponentele reale și conexiunile dintre porturi (1, 2, 4, 3, 5, 12, 14).
    3. Generează în `cod_html_svg_interactiv` un simulator vizual HTML5 + SVG + JS complet funcțional, cu fundal deschis pentru
       diagramă, desene ISO 1219 clare și butoane mari pe care utilizatorul poate apăsa pe telefon pentru a comuta pozițiile
       (ex: 14, 0, 12) și a vedea mișcarea sau traseul aerului.
    """

    continut = [imagine, prompt_baza + f"\nDetalii suplimentare: {cerinta_text or ''}"] if imagine else [
        prompt_baza + f"\nCerință: {cerinta_text}"]

    response = client.models.generate_content(
        model=model_name,
        contents=continut,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SchemaPneumatica,
            temperature=0.2,
        ),
    )

    rezultat = json.loads(response.text)
    cache[cheie_cache] = rezultat
    json.dump(cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    return rezultat


# =====================================================================
# 4. USER INTERFACE (PC & MOBILE)
# =====================================================================
st.set_page_config(page_title="FluidSIM AI Copilot", layout="centered")
st.title("⚙️ FluidSIM-P & Mobile Simulator AI")

try:
    default_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    default_key = os.environ.get("GEMINI_API_KEY", "")

api_key = st.sidebar.text_input("Gemini API Key:", value=default_key, type="password")

# Updated default model to gemini-3.1-pro-preview as requested by API
optiune_model = st.sidebar.selectbox(
    "Alege Modelul AI:",
    [
        "gemini-3.1-pro-preview (Gemini 3.1 Pro - Recomandat)",
        "gemini-2.5-flash (Rapid & Nelimitat pt. teste)",
        "Custom Model ID"
    ],
    index=0
)

if "Custom" in optiune_model:
    model_ales = st.sidebar.text_input("Scrie ID-ul exact al modelului:", value="gemini-3.1-pro-preview")
else:
    model_ales = optiune_model.split(" ")[0]

foloseste_cache = st.sidebar.checkbox("Folosește Cache Local (Economisește utilizările limitate)", value=True)

tab_text, tab_foto = st.tabs(["🔤 Caută după Denumire / Ansamblu", "📸 Recunoaște din Poză"])

rezultat_curent = None

with tab_text:
    cerinta = st.text_input(
        "Scrie denumirea piesei sau a ansamblului:",
        placeholder="Ex: distribuitor 5/3 cu centru ventilat + cilindru dublu efect"
    )
    if st.button("Generează Schema din Text", use_container_width=True):
        if not api_key:
            st.error("Te rog introdu cheia Gemini API în meniul din stânga (Sidebar)!")
        elif cerinta:
            with st.spinner(f"Se generează schema folosind {model_ales}..."):
                rezultat_curent = apeleaza_gemini(api_key, model_ales, cerinta_text=cerinta,
                                                  foloseste_cache=foloseste_cache)

with tab_foto:
    fisier_foto = st.file_uploader("Încarcă o poză sau fă o poză cu telefonul", type=["jpg", "jpeg", "png"])
    indicatii_foto = st.text_input("Indicații opționale pentru poză:",
                                   placeholder="Ex: explică-mi ce face distribuitorul din mijloc")
    if fisier_foto:
        img = Image.open(fisier_foto)
        st.image(img, caption="Imagine încărcată", use_container_width=True)
        if st.button("Analizează Poza & Generează Simulator", use_container_width=True):
            if not api_key:
                st.error("Te rog introdu cheia Gemini API în meniul din stânga (Sidebar)!")
            else:
                with st.spinner(f"Se scanează simbolurile folosind {model_ales}..."):
                    rezultat_curent = apeleaza_gemini(api_key, model_ales, cerinta_text=indicatii_foto, imagine=img,
                                                      foloseste_cache=foloseste_cache)

if rezultat_curent:
    st.divider()
    st.subheader(rezultat_curent["titlu"])
    st.info(
        f"**Simbol Simplificat vs. Ansamblu:** {rezultat_curent['simbol_pe_scurt']}\n\n**Funcționare:** {rezultat_curent['explicatie_functionare']}")

    st.markdown("### 📱 Simulator Interactiv (Apasă pe butoane)")
    st.components.v1.html(rezultat_curent["cod_html_svg_interactiv"], height=520, scrolling=True)

    st.markdown("### 🛠️ Componente Desfășurate Identificate")
    for c in rezultat_curent["componente_desfasurate"]:
        st.markdown(f"- **{c['id']}** – {c['nume']} *(Porturi: {', '.join(c['porturi'])})*")

    col1, col2 = st.columns(2)
    with col1:
        dxf_bytes = genereaza_fisier_dxf(rezultat_curent)
        st.download_button(
            "📥 Descarcă .DXF (Import FluidSIM)",
            data=dxf_bytes,
            file_name="schema_fluidsim.dxf",
            mime="application/dxf",
            use_container_width=True
        )
    with col2:
        st.download_button(
            "📥 Descarcă .JSON (Date + Conexiuni)",
            data=json.dumps(rezultat_curent, indent=2, ensure_ascii=False),
            file_name="schema_fluidsim.json",
            mime="application/json",
            use_container_width=True
        )