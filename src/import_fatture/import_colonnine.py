"""
Import colonnine (stazioni di ricarica) da file Excel al database MySQL.

Struttura Excel tipica:
  - REGIONE, PROVINCIA, COMUNE, Nr IdR installate, Nr IdR allacciate,
    ID Piattaforma Unica Nazionale (multi-valore ;), Nr BdC (multi-valore spazi)

Tabelle DB:
  - colonnine: dati principali per comune
  - colonnine_id_pun: valori multipli ID Piattaforma Unica Nazionale
  - colonnine_bdc: valori multipli Nr BdC

Uso standalone:
  python import_colonnine.py --sal 9 --file "Report colonnine.xlsx"
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

import pandas as pd
import mysql.connector
from mysql.connector import Error

logger = logging.getLogger("import_colonnine")


class ImportatoreColonnine:
    """Importatore file Excel colonnine (stazioni di ricarica)."""

    def __init__(self, host, port, user, password, database, ai_caller=None):
        self.connection = None
        self.cursor = None
        self.db_config = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
        }
        self.ai_caller = ai_caller  # callable(prompt, system_message, max_tokens) -> str

        # Cache
        self.sal_cache = {}        # numero_sal -> id_sal
        self.colonnine_cache = {}  # (id_sal, regione, provincia, comune) -> id_colonnina

        # Statistiche
        self.stats = {
            "righe_processate": 0,
            "righe_inserite": 0,
            "righe_aggiornate": 0,
            "righe_errore": 0,
            "id_pun_inseriti": 0,
            "bdc_inseriti": 0,
        }

    # ── Connessione DB ──────────────────────────────────────────────────

    def connetti_db(self) -> bool:
        try:
            self.connection = mysql.connector.connect(**self.db_config)
            self.cursor = self.connection.cursor(buffered=True)
            logger.info("Connessione DB stabilita")
            return True
        except Error as e:
            logger.error("Connessione DB fallita: %s", e)
            return False

    # ── Cache ───────────────────────────────────────────────────────────

    def carica_cache(self):
        # Cache SAL
        self.cursor.execute("SELECT id_sal, numero_sal FROM sal")
        for id_sal, numero_sal in self.cursor.fetchall():
            self.sal_cache[numero_sal] = id_sal
        logger.info("Cache SAL caricata: %d record", len(self.sal_cache))

        # Cache colonnine esistenti
        self.cursor.execute(
            "SELECT id_colonnina, id_sal, regione, provincia, comune FROM colonnine"
        )
        for id_col, id_sal, reg, prov, com in self.cursor.fetchall():
            chiave = (id_sal, (reg or "").upper().strip(), (prov or "").upper().strip(), (com or "").upper().strip())
            self.colonnine_cache[chiave] = id_col
        logger.info("Cache colonnine caricata: %d record", len(self.colonnine_cache))

    # ── AI Mapping colonne ──────────────────────────────────────────────

    def crea_mapping_colonne_con_ai(self, file_path, sheet_name):
        """
        Usa AI per individuare riga header e mappare colonne Excel -> colonne DB.
        Restituisce (column_mapping, header_row_0indexed).
        """
        try:
            # Leggi prime 5 righe raw
            df_raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None, nrows=5)
            if df_raw.empty:
                logger.warning("File Excel vuoto")
                return self.crea_mapping_colonne_fallback()

            righe_excel = {}
            for idx, row in df_raw.iterrows():
                valori = []
                for val in row:
                    valori.append(str(val).strip() if pd.notna(val) else "")
                righe_excel[f"Riga {idx + 1}"] = valori

            colonne_standard = self._colonne_standard()

            prompt = f"""Sei un esperto analista di dati che deve analizzare un file Excel per:
1. Identificare quale riga contiene l'header delle colonne
2. Mappare le colonne Excel alle colonne standard del database

PRIME RIGHE DEL FILE EXCEL:
{json.dumps(righe_excel, indent=2, ensure_ascii=False)}

COLONNE STANDARD DEL DATABASE:
{json.dumps(colonne_standard, indent=2, ensure_ascii=False)}

ISTRUZIONI:
1. Analizza le righe e identifica quale riga contiene l'header (intestazioni colonne)
2. Cerca parole chiave come "REGIONE", "PROVINCIA", "COMUNE", "IdR", "installate",
   "allacciate", "Piattaforma", "BdC"
3. Considera variazioni: maiuscole/minuscole, spazi extra, troncature, date nei nomi colonna
   (es. "Nr IdR installate a tutto Dic-25" -> "nr_idr_installate")
4. Restituisci SOLO un JSON con questa struttura:

