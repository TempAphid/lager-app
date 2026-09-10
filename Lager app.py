"""
Verksted-- og Lagerinventar (Med dynamiske lagre, bedriftsregistrering og sikker utlogging)
-----------------------------------------------------------------------------------------------------------------
"""

import shutil
import pytesseract
import streamlit as st

tesseract_path = shutil.which("tesseract")
if tesseract_path:
    pytesseract.pytesseract.tesseract_cmd = tesseract_path
    OCR_AVAILABLE = True
else:
    OCR_AVAILABLE = False

# Midlertidig feilsøking
st.write(f"Tesseract funnet på sti: {tesseract_path}")
st.write(f"OCR_AVAILABLE satt til: {OCR_AVAILABLE}")

from datetime import datetime
from contextlib import contextmanager
import hashlib

import pandas as pd
import psycopg2
from psycopg2 import pool
import streamlit as st
import numpy as np
import cv2
import extra_streamlit_components as stx
import bcrypt

try:
    from streamlit_qrcode_scanner import qrcode_scanner
    HAR_QR_MODUL = True
except ImportError:
    HAR_QR_MODUL = False

try:
    import easyocr
    HAR_OCR_MODUL = True
    
    @st.cache_resource
    def hent_ocr_leser():
        return easyocr.Reader(['en'], gpu=False)
except ImportError:
    HAR_OCR_MODUL = False

cookie_manager = stx.CookieManager()

LAV_BEHOLDNING_GRENSE = 1
MAKS_LOGG_RADER = 50

