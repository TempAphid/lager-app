"""Verksted-- og Lagerinventar """

from datetime import datetime, timedelta, date
import random
import string
from contextlib import contextmanager
import hashlib
import os
import json
import time
import resend
import secrets
import pandas as pd
import psycopg2
from psycopg2 import pool
import streamlit as st
import numpy as np
import cv2
import extra_streamlit_components as stx
import bcrypt
import logging
import html

try:
    from streamlit_qrcode_scanner import qrcode_scanner
    HAR_QR_MODUL = True
except ImportError:
    HAR_QR_MODUL = False

try:
    import pytesseract
    HAR_OCR_MODUL = True
except ImportError:
    HAR_OCR_MODUL = False

cookie_manager = stx.CookieManager()

LAV_BEHOLDNING_GRENSE = 1
MAKS_LOGG_RADER = 50
BACKUP_ETTER_ANTALL_ENDRINGER = 5  # Kjør backup automatisk etter dette antall endringer

def hash_passord(passord: str) -> str:
    return bcrypt.hashpw(passord.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def sjekk_passord(passord: str, hashed: str) -> bool:
    if not hashed:
        return False
    return bcrypt.checkpw(passord.encode('utf-8'), hashed.encode('utf-8'))

def hash_sesjon_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lag_backup(bedrift_id: int):
    """
    Henter alle rader som tilhører bedriften og lagrer som JSON i databasen.
    Én rad per bedrift i backups-tabellen — overskrives hver gang.
    Kan lastes ned via appen.
    """
    tabeller = {
        "inventar":             "SELECT * FROM inventar WHERE bedrift_id = %s",
        "lagre":                "SELECT * FROM lagre WHERE bedrift_id = %s",
        "historikk":            "SELECT * FROM historikk WHERE bedrift_id = %s",
        "samarbeid_relasjoner": """
            SELECT * FROM samarbeid_relasjoner
            WHERE eier_bedrift_id = %s OR partner_bedrift_id = %s
        """,
    }

    backup_data = {
        "bedrift_id": bedrift_id,
        "tidspunkt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tabeller": {}
    }

    conn = db_pool.getconn()
    try:
        for tabell, query in tabeller.items():
            try:
                params = (bedrift_id, bedrift_id) if tabell == "samarbeid_relasjoner" else (bedrift_id,)
                df = pd.read_sql(query, conn, params=params)
                for col in df.columns:
                    df[col] = df[col].astype(str)
                backup_data["tabeller"][tabell] = df.to_dict(orient="records")
            except Exception as e:
                backup_data["tabeller"][tabell] = f"FEIL: {e}"
    finally:
        db_pool.putconn(conn)

    backup_json = json.dumps(backup_data, ensure_ascii=False, indent=2)

    # Lagre/overskriv i backups-tabellen (én rad per bedrift)
    with db_handling("Feil ved lagring av backup") as cur:
        cur.execute("""
            INSERT INTO backups (bedrift_id, tidspunkt, data)
            VALUES (%s, %s, %s)
            ON CONFLICT (bedrift_id)
            DO UPDATE SET tidspunkt = EXCLUDED.tidspunkt, data = EXCLUDED.data
        """, (bedrift_id, datetime.now(), backup_json))

    return backup_json


def tell_endring_og_backup(bedrift_id: int):
    """
    Teller opp endringer og kjører automatisk backup hver BACKUP_ETTER_ANTALL_ENDRINGER.
    """
    st.session_state["endring_teller"] = st.session_state.get("endring_teller", 0) + 1

    if st.session_state["endring_teller"] >= BACKUP_ETTER_ANTALL_ENDRINGER:
        try:
            lag_backup(bedrift_id)
            st.session_state["endring_teller"] = 0
            st.session_state["siste_backup_tid"] = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            logging.exception("Automatisk backup feilet: %s", e)


def tilbakekall_sesjon(token: str | None) -> None:
    if not token:
        return
    token_hash = hash_sesjon_token(token)
    with db_handling("Feil ved tilbakekalling av sesjon") as cur:
        cur.execute("DELETE FROM bruker_sesjoner WHERE token_hash = %s", (token_hash,))


def opprett_sesjon(bruker_id: int, husk_meg: bool = True) -> str:
    token = secrets.token_urlsafe(48)
    token_hash = hash_sesjon_token(token)
    utlop_tid = datetime.now() + (timedelta(days=30) if husk_meg else timedelta(hours=12))

    with db_handling("Feil ved opprettelse av sesjon") as cur:
        cur.execute(
            """
            INSERT INTO bruker_sesjoner
                (bruker_id, token_hash, opprettet_tid, utlop_tid, sist_brukt_tid, tilbakekalt_tid)
            VALUES (%s, %s, %s, %s, %s, NULL)
            """,
            (bruker_id, token_hash, datetime.now(), utlop_tid, datetime.now())
        )

    if husk_meg:
        cookie_manager.set("sesjon_token", token, max_age=30 * 24 * 60 * 60, key="set_sesjon_token")
    else:
        cookie_manager.set("sesjon_token", token, key="set_sesjon_token")

    return token

def send_deleforesporsel_epost(mottaker_eposter, avsender_bedrift_navn, bilmerke, delenr, delenavn, avsender_kontakt, plassering_info):
    try:
        avsender_bedrift_navn = html.escape(str(avsender_bedrift_navn or ""))
        bilmerke = html.escape(str(bilmerke or ""))
        delenr = html.escape(str(delenr or ""))
        delenavn = html.escape(str(delenavn or ""))
        avsender_kontakt = html.escape(str(avsender_kontakt or ""))
        plassering_info = html.escape(str(plassering_info or ""))

        try:
            resend.api_key = st.secrets["resend"]["RESEND_API_KEY"]
        except Exception:
            resend.api_key = os.environ.get("RESEND_API_KEY", "")
        if not resend.api_key:
            st.error("Resend API-nøkkel mangler.")
            return False
        
        emne = f"Deleforesørsel fra {avsender_bedrift_navn}"
        
        innhold = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e0e0e0; border-radius: 8px; background-color: #f9f9f9;">
            <h2 style="color: #1f2937; margin-top: 0; font-size: 20px;">Ny deleforespørsel 📦</h2>
            
            <p style="color: #4b5563; font-size: 15px; line-height: 1.5;">
                <strong>{avsender_bedrift_navn}</strong> har sendt en forespørsel på en del fra deres lager:
            </p>
            
            <div style="background-color: #ffffff; padding: 15px 20px; border-radius: 6px; border: 1px solid #e5e7eb; margin: 20px 0;">
                <p style="margin: 8px 0; color: #1f2937; font-size: 15px;"><strong>Bilmerke:</strong> {bilmerke}</p>
                <p style="margin: 8px 0; color: #1f2937; font-size: 15px;"><strong>Delenr:</strong> {delenr or '-'}</p>
                <p style="margin: 8px 0; color: #1f2937; font-size: 15px;"><strong>Delenavn:</strong> {delenavn}</p>
                <p style="margin: 8px 0; color: #1f2937; font-size: 15px;"><strong>Lagerplassering:</strong> {plassering_info}</p>
                <p style="margin: 8px 0; color: #1f2937; font-size: 15px;"><strong>Kundekontakt:</strong> {avsender_kontakt}</p>
            </div>
            
            <p style="color: #4b5563; font-size: 14px; line-height: 1.4;">
                Ta kontakt dersom dere har den tilgjengelig!
            </p>
            
            <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 25px 0 15px 0;">
            
            <p style="color: #9ca3af; font-size: 12px; text-align: center; margin: 0;">
                Denne e-posten ble sendt automatisk etter forespørsel på del.
            </p>
        </div>
        """

        gyldige_mottakere = [
            epost.strip() for epost in mottaker_eposter if epost and epost.strip()
        ]

        if not gyldige_mottakere:
            st.warning("Fant ingen gyldige e-postadresser å sende til.")
            return False

        params = {
            "from": "Delelager <onboarding@resend.dev>",  
            "to": gyldige_mottakere,
            "subject": emne,
            "html": innhold,
        }

        response = resend.Emails.send(params)
        print(f"Resend respons: {response}")
        return True

    except Exception as e:
        print(f"Resend-feil: {repr(e)}")
        st.error(f"Kunne ikke sende e-post: {e}")
        return False

@st.cache_resource
def hent_tilkoblingspool():
    try:
        try:
            har_secrets_toml = "database" in st.secrets
        except Exception:
            har_secrets_toml = False

        if har_secrets_toml:
            db_config = st.secrets["database"]
            dbname, user, passord, host, port = (
                db_config["dbname"], db_config["user"], db_config["password"],
                db_config["host"], db_config["port"],
            )
        else:
            dbname = os.environ["DB_NAME"]
            user = os.environ["DB_USER"]
            passord = os.environ["DB_PASSWORD"]
            host = os.environ["DB_HOST"]
            port = os.environ.get("DB_PORT", "5432")

        return pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dbname=dbname,
            user=user,
            password=passord,
            host=host,
            port=port
        )
    except Exception as e:
        st.error(f"Kunne ikke opprette databasetilkoblingspool: {e}")
        st.stop()

db_pool = hent_tilkoblingspool()

@contextmanager
def db_handling(feilmelding: str):
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

def logg_handling(handling: str, detaljer: str, bedrift_id: int, del_id: int | None = None, endring_antall: int | None = None, delenummer: str | None = None, plassering: str | None = None, bruker_id: int | None = None):
    bruker_navn = st.session_state.get("bruker", "Ukjent bruker")
    aktuell_bruker_id = bruker_id if bruker_id is not None else st.session_state.get("bruker_id")

    try:
        with db_handling("Feil ved logging av handling") as cur:
            cur.execute(
                """
                INSERT INTO historikk
                    (bedrift_id, tidspunkt, bruker, bruker_id, handling, detaljer, del_id, endring_antall, delenummer, plassering)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    bedrift_id,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    bruker_navn,
                    aktuell_bruker_id,
                    handling,
                    detaljer,
                    del_id,
                    endring_antall,
                    delenummer,
                    plassering,
                ),
            )
    except Exception:
        logging.exception("Klarte ikke å logge handling")