{{
  "header_row": numero_riga_header (1, 2, 3, 4 o 5),
  "mapping": {{
    "colonna_excel_esatta": "colonna_standard_db",
    ...
  }}
}}

ESEMPIO OUTPUT:
{{
  "header_row": 2,
  "mapping": {{
    "REGIONE": "regione",
    "PROVINCIA": "provincia",
    "COMUNE": "comune",
    "Nr IdR installate a tutto Dic-25": "nr_idr_installate",
    "Nr IdR allacciate a tutto Dic-25": "nr_idr_allacciate",
    "ID Piattaforma Unica Nazionale": "id_piattaforma_unica_nazionale",
    "Nr BdC": "nr_bdc"
  }}
}}
"""

            # Chiama AI
            if self.ai_caller:
                response_text = self.ai_caller(prompt, system_message="", max_tokens=2048)
            else:
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    logger.warning("ANTHROPIC_API_KEY non configurata")
                    return self.crea_mapping_colonne_fallback()

                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                message = client.messages.create(
                    model="claude-opus-4-1-20250805",
                    max_tokens=2048,
                    temperature=0.1,
                    messages=[{"role": "user", "content": prompt}],
                )
                response_text = message.content[0].text.strip()

            logger.debug("Risposta AI mapping: %s", response_text)

            # Parsing JSON (gestisci wrapping markdown)
            text = response_text.strip()
            if "```json" in text:
                text = text[text.find("```json") + 7:]
                text = text[:text.find("```")]
            elif "```" in text:
                text = text[text.find("```") + 3:]
                text = text[:text.rfind("```")]
            text = text.strip()

            ai_result = json.loads(text)
            header_row = ai_result.get("header_row", 2)
            mapping_ai = ai_result.get("mapping", {})

            logger.info("AI: header alla riga %d, %d colonne mappate", header_row, len(mapping_ai))

            # Inverti mapping: da {excel_col: db_col} a {db_col: [excel_cols]}
            column_mapping = {}
            for db_col in colonne_standard.keys():
                column_mapping[db_col] = []
            for excel_col, db_col in mapping_ai.items():
                if db_col in column_mapping:
                    column_mapping[db_col].append(excel_col)
            # Rimuovi vuoti
            column_mapping = {k: v for k, v in column_mapping.items() if v}

            return column_mapping, header_row - 1  # 0-indexed

        except json.JSONDecodeError as e:
            logger.warning("Errore parsing JSON da AI: %s", e)
            return self.crea_mapping_colonne_fallback()
        except Exception as e:
            logger.error("Errore mapping AI: %s", e)
            return self.crea_mapping_colonne_fallback()

    def _colonne_standard(self) -> dict:
        """Dizionario colonne standard con varianti note."""
        return {
            "regione": ["REGIONE", "Regione"],
            "provincia": ["PROVINCIA", "Provincia"],
            "comune": ["COMUNE", "Comune"],
            "nr_idr_installate": [
                "Nr IdR installate",
                "IdR installate",
                "Installate",
            ],
            "nr_idr_allacciate": [
                "Nr IdR allacciate",
                "IdR allacciate",
                "Allacciate",
            ],
            "id_piattaforma_unica_nazionale": [
                "ID Piattaforma Unica Nazionale",
                "ID PUN",
                "Piattaforma Unica Nazionale",
            ],
            "nr_bdc": [
                "Nr BdC",
                "BdC",
                "Numero BdC",
            ],
        }

    def crea_mapping_colonne_fallback(self) -> tuple:
        """Mapping statico di fallback. Restituisce (mapping, header_row_0indexed)."""
        colonne_standard = self._colonne_standard()
        # Mapping identita': ogni colonna standard -> le sue varianti
        mapping = {db_col: varianti for db_col, varianti in colonne_standard.items()}
        return mapping, 1  # header riga 2 (0-indexed = 1)

    # ── Parsing valori multipli ─────────────────────────────────────────

    @staticmethod
    def _parse_valori_multipli_pun(valore) -> list[str]:
        """
        Splitta ID Piattaforma Unica Nazionale.
        Formato: "IT*BEC*E007588; IT*BEC*E007590"
        Separatore principale: ;
        """
        if pd.isna(valore) or str(valore).strip() == "":
            return []
        testo = str(valore).strip()
        # Split su ; poi strip ogni parte
        parti = [p.strip() for p in testo.split(";")]
        # Filtra vuoti
        return [p for p in parti if p]

    @staticmethod
    def _parse_valori_multipli_bdc(valore) -> list[str]:
        """
        Splitta Nr BdC.
        Formato: "4100617825 4100596745" (spazi, doppi spazi, newline)
        """
        if pd.isna(valore) or str(valore).strip() == "":
            return []
        testo = str(valore).strip()
        # Normalizza newline e tab a spazi, poi split (gestisce spazi multipli)
        testo = re.sub(r"[\n\r\t]+", " ", testo)
        parti = testo.split()
        return [p.strip() for p in parti if p.strip()]

    # ── Trova colonna nel DataFrame ─────────────────────────────────────

    @staticmethod
    def _trova_colonna(df, nomi_possibili: list[str]):
        """Cerca una colonna nel DataFrame provando i nomi possibili (case-insensitive, parziale)."""
        for nome in nomi_possibili:
            # Match esatto (case-insensitive)
            for col in df.columns:
                if str(col).strip().lower() == nome.lower():
                    return col
        # Match parziale
        for nome in nomi_possibili:
            for col in df.columns:
                if nome.lower() in str(col).strip().lower():
                    return col
        return None

    # ── Import principale ───────────────────────────────────────────────

    def importa_excel(self, file_path, numero_sal) -> bool:
        """
        Importa un file Excel colonnine nel database.
        Restituisce True se l'import è andato a buon fine.
        """
        logger.info("Avvio import colonnine: file='%s', SAL=%d", file_path, numero_sal)

        # Verifica SAL
        id_sal = self.sal_cache.get(numero_sal)
        if not id_sal:
            logger.error("SAL %d non trovato in cache", numero_sal)
            return False

        # Determina foglio Excel
        xls = pd.ExcelFile(file_path)
        sheet_names = xls.sheet_names
        if not sheet_names:
            logger.error("Nessun foglio trovato nel file")
            return False

        sheet_name = sheet_names[0]
        logger.info("Foglio selezionato: '%s'", sheet_name)

        # AI mapping colonne
        use_ai = os.getenv("USE_AI_MAPPING", "true").lower() == "true"
        if use_ai and (self.ai_caller or os.getenv("ANTHROPIC_API_KEY")):
            try:
                column_mapping, header_row = self.crea_mapping_colonne_con_ai(file_path, sheet_name)
                logger.info("Mapping AI: %s (header riga %d)", column_mapping, header_row + 1)
            except Exception as e:
                logger.warning("Mapping AI fallito: %s. Uso fallback.", e)
                column_mapping, header_row = self.crea_mapping_colonne_fallback()
        else:
            column_mapping, header_row = self.crea_mapping_colonne_fallback()

        # Leggi Excel con header corretto
        df = pd.read_excel(file_path, sheet_name=sheet_name, header=header_row)
        logger.info("Lette %d righe dal foglio '%s'", len(df), sheet_name)

        if df.empty:
            logger.warning("Nessun dato nel file")
            return True

        # Risolvi nomi colonne Excel
        col_regione = self._trova_colonna(df, column_mapping.get("regione", ["REGIONE"]))
        col_provincia = self._trova_colonna(df, column_mapping.get("provincia", ["PROVINCIA"]))
        col_comune = self._trova_colonna(df, column_mapping.get("comune", ["COMUNE"]))
        col_installate = self._trova_colonna(df, column_mapping.get("nr_idr_installate", ["Nr IdR installate"]))
        col_allacciate = self._trova_colonna(df, column_mapping.get("nr_idr_allacciate", ["Nr IdR allacciate"]))
        col_pun = self._trova_colonna(df, column_mapping.get("id_piattaforma_unica_nazionale", ["ID Piattaforma Unica Nazionale"]))
        col_bdc = self._trova_colonna(df, column_mapping.get("nr_bdc", ["Nr BdC"]))

        # Verifica colonne obbligatorie
        if not col_regione or not col_provincia or not col_comune:
            logger.error(
                "Colonne obbligatorie non trovate: regione=%s, provincia=%s, comune=%s",
                col_regione, col_provincia, col_comune,
            )
            return False

        logger.info(
            "Colonne trovate: regione='%s', provincia='%s', comune='%s', "
            "installate='%s', allacciate='%s', pun='%s', bdc='%s'",
            col_regione, col_provincia, col_comune,
            col_installate, col_allacciate, col_pun, col_bdc,
        )

        # Processa righe
        for idx, row in df.iterrows():
            self.stats["righe_processate"] += 1
            try:
                self._importa_riga(row, id_sal, col_regione, col_provincia, col_comune,
                                   col_installate, col_allacciate, col_pun, col_bdc)
            except Exception as e:
                self.stats["righe_errore"] += 1
                logger.warning("Errore riga %d: %s", idx + 1, e)

        self.connection.commit()

        logger.info(
            "Import colonnine completato: %d processate, %d inserite, %d aggiornate, "
            "%d errori, %d ID PUN, %d BdC",
            self.stats["righe_processate"],
            self.stats["righe_inserite"],
            self.stats["righe_aggiornate"],
            self.stats["righe_errore"],
            self.stats["id_pun_inseriti"],
            self.stats["bdc_inseriti"],
        )
        return True

    def _importa_riga(self, row, id_sal, col_regione, col_provincia, col_comune,
                      col_installate, col_allacciate, col_pun, col_bdc):
        """Importa una singola riga: INSERT/UPDATE colonnine + figli multi-valore."""
        regione = str(row.get(col_regione, "")).strip() if pd.notna(row.get(col_regione)) else ""
        provincia = str(row.get(col_provincia, "")).strip() if pd.notna(row.get(col_provincia)) else ""
        comune = str(row.get(col_comune, "")).strip() if pd.notna(row.get(col_comune)) else ""

        # Skip righe senza comune (righe vuote/totali)
        if not comune:
            return

        # Valori numerici
        nr_installate = self._converti_intero(row.get(col_installate) if col_installate else None)
        nr_allacciate = self._converti_intero(row.get(col_allacciate) if col_allacciate else None)

        # Chiave cache
        chiave = (id_sal, regione.upper().strip(), provincia.upper().strip(), comune.upper().strip())

        if chiave in self.colonnine_cache:
            # UPDATE
            id_colonnina = self.colonnine_cache[chiave]
            self.cursor.execute(
                """UPDATE colonnine
                   SET nr_idr_installate = %s, nr_idr_allacciate = %s,
                       data_importazione = %s
                   WHERE id_colonnina = %s""",
                (nr_installate, nr_allacciate, datetime.now(), id_colonnina),
            )
            self.stats["righe_aggiornate"] += 1
        else:
            # INSERT
            self.cursor.execute(
                """INSERT INTO colonnine
                   (id_sal, regione, provincia, comune, nr_idr_installate, nr_idr_allacciate)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (id_sal, regione, provincia, comune, nr_installate, nr_allacciate),
            )
            id_colonnina = self.cursor.lastrowid
            self.colonnine_cache[chiave] = id_colonnina
            self.stats["righe_inserite"] += 1

        # Multi-valore: elimina figli esistenti e re-inserisci
        self.cursor.execute("DELETE FROM colonnine_id_pun WHERE id_colonnina = %s", (id_colonnina,))
        self.cursor.execute("DELETE FROM colonnine_bdc WHERE id_colonnina = %s", (id_colonnina,))

        # ID Piattaforma Unica Nazionale
        valori_pun = self._parse_valori_multipli_pun(row.get(col_pun) if col_pun else None)
        for val in valori_pun:
            self.cursor.execute(
                "INSERT IGNORE INTO colonnine_id_pun (id_colonnina, id_piattaforma) VALUES (%s, %s)",
                (id_colonnina, val),
            )
            self.stats["id_pun_inseriti"] += 1

        # Nr BdC
        valori_bdc = self._parse_valori_multipli_bdc(row.get(col_bdc) if col_bdc else None)
        for val in valori_bdc:
            self.cursor.execute(
                "INSERT IGNORE INTO colonnine_bdc (id_colonnina, numero_bdc) VALUES (%s, %s)",
                (id_colonnina, val),
            )
            self.stats["bdc_inseriti"] += 1

    @staticmethod
    def _converti_intero(valore):
        """Converte un valore in intero, restituisce None se non possibile."""
        if pd.isna(valore) or valore is None:
            return None
        try:
            return int(float(str(valore).strip()))
        except (ValueError, TypeError):
            return None

    # ── Chiusura ────────────────────────────────────────────────────────

    def chiudi_connessione(self):
        try:
            if self.cursor:
                self.cursor.close()
            if self.connection:
                self.connection.close()
            logger.info("Connessione DB chiusa")
        except Error as e:
            logger.error("Errore chiusura connessione: %s", e)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import colonnine (stazioni di ricarica)")
    parser.add_argument("--sal", type=int, required=True, help="Numero SAL")
    parser.add_argument("--file", type=str, required=True, help="Percorso file Excel")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # DB config dev locale
    DB_CONFIG = {
        "host": "localhost",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "polis",
    }

    importer = ImportatoreColonnine(
        DB_CONFIG["host"],
        DB_CONFIG["port"],
        DB_CONFIG["user"],
        DB_CONFIG["password"],
        DB_CONFIG["database"],
    )

    if not importer.connetti_db():
        sys.exit(1)

    importer.carica_cache()
    success = importer.importa_excel(args.file, args.sal)
    importer.chiudi_connessione()

    if not success:
        sys.exit(1)
    logger.info("Import completato con successo")


if __name__ == "__main__":
    main()