# --------------------------------------------------------------------------
# HJELPEFUNKSJONER
# --------------------------------------------------------------------------
def hash_passord(passord: str) -> str:
    return bcrypt.hashpw(passord.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def sjekk_passord(passord: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(passord.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return hashlib.sha256(passord.encode()).hexdigest() == hashed

# --------------------------------------------------------------------------
# DATABASE CONNECTION POOL (SUPABASE POSTGRESQL)
# --------------------------------------------------------------------------
@st.cache_resource
def hent_tilkoblingspool():
    try:
        db_config = st.secrets["database"]
        return pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dbname=db_config["dbname"],
            user=db_config["user"],
            password=db_config["password"],
            host=db_config["host"],
            port=db_config["port"]
        )
    except Exception as e:
        st.error(f"Kunne ikke opprette databasetilkoblingspool: {e}")
        st.stop()

db_pool = hent_tilkoblingspool()

@contextmanager
def db_handling(feilmelding: str):
    """Henter en midlertidig tilkobling fra poolen, og leverer den automatisk tilbake etter bruk."""
    conn = db_pool.getconn()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        st.session_state.varsel = ("error", f"{feilmelding}: {e}")
        raise
    finally:
        cursor.close()
        db_pool.putconn(conn)

def logg_handling(handling: str, detaljer: str, bedrift_id: int, del_id: int | None = None, endring_antall: int | None = None):
    conn = db_pool.getconn()
    cursor = conn.cursor()
    try:
        bruker_navn = st.session_state.get("bruker", "Ukjent bruker")
        cursor.execute(
            "INSERT INTO historikk (bedrift_id, tidspunkt, bruker, handling, detaljer, del_id, endring_antall) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (bedrift_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), bruker_navn, handling, detaljer, del_id, endring_antall),
        )
        conn.commit()
    finally:
        cursor.close()
        db_pool.putconn(conn)

# --- SIKRER AT TABELLER OG KOLONNER FINNES ---
with db_handling("Feil ved initialisering av tabeller") as cur:
    cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS kode VARCHAR(50);")
    cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS orgnr VARCHAR(50);")
    cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS org_nr VARCHAR(50);")
    cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS bedrift_passord_hash VARCHAR(255);")
    cur.execute("UPDATE bedrifter SET orgnr = org_nr WHERE orgnr IS NULL AND org_nr IS NOT NULL;")
    cur.execute("UPDATE bedrifter SET org_nr = orgnr WHERE org_nr IS NULL AND orgnr IS NOT NULL;")
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS brukere (
            id SERIAL PRIMARY KEY,
            navn VARCHAR(100) NOT NULL,
            jobbmail VARCHAR(150) UNIQUE NOT NULL,
            epost VARCHAR(150),
            passord_hash VARCHAR(255),
            bedrift_id INTEGER,
            opprettet_tid TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lagre (
            id SERIAL PRIMARY KEY,
            bedrift_id INTEGER NOT NULL,
            navn VARCHAR(150) NOT NULL
        );
    """)
    
    cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS passord_hash VARCHAR(255);")
    cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS jobbmail VARCHAR(150);")
    cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS epost VARCHAR(150);")
    cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS bedrift_id INTEGER;")
    cur.execute("UPDATE brukere SET epost = jobbmail WHERE epost IS NULL;")
    
    cur.execute("ALTER TABLE inventar ADD COLUMN IF NOT EXISTS hylle VARCHAR(100);")
    cur.execute("ALTER TABLE historikk ADD COLUMN IF NOT EXISTS del_id INTEGER;")
    cur.execute("ALTER TABLE historikk ADD COLUMN IF NOT EXISTS endring_antall INTEGER;")

st.set_page_config(page_title="Verksted- og Lagerinventar", page_icon="🛠️", layout="wide")

st.markdown(
    """
    <link rel="manifest" href="app/static/manifest.json">
    <meta name="theme-color" content="#0e1117">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <script>
      if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
          navigator.serviceWorker.register('/app/static/sw.js');
        });
      }
    </script>
    """,
    unsafe_allow_html=True
)
# --- SIKRER SESSION STATE ---
for key, default in {
    "side": "Se lager",
    "valgt_id": None,
    "varsel": None,
    "plassering_filter": "Alle",
    "scannet_kode": "",
    "aktiv_bedrift_id": None,
    "bruker": None,
    "epost": None,
    "vis_registrering": False,
    "vis_bedrift_registrering": False,
    "ny_opprettet_bedrift_id": None,
    "ocr_sist_bilde": None,
    "ocr_tekst_input": "",
    "form_key_counter": 0,
    "nettopp_logget_av": False,
    "batch_liste": [],
    "rediger_index": None,
    "rediger_lager_navn": None,
    "kamera_teller": 0
}.items():
    st.session_state.setdefault(key, default)

if st.session_state.get("nettopp_logget_av", False):
    st.session_state.aktiv_bedrift_id = None
    st.session_state.bruker = None
    st.session_state.epost = None
else:
    lagret_bedrift_cookie = cookie_manager.get("aktiv_bedrift_id")
    lagret_bruker_cookie = cookie_manager.get("aktiv_bruker")
    lagret_epost_cookie = cookie_manager.get("aktiv_epost")

    if not st.session_state.aktiv_bedrift_id and lagret_bedrift_cookie:
        try:
            st.session_state.aktiv_bedrift_id = int(lagret_bedrift_cookie)
        except ValueError:
            pass

    if not st.session_state.bruker and lagret_bruker_cookie:
        st.session_state.bruker = lagret_bruker_cookie

    if not st.session_state.epost and lagret_epost_cookie:
        st.session_state.epost = lagret_epost_cookie

def hent_lagre(bedrift_id):
    conn = db_pool.getconn()
    try:
        df_l = pd.read_sql("SELECT navn FROM lagre WHERE bedrift_id = %s ORDER BY navn", conn, params=(bedrift_id,))
        return df_l["navn"].tolist()
    finally:
        db_pool.putconn(conn)

# --------------------------------------------------------------------------
# DIALOGER / POPUPS
# --------------------------------------------------------------------------
@st.dialog("📉 Bekreft vareuttak")
def uttak_dialog(valgt_id, delnavn_org, antall_org, antall_ut, aktiv_bedrift_id):
    gjenstaaende = antall_org - antall_ut
    st.markdown(f"Du tar ut **{antall_ut} stk** av **{delnavn_org}**.")
    st.markdown(f"Antall igjen på lager etter uttak: **{gjenstaaende} stk**")
    
    if gjenstaaende <= 0:
        st.error("⚠️ **Siste del!** Dette tømmer lageret fullstendig for denne varen, og den vil bli slettet automatisk. **Husk å bestille mer!**")
    elif gjenstaaende == 1:
        st.warning("⚠️ **Nest siste del!** Det er kun 1 stk igjen på lager etter dette uttaket. **Vurder å bestille mer snarest.**")
        
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Bekreft uttak", type="primary", use_container_width=True):
            with db_handling("Feil ved uttak") as cur_w:
                if gjenstaaende <= 0:
                    cur_w.execute("DELETE FROM inventar WHERE id = %s", (valgt_id,))
                    logg_handling("Uttak", f"Tok ut siste {antall_ut} stk og slettet '{delnavn_org}'", aktiv_bedrift_id, valgt_id, -antall_ut)
                else:
                    cur_w.execute("UPDATE inventar SET antall = %s WHERE id = %s", (gjenstaaende, valgt_id))
                    logg_handling("Uttak", f"Tok ut {antall_ut} stk av '{delnavn_org}'", aktiv_bedrift_id, valgt_id, -antall_ut)
            st.session_state.varsel = ("success", "Uttak gjennomført!")
            st.session_state.side = "Se lager"
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("⚠️ Manglende obligatoriske felt")
def manglende_felt_dialog(mangler, handling_type, data):
    st.error(f"Følgende obligatoriske felt mangler:\n\n- " + "\n- ".join(mangler))
    st.write("Du kan enten lukke denne, fylle ut feltene, eller krysse av for å tvinge igjennom lagring direkte herfra:")
    
    tving_valg = st.checkbox("⚠️ Tving igjennom lagring (overstyr manglende felt)")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Fortsett lagring", type="primary", use_container_width=True, disabled=not tving_valg):
            if handling_type == "rediger":
                valgt_id, bm, dn, de, ant, plass, hylle, bed_id = data
                with db_handling("Feil ved lagring") as cur_w:
                    cur_w.execute(
                        "UPDATE inventar SET bilmerke = %s, delnavn = %s, delenummer = %s, antall = %s, plassering = %s, hylle = %s WHERE id = %s", 
                        (bm, dn, de, ant, plass, hylle, valgt_id)
                    )
                    logg_handling("Redigering", f"Endret info på '{dn}' (tvunget gjennom)", bed_id, valgt_id)
                st.session_state.form_key_counter += 1
                st.session_state.varsel = ("success", f"Endringer på '{dn}' er lagret!")
                st.session_state.side = "Se lager"
                st.rerun()
            elif handling_type == "ny":
                bm, dn, de, ant, plass, hylle, bed_id = data
                with db_handling("Feil ved lagring") as cur:
                    cur.execute(
                        "INSERT INTO inventar (bedrift_id, bilmerke, delnavn, delenummer, antall, plassering, hylle) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                        (bed_id, bm, dn, de, ant, plass, hylle)
                    )
                    ny_id = cur.fetchone()[0]
                    logg_handling("Ny del", f"La til {ant} stk av '{dn}' (tvunget gjennom)", bed_id, ny_id, ant)
                st.session_state.form_key_counter += 1
                st.success("Ny del lagret!")
                st.rerun()
    with col2:
        if st.button("Tilbake", use_container_width=True):
            st.rerun()

@st.dialog("✏️ Bekreft endringer")
def rediger_dialog(valgt_id, ny_bilmerke, ny_delnavn, ny_delenummer, ny_antall, ny_plassering, ny_hylle, aktiv_bedrift_id):
    st.write("Du er i ferd med å lagre følgende endringer på varen:")
    st.markdown(f"- **Bilmerke:** {ny_bilmerke}")
    st.markdown(f"- **Navn på del:** {ny_delnavn}")
    st.markdown(f"- **Delenummer:** {ny_delenummer if ny_delenummer else '-'}")
    st.markdown(f"- **Antall:** {ny_antall}")
    st.markdown(f"- **Lager / Plassering:** {ny_plassering}")
    st.markdown(f"- **Hylle:** {ny_hylle}")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Lagre endringer", type="primary", use_container_width=True):
            with db_handling("Feil ved lagring") as cur_w:
                cur_w.execute(
                    "UPDATE inventar SET bilmerke = %s, delnavn = %s, delenummer = %s, antall = %s, plassering = %s, hylle = %s WHERE id = %s", 
                    (ny_bilmerke, ny_delnavn, ny_delenummer, ny_antall, ny_plassering, ny_hylle, valgt_id)
                )
                logg_handling("Redigering", f"Endret info på '{ny_delnavn}'", aktiv_bedrift_id, valgt_id)
            st.session_state.form_key_counter += 1
            st.session_state.varsel = ("success", f"Endringer på '{ny_delnavn}' er lagret!")
            st.session_state.side = "Se lager"
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("🗑️ Bekreft permanent sletting")
def slett_dialog(valgt_id, delnavn_org, aktiv_bedrift_id):
    st.warning(f"Er du sikker på at du vil slette **{delnavn_org}** permanent fra lageret? Dette kan ikke angres.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Ja, slett permanent", type="primary", use_container_width=True):
            with db_handling("Feil ved sletting") as cur_w:
                cur_w.execute("DELETE FROM inventar WHERE id = %s", (valgt_id,))
                logg_handling("Sletting", f"Slettet '{delnavn_org}'", aktiv_bedrift_id, valgt_id, -999)
            st.session_state.varsel = ("success", f"'{delnavn_org}' ble slettet.")
            st.session_state.side = "Se lager"
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("🗑️ Bekreft sletting av lager")
def slett_lager_dialog(aktiv_bedrift_id, l_navn):
    st.warning(f"Er du sikker på at du vil slette lageret **'{l_navn}'**? (Deler som ligger her vil fremdeles finnes i systemet, men miste tilknytningen til dette lagernavnet).")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Ja, slett lager", type="primary", use_container_width=True):
            with db_handling("Feil ved sletting av lager") as cur:
                cur.execute("DELETE FROM lagre WHERE bedrift_id = %s AND navn = %s", (aktiv_bedrift_id, l_navn))
            st.session_state.varsel = ("success", f"Lageret '{l_navn}' ble slettet.")
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("➕ Bekreft registrering av ny del")
def ny_del_dialog(bm, dn, de, ant, plass, hylle, aktiv_bedrift_id):
    st.write("Du er i ferd med å legge til en ny del på lageret:")
    st.markdown(f"- **Bilmerke:** {bm}")
    st.markdown(f"- **Navn på del:** {dn}")
    st.markdown(f"- **Delenummer:** {de if de else '-'}")
    st.markdown(f"- **Antall:** {ant}")
    st.markdown(f"- **Lager / Plassering:** {plass}")
    st.markdown(f"- **Hylle:** {hylle}")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Bekreft og lagre", type="primary", use_container_width=True):
            with db_handling("Feil ved lagring") as cur:
                cur.execute(
                    "INSERT INTO inventar (bedrift_id, bilmerke, delnavn, delenummer, antall, plassering, hylle) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                    (aktiv_bedrift_id, bm, dn, de, ant, plass, hylle)
                )
                ny_id = cur.fetchone()[0]
                logg_handling("Ny del", f"La til {ant} stk av '{dn}'", aktiv_bedrift_id, ny_id, ant)
            
            st.session_state.form_key_counter += 1
            st.success("Ny del lagret!")
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

# --------------------------------------------------------------------------
# INNGANGSSIDE / AUTENTISERING
# --------------------------------------------------------------------------
if not st.session_state.aktiv_bedrift_id or not st.session_state.bruker:
    
    if st.session_state.get("nettopp_logget_av", False):
        st.session_state.nettopp_logget_av = False

    if st.session_state.vis_bedrift_registrering:
        st.title("🛠️ Verksted- og Lagerinventar — Registrer bedrift")
        
        with st.form("bedrift_registrering_form"):
            b_navn = st.text_input("Bedrifts navn:")
            b_orgnr = st.text_input("Organisasjons nr:")
            b_kode = st.text_input("Hemmelig bedriftskode (unik kode):")
            b_bedrift_passord = st.text_input("Bedriftens passord / PIN (kreves for at andre skal kunne koble seg til):", type="password")
            submit_bedrift = st.form_submit_button("Neste: Opprett bruker", type="primary", key="submit_ny_bedrift")
            
            if submit_bedrift:
                if b_navn.strip() and b_orgnr.strip() and b_kode.strip() and b_bedrift_passord.strip():
                    try:
                        b_pass_hash = hash_passord(b_bedrift_passord.strip())
                        with db_handling("Feil ved opprettelse av bedrift") as cur:
                            cur.execute(
                                "INSERT INTO bedrifter (navn, orgnr, org_nr, kode, bedrift_passord_hash) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                (b_navn.strip(), b_orgnr.strip(), b_orgnr.strip(), b_kode.strip(), b_pass_hash)
                            )
                            ny_b_id = cur.fetchone()[0]
                            
                            cur.execute("INSERT INTO lagre (bedrift_id, navn) VALUES (%s, %s)", (ny_b_id, "Hovedlager"))
                        
                        st.session_state.ny_opprettet_bedrift_id = ny_b_id
                        st.session_state.vis_bedrift_registrering = False
                        st.session_state.vis_registrering = True 
                        st.success("Bedrift registrert med passordbeskyttelse! Nå må du opprette din bruker.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Feil ved opprettelse av bedrift: {e}")
                else:
                    st.warning("Vennligst fyll ut alle feltene inklusive bedriftens passord.")
        
        if st.button("Tilbake til logg inn"):
            st.session_state.vis_bedrift_registrering = False
            st.rerun()

    elif st.session_state.vis_registrering:
        st.title("🛠️ Verksted- og Lagerinventar — Registrering")
        
        kom_fra_ny_bedrift = st.session_state.ny_opprettet_bedrift_id is not None
        if kom_fra_ny_bedrift:
            with db_handling("Feil ved henting av bedrift") as cur:
                cur.execute("SELECT navn FROM bedrifter WHERE id = %s", (st.session_state.ny_opprettet_bedrift_id,))
                b_treff = cur.fetchone()
            st.info(f"Oppretter bruker for ny bedrift: **{b_treff[0] if b_treff else ''}**")

        with st.form("registrering_form"):
            reg_navn = st.text_input("Ditt navn:")
            reg_mail = st.text_input("Din mail:")
            reg_passord = st.text_input("Ditt personlige passord:", type="password")
            
            if not kom_fra_ny_bedrift:
                reg_kode = st.text_input("Bedriftens hemmelige kode:")
                reg_bedrift_passord = st.text_input("Bedriftens passord:", type="password")
            else:
                reg_kode = ""
                reg_bedrift_passord = ""
                
            submit_reg = st.form_submit_button("Registrer", type="primary")
            
            if submit_reg:
                if reg_navn.strip() and reg_mail.strip() and reg_passord.strip() and (kom_fra_ny_bedrift or (reg_kode.strip() and reg_bedrift_passord.strip())):
                    try:
                        with db_handling("Feil ved registrering") as cur:
                            if kom_fra_ny_bedrift:
                                b_id = st.session_state.ny_opprettet_bedrift_id
                            else:
                                cur.execute("SELECT id, bedrift_passord_hash FROM bedrifter WHERE kode = %s", (reg_kode.strip(),))
                                bedrift_treff = cur.fetchone()
                                if bedrift_treff:
                                    b_id, db_bedrift_hash = bedrift_treff
                                    
                                    if db_bedrift_hash and not sjekk_passord(reg_bedrift_passord.strip(), db_bedrift_hash):
                                        b_id = None
                                        st.error("Feil passord for denne bedriften!")
                                else:
                                    b_id = None
                                    st.error("Ugyldig hemmelig bedriftskode.")
                            
                            if b_id:
                                skrevet_hash = hash_passord(reg_passord.strip())
                                
                                cur.execute(
                                    "INSERT INTO brukere (navn, jobbmail, epost, passord_hash, bedrift_id) VALUES (%s, %s, %s, %s, %s)",
                                    (reg_navn.strip(), reg_mail.strip().lower(), reg_mail.strip().lower(), skrevet_hash, b_id)
                                )
                                
                                st.session_state.bruker = reg_navn.strip()
                                st.session_state.epost = reg_mail.strip().lower()
                                st.session_state.aktiv_bedrift_id = b_id
                                st.session_state.ny_opprettet_bedrift_id = None
                                
                                cookie_manager.set("aktiv_bedrift_id", str(b_id), max_age=30*24*60*60)
                                cookie_manager.set("aktiv_bruker", reg_navn.strip(), max_age=30*24*60*60)
                                cookie_manager.set("aktiv_epost", reg_mail.strip().lower(), max_age=30*24*60*60)
                                
                                st.session_state.vis_registrering = False
                                st.success("Bruker opprettet og innlogget!")
                                st.rerun()
                    except Exception as e:
                        st.error(f"Feil ved registrering: {e}")
                else:
                    st.warning("Vennligst fyll ut alle feltene (inkludert bedriftspassord).")
        
        if st.button("Tilbake til logg inn"):
            st.session_state.vis_registrering = False
            st.session_state.ny_opprettet_bedrift_id = None
            st.rerun()

    else:
        st.title("🛠️ Verksted- og Lagerinventar — Logg inn")
        
        with st.form("innlogging_form"):
            inn_mail = st.text_input("Mail:")
            inn_passord = st.text_input("Passord:", type="password")
            husk_meg = st.checkbox("Husk meg på denne enheten", value=True)
            submit_inn = st.form_submit_button("Logg inn", type="primary")
            
            if submit_inn:
                if inn_mail.strip() and inn_passord.strip():
                    try:
                        with db_handling("Feil ved innlogging") as cur:
                            cur.execute("SELECT navn, passord_hash, bedrift_id FROM brukere WHERE jobbmail = %s", (inn_mail.strip().lower(),))
                            bruker_treff = cur.fetchone()
                        
                        if bruker_treff:
                            db_navn, db_hash, db_bedrift_id = bruker_treff
                            
                            if sjekk_passord(inn_passord.strip(), db_hash):
                                st.session_state.bruker = db_navn
                                st.session_state.epost = inn_mail.strip().lower()
                                st.session_state.aktiv_bedrift_id = db_bedrift_id
                                
                                if husk_meg:
                                    if db_bedrift_id:
                                        cookie_manager.set("aktiv_bedrift_id", str(db_bedrift_id), max_age=30*24*60*60)
                                    cookie_manager.set("aktiv_bruker", db_navn, max_age=30*24*60*60)
                                    cookie_manager.set("aktiv_epost", inn_mail.strip().lower(), max_age=30*24*60*60)
                                    
                                st.success("Innlogget!")
                                st.rerun()
                            else:
                                st.error("Feil passord.")
                                st.stop()
                        else:
                            st.error("Fant ingen bruker med denne mailen.")
                    except Exception as e:
                        st.error(f"Feil ved innlogging: {e}")
                else:
                    st.warning("Vennligst fyll ut både mail og passord.")
        
        if st.button("Har du ikke bruker? Trykk her for å registrere", use_container_width=True):
            st.session_state.vis_registrering = True
            st.rerun()
            
        if st.button("Registrer ny bedrift", use_container_width=True):
            st.session_state.vis_bedrift_registrering = True
            st.rerun()

    st.stop()

# --------------------------------------------------------------------------
# HENT AKTIV BEDRIFTSINFO NÅR INNLOGGET
# --------------------------------------------------------------------------
aktiv_bedrift_id = st.session_state.aktiv_bedrift_id
conn_temp = db_pool.getconn()
try:
    df_aktiv = pd.read_sql("SELECT navn FROM bedrifter WHERE id = %s", conn_temp, params=(aktiv_bedrift_id,))
finally:
    db_pool.putconn(conn_temp)

if df_aktiv.empty:
    try:
        cookie_manager.delete("aktiv_bedrift_id")
    except Exception:
        pass
    st.session_state.aktiv_bedrift_id = None
    st.rerun()

valgt_bedrift_navn = df_aktiv.iloc[0]["navn"]
tilgjengelige_lagre = hent_lagre(aktiv_bedrift_id)

st.title(f"🛠️ Verksted- och Lagerinventar — {valgt_bedrift_navn}")

# --- SIDEMENY ---
st.sidebar.header("👤 Innlogget sesjon")
st.sidebar.info(f"Bedrift: **{valgt_bedrift_navn}**\n\nBruker: **{st.session_state.bruker}**\n*({st.session_state.get('epost', '')})*")

if st.sidebar.button("🔄 Logg av / Bytt bruker"):
    try:
        cookie_manager.delete("aktiv_bedrift_id")
        cookie_manager.delete("aktiv_bruker")
        cookie_manager.delete("aktiv_epost")
    except Exception:
        pass
    st.session_state.aktiv_bedrift_id = None
    st.session_state.bruker = None
    st.session_state.epost = None
    st.session_state.nettopp_logget_av = True
    
    st.markdown("<script>window.location.href = window.location.href + '?t=' + new Date().getTime();</script>", unsafe_allow_html=True)
    st.rerun()

st.sidebar.divider()

if st.session_state.varsel:
    type_varsel, melding_tekst = st.session_state.varsel
    getattr(st, type_varsel, st.info)(melding_tekst)
    st.session_state.varsel = None

# --- MENY-KNAPPER ØVERST ---
sider = [
    ("📋 Se lager", "Se lager"),
    ("📷 Skann / OCR", "Skann og OCR"),
    ("🛒 Bestill", "Bestillingsliste"),
    ("📦 Administrer", "Administrer deler"),
    ("➕ Legg til", "Legg til ny del"),
    ("🏢 Opprett lagre", "Opprett lagre"),
    ("📜 Logg", "Logg"),
]

menykolonner = st.columns(len(sider))
for kolonne, (etikett, side_navn) in zip(menykolonner, sider):
    with kolonne:
        if st.button(etikett, use_container_width=True, type="primary" if st.session_state.side == side_navn else "secondary"):
            st.session_state.side = side_navn
            st.rerun()

st.divider()

# --- 1. SE LAGER & SØK ---
if st.session_state.side == "Se lager":
    st.header(f"Oversikt over lageret — {valgt_bedrift_navn}")
    conn_temp = db_pool.getconn()
    try:
        df = pd.read_sql("SELECT * FROM inventar WHERE bedrift_id = %s", conn_temp, params=(aktiv_bedrift_id,))
    finally:
        db_pool.putconn(conn_temp)

    if not df.empty:
        if tilgjengelige_lagre:
            st.write("📍 **Filtrer direkte på lagerlokasjon:**")
            p_cols = st.columns(len(tilgjengelige_lagre) + 1)
            alle_valg = ["Alle"] + tilgjengelige_lagre
            for kol, p_navn in zip(p_cols, alle_valg):
                with kol:
                    if st.button(p_navn, use_container_width=True, type="primary" if st.session_state.plassering_filter == p_navn else "secondary"):
                        st.session_state.plassering_filter = p_navn
                        st.rerun()

        st.markdown("---")
        sok = st.text_input("🔍 Søk på delenummer, bilmerke, delnavn, plassering eller hylle...", value=st.session_state.scannet_kode).lower()
        st.session_state.scannet_kode = ""

        df_filtered = df.copy()
        if st.session_state.plassering_filter != "Alle":
            df_filtered = df_filtered[df_filtered["plassering"] == st.session_state.plassering_filter]
        if sok:
            df_filtered = df_filtered[
                df_filtered["bilmerke"].str.lower().str.contains(sok, na=False)
                | df_filtered["delnavn"].str.lower().str.contains(sok, na=False)
                | df_filtered["delenummer"].str.lower().str.contains(sok, na=False)
                | df_filtered["plassering"].str.lower().str.contains(sok, na=False)
                | df_filtered["hylle"].str.lower().str.contains(sok, na=False)
            ]

        # --- PAGINERING ---
        rader_per_side = 50
        totalt_rader = len(df_filtered)
        
        start_idx = 0
        df_visning_side = df_filtered.copy()
        
        if totalt_rader > rader_per_side:
            antall_sider = (totalt_rader // rader_per_side) + (1 if totalt_rader % rader_per_side > 0 else 0)
            
            col_p1, col_p2 = st.columns([2, 4])
            with col_p1:
                side_valg = st.selectbox("Side:", range(1, antall_sider + 1), format_func=lambda x: f"Side {x} av {antall_sider}", key="paginering_side")
            with col_p2:
                st.caption(f"Viser {rader_per_side} av totalt {totalt_rader} rader")
                
            start_idx = (side_valg - 1) * rader_per_side
            slutt_idx = start_idx + rader_per_side
            df_visning_side = df_filtered.iloc[start_idx:slutt_idx]
        else:
            st.caption(f"Viser alle {totalt_rader} rader")

        kolonner_aa_vise = [c for c in df_filtered.columns if c not in ["id", "bedrift_id"]]
        df_tabell_visning = df_visning_side[kolonner_aa_vise]

        # Konfigurer tabellen slik at man kan klikke for å velge/redigere
        event_valg = st.dataframe(
            df_tabell_visning,
            use_container_width=True,
            height=400,
            key="lager_tabell",
            selection_mode="single-row",
            on_select="rerun"
        )

        # Håndter valg av rad i tabellen for å hoppe til Administrer
        try:
            valgte_rader = event_valg.get("selection", {}).get("rows", [])
            if valgte_rader:
                valgt_rad_index = valgte_rader[0]
                faktisk_id = df_visning_side.iloc[valgt_rad_index]["id"]
                st.session_state.valgt_id = int(faktisk_id)
                st.session_state.side = "Administrer deler"
                st.rerun()
        except Exception:
            pass

        csv_data = df_tabell_visning.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Last ned vist lagerliste som CSV",
            data=csv_data,
            file_name=f"lager_oversikt_{valgt_bedrift_navn}.csv",
            mime="text/csv",
        )
    else:
        st.info("Lageret for denne bedriften er helt tomt ennå. Opprett gjerne lagre og legg til deler!")

# --- 2. SKANN OG OCR ---
elif st.session_state.side == "Skann og OCR":
    st.header("📷 Skann / OCR")
    
    valg_metode = st.radio("Velg metode:", ["📌 QR / Strekkode", "📸 OCR (Les av bilde)"], horizontal=True)
    
    if valg_metode.startswith("📌"):
        st.info("Skann QR- eller strekkode for å finne varen direkte.")
        if HAR_QR_MODUL:
            scannet_verdi = qrcode_scanner(key="qr_scanner_enkelt")
            if scannet_verdi:
                with db_handling("Feil ved QR-søk") as cur:
                    cur.execute("SELECT id FROM inventar WHERE bedrift_id = %s AND delenummer = %s", (aktiv_bedrift_id, scannet_verdi))
                    treff = cur.fetchone()
                if treff:
                    st.session_state.valgt_id = treff[0]
                    st.session_state.side = "Administrer deler"
                else:
                    st.warning(f"Fant ikke delenummer '{scannet_verdi}' i lageret.")
                st.rerun()
        else:
            st.error("QR-modul ikke installert.")
            
    else:
        if HAR_OCR_MODUL:
            st.info("💡 **Tips:** Ta bilde av et delenummer. Bildet blir automatisk behandlet, lagt til i listen, og kameraet åpner seg igjen med en gang for neste bilde!")
            
            kamera_bilde = st.camera_input("Ta bilde av et delenummer", key=f"kamera_input_{st.session_state.kamera_teller}")
            
            if kamera_bilde:
                with st.spinner("Analyserer bilde og nullstiller kamera..."):
                    cv_img = cv2.imdecode(np.frombuffer(kamera_bilde.getvalue(), np.uint8), cv2.IMREAD_COLOR)
                    reader = hent_ocr_leser()
                    resultater = reader.readtext(cv_img)
                    
                    funn = [t.strip() for _, t, p in resultater if p > 0.15 and len(t.strip()) > 1]
                    
                    nye_lagt_til = 0
                    for tekst_funn in funn:
                        if tekst_funn and tekst_funn not in st.session_state.batch_liste:
                            st.session_state.batch_liste.append(tekst_funn)
                            nye_lagt_til += 1
                            
                    if nye_lagt_til > 0:
                        st.success(f"La til {nye_lagt_til} delenummer i listen!")
                    else:
                        st.warning("Fant ingen gyldige delenumre i dette bildet.")
                
                st.session_state.kamera_teller += 1
                st.rerun()

            st.divider()

            st.markdown(f"### 📋 Skannet liste ({len(st.session_state.batch_liste)} deler):")
            
            if st.session_state.batch_liste:
                with db_handling("Feil ved henting av batch") as cur:
                    cur.execute(
                        """
                        SELECT id, bilmerke, delnavn, delenummer, antall, plassering, hylle 
                        FROM inventar 
                        WHERE bedrift_id = %s AND delenummer = ANY(%s)
                        """,
                        (aktiv_bedrift_id, st.session_state.batch_liste)
                    )
                    treff = cur.fetchall()
                funnede_delenummer = {t[3].lower(): t for t in treff}
                
                for idx, soke_nr in enumerate(st.session_state.batch_liste):
                    soke_lower = soke_nr.lower()
                    status = "✅ På lager" if soke_lower in funnede_delenummer else "❌ Ikke på lager"
                    t = funnede_delenummer.get(soke_lower)
                    
                    delnavn_vis = t[2] if t else "-"
                    bilmerke_vis = t[1] if t else "-"
                    antall_vis = t[4] if t else 0
                    
                    if st.session_state.rediger_index == idx:
                        with st.container(border=True):
                            st.write(f"Korrigér delenummer (opprinnelig lest: *{soke_nr}*):")
                            nytt_nr_input = st.text_input("Delenummer", value=soke_nr, key=f"edit_nr_{idx}")
                            col_e1, col_e2 = st.columns(2)
                            with col_e1:
                                if st.button("Lagre endring", key=f"lagre_btn_{idx}", type="primary", use_container_width=True):
                                    st.session_state.batch_liste[idx] = nytt_nr_input.strip()
                                    st.session_state.rediger_index = None
                                    st.rerun()
                            with col_e2:
                                if st.button("Avbryt", key=f"avbryt_btn_{idx}", use_container_width=True):
                                    st.session_state.rediger_index = None
                                    st.rerun()
                    else:
                        col_info1, col_info2, col_info3, col_info4, col_red, col_del = st.columns([2, 2, 2, 1, 1, 1])
                        with col_info1:
                            st.markdown(f"**{soke_nr}**")
                        with col_info2:
                            st.markdown(status)
                        with col_info3:
                            st.markdown(f"{delnavn_vis} ({bilmerke_vis})")
                        with col_info4:
                            st.markdown(f"Ant: {antall_vis}")
                        with col_red:
                            if st.button("✏️ Endre", key=f"rediger_batch_{idx}", use_container_width=True):
                                st.session_state.rediger_index = idx
                                st.rerun()
                        with col_del:
                            if st.button("🗑️ Fjern", key=f"fjern_batch_{idx}", use_container_width=True):
                                st.session_state.batch_liste.pop(idx)
                                st.rerun()
                
                st.divider()
                col_b1, col_b2 = st.columns(2)
                with col_b1:
                    if st.button("🗑️ Tøm hele listen", use_container_width=True):
                        st.session_state.batch_liste = []
                        st.session_state.rediger_index = None
                        st.rerun()
                with col_b2:
                    eksport_data = []
                    for soke_nr in st.session_state.batch_liste:
                        t = funnede_delenummer.get(soke_nr.lower())
                        eksport_data.append({
                            "Delenummer": soke_nr,
                            "Status": "✅ På lager" if t else "❌ Ikke på lager",
                            "Delnavn": t[2] if t else "-",
                            "Bilmerke": t[1] if t else "-",
                            "Antall": t[4] if t else 0,
                            "Lager": t[5] if t else "-",
                            "Hylle": t[6] if t else "-"
                        })
                    df_batch = pd.DataFrame(eksport_data)
                    csv_b = df_batch.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        label="📥 Last ned sjekkliste",
                        data=csv_b,
                        file_name=f"batch_sjekk_{valgt_bedrift_navn}.csv",
                        mime="text/csv",
                        use_container_width=True
                    )
            else:
                st.caption("Ingen deler registrert i listen enda. Ta bilde av det første delenummeret over.")
        else:
            st.error("OCR-modul ikke installert.")

# --- 3. BESTILLINGSLISTE ---
elif st.session_state.side == "Bestillingsliste":
    st.header(f"🛒 Bestillingsliste — {valgt_bedrift_navn}")
    conn_temp = db_pool.getconn()
    try:
        df_lav = pd.read_sql("SELECT * FROM inventar WHERE bedrift_id = %s AND antall <= %s ORDER BY antall ASC", conn_temp, params=(aktiv_bedrift_id, LAV_BEHOLDNING_GRENSE))
    finally:
        db_pool.putconn(conn_temp)

    if not df_lav.empty:
        kolonner_aa_vise_lav = [c for c in df_lav.columns if c not in ["id", "bedrift_id"]]
        df_lav_visning = df_lav[kolonner_aa_vise_lav]

        st.dataframe(df_lav_visning, use_container_width=True, height=400)
        bestill_csv = df_lav_visning.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Last ned bestillingsliste som CSV",
            data=bestill_csv,
            file_name=f"bestillingsliste_{valgt_bedrift_navn}.csv",
            mime="text/csv",
        )
    else:
        st.success("Ingen deler med lav beholdning.")

# --- 4. ADMINISTRER DELER ---
elif st.session_state.side == "Administrer deler":
    st.header("📦 Administrer deler")
    with db_handling("Feil ved henting av deler") as cur:
        cur.execute("SELECT id, delnavn, bilmerke, delenummer, antall, plassering, hylle FROM inventar WHERE bedrift_id = %s ORDER BY delnavn", (aktiv_bedrift_id,))
        deler = cur.fetchall()
    
    if deler:
        def lag_etikett(row):
            d_id, d_navn, d_merke, d_nr, d_ant, d_plass, d_hylle = row
            hylle_info = f" - Hylle: {d_hylle}" if d_hylle else ""
            return f"{d_navn} ({d_merke} - Delenr: {d_nr or 'Ingen'} - Ant: {d_ant} stk - Lager: {d_plass}{hylle_info})"

        deler_dict = {lag_etikett(row): row[0] for row in deler}
        
        standard_indeks = 0
        if st.session_state.valgt_id:
            for i, d_id in enumerate(deler_dict.values()):
                if d_id == st.session_state.valgt_id:
                    standard_indeks = i
                    break

        valgt_id = deler_dict[st.selectbox("Velg del:", list(deler_dict.keys()), index=standard_indeks)]
        if st.session_state.valgt_id != valgt_id:
            st.session_state.valgt_id = valgt_id
        
        with db_handling("Feil ved henting av deldetaljer") as cur:
            cur.execute("SELECT bilmerke, delnavn, delenummer, antall, plassering, hylle FROM inventar WHERE id = %s", (valgt_id,))
            res = cur.fetchone()
        bilmerke_org, delnavn_org, delenummer_org, antall_org, plassering_org, hylle_org = res

        handling = st.radio("Handling:", ["📉 Ta ut", "✏️ Rediger", "🗑️ Slett"])
        
        if handling == "📉 Ta ut":
            antall_ut = st.number_input("Antall som skal tas ut:", min_value=1, max_value=max(antall_org, 1), value=1)
            if st.button("Gå til uttak", type="primary"):
                uttak_dialog(valgt_id, delnavn_org, antall_org, antall_ut, aktiv_bedrift_id)
                
        elif handling == "✏️ Rediger":
            with st.form(f"rediger_form_{st.session_state.form_key_counter}"):
                ny_bilmerke = st.text_input("Bilmerke *", value=bilmerke_org)
                ny_delnavn = st.text_input("Navn på del *", value=delnavn_org)
                ny_delenummer = st.text_input("Delenummer (valgfritt)", value=delenummer_org if delenummer_org else "")
                ny_antall = st.number_input("Antall *", min_value=0, value=antall_org)
                
                p_idx = tilgjengelige_lagre.index(plassering_org) if plassering_org in tilgjengelige_lagre else 0
                ny_plassering = st.selectbox("Lager / Plassering *", tilgjengelige_lagre, index=p_idx)
                ny_hylle = st.text_input("Hylle * (f.eks. Hylle 3B)", value=hylle_org if hylle_org else "")
                
                submit_rediger = st.form_submit_button("Forhåndsvis endringer")
                
                if submit_rediger:
                    mangler = []
                    if not ny_bilmerke.strip(): mangler.append("Bilmerke")
                    if not ny_delnavn.strip(): mangler.append("Navn på del")
                    if not ny_plassering: mangler.append("Lager")
                    if not ny_hylle.strip(): mangler.append("Hylle")
                    
                    if not mangler:
                        rediger_dialog(valgt_id, ny_bilmerke.strip(), ny_delnavn.strip(), ny_delenummer.strip(), ny_antall, ny_plassering, ny_hylle.strip(), aktiv_bedrift_id)
                    else:
                        data_pakke = (valgt_id, ny_bilmerke.strip(), ny_delnavn.strip(), ny_delenummer.strip(), ny_antall, ny_plassering, ny_hylle.strip(), aktiv_bedrift_id)
                        manglende_felt_dialog(mangler, "rediger", data_pakke)
                    
        elif handling == "🗑️ Slett":
            if st.button("Slett permanent", type="primary"):
                slett_dialog(valgt_id, delnavn_org, aktiv_bedrift_id)
    else:
        st.info("Lageret er tomt.")

# --- 5. LEGG TIL NY DEL ---
elif st.session_state.side == "Legg til ny del":
    st.header(f"Registrer ny del — {valgt_bedrift_navn}")
    
    if not tilgjengelige_lagre:
        st.warning("⚠️ Du må opprette minst ett lager under 'Opprett lagre' før du kan legge til deler!")
    else:
        with st.form(f"ny_del_{st.session_state.form_key_counter}"):
            bm = st.text_input("Bilmerke *")
            dn = st.text_input("Navn på del *")
            de = st.text_input("Delenummer (valgfritt)")
            ant = st.number_input("Antall *", min_value=1, value=1)
            plass = st.selectbox("Lager / Plassering *", tilgjengelige_lagre)
            hylle = st.text_input("Hylle * (f.eks. Hylle 3B / Rad 2)")
            
            submit_ny = st.form_submit_button("Forhåndsvis og lagre")
            
            if submit_ny:
                mangler = []
                if not bm.strip(): mangler.append("Bilmerke")
                if not dn.strip(): mangler.append("Navn på del")
                if not plass: mangler.append("Lager")
                if not hylle.strip(): mangler.append("Hylle")
                
                if not mangler:
                    ny_del_dialog(bm.strip(), dn.strip(), de.strip(), ant, plass, hylle.strip(), aktiv_bedrift_id)
                else:
                    data_pakke = (bm.strip(), dn.strip(), de.strip(), ant, plass, hylle.strip(), aktiv_bedrift_id)
                    manglende_felt_dialog(mangler, "ny", data_pakke)

# --- 6. OPPRETT OG REDIGER LAGRE ---
elif st.session_state.side == "Opprett lagre":
    st.header(f"🏢 Administrer lagerlokasjoner — {valgt_bedrift_navn}")
    
    st.write("Her kan du legge til nye lagerlokasjoner, eller endre navn på eksisterende lagre (alle tilknyttede deler oppdateres automatisk).")
    
    with st.form("nytt_lager_form", clear_on_submit=True):
        nytt_lager_navn = st.text_input("Navn på nytt lager:")
        submit_nytt_lager = st.form_submit_button("Opprett lager", type="primary")
        
        if submit_nytt_lager:
            if nytt_lager_navn.strip():
                try:
                    with db_handling("Feil ved opprettelse av lager") as cur:
                        cur.execute("INSERT INTO lagre (bedrift_id, navn) VALUES (%s, %s)", (aktiv_bedrift_id, nytt_lager_navn.strip()))
                    st.session_state.varsel = ("success", f"Lageret '{nytt_lager_navn.strip()}' ble opprettet!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Feil ved opprettelse av lager: {e}")
            else:
                st.warning("Vennligst skriv inn et navn på lageret.")
                
    st.divider()
    st.subheader("Eksisterende lagre:")
    if tilgjengelige_lagre:
        for l_navn in tilgjengelige_lagre:
            with st.container(border=True):
                if st.session_state.rediger_lager_navn == l_navn:
                    st.write(f"Endre navn på lager: **{l_navn}**")
                    nytt_navn_input = st.text_input("Nytt navn", value=l_navn, key=f"input_nytt_navn_{l_navn}")
                    
                    col_re1, col_re2 = st.columns(2)
                    with col_re1:
                        if st.button("Lagre nytt navn", key=f"lagre_lager_{l_navn}", type="primary", use_container_width=True):
                            if nytt_navn_input.strip() and nytt_navn_input.strip() != l_navn:
                                try:
                                    with db_handling("Feil ved oppdatering av lager") as cur:
                                        cur.execute(
                                            "UPDATE lagre SET navn = %s WHERE bedrift_id = %s AND navn = %s",
                                            (nytt_navn_input.strip(), aktiv_bedrift_id, l_navn)
                                        )
                                        cur.execute(
                                            "UPDATE inventar SET plassering = %s WHERE bedrift_id = %s AND plassering = %s",
                                            (nytt_navn_input.strip(), aktiv_bedrift_id, l_navn)
                                        )
                                    st.session_state.rediger_lager_navn = None
                                    st.session_state.varsel = ("success", f"Lageret '{l_navn}' ble endret til '{nytt_navn_input.strip()}'! Alle deler ble oppdatert.")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Klarte ikke å oppdatere lageret: {e}")
                            else:
                                st.warning("Skriv inn et nytt og gyldig navn.")
                    with col_re2:
                        if st.button("Avbryt", key=f"avbryt_lager_{l_navn}", use_container_width=True):
                            st.session_state.rediger_lager_navn = None
                            st.rerun()
                else:
                    col_l1, col_l2, col_l3 = st.columns([3, 1, 1])
                    with col_l1:
                        st.markdown(f"📦 **{l_navn}**")
                    with col_l2:
                        if st.button("✏️ Rediger", key=f"rediger_lager_btn_{l_navn}", use_container_width=True):
                            st.session_state.rediger_lager_navn = l_navn
                            st.rerun()
                    with col_l3:
                        if st.button("🗑️ Slett", key=f"slett_lager_{l_navn}", type="secondary", use_container_width=True):
                            slett_lager_dialog(aktiv_bedrift_id, l_navn)
    else:
        st.info("Ingen lagre registrert ennå.")

# --- 7. LOGG ---
elif st.session_state.side == "Logg":
    st.header(f"📜 Logg — {valgt_bedrift_navn}")
    st.write("Her kan du se nylige handlinger som har blitt utført på lageret.")
    
    with db_handling("Feil ved henting av logg") as cur:
        cur.execute("SELECT tidspunkt, bruker, handling, detaljer FROM historikk WHERE bedrift_id = %s ORDER BY id DESC LIMIT %s", (aktiv_bedrift_id, MAKS_LOGG_RADER))
        logg_poster = cur.fetchall()
    
    if logg_poster:
        for tid, brukt_av, hand, det in logg_poster:
            with st.container(border=True):
                st.markdown(f"**[{tid}] {brukt_av}** — *{hand}*\n\n{det}")
    else:
        st.info("Ingen historikk å vise enda.")