@st.cache_resource
def initialiser_database():
    with db_handling("Feil ved initialisering av tabeller") as cur:
        # 1. Opprett grunntabeller først hvis de ikke finnes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bedrifter (
                id SERIAL PRIMARY KEY,
                navn VARCHAR(150) NOT NULL,
                orgnr VARCHAR(50),
                org_nr VARCHAR(50),
                kode VARCHAR(50),
                bedrift_passord_hash VARCHAR(255)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS inventar (
                id SERIAL PRIMARY KEY,
                bedrift_id INTEGER NOT NULL REFERENCES bedrifter(id) ON DELETE CASCADE,
                bilmerke VARCHAR(100) NOT NULL,
                delnavn VARCHAR(150) NOT NULL,
                delenummer VARCHAR(100),
                antall INTEGER NOT NULL DEFAULT 1,
                plassering VARCHAR(100) NOT NULL,
                hylle VARCHAR(100),
                alias VARCHAR(255)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS historikk (
                id SERIAL PRIMARY KEY,
                bedrift_id INTEGER NOT NULL REFERENCES bedrifter(id) ON DELETE CASCADE,
                tidspunkt VARCHAR(50) NOT NULL,
                bruker VARCHAR(100) NOT NULL,
                bruker_id INTEGER,
                handling VARCHAR(100) NOT NULL,
                detaljer TEXT,
                del_id INTEGER,
                endring_antall INTEGER,
                delenummer VARCHAR(100),
                plassering VARCHAR(100)
            );
        """)

        # Sikre kolonner på bedrifter
        cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS kode VARCHAR(50);")
        cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS orgnr VARCHAR(50);")
        cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS org_nr VARCHAR(50);")
        cur.execute("ALTER TABLE bedrifter ADD COLUMN IF NOT EXISTS bedrift_passord_hash VARCHAR(255);")
        cur.execute("UPDATE bedrifter SET orgnr = org_nr WHERE orgnr IS NULL AND org_nr IS NOT NULL;")
        cur.execute("UPDATE bedrifter SET org_nr = orgnr WHERE org_nr IS NULL AND orgnr IS NOT NULL;")

        # Opprydding i sesjoner og koder
        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = current_schema()
                    AND table_name = 'bruker_sesjoner'
                ) THEN
                    DELETE FROM bruker_sesjoner
                    WHERE utlop_tid <= CURRENT_TIMESTAMP
                    OR tilbakekalt_tid IS NOT NULL;
                END IF;
            END $$;
        """)

        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = current_schema()
                    AND table_name = 'samarbeid_koder'
                ) THEN
                    DELETE FROM samarbeid_koder
                    WHERE utlop_tid <= CURRENT_TIMESTAMP;
                END IF;
            END $$;
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS brukere (
                id SERIAL PRIMARY KEY,
                navn VARCHAR(100) NOT NULL,
                jobbmail VARCHAR(150) UNIQUE NOT NULL,
                epost VARCHAR(150),
                passord_hash VARCHAR(255),
                bedrift_id INTEGER REFERENCES bedrifter(id) ON DELETE CASCADE,
                opprettet_tid TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lagre (
                id SERIAL PRIMARY KEY,
                bedrift_id INTEGER NOT NULL REFERENCES bedrifter(id) ON DELETE CASCADE,
                navn VARCHAR(150) NOT NULL
            );
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bruker_sesjoner (
                id BIGSERIAL PRIMARY KEY,
                bruker_id INTEGER NOT NULL REFERENCES brukere(id) ON DELETE CASCADE,
                token_hash VARCHAR(64) NOT NULL,
                opprettet_tid TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                utlop_tid TIMESTAMP NOT NULL,
                sist_brukt_tid TIMESTAMP NULL,
                tilbakekalt_tid TIMESTAMP NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS samarbeidspartnere (
                id SERIAL PRIMARY KEY,
                bedrift_id INTEGER NOT NULL REFERENCES bedrifter(id) ON DELETE CASCADE,
                partner_bedrift_id INTEGER NOT NULL REFERENCES bedrifter(id) ON DELETE CASCADE
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS samarbeid_koder (
                id SERIAL PRIMARY KEY,
                bedrift_id INT REFERENCES bedrifter(id) ON DELETE CASCADE,
                kode VARCHAR(10) NOT NULL,
                utlop_tid TIMESTAMP NOT NULL
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS samarbeid_relasjoner (
                id SERIAL PRIMARY KEY,
                eier_bedrift_id INT REFERENCES bedrifter(id) ON DELETE CASCADE,
                partner_bedrift_id INT REFERENCES bedrifter(id) ON DELETE CASCADE,
                UNIQUE(eier_bedrift_id, partner_bedrift_id)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                bedrift_id INTEGER PRIMARY KEY REFERENCES bedrifter(id) ON DELETE CASCADE,
                tidspunkt TIMESTAMP NOT NULL,
                data TEXT NOT NULL
            );
        """)

        # Sikre ekstra kolonner på eksisterende tabeller hvis oppgradert
        cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS passord_hash VARCHAR(255);")
        cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS jobbmail VARCHAR(150);")
        cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS epost VARCHAR(150);")
        cur.execute("ALTER TABLE brukere ADD COLUMN IF NOT EXISTS bedrift_id INTEGER;")
        cur.execute("UPDATE brukere SET epost = jobbmail WHERE epost IS NULL;")

        cur.execute("ALTER TABLE inventar ADD COLUMN IF NOT EXISTS hylle VARCHAR(100);")
        cur.execute("ALTER TABLE inventar ADD COLUMN IF NOT EXISTS alias VARCHAR(255);")
        cur.execute("ALTER TABLE historikk ADD COLUMN IF NOT EXISTS del_id INTEGER;")
        cur.execute("ALTER TABLE historikk ADD COLUMN IF NOT EXISTS endring_antall INTEGER;")
        cur.execute("ALTER TABLE historikk ADD COLUMN IF NOT EXISTS delenummer VARCHAR(100);")
        cur.execute("ALTER TABLE historikk ADD COLUMN IF NOT EXISTS plassering VARCHAR(100);")
    return True

initialiser_database()

st.set_page_config(page_title="Delelager", page_icon="🛠️", layout="wide")

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

      const originalGetUserMedia = navigator.mediaDevices?.getUserMedia?.bind(navigator.mediaDevices);
      if (originalGetUserMedia) {
        navigator.mediaDevices.getUserMedia = function(constraints) {
          if (constraints && constraints.video) {
            if (typeof constraints.video === 'object') {
              constraints.video.facingMode = { ideal: "environment" };
            } else if (constraints.video === true) {
              constraints.video = { facingMode: { ideal: "environment" } };
            }
          }
          return originalGetUserMedia(constraints);
        };
      }
    </script>
    """,
    unsafe_allow_html=True
)

for key, default in {
    "side": "Se lager",
    "valgt_id": None,
    "varsel": None,
    "plassering_filter": "Alle",
    "bilmerke_filter": "Alle",
    "antall_sortering": "Standard",
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
    "batch_liste": [],
    "rediger_index": None,
    "rediger_lager_navn": None,
    "kamera_teller": 0,
    "aktiv_side": "Se lager",
    "viser_partner_id": None,
    "viser_partner_navn": None,
    "endring_teller": 0,
    "siste_backup_tid": None,
}.items():
    st.session_state.setdefault(key, default)

nettopp_logget_inn = st.session_state.get("nettopp_logget_inn", False)

if nettopp_logget_inn:
    st.session_state.nettopp_logget_inn = False

elif not st.session_state.aktiv_bedrift_id or not st.session_state.bruker:
    try:
        sesjon_token_cookie = cookie_manager.get("sesjon_token")

        if sesjon_token_cookie:
            token_hash = hash_sesjon_token(sesjon_token_cookie)
            conn_sjekk = db_pool.getconn()
            cur_sjekk = conn_sjekk.cursor()
            try:
                cur_sjekk.execute(
                    """
                    SELECT bs.id, bs.bruker_id, b.navn, b.jobbmail, b.bedrift_id
                    FROM bruker_sesjoner bs
                    JOIN brukere b ON bs.bruker_id = b.id
                    WHERE bs.token_hash = %s
                      AND bs.utlop_tid > CURRENT_TIMESTAMP
                      AND bs.tilbakekalt_tid IS NULL
                    """,
                    (token_hash,)
                )
                sesjon_data = cur_sjekk.fetchone()

                if sesjon_data:
                    cur_sjekk.execute(
                        "UPDATE bruker_sesjoner SET sist_brukt_tid = CURRENT_TIMESTAMP WHERE id = %s",
                        (sesjon_data[0],)
                    )
                    conn_sjekk.commit()

                    st.session_state.bruker = sesjon_data[2]
                    st.session_state.epost = sesjon_data[3]
                    st.session_state.aktiv_bedrift_id = sesjon_data[4]
                else:
                    conn_sjekk.rollback()
                    try:
                        cookie_manager.delete("sesjon_token", key="del_ugyldig_sesjon_token")
                    except Exception:
                        pass
            finally:
                cur_sjekk.close()
                db_pool.putconn(conn_sjekk)
    except Exception:
        logging.exception("Feil ved validering av sesjonstoken:")
        st.session_state.bruker = None
        st.session_state.epost = None
        st.session_state.aktiv_bedrift_id = None

@st.cache_data(ttl=60)
def hent_lagre(bedrift_id):
    conn = db_pool.getconn()
    try:
        df_l = pd.read_sql("SELECT navn FROM lagre WHERE bedrift_id = %s ORDER BY navn", conn, params=(bedrift_id,))
        return df_l["navn"].tolist()
    finally:
        db_pool.putconn(conn)

@st.cache_data(ttl=20)
def hent_inventar(bedrift_id):
    conn = db_pool.getconn()
    try:
        return pd.read_sql("SELECT * FROM inventar WHERE bedrift_id = %s", conn, params=(bedrift_id,))
    finally:
        db_pool.putconn(conn)


def hent_samarbeidspartnere(bedrift_id):
    conn = db_pool.getconn()
    try:
        query = """
            SELECT DISTINCT b.id, b.navn 
            FROM samarbeid_relasjoner s 
            JOIN bedrifter b ON (s.eier_bedrift_id = b.id OR s.partner_bedrift_id = b.id)
            WHERE (s.eier_bedrift_id = %s OR s.partner_bedrift_id = %s) AND b.id != %s
        """
        df_p = pd.read_sql(query, conn, params=(bedrift_id, bedrift_id, bedrift_id))
        return df_p.to_dict(orient="records")
    finally:
        db_pool.putconn(conn)

@st.dialog("📉 Bekreft vareuttak")
def uttak_dialog(valgt_id, delnavn_org, delenummer_org, plassering_org, antall_org, antall_ut, aktiv_bedrift_id):
    gjenstaaende = antall_org - antall_ut
    st.markdown(f"Du tar ut **{antall_ut} stk** av **{delnavn_org}** (Delenr: {delenummer_org or '-'}).")
    st.markdown(f"Plassering: **{plassering_org}** | Antall igjen på lager: **{gjenstaaende} stk**")

    if gjenstaaende <= 0:
        st.error("⚠️ **Siste del!** Dette tømmer lageret fullstendig for denne varen, og den vil bli slettet automatisk. **Husk å bestille mer!**")
    elif gjenstaaende == 1:
        st.warning("⚠️ **Nest siste del!** Det er kun 1 stk igjen på lager etter dette uttaket. **Vurder å bestille mer snarest.**")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Bekreft uttak", type="primary", use_container_width=True):
            detaljer_str = f"Tok ut {antall_ut} stk av '{delnavn_org}' (Delenr: {delenummer_org or '-'}, Plassering: {plassering_org})"
            with db_handling("Feil ved uttak") as cur_w:
                if gjenstaaende <= 0:
                    cur_w.execute("DELETE FROM inventar WHERE id = %s AND bedrift_id = %s", (valgt_id, aktiv_bedrift_id))
                    logg_handling("Uttak", f"Tok ut siste {antall_ut} stk og slettet '{delnavn_org}'", aktiv_bedrift_id, valgt_id, -antall_ut, delenummer_org, plassering_org)
                else:
                    cur_w.execute("UPDATE inventar SET antall = %s WHERE id = %s AND bedrift_id = %s", (gjenstaaende, valgt_id, aktiv_bedrift_id))
                    logg_handling("Uttak", detaljer_str, aktiv_bedrift_id, valgt_id, -antall_ut, delenummer_org, plassering_org)
            st.session_state.varsel = ("success", "Uttak gjennomført!")
            hent_inventar.clear()
            tell_endring_og_backup(aktiv_bedrift_id)
            st.session_state.side = "Se lager"
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("⚠️ Delen finnes allerede i systemet")
def duplikat_del_dialog(eksisterende_id, eksisterende_plassering, eksisterende_hylle, ny_data, aktiv_bedrift_id):
    bm, dn, de, ant, plass, hylle, alias = ny_data
    if eksisterende_plassering.lower() == plass.lower():
        st.warning(f"Det finnes allerede en del med delenummer **{de}** på dette lageret (**{plass}**, Hylle: **{eksisterende_hylle or '-'}**).")
    else:
        st.warning(f"⚠️ **Delen finnes allerede på et annet lager!**\n\nDen er registrert på **{eksisterende_plassering}** (Hylle: **{eksisterende_hylle or '-'}**), mens du prøver å legge den til på **{plass}**.")
    
    st.write("Vil du slå sammen antallene (legge til på det eksisterende lageret), eller opprette den som en egen rad på dette nye lageret likevel?")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("➕ Slå sammen (legg til eksisterende)", type="primary", use_container_width=True):
            with db_handling("Feil ved sammenslåing") as cur:
                cur.execute("UPDATE inventar SET antall = antall + %s WHERE id = %s AND bedrift_id = %s", (ant, eksisterende_id, aktiv_bedrift_id))
                logg_handling("Sammenslåing", f"La til {ant} stk i eksisterende del '{dn}' på {eksisterende_plassering}", aktiv_bedrift_id, eksisterende_id, ant, de, eksisterende_plassering)
            st.session_state.form_key_counter += 1
            hent_inventar.clear()
            tell_endring_og_backup(aktiv_bedrift_id)
            st.session_state.varsel = ("success", f"Slo sammen og la til {ant} stk på {eksisterende_plassering}!")
            st.rerun()
    with col2:
        if st.button("Opprett som ny her", use_container_width=True):
            with db_handling("Feil ved lagring") as cur:
                cur.execute(
                    "INSERT INTO inventar (bedrift_id, bilmerke, delnavn, delenummer, antall, plassering, hylle, alias) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                    (aktiv_bedrift_id, bm, dn, de, ant, plass, hylle, alias)
                )
                ny_id = cur.fetchone()[0]
                logg_handling("Ny del", f"La til ny rad med {ant} stk av '{dn}' på {plass}", aktiv_bedrift_id, ny_id, ant, de, plass)
            st.session_state.form_key_counter += 1
            hent_inventar.clear()
            tell_endring_og_backup(aktiv_bedrift_id)
            st.session_state.varsel = ("success", "Ny del lagret som egen rad!")
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
                valgt_id, bm, dn, de, ant, plass, hylle, alias, bed_id = data
                with db_handling("Feil ved lagring") as cur_w:
                    cur_w.execute(
                        "UPDATE inventar SET bilmerke = %s, delnavn = %s, delenummer = %s, antall = %s, plassering = %s, hylle = %s, alias = %s WHERE id = %s AND bedrift_id = %s",
                        (bm, dn, de, ant, plass, hylle, alias, valgt_id, bed_id)
                    )
                    logg_handling("Redigering", f"Endret info på '{dn}' (tvunget gjennom)", bed_id, valgt_id, None, de, plass)
                st.session_state.form_key_counter += 1
                hent_inventar.clear()
                tell_endring_og_backup(aktiv_bedrift_id)
                st.session_state.varsel = ("success", f"Endringer på '{dn}' er lagret!")
                st.session_state.side = "Se lager"
                st.rerun()
            elif handling_type == "ny":
                bm, dn, de, ant, plass, hylle, alias, bed_id = data
                with db_handling("Feil ved lagring") as cur:
                    cur.execute(
                        "INSERT INTO inventar (bedrift_id, bilmerke, delnavn, delenummer, antall, plassering, hylle, alias) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                        (bed_id, bm, dn, de, ant, plass, hylle, alias)
                    )
                    ny_id = cur.fetchone()[0]
                    logg_handling("Ny del", f"La til {ant} stk av '{dn}' (tvunget gjennom)", bed_id, ny_id, ant, de, plass)
                st.session_state.form_key_counter += 1
                hent_inventar.clear()
                tell_endring_og_backup(aktiv_bedrift_id)
                st.session_state.varsel = ("success", "Ny del lagret!")
                st.rerun()
    with col2:
        if st.button("Tilbake", use_container_width=True):
            st.rerun()

@st.dialog("✏️ Bekreft endringer")
def rediger_dialog(valgt_id, ny_bilmerke, ny_delnavn, ny_delenummer, ny_antall, ny_plassering, ny_hylle, ny_alias, aktiv_bedrift_id):
    st.write("Du er i ferd med å lagre følgende endringer på varen:")
    st.markdown(f"- **Bilmerke:** {ny_bilmerke}")
    st.markdown(f"- **Navn på del:** {ny_delnavn}")
    st.markdown(f"- **Alias / Synonymer:** {ny_alias if ny_alias else '-'}")
    st.markdown(f"- **Delenummer:** {ny_delenummer if ny_delenummer else '-'}")
    st.markdown(f"- **Antall:** {ny_antall}")
    st.markdown(f"- **Lager / Plassering:** {ny_plassering}")
    st.markdown(f"- **Hylle:** {ny_hylle}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Lagre endringer", type="primary", use_container_width=True):
            with db_handling("Feil ved lagring") as cur_w:
                cur_w.execute(
                    "UPDATE inventar SET bilmerke = %s, delnavn = %s, delenummer = %s, antall = %s, plassering = %s, hylle = %s, alias = %s WHERE id = %s AND bedrift_id = %s",
                    (ny_bilmerke, ny_delnavn, ny_delenummer, ny_antall, ny_plassering, ny_hylle, ny_alias, valgt_id, aktiv_bedrift_id)
                )
                logg_handling("Redigering", f"Endret info på '{ny_delnavn}'", aktiv_bedrift_id, valgt_id, None, ny_delenummer, ny_plassering)
            st.session_state.form_key_counter += 1
            hent_inventar.clear()
            tell_endring_og_backup(aktiv_bedrift_id)
            st.session_state.varsel = ("success", f"Endringer på '{ny_delnavn}' er lagret!")
            st.session_state.side = "Se lager"
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("🗑️ Bekreft permanent sletting")
def slett_dialog(valgt_id, delnavn_org, delenummer_org, plassering_org, aktiv_bedrift_id):
    st.warning(f"Er du sikker på at du vil slette **{delnavn_org}** permanent fra lageret? Dette kan ikke angres.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Ja, slett permanent", type="primary", use_container_width=True):
            with db_handling("Feil ved sletting") as cur_w:
                cur_w.execute("DELETE FROM inventar WHERE id = %s AND bedrift_id = %s", (valgt_id, aktiv_bedrift_id))
                logg_handling("Sletting", f"Slettet '{delnavn_org}'", aktiv_bedrift_id, valgt_id, -999, delenummer_org, plassering_org)
            hent_inventar.clear()
            tell_endring_og_backup(aktiv_bedrift_id)
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
            hent_lagre.clear()
            st.session_state.varsel = ("success", f"Lageret '{l_navn}' ble slettet.")
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("➕ Bekreft registrering av ny del")
def ny_del_dialog(bm, dn, de, ant, plass, hylle, alias, aktiv_bedrift_id):
    st.write("Du er i ferd med å legge til en ny del på lageret:")
    st.markdown(f"- **Bilmerke:** {bm}")
    st.markdown(f"- **Navn på del:** {dn}")
    st.markdown(f"- **Alias / Synonymer:** {alias if alias else '-'}")
    st.markdown(f"- **Delenummer:** {de if de else '-'}")
    st.markdown(f"- **Antall:** {ant}")
    st.markdown(f"- **Lager / Plassering:** {plass}")
    st.markdown(f"- **Hylle:** {hylle}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Bekreft og lagre", type="primary", use_container_width=True):
            with db_handling("Feil ved lagring") as cur:
                cur.execute(
                    "INSERT INTO inventar (bedrift_id, bilmerke, delnavn, delenummer, antall, plassering, hylle, alias) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                    (aktiv_bedrift_id, bm, dn, de, ant, plass, hylle, alias)
                )
                ny_id = cur.fetchone()[0]
                logg_handling("Ny del", f"La til {ant} stk av '{dn}'", aktiv_bedrift_id, ny_id, ant, de, plass)

            st.session_state.form_key_counter += 1
            hent_inventar.clear()
            tell_endring_og_backup(aktiv_bedrift_id)
            st.session_state.varsel = ("success", "Ny del lagret!")
            st.rerun()
    with col2:
        if st.button("Avbryt", use_container_width=True):
            st.rerun()

@st.dialog("✉️ Send forespørsel på del")
def send_foresporsel_dialog(rad, partner_id, partner_navn, aktiv_bedrift_id, valgt_bedrift_navn):
    st.markdown(f"Du sender forespørsel på:")
    st.markdown(f"- **Del:** {rad['delnavn']} ({rad['bilmerke']})")
    st.markdown(f"- **Delenummer:** {rad['delenummer'] or '-'}")
    st.markdown(f"- **Lagerplassering:** {rad['plassering']}" + (f" (Hylle: {rad['hylle']})" if rad['hylle'] else ""))

    st.divider()

    innlogget_epost = st.session_state.get("epost", "") or ""

    st.markdown("**Kontaktinformasjon** *(obligatorisk — partneren bruker dette for å svare deg)*")
    avsender_kontakt = st.text_input(
        "Din e-post eller telefonnummer *",
        value=innlogget_epost,
        placeholder="f.eks. navn@firma.no eller 900 12 345",
        help="Fylt inn fra din brukerprofil — endre om du ønsker å bruke en annen adresse."
    )
    if not avsender_kontakt.strip():
        st.caption("⚠️ Du må fylle inn kontaktinfo før du kan sende forespørselen.")

    st.write(f"Forespørselen sendes på e-post til alle registrerte brukere hos **{partner_navn}**.")

    if st.button("Bekreft og send forespørsel", type="primary", use_container_width=True, disabled=not avsender_kontakt.strip()):
        with db_handling("Feil ved henting av e-poster") as cur:
            cur.execute("SELECT epost FROM brukere WHERE bedrift_id = %s AND epost IS NOT NULL", (partner_id,))
            eposter = [row[0] for row in cur.fetchall()]

        if eposter:
            plassering_full = f"{rad['plassering']}" + (f" (Hylle: {rad['hylle']})" if rad['hylle'] else "")
            suksess = send_deleforesporsel_epost(
                eposter,
                valgt_bedrift_navn,
                rad["bilmerke"],
                rad["delenummer"],
                rad["delnavn"],
                avsender_kontakt.strip(),
                plassering_full
            )

            if suksess:
                st.session_state.varsel = ("success", "Forespørsel sendt på e-post til partneren!")
                st.rerun()
            else:
                st.error("Klarte ikke å sende e-post (sjekk feilmelding over).")
        else:
            st.warning("Fant ingen registrerte e-poster hos partnerbedriften.")

if not st.session_state.aktiv_bedrift_id or not st.session_state.bruker:

    if st.session_state.vis_bedrift_registrering:
        st.title("🛠️ Delelager — Registrer bedrift")

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
                        st.session_state.varsel = ("success", "Bedrift registrert! Nå må du opprette din bruker.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Feil ved opprettelse av bedrift: {e}")
                else:
                    st.warning("Vennligst fyll ut alle feltene inklusive bedriftens passord.")

        if st.button("Tilbake til logg inn"):
            st.session_state.vis_bedrift_registrering = False
            st.rerun()

    elif st.session_state.vis_registrering:
        st.title("🛠️ Delelager — Registrering")

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
                        b_id = None
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
                                    "INSERT INTO brukere (navn, jobbmail, epost, passord_hash, bedrift_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                                    (reg_navn.strip(), reg_mail.strip().lower(), reg_mail.strip().lower(), skrevet_hash, b_id)
                                )
                                ny_bruker_id = cur.fetchone()[0]

                        if b_id:
                            opprett_sesjon(ny_bruker_id, husk_meg=True)

                            st.session_state.bruker = reg_navn.strip()
                            st.session_state.epost = reg_mail.strip().lower()
                            st.session_state.aktiv_bedrift_id = b_id
                            st.session_state.ny_opprettet_bedrift_id = None
                            st.session_state.nettopp_logget_av = False
                            st.session_state.nettopp_logget_inn = True

                            for navn, key in [
                                ("logget_av", "del_reg_logget_av"),
                                ("aktiv_bedrift_id", "del_reg_bedrift_id"),
                                ("aktiv_bruker", "del_reg_bruker"),
                                ("aktiv_epost", "del_reg_epost"),
                            ]:
                                try:
                                    cookie_manager.delete(navn, key=key)
                                except Exception:
                                    pass

                            time.sleep(0.6)

                            st.session_state.vis_registrering = False
                            st.session_state.varsel = ("success", "Bruker opprettet og innlogget!")
                            st.rerun()
                    except Exception as e:
                        st.session_state.bruker = None
                        st.session_state.epost = None
                        st.session_state.aktiv_bedrift_id = None
                        st.error(f"Feil ved registrering: {e}")
                else:
                    st.warning("Vennligst fyll ut alle feltene (inkludert bedriftspassord).")

        if st.button("Tilbake til logg inn"):
            st.session_state.vis_registrering = False
            st.session_state.ny_opprettet_bedrift_id = None
            st.rerun()

    else:
        st.title("🛠️ Delelager — Logg inn")

        with st.form("innlogging_form"):
            inn_mail = st.text_input("Mail:")
            inn_passord = st.text_input("Passord:", type="password")
            husk_meg = st.checkbox("Husk meg på denne enheten", value=True)
            submit_inn = st.form_submit_button("Logg inn", type="primary")

            if submit_inn:
                if inn_mail.strip() and inn_passord.strip():
                    try:
                        with db_handling("Feil ved innlogging") as cur:
                            cur.execute("SELECT id, navn, passord_hash, bedrift_id FROM brukere WHERE jobbmail = %s", (inn_mail.strip().lower(),))
                            bruker_treff = cur.fetchone()

                        if bruker_treff:
                            bruker_id, db_navn, db_hash, db_bedrift_id = bruker_treff

                            if sjekk_passord(inn_passord.strip(), db_hash):
                                st.session_state.bruker = db_navn
                                st.session_state.epost = inn_mail.strip().lower()
                                st.session_state.aktiv_bedrift_id = db_bedrift_id
                                st.session_state.nettopp_logget_av = False

                                gammel_token = cookie_manager.get("sesjon_token")
                                if gammel_token:
                                    tilbakekall_sesjon(gammel_token)

                                opprett_sesjon(bruker_id, husk_meg=husk_meg)
                                st.session_state.nettopp_logget_inn = True

                                for navn, key in [
                                    ("logget_av", "del_login_logget_av"),
                                    ("aktiv_bedrift_id", "del_login_bedrift_id"),
                                    ("aktiv_bruker", "del_login_bruker"),
                                    ("aktiv_epost", "del_login_epost"),
                                ]:
                                    try:
                                        cookie_manager.delete(navn, key=key)
                                    except Exception:
                                        pass

                                time.sleep(0.6)
                                st.session_state.varsel = ("success", "Innlogget!")
                                st.rerun()
                            else:
                                st.error("Feil passord.")
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

aktiv_bedrift_id = st.session_state.aktiv_bedrift_id
conn_temp = db_pool.getconn()
try:
    df_aktiv = pd.read_sql("SELECT navn FROM bedrifter WHERE id = %s", conn_temp, params=(aktiv_bedrift_id,))
finally:
    db_pool.putconn(conn_temp)

if df_aktiv.empty:
    try:
        cookie_manager.delete("aktiv_bedrift_id", key="del_notfound_bedrift_id")
    except Exception:
        pass
    st.session_state.aktiv_bedrift_id = None
    st.rerun()

valgt_bedrift_navn = df_aktiv.iloc[0]["navn"]
tilgjengelige_lagre = hent_lagre(aktiv_bedrift_id)

st.title(f"🛠️ Delelager — {valgt_bedrift_navn}")

st.sidebar.header("👤 Innlogget sesjon")
st.sidebar.info(f"Bedrift: **{valgt_bedrift_navn}**\n\nBruker: **{st.session_state.bruker}**\n*({st.session_state.get('epost', '')})*")

if st.sidebar.button("🔄 Logg av / Bytt bruker"):
    sesjon_token_ved_utlogging = cookie_manager.get("sesjon_token")
    try:
        tilbakekall_sesjon(sesjon_token_ved_utlogging)
    except Exception:
        logging.exception("Feil ved tilbakekalling av sesjon under utlogging:")

    st.session_state.aktiv_bedrift_id = None
    st.session_state.bruker = None
    st.session_state.epost = None
    st.session_state.nettopp_logget_av = True
    st.session_state.nettopp_logget_inn = False

    for navn, key in [
        ("sesjon_token", "del_logout_sesjon_token"),
        ("logget_av", "del_logout_logget_av"),
        ("aktiv_bedrift_id", "del_logout_bedrift_id"),
        ("aktiv_bruker", "del_logout_bruker"),
        ("aktiv_epost", "del_logout_epost"),
    ]:
        try:
            cookie_manager.delete(navn, key=key)
        except Exception:
            logging.exception("Feil ved sletting av cookie %s under utlogging", navn)

    time.sleep(0.6)
    st.rerun()

st.sidebar.divider()
st.sidebar.subheader("🤝 Samarbeidslagre")
partnere = hent_samarbeidspartnere(aktiv_bedrift_id)
for partner in partnere:
    col_p1, col_p2 = st.sidebar.columns([3, 1])
    with col_p1:
        if st.button(f"🏢 {partner['navn']}", key=f"partner_{partner['id']}"):
            st.session_state.aktiv_side = "samarbeidets_lager"
            st.session_state.viser_partner_id = partner['id']
            st.session_state.viser_partner_navn = partner['navn']
            st.session_state.side = "samarbeidets_lager"
            st.rerun()
    with col_p2:
        if st.button("❌", key=f"fjern_partner_{partner['id']}", help=f"Fjern samarbeidet med {partner['navn']}"):
            with db_handling("Feil ved sletting av relasjon") as cur:
                cur.execute(
                    "DELETE FROM samarbeid_relasjoner WHERE (eier_bedrift_id = %s AND partner_bedrift_id = %s) OR (eier_bedrift_id = %s AND partner_bedrift_id = %s)",
                    (aktiv_bedrift_id, partner['id'], partner['id'], aktiv_bedrift_id)
                )
            st.session_state.varsel = ("success", f"Fjernet samarbeid med {partner['navn']}")
            st.rerun()

st.sidebar.divider()

st.sidebar.subheader("💻 Samarbeid og koder")

if st.sidebar.button("Generer samarbeidskode (10 min)"):
    kode = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    utlop_tid = datetime.now() + timedelta(minutes=10)

    with db_handling("Feil ved generering av kode") as cur:
        cur.execute("DELETE FROM samarbeid_koder WHERE utlop_tid <= CURRENT_TIMESTAMP")
        cur.execute(
            "INSERT INTO samarbeid_koder (bedrift_id, kode, utlop_tid) VALUES (%s, %s, %s)",
            (aktiv_bedrift_id, kode, utlop_tid)
        )
    st.sidebar.success(f"Din midlertidige kode: **{kode}** (Varer i 10 min)")

skrevet_kode = st.sidebar.text_input("Skriv inn samarbeidskode").strip().upper()
if st.sidebar.button("Koble til partner med kode"):
    if skrevet_kode:
        naa = datetime.now()
        with db_handling("Feil ved sjekk av kode") as cur:
            # Rydd bort utløpte koder først
            cur.execute("DELETE FROM samarbeid_koder WHERE utlop_tid <= CURRENT_TIMESTAMP")

            cur.execute(
                "SELECT bedrift_id FROM samarbeid_koder WHERE kode = %s AND utlop_tid > %s",
                (skrevet_kode, naa)
            )
            kode_treff = cur.fetchone()

        if kode_treff:
            eier_id = kode_treff[0]
            if eier_id == aktiv_bedrift_id:
                st.sidebar.warning("Du kan ikke koble til din egen bedrift.")
            else:
                with db_handling("Feil ved oppretting av relasjon") as cur:
                    cur.execute(
                        "INSERT INTO samarbeid_relasjoner (eier_bedrift_id, partner_bedrift_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (eier_id, aktiv_bedrift_id)
                    )
                st.session_state.varsel = ("success", "Vellykket! Tilgang til lageret er opprettet.")
                st.rerun()
        else:
            st.sidebar.error("Ugyldig eller utløpt kode.")
    else:
        st.sidebar.warning("Vennligst skriv inn en kode først.")

st.sidebar.divider()
st.sidebar.divider()
st.sidebar.subheader("💾 Sikkerhetskopi")
siste_backup = st.session_state.get("siste_backup_tid")
endring_teller = st.session_state.get("endring_teller", 0)
if siste_backup:
    st.sidebar.caption(f"Siste backup: {siste_backup} | Endringer siden: {endring_teller}/{BACKUP_ETTER_ANTALL_ENDRINGER}")
else:
    st.sidebar.caption(f"Ingen backup kjørt ennå | Endringer: {endring_teller}/{BACKUP_ETTER_ANTALL_ENDRINGER}")

if st.sidebar.button("🔄 Lag backup nå", use_container_width=True):
    try:
        backup_json = lag_backup(aktiv_bedrift_id)
        st.session_state["endring_teller"] = 0
        st.session_state["siste_backup_tid"] = datetime.now().strftime("%H:%M:%S")
        st.sidebar.success("Backup lagret i databasen!")
        st.sidebar.download_button(
            label="📥 Last ned backup",
            data=backup_json.encode("utf-8"),
            file_name=f"backup_{valgt_bedrift_navn}_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            mime="application/json",
            use_container_width=True,
            key="dl_manuell_backup"
        )
    except Exception as e:
        st.sidebar.error(f"Backup feilet: {e}")

# Vis nedlasting av siste lagrede backup hvis den finnes
try:
    conn_b = db_pool.getconn()
    try:
        df_b = pd.read_sql(
            "SELECT tidspunkt, data FROM backups WHERE bedrift_id = %s",
            conn_b, params=(aktiv_bedrift_id,)
        )
    finally:
        db_pool.putconn(conn_b)
    if not df_b.empty:
        st.sidebar.download_button(
            label=f"📥 Last ned siste backup ({str(df_b.iloc[0]['tidspunkt'])[:16]})",
            data=str(df_b.iloc[0]["data"]).encode("utf-8"),
            file_name=f"backup_{valgt_bedrift_navn}.json",
            mime="application/json",
            use_container_width=True,
            key="dl_lagret_backup"
        )
except Exception:
    pass

st.sidebar.divider()
if st.session_state.varsel:
    type_varsel, melding_tekst = st.session_state.varsel
    if type_varsel == "success":
        st.toast(melding_tekst, icon="✅")
    elif type_varsel == "error":
        st.toast(melding_tekst, icon="❌")
        st.error(melding_tekst)
    elif type_varsel == "warning":
        st.toast(melding_tekst, icon="⚠️")
        st.warning(melding_tekst)
    else:
        st.toast(melding_tekst, icon="ℹ️")
    st.session_state.varsel = None

sider = [
    ("📋 Se lager", "Se lager"),
    ("📷 Skann / OCR", "Skann og OCR"),
    ("📦 Administrer", "Administrer deler"),
    ("➕ Legg til", "Legg til ny del"),
    ("📂 Importer", "Importer fra fil"),
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

if st.session_state.side == "samarbeidets_lager":
    partner_id = st.session_state.get("viser_partner_id")
    partner_navn = st.session_state.get("viser_partner_navn", "Partner")
    st.header(f"🏢 Samarbeidslager: {partner_navn}")
    
    if st.button("⬅️ Tilbake til eget lager"):
        st.session_state.side = "Se lager"
        st.rerun()
        
    df_partner = hent_inventar(partner_id)
    if not df_partner.empty:
        sok_p = st.text_input("🔍 Søk i partnerens lager...", key="sok_partner").lower()
        df_p_filtered = df_partner.copy()
        if sok_p:
            df_p_filtered = df_p_filtered[
                df_p_filtered["bilmerke"].str.lower().str.contains(sok_p, na=False)
                | df_p_filtered["delnavn"].str.lower().str.contains(sok_p, na=False)
                | df_p_filtered["delenummer"].str.lower().str.contains(sok_p, na=False)
            ]
        
        st.markdown("### 📋 Lageroversikt:")
        
        for index, rad in df_p_filtered.iterrows():
            with st.container(border=True):
                col_info1, col_info2, col_info3, col_info4, col_info5, col_btn = st.columns([2, 2, 2, 1, 1, 1.5])
                with col_info1:
                    st.markdown(f"{rad['bilmerke']}")
                    if rad['alias']:
                        st.caption(f"Alias: {rad['alias']}")
                with col_info2:
                    st.markdown(f"{rad['delnavn']}")
                with col_info3:
                    st.markdown(f"Delenr: `{rad['delenummer'] or '-'}`")
                with col_info4:
                    st.markdown(f"Ant: **{rad['antall']}**")
                with col_info5:
                    st.markdown(f"Lager: *[Skjult]*")
                with col_btn:
                    if st.button("📨 Forespør", key=f"req_btn_{rad['id']}", use_container_width=True):
                        send_foresporsel_dialog(rad, partner_id, partner_navn, aktiv_bedrift_id, valgt_bedrift_navn)
    else:
        st.info("Partnerens lager er tomt.")

elif st.session_state.side == "Se lager":
    st.header(f"Oversikt over lageret — {valgt_bedrift_navn}")
    df = hent_inventar(aktiv_bedrift_id)

    if not df.empty:
        sok = st.text_input("🔍 Søk på delenummer, bilmerke, delnavn, alias, plassering eller hylle...", value=st.session_state.scannet_kode).lower()
        st.session_state.scannet_kode = ""

        df_filtered = df.copy()
            
        if sok:
            df_filtered = df_filtered[
                df_filtered["bilmerke"].str.lower().str.contains(sok, na=False)
                | df_filtered["delnavn"].str.lower().str.contains(sok, na=False)
                | df_filtered["alias"].str.lower().str.contains(sok, na=False)
                | df_filtered["delenummer"].str.lower().str.contains(sok, na=False)
                | df_filtered["plassering"].str.lower().str.contains(sok, na=False)
                | df_filtered["hylle"].str.lower().str.contains(sok, na=False)
            ]

        st.markdown(f"### 📋 Lageroversikt ({len(df_filtered)} treff)")

        for index, rad in df_filtered.iterrows():
            with st.container(border=True):
                col_info1, col_info2, col_info3, col_info4, col_info5, col_btn1, col_btn2 = st.columns([2, 2, 1.8, 0.9, 1.4, 1.1, 1.1])
                with col_info1:
                    st.markdown(f"{rad['bilmerke']}")
                    if rad['alias']:
                        st.caption(f"Alias: {rad['alias']}")
                with col_info2:
                    st.markdown(f"{rad['delnavn']}")
                with col_info3:
                    st.markdown(f"Delenr: `{rad['delenummer'] or '-'}`")
                with col_info4:
                    st.markdown(f"**{rad['antall']}**")
                with col_info5:
                    hylle_txt = f" / Hylle: {rad['hylle']}" if rad['hylle'] else ""
                    st.markdown(f"📍 {rad['plassering']}{hylle_txt}")
                with col_btn1:
                    if rad['antall'] >= 1:
                        if st.button("➖ 1", key=f"ta_ut_1_{rad['id']}", use_container_width=True, help="Du tar ut 1 stk ved å trykke på knappen"):
                            uttak_dialog(
                                int(rad['id']),
                                rad['delnavn'],
                                rad['delenummer'],
                                rad['plassering'],
                                int(rad['antall']),
                                1,
                                aktiv_bedrift_id
                            )
                with col_btn2:
                    if st.button("⚙️", key=f"adm_btn_{rad['id']}", use_container_width=True, help="Administrer"):
                        st.session_state.valgt_id = int(rad['id'])
                        st.session_state.side = "Administrer deler"
                        st.rerun()

        st.divider()
        df_export = df_filtered.copy()
        kolonner_aa_vise = [c for c in df_export.columns if c not in ["id", "bedrift_id"]]
        csv_data = df_export[kolonner_aa_vise].to_csv(index=False, sep=";").encode("utf-8-sig")
        
        st.download_button(
            label="📥 Last ned vist lagerliste (Excel-vennlig)",
            data=csv_data,
            file_name=f"lager_oversikt_{valgt_bedrift_navn}.csv",
            mime="text/csv",
        )
    else:
        st.info("Lageret for denne bedriften er helt tomt ennå.")

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
            st.info("💡 Ta bilde av et delenummer med bak-kameraet. Listen fyller seg opp automatisk!")

            kamera_bilde = st.camera_input("Ta bilde av et delenummer", key=f"kamera_input_{st.session_state.kamera_teller}")

            if kamera_bilde:
                with st.spinner("Analyserer bilde..."):
                    cv_img = cv2.imdecode(np.frombuffer(kamera_bilde.getvalue(), np.uint8), cv2.IMREAD_COLOR)

                    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
                    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

                    tekst_resultat = pytesseract.image_to_string(gray, lang="nor+eng")

                    funn = [linje.strip() for linje in tekst_resultat.splitlines() if len(linje.strip()) > 1]

                    nye_lagt_til = 0
                    for tekst_funn in funn:
                        if tekst_funn and tekst_funn not in st.session_state.batch_liste:
                            st.session_state.batch_liste.append(tekst_funn)
                            nye_lagt_til += 1

                    if nye_lagt_til > 0:
                        st.session_state.varsel = ("success", f"La til {nye_lagt_til} delenummer i listen!")
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
                    if not df_batch.empty and "Bilmerke" in df_batch.columns:
                        df_batch = df_batch.sort_values(by="Bilmerke", key=lambda col: col.str.lower())
                        
                    csv_b = df_batch.to_csv(index=False, sep=";").encode("utf-8-sig")
                    st.download_button(
                        label="📥 Last ned sjekkliste (Excel-vennlig)",
                        data=csv_b,
                        file_name=f"batch_sjekk_{valgt_bedrift_navn}.csv",
                        mime="text/csv",
                        use_container_width=True
                    )
            else:
                st.caption("Ingen deler registrert i listen enda.")
        else:
            st.error("OCR-modul ikke installert.")

elif st.session_state.side == "Administrer deler":
    st.header("📦 Administrer deler")
    with db_handling("Feil ved henting av deler") as cur:
        cur.execute("SELECT id, delnavn, bilmerke, delenummer, antall, plassering, hylle, alias FROM inventar WHERE bedrift_id = %s ORDER BY delnavn", (aktiv_bedrift_id,))
        deler = cur.fetchall()

    if deler:
        def lag_etikett(row):
            d_id, d_navn, d_merke, d_nr, d_ant, d_plass, d_hylle, d_alias = row
            alias_info = f" (Alias: {d_alias})" if d_alias else ""
            hylle_info = f" - Hylle: {d_hylle}" if d_hylle else ""
            return f"{d_navn}{alias_info} ({d_merke} - Delenr: {d_nr or 'Ingen'} - Ant: {d_ant} stk - Lager: {d_plass}{hylle_info})"

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

        deler_oppslag = {row[0]: row for row in deler}
        _, delnavn_org, bilmerke_org, delenummer_org, antall_org, plassering_org, hylle_org, alias_org = deler_oppslag[valgt_id]

        handling = st.radio("Handling:", ["📉 Ta ut", "✏️ Rediger", "🗑️ Slett"])

        if handling == "📉 Ta ut":
            antall_ut = st.number_input("Antall som skal tas ut:", min_value=1, max_value=max(antall_org, 1), value=1)
            if st.button("Gå til uttak", type="primary"):
                uttak_dialog(valgt_id, delnavn_org, delenummer_org, plassering_org, antall_org, antall_ut, aktiv_bedrift_id)

        elif handling == "✏️ Rediger":
            with st.form(f"rediger_form_{st.session_state.form_key_counter}"):
                ny_bilmerke = st.text_input("Bilmerke *", value=bilmerke_org)
                ny_delnavn = st.text_input("Navn på del *", value=delnavn_org)
                ny_alias = st.text_input("Alias / Synonymer (f.eks. skvettlapp, mudflap)", value=alias_org if alias_org else "")
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
                        rediger_dialog(valgt_id, ny_bilmerke.strip(), ny_delnavn.strip(), ny_delenummer.strip(), ny_antall, ny_plassering, ny_hylle.strip(), ny_alias.strip(), aktiv_bedrift_id)
                    else:
                        data_pakke = (valgt_id, ny_bilmerke.strip(), ny_delnavn.strip(), ny_delenummer.strip(), ny_antall, ny_plassering, ny_hylle.strip(), ny_alias.strip(), aktiv_bedrift_id)
                        manglende_felt_dialog(mangler, "rediger", data_pakke)

        elif handling == "🗑️ Slett":
            if st.button("Slett permanent", type="primary"):
                slett_dialog(valgt_id, delnavn_org, delenummer_org, plassering_org, aktiv_bedrift_id)
    else:
        st.info("Lageret er tomt.")

elif st.session_state.side == "Legg til ny del":
    st.header(f"Registrer ny del — {valgt_bedrift_navn}")

    if not tilgjengelige_lagre:
        st.warning("⚠️ Du må opprette minst ett lager under 'Opprett lagre' før du kan legge til deler!")
    else:
        with st.form(f"ny_del_{st.session_state.form_key_counter}"):
            bm = st.text_input("Bilmerke *")
            dn = st.text_input("Navn på del *")
            alias = st.text_input("Alias / Synonymer (f.eks. skvettlapp, mud flap)")
            de = st.text_input("Delenummer (valgfritt)")
            ant = st.number_input("Antall *", min_value=1, value=1)
            plass = st.selectbox("Lager / Plassering *", tilgjengelige_lagre)
            hylle = st.text_input("Hylle * (f.eks. Hylle 3B / Rad 2)")

            submit_ny = st.form_submit_button("Forhåndsvis og lagre")

            if submit_ny:
                renset_bm = bm.strip().capitalize() if bm else ""
                renset_dn = dn.strip().capitalize() if dn else ""
                renset_de = de.strip() if de else ""
                renset_alias = alias.strip().capitalize() if alias else ""
                renset_hylle = hylle.strip() if hylle else ""

                eksisterende_id = None
                eksisterende_plassering = None
                eksisterende_hylle = None

                sjekk_merke = renset_bm.replace(" ", "").lower()
                sjekk_delenummer = renset_de.replace(" ", "").lower()

                if sjekk_delenummer and sjekk_merke:
                    with db_handling("Feil ved duplikat-sjekk") as cur:
                        cur.execute(
                            """
                            SELECT id, plassering, hylle FROM inventar 
                            WHERE bedrift_id = %s 
                              AND REPLACE(LOWER(bilmerke), ' ', '') = %s 
                              AND REPLACE(LOWER(delenummer), ' ', '') = %s
                            """,
                            (aktiv_bedrift_id, sjekk_merke, sjekk_delenummer)
                        )
                        res = cur.fetchone()
                        if res:
                            eksisterende_id, eksisterende_plassering, eksisterende_hylle = res

                if eksisterende_id:
                    ny_data_pakke = (renset_bm, renset_dn, renset_de, ant, plass, renset_hylle, renset_alias)
                    duplikat_del_dialog(eksisterende_id, eksisterende_plassering, eksisterende_hylle, ny_data_pakke, aktiv_bedrift_id)
                else:
                    mangler = []
                    if not renset_bm: mangler.append("Bilmerke")
                    if not renset_dn: mangler.append("Navn på del")
                    if not plass: mangler.append("Lager")
                    if not renset_hylle: mangler.append("Hylle")

                    if not mangler:
                        ny_del_dialog(renset_bm, renset_dn, renset_de, ant, plass, renset_hylle, renset_alias, aktiv_bedrift_id)
                    else:
                        data_pakke = (renset_bm, renset_dn, renset_de, ant, plass, renset_hylle, renset_alias, aktiv_bedrift_id)
                        manglende_felt_dialog(mangler, "ny", data_pakke)

elif st.session_state.side == "Importer fra fil":
    st.header(f"📂 Importer deler fra Excel / CSV — {valgt_bedrift_navn}")

    if not tilgjengelige_lagre:
        st.warning("⚠️ Du må opprette minst ett lager under 'Opprett lagre' før du kan importere deler!")
        st.stop()

    st.write(
        "Last opp en Excel- eller CSV-fil fra ditt eksisterende lagersystem. "
        "Du velger selv hvilken kolonne i filen som tilsvarer hva i appen — "
        "det spiller ingen rolle hva kolonnene heter i filen din."
    )

    opplastet_fil = st.file_uploader(
        "Last opp fil", 
        type=["xlsx", "xls", "csv"],
        help="Excel (.xlsx/.xls) eller CSV-fil. Første rad bør være kolonneoverskrifter."
    )

    if opplastet_fil:
        try:
            if opplastet_fil.name.endswith(".csv"):
                try:
                    df_import = pd.read_csv(opplastet_fil, sep=";", encoding="utf-8-sig")
                    if len(df_import.columns) <= 1:
                        opplastet_fil.seek(0)
                        df_import = pd.read_csv(opplastet_fil, sep=",", encoding="utf-8-sig")
                except Exception:
                    opplastet_fil.seek(0)
                    df_import = pd.read_csv(opplastet_fil, sep=",", encoding="latin-1")
            else:
                df_import = pd.read_excel(opplastet_fil)

            df_import.columns = [str(c).strip() for c in df_import.columns]
            kolonner_i_fil = list(df_import.columns)

            st.success(f"Filen er lest inn — fant **{len(df_import)} rader** og **{len(kolonner_i_fil)} kolonner**.")
            st.markdown("**Forhåndsvisning (de 5 første radene):**")
            st.dataframe(df_import.head(5), use_container_width=True)

            st.divider()
            st.markdown("### 🔗 Koble kolonner til feltene i appen")
            st.caption(
                "Velg hvilken kolonne i filen din som tilsvarer hvert felt. "
                "Felt merket med * er obligatoriske."
            )

            ingen_valg = "— Ikke i filen —"
            alternativer = [ingen_valg] + kolonner_i_fil

            def gjett_kolonne(kandidater, kolonner):
                for k in kandidater:
                    for col in kolonner:
                        if k.lower() in col.lower():
                            return col
                return ingen_valg

            col_a, col_b = st.columns(2)
            with col_a:
                kol_bilmerke = st.selectbox(
                    "Bilmerke *",
                    alternativer,
                    index=alternativer.index(gjett_kolonne(["bilmerke", "merke", "make", "brand", "bil"], kolonner_i_fil))
                )
                kol_delnavn = st.selectbox(
                    "Navn på del *",
                    alternativer,
                    index=alternativer.index(gjett_kolonne(["delnavn", "navn", "varenavn", "beskrivelse", "name", "description", "del ", "vare"], kolonner_i_fil))
                )
                kol_delenummer = st.selectbox(
                    "Delenummer",
                    alternativer,
                    index=alternativer.index(gjett_kolonne(["delenr", "delenummer", "varenr", "varenummer", "part", "nummer", "nr"], kolonner_i_fil))
                )
                kol_alias = st.selectbox(
                    "Alias / Synonymer",
                    alternativer,
                    index=0
                )
            with col_b:
                kol_antall = st.selectbox(
                    "Antall *",
                    alternativer,
                    index=alternativer.index(gjett_kolonne(["antall", "qty", "quantity", "stock", "beholdning", "lager"], kolonner_i_fil))
                )
                kol_hylle = st.selectbox(
                    "Hylle",
                    alternativer,
                    index=alternativer.index(gjett_kolonne(["hylle", "shelf", "rad", "hylleplass"], kolonner_i_fil))
                )
                kol_plassering = st.selectbox(
                    "Lager / Plassering *",
                    alternativer,
                    index=alternativer.index(gjett_kolonne(["lager", "plassering", "location", "warehouse", "avdeling"], kolonner_i_fil)),
                    help="Velg kolonnen i filen som angir lager/plassering per del. Obligatorisk."
                )

            mangler_obligatoriske = []
            if kol_bilmerke == ingen_valg: mangler_obligatoriske.append("Bilmerke")
            if kol_delnavn == ingen_valg: mangler_obligatoriske.append("Navn på del")
            if kol_antall == ingen_valg: mangler_obligatoriske.append("Antall")
            if kol_plassering == ingen_valg: mangler_obligatoriske.append("Lager / Plassering")

            if mangler_obligatoriske:
                st.warning(f"Koble disse obligatoriske feltene før du kan importere: **{', '.join(mangler_obligatoriske)}**")
            else:
                st.divider()

                st.markdown("**Forhåndsvisning etter kobling (de 5 første radene):**")
                preview_rader = []
                for _, rad in df_import.head(5).iterrows():
                    preview_rader.append({
                        "Bilmerke": str(rad[kol_bilmerke]).strip() if kol_bilmerke != ingen_valg else "",
                        "Delnavn": str(rad[kol_delnavn]).strip() if kol_delnavn != ingen_valg else "",
                        "Delenummer": str(rad[kol_delenummer]).strip() if kol_delenummer != ingen_valg else "",
                        "Antall": rad[kol_antall] if kol_antall != ingen_valg else 0,
                        "Hylle": str(rad[kol_hylle]).strip() if kol_hylle != ingen_valg else "",
                        "Alias": str(rad[kol_alias]).strip() if kol_alias != ingen_valg else "",
                        "Lager": str(rad[kol_plassering]).strip() if kol_plassering != ingen_valg else "",
                    })
                st.dataframe(pd.DataFrame(preview_rader), use_container_width=True)

                st.info(f"Klar til å importere **{len(df_import)} deler**. Rader der lager, antall eller navn mangler/er tomme hoppes over automatisk.")

                if st.button("🚀 Start import", type="primary", use_container_width=True):
                    importert = 0
                    hoppet_over = 0
                    feil_rader = []

                    progress = st.progress(0, text="Importerer...")

                    for i, (_, rad) in enumerate(df_import.iterrows()):
                        try:
                            bilmerke = str(rad[kol_bilmerke]).strip().capitalize() if kol_bilmerke != ingen_valg else ""
                            delnavn = str(rad[kol_delnavn]).strip().capitalize() if kol_delnavn != ingen_valg else ""
                            delenummer = str(rad[kol_delenummer]).strip() if kol_delenummer != ingen_valg else ""
                            alias = str(rad[kol_alias]).strip() if kol_alias != ingen_valg else ""
                            hylle = str(rad[kol_hylle]).strip() if kol_hylle != ingen_valg else ""
                            brukt_lager = str(rad[kol_plassering]).strip() if kol_plassering != ingen_valg else ""

                            try:
                                antall = int(float(str(rad[kol_antall]).replace(",", ".")))
                            except (ValueError, TypeError):
                                antall = 0

                            if not bilmerke or bilmerke.lower() in ["nan", "none", ""] \
                               or not delnavn or delnavn.lower() in ["nan", "none", ""] \
                               or not brukt_lager or brukt_lager.lower() in ["nan", "none", ""] \
                               or antall <= 0:
                                hoppet_over += 1
                                progress.progress((i + 1) / len(df_import), text=f"Importerer... ({i+1}/{len(df_import)})")
                                continue

                            delenummer = "" if delenummer.lower() in ["nan", "none"] else delenummer
                            alias = "" if alias.lower() in ["nan", "none"] else alias
                            hylle = "" if hylle.lower() in ["nan", "none"] else hylle

                            with db_handling("Feil ved import") as cur:
                                cur.execute(
                                    """INSERT INTO inventar 
                                       (bedrift_id, bilmerke, delnavn, delenummer, antall, plassering, hylle, alias) 
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                                    (aktiv_bedrift_id, bilmerke, delnavn,
                                     delenummer or None, antall, brukt_lager,
                                     hylle or None, alias or None)
                                )

                            importert += 1
                        except Exception as e:
                            feil_rader.append(f"Rad {i+2}: {e}")

                        progress.progress((i + 1) / len(df_import), text=f"Importerer... ({i+1}/{len(df_import)})")

                    hent_inventar.clear()
                    progress.empty()
                    # Ved import teller vi kun én endring (ikke én per rad) men kjører backup direkte
                    st.session_state["endring_teller"] = BACKUP_ETTER_ANTALL_ENDRINGER
                    tell_endring_og_backup(aktiv_bedrift_id)
                    st.session_state.varsel = ("success", f"Import fullført! {importert} deler ble lagt til.")
                    if hoppet_over > 0:
                        st.info(f"ℹ️ {hoppet_over} rader ble hoppet over (mangler lager/navn/antall eller er ugyldige).")
                    if feil_rader:
                        with st.expander(f"⚠️ {len(feil_rader)} rader feilet — trykk for detaljer"):
                            for f in feil_rader:
                                st.caption(f)
                    st.rerun()

        except Exception as e:
            st.error(f"Klarte ikke å lese filen: {e}. Prøv å lagre filen som .xlsx eller .csv (UTF-8) fra Excel og last opp på nytt.")

elif st.session_state.side == "Opprett lagre":
    st.header(f"🏢 Administrer lagerlokasjoner — {valgt_bedrift_navn}")

    with st.form("nytt_lager_form", clear_on_submit=True):
        nytt_lager_navn = st.text_input("Navn på nytt lager:")
        submit_nytt_lager = st.form_submit_button("Opprett lager", type="primary")

        if submit_nytt_lager:
            if nytt_lager_navn.strip():
                try:
                    with db_handling("Feil ved opprettelse av lager") as cur:
                        cur.execute("INSERT INTO lagre (bedrift_id, navn) VALUES (%s, %s)", (aktiv_bedrift_id, nytt_lager_navn.strip()))
                    hent_lagre.clear()
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
                                    hent_lagre.clear()
                                    hent_inventar.clear()
                                    st.session_state.varsel = ("success", f"Lageret '{l_navn}' ble endret til '{nytt_navn_input.strip()}'!")
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

elif st.session_state.side == "Logg":
    st.header(f"📜 Logg — {valgt_bedrift_navn}")
    st.write("Her kan du se nylige handlinger med tilhørende delenummer og plassering.")

    with db_handling("Feil ved henting av logg") as cur:
        cur.execute("SELECT tidspunkt, bruker, handling, detaljer, delenummer, plassering FROM historikk WHERE bedrift_id = %s ORDER BY id DESC LIMIT %s", (aktiv_bedrift_id, MAKS_LOGG_RADER))
        logg_poster = cur.fetchall()

    if logg_poster:
        for tid, brukt_av, hand, det, d_nr, d_plass in logg_poster:
            with st.container(border=True):
                ekstra_info = ""
                if d_nr or d_plass:
                    ekstra_info = f"\n\n* **Delenr:** {d_nr if d_nr else '-'} | **Plassering:** {d_plass if d_plass else '-'}"
                st.markdown(f"**[{tid}] {brukt_av}** — *{hand}*\n\n{det}{ekstra_info}")
    else:
        st.info("Ingen historikk å vise enda.")
