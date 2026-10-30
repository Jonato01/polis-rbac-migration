import urllib

import pandas as pd
import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime, date
import os
import sys
import re
from typing import Dict, List, Any, Optional, Tuple
from dateutil.relativedelta import relativedelta
from import_error_handler import ImportErrorHandler
import requests
import json
import tempfile
from urllib.parse import urljoin


# ─── Eccezioni Custom per Personale SAL 7+ ────────────────────────────────────
class ImportPersonaleSAL7Error(Exception):
    """Eccezione base per errori di importazione personale SAL 7+"""
    pass


class DipendenteError(ImportPersonaleSAL7Error):
    """Errore relativo ai dipendenti"""
    pass


class QualificaError(ImportPersonaleSAL7Error):
    """Errore relativo alle qualifiche"""
    pass


class SedeError(ImportPersonaleSAL7Error):
    """Errore relativo alle sedi"""
    pass


class CUPError(ImportPersonaleSAL7Error):
    """Errore relativo ai CUP"""
    pass


class ValidazionePersonaleSAL7Error(ImportPersonaleSAL7Error):
    """Errore di validazione dei dati personale SAL 7+"""
    pass


class CostoPersonaleError(ImportPersonaleSAL7Error):
    """Errore nell'inserimento costi personale"""
    pass


# ─── Configurazione logging ───────────────────────────────────────────────────
def setup_logging():
    """Configura il sistema di logging su file e console"""
    if not os.path.exists('logs'):
        os.makedirs('logs')

    log_filename = f'logs/import_personale_sal7_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = logging.getLogger('ImportPersonaleSAL7')
    logger.setLevel(logging.INFO)

    # Handler per file
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.INFO)

    # Handler per console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # Formato del log
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


logger = setup_logging()


# ─── Gestione SAL 7+ ──────────────────────────────────────────────────────────
class SALManagerSAL7:
    """Gestisce la logica temporale per SAL 7+ (formato standardizzato)"""

    # SAL 7 inizia il 1° settembre 2024
    SAL7_START_DATE = date(2024, 9, 1)
    MESI_PER_SAL = 4

    # Mapping mesi italiano
    MESI_ITA = {
        1: 'Gennaio', 2: 'Febbraio', 3: 'Marzo', 4: 'Aprile',
        5: 'Maggio', 6: 'Giugno', 7: 'Luglio', 8: 'Agosto',
        9: 'Settembre', 10: 'Ottobre', 11: 'Novembre', 12: 'Dicembre'
    }

    # Abbreviazioni mesi
    MESI_ABBR = {
        1: 'gen', 2: 'feb', 3: 'mar', 4: 'apr',
        5: 'mag', 6: 'giu', 7: 'lug', 8: 'ago',
        9: 'set', 10: 'ott', 11: 'nov', 12: 'dic'
    }

    @classmethod
    def get_periodo_sal(cls, numero_sal):
        """Restituisce il periodo (data_inizio, data_fine) per qualsiasi SAL"""
        # SAL 7 inizia il 1° settembre 2024
        if numero_sal >= 7:
            mesi_dal_sal7 = (numero_sal - 7) * cls.MESI_PER_SAL
            data_inizio = cls.SAL7_START_DATE + relativedelta(months=mesi_dal_sal7)
            data_fine = data_inizio + relativedelta(months=cls.MESI_PER_SAL, days=-1)
        else:
            # Adatta qui la logica per SAL < 7 (esempio: 4 mesi a ritroso)
            mesi_prima_sal7 = (7 - numero_sal) * cls.MESI_PER_SAL
            data_fine = cls.SAL7_START_DATE + relativedelta(months=-mesi_prima_sal7, days=-1)
            data_inizio = data_fine - relativedelta(months=cls.MESI_PER_SAL) + relativedelta(days=1)
        return data_inizio, data_fine

    @classmethod
    def get_mesi_sal(cls, numero_sal):
        """
        Restituisce la lista dei mesi per qualsiasi SAL.
        Per SAL >= 7 usa la logica standardizzata, per SAL < 7 calcola a ritroso.
        """
        data_inizio, _ = cls.get_periodo_sal(numero_sal)

        mesi = []
        for i in range(cls.MESI_PER_SAL):
            data_mese = data_inizio + relativedelta(months=i)
            mesi.append({
                'numero': data_mese.month,
                'nome': cls.MESI_ITA[data_mese.month],
                'nome_breve': cls.MESI_ABBR[data_mese.month],
                'anno': data_mese.year
            })

        return mesi


# ─── Classe per tracciare lo stato dell'importazione ──────────────────────────
class ImportStatusSAL7:
    """Classe per gestire lo stato dell'importazione SAL 7+"""

    def __init__(self):
        self.status = "pending"
        self.progress = 0
        self.message = "In attesa di avvio"
        self.start_time = None
        self.end_time = None
        self.rows_total = 0
        self.rows_imported = 0
        self.rows_failed = 0
        self.errors = []
        self.warnings = []

    def update(self, status: str, progress: int, message: str):
        """Aggiorna lo stato dell'importazione"""
        self.status = status
        self.progress = progress
        self.message = message
        logger.info(f"Stato: {status} ({progress}%): {message}")

    def add_error(self, error: Dict[str, Any]):
        """Aggiunge un errore alla lista"""
        self.errors.append(error)
        self.rows_failed += 1

    def add_warning(self, warning: str):
        """Aggiunge un warning alla lista"""
        self.warnings.append(warning)

    def get_summary(self) -> Dict[str, Any]:
        """Restituisce un riepilogo dell'importazione"""
        return {
            'status': self.status,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration': (self.end_time - self.start_time).total_seconds() if self.end_time and self.start_time else 0,
            'rows_total': self.rows_total,
            'rows_imported': self.rows_imported,
            'rows_failed': self.rows_failed,
            'success_rate': (self.rows_imported / self.rows_total * 100) if self.rows_total > 0 else 0,
            'errors': self.errors,
            'warnings': self.warnings
        }


# ─── Classe ImportatorePersonaleSAL7 ───────────────────────────────────────────
class ImportatorePersonaleSAL7:
    def __init__(self, host, port, user, password, database):
        """Inizializza la connessione al database per SAL 7+"""
        self.connection = None
        self.cursor = None
        self.sal_manager = SALManagerSAL7()
        self.db_config = {
            'host': host,
            'port': port,
            'user': user,
            'password': password,
            'database': database
        }

        # Cache per lookup rapido
        self.dipendenti_cache = {}
        self.qualifiche_cache = {}
        self.sedi_cache = {}
        self.categorie_cache = {}
        self.cup_cache = {}
        self.sal_cache = {}

        # Stato dell'importazione e error handler
        self.import_status = ImportStatusSAL7()
        self.error_handler = None

    def connetti_db(self):
        """Stabilisce la connessione al database"""
        try:
            self.connection = mysql.connector.connect(**self.db_config)
            self.cursor = self.connection.cursor(buffered=True)
            logger.info(f"Connessione al database {self.db_config['database']} stabilita con successo")

            # Inizializza error handler
            self.error_handler = ImportErrorHandler(self.connection, self.cursor)

            return True
        except Error as e:
            logger.error(f"Errore nella connessione al database: {e}")
            return False

    def chiudi_connessione(self):
        """Chiude la connessione al database"""
        if self.cursor:
            self.cursor.close()
        if self.connection and self.connection.is_connected():
            self.connection.close()
            logger.info("Connessione al database chiusa")

    def carica_cache(self):
        """Carica in cache gli ID delle tabelle esistenti per SAL 7+"""
        try:
            # Cache qualifiche
            self.cursor.execute("SELECT cod_qualifica, des_qualifica FROM qualifica")
            for cod, des in self.cursor.fetchall():
                self.qualifiche_cache[des] = cod

            # Cache sedi
            self.cursor.execute("SELECT cod_sede, denominazione, città FROM localizzazione_sedi")
            for cod, denom, citta in self.cursor.fetchall():
                self.sedi_cache[citta] = cod

            # Cache categorie spesa
            self.cursor.execute("SELECT id_categoria, descrizione FROM categoria_spesa")
            for id_c, desc in self.cursor.fetchall():
                self.categorie_cache[desc] = id_c

            # Cache CUP
            self.cursor.execute("SELECT codice_cup, descrizione FROM codice_unico_progetto")
            for cup, desc in self.cursor.fetchall():
                self.cup_cache[cup] = cup

            # Cache SAL
            self.cursor.execute("SELECT id_sal, numero_sal FROM sal")
            for id_s, num in self.cursor.fetchall():
                self.sal_cache[num] = id_s

            logger.info("Cache delle tabelle SAL 7+ caricata con successo")
        except Error as e:
            logger.error(f"Errore nel caricamento della cache: {e}")

    # ─── Funzioni di conversione SICURE per SAL 7+ ────────────────────────────

    def converti_valuta_sal7(self, valore):
        """Converte un valore di valuta in float, gestendo il formato SAL 7+ (€ e virgole)"""
        if pd.isna(valore) or valore is None:
            return 0.0

        if isinstance(valore, (int, float)):
            return float(valore)

        if isinstance(valore, str):
            # Rimuovi € e spazi
            valore = valore.replace('€', '').strip()

            # Gestisci formato italiano: 1.234,56
            if ',' in valore:
                if '.' in valore and valore.rindex(',') > valore.rindex('.'):
                    # Formato: 1.234,56
                    valore = valore.replace('.', '').replace(',', '.')
                else:
                    # Solo virgola: 1234,56
                    valore = valore.replace(',', '.')

            try:
                return float(valore)
            except ValueError:
                raise ValidazionePersonaleSAL7Error(f"Impossibile convertire il valore '{valore}' in numero")

        return 0.0

    def converti_numero_sal7(self, valore):
        """Converte un valore in float per SAL 7+ - versione sicura"""
        # Se il valore e' una Series (colonne duplicate nel DataFrame),
        # estrai il primo valore scalare
        if isinstance(valore, pd.Series):
            valore = valore.iloc[0]
        if pd.isna(valore) or valore == '':
            return None
        if isinstance(valore, (int, float)):
            return float(valore)

        # Gestione stringhe: rimuove spazi e converte virgola in punto
        valore_str = str(valore).strip().replace(',', '.')
        try:
            return float(valore_str)
        except ValueError:
            raise ValidazionePersonaleSAL7Error(f"Valore numerico non valido: {valore}")

    def converti_data_sal7(self, data_str):
        """Converte una data dal formato stringa al formato MySQL per SAL 7+"""
        if pd.isna(data_str) or not data_str:
            return None
        if isinstance(data_str, datetime):
            return data_str.date()

        try:
            # Formati comuni per SAL 7+
            formati = ['%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y']
            for formato in formati:
                try:
                    return datetime.strptime(str(data_str), formato).date()
                except:
                    continue
            return pd.to_datetime(data_str).date()
        except Exception:
            raise ValidazionePersonaleSAL7Error(f"Data non valida: {data_str}")

    # ─── Metodi "inserisci o recupera" SICURI per SAL 7+ ──────────────────────

    def inserisci_o_recupera_dipendente_sicuro(self, nome, cognome):
        """Inserisce un nuovo dipendente o recupera l'ID esistente - versione SAL 7+"""
        if not nome or not cognome:
            raise DipendenteError("Nome e cognome sono obbligatori")

        nome = str(nome).strip()
        cognome = str(cognome).strip()

        if not nome or not cognome:
            raise DipendenteError("Nome e cognome non possono essere vuoti")

        # Crea una chiave univoca per il dipendente
        chiave = f"{cognome.upper()}_{nome.upper()}"

        # Controlla cache
        if chiave in self.dipendenti_cache:
            return self.dipendenti_cache[chiave]

        try:
            # Verifica se esiste già
            query = "SELECT cod_dipendente FROM dipendenti WHERE UPPER(nome) = %s AND UPPER(cognome) = %s"
            self.cursor.execute(query, (nome.upper(), cognome.upper()))
            result = self.cursor.fetchone()

            if result:
                cod_dipendente = result[0]
                self.dipendenti_cache[chiave] = cod_dipendente
                return cod_dipendente

            # Trova il prossimo codice dipendente disponibile
            self.cursor.execute("SELECT MAX(cod_dipendente) FROM dipendenti")
            max_cod = self.cursor.fetchone()[0]
            nuovo_cod = (max_cod + 1) if max_cod else 1

            # Inserisci nuovo dipendente
            query = "INSERT INTO dipendenti (cod_dipendente, nome, cognome) VALUES (%s, %s, %s)"
            self.cursor.execute(query, (nuovo_cod, nome, cognome))

            self.dipendenti_cache[chiave] = nuovo_cod
            logger.debug(f"Inserito nuovo dipendente: {nome} {cognome} (Cod: {nuovo_cod})")
            return nuovo_cod

        except Error as e:
            raise DipendenteError(f"Errore database nell'inserimento dipendente '{nome} {cognome}': {e}")

    def inserisci_o_recupera_qualifica_sicuro(self, qualifica, costo_orario):
        """Inserisce una nuova qualifica o recupera l'ID esistente - SAL 7+ standardizzato"""
        if not qualifica:
            raise QualificaError("Qualifica è obbligatoria")

        # Normalizza la qualifica (formato SAL 7+ standardizzato)
        qualifica = str(qualifica).strip().upper()

        # Controlla cache
        if qualifica in self.qualifiche_cache:
            return self.qualifiche_cache[qualifica]

        try:
            # Mappa delle qualifiche per SAL 7+ (standardizzata)
            mappa_qualifiche = {
                'A1': 1, 'A2': 2, 'B': 3, 'C': 4, 'D': 5, 'E': 6, 'DIRIGENTE': 7
            }

            cod_qualifica = mappa_qualifiche.get(qualifica)
            if not cod_qualifica:
                raise QualificaError(f"Qualifica non riconosciuta: {qualifica}")

            # Verifica se esiste già
            self.cursor.execute("SELECT cod_qualifica FROM qualifica WHERE cod_qualifica = %s",
                                (cod_qualifica,))
            result = self.cursor.fetchone()

            if result:
                self.qualifiche_cache[qualifica] = cod_qualifica
                return cod_qualifica

            # Inserisci nuova qualifica
            query = "INSERT INTO qualifica (cod_qualifica, des_qualifica) VALUES (%s, %s)"
            self.cursor.execute(query, (cod_qualifica, qualifica))

            self.qualifiche_cache[qualifica] = cod_qualifica
            logger.debug(f"Inserita nuova qualifica: {qualifica} (Cod: {cod_qualifica})")
            return cod_qualifica

        except Error as e:
            raise QualificaError(f"Errore database nell'inserimento qualifica '{qualifica}': {e}")

    def inserisci_o_recupera_sede_sicuro(self, citta):
        """Inserisce una nuova sede o recupera l'ID esistente - versione SAL 7+"""
        if not citta:
            raise SedeError("Città è obbligatoria per la sede")

        # Normalizza la città
        citta = str(citta).strip()

        if not citta:
            raise SedeError("Nome città non può essere vuoto")

        # Controlla cache
        if citta in self.sedi_cache:
            return self.sedi_cache[citta]

        try:
            # Verifica se esiste già
            query = "SELECT cod_sede FROM localizzazione_sedi WHERE città = %s"
            self.cursor.execute(query, (citta,))
            result = self.cursor.fetchone()

            if result:
                cod_sede = result[0]
                self.sedi_cache[citta] = cod_sede
                return cod_sede

            # Trova il prossimo codice sede disponibile
            self.cursor.execute("SELECT MAX(cod_sede) FROM localizzazione_sedi")
            max_cod = self.cursor.fetchone()[0]
            nuovo_cod = (max_cod + 1) if max_cod else 1

            # Determina la denominazione in base alla città (standardizzato SAL 7+)
            denominazione = self.determina_area_geografica_sal7(citta)

            # Inserisci nuova sede
            query = """INSERT INTO localizzazione_sedi
                           (cod_sede, denominazione, città)
                       VALUES (%s, %s, %s)"""
            self.cursor.execute(query, (nuovo_cod, denominazione, citta))

            self.sedi_cache[citta] = nuovo_cod
            logger.debug(f"Inserita nuova sede: {citta} (Cod: {nuovo_cod})")
            return nuovo_cod

        except Error as e:
            raise SedeError(f"Errore database nell'inserimento sede '{citta}': {e}")

    def inserisci_o_recupera_cup_sicuro(self, codice_cup, descrizione):
        """Inserisce un nuovo CUP o recupera quello esistente - versione SAL 7+"""
        if not codice_cup:
            return None  # CUP opzionale

        codice_cup = str(codice_cup).strip()

        # Controlla cache
        if codice_cup in self.cup_cache:
            return codice_cup

        try:
            # Verifica se esiste già
            query = "SELECT codice_cup FROM codice_unico_progetto WHERE codice_cup = %s"
            self.cursor.execute(query, (codice_cup,))
            result = self.cursor.fetchone()

            if result:
                self.cup_cache[codice_cup] = codice_cup
                return codice_cup

            # Inserisci nuovo CUP
            query = """INSERT INTO codice_unico_progetto
                           (codice_cup, descrizione)
                       VALUES (%s, %s)"""
            self.cursor.execute(query, (codice_cup, descrizione or ""))

            self.cup_cache[codice_cup] = codice_cup
            logger.debug(f"Inserito nuovo CUP: {codice_cup}")
            return codice_cup

        except Error as e:
            raise CUPError(f"Errore database nell'inserimento CUP '{codice_cup}': {e}")

    def inserisci_costo_medio_unitario_sal_sicuro(self, cod_qualifica, id_sal, costo_orario):
        """Inserisce o aggiorna il costo medio unitario per qualifica e SAL 7+"""
        if not cod_qualifica or not id_sal or not costo_orario:
            return

        try:
            # Verifica se esiste già
            query = """SELECT costo_orario_medio \
                       FROM costo_medio_unitario_sal
                       WHERE cod_qualifica = %s \
                         AND id_sal = %s"""
            self.cursor.execute(query, (cod_qualifica, id_sal))
            result = self.cursor.fetchone()

            if result:
                # Aggiorna se diverso
                if abs(float(result[0]) - float(costo_orario)) > 0.01:
                    query = """UPDATE costo_medio_unitario_sal
                               SET costo_orario_medio = %s
                               WHERE cod_qualifica = %s \
                                 AND id_sal = %s"""
                    self.cursor.execute(query, (costo_orario, cod_qualifica, id_sal))
                    logger.debug(f"Aggiornato costo orario per qualifica {cod_qualifica} SAL {id_sal}: {costo_orario}")
            else:
                # Inserisci nuovo
                query = """INSERT INTO costo_medio_unitario_sal
                               (cod_qualifica, id_sal, costo_orario_medio)
                           VALUES (%s, %s, %s)"""
                self.cursor.execute(query, (cod_qualifica, id_sal, costo_orario))
                logger.debug(f"Inserito costo orario per qualifica {cod_qualifica} SAL {id_sal}: {costo_orario}")

        except Error as e:
            raise CostoPersonaleError(f"Errore nell'inserimento costo medio unitario: {e}")

    def determina_area_geografica_sal7(self, citta):
        """Determina l'area geografica in base alla città - mapping standardizzato SAL 7+"""
        # Usa 'Strutture Centrali' per Roma, altrimenti determina l'area
        if citta.upper() == 'ROMA':
            return 'Strutture Centrali'

        # Dizionario delle aree geografiche (standardizzato per SAL 7+)
        aree = {
            'Nord Ovest': ['MILANO', 'TORINO', 'GENOVA', 'AOSTA', 'COMO', 'VARESE',
                           'BERGAMO', 'BRESCIA', 'LECCO', 'LODI', 'MANTOVA', 'MONZA',
                           'PAVIA', 'SONDRIO', 'CREMONA', 'ALESSANDRIA', 'ASTI',
                           'BIELLA', 'CUNEO', 'NOVARA', 'VERBANIA', 'VERCELLI',
                           'IMPERIA', 'LA SPEZIA', 'SAVONA', 'ALBA', 'PINEROLO',
                           'BUSTO ARSIZIO'],
            'Nord Est': ['VENEZIA', 'PADOVA', 'VERONA', 'VICENZA', 'TREVISO',
                         'BELLUNO', 'ROVIGO', 'BOLZANO', 'TRENTO', 'UDINE',
                         'GORIZIA', 'PORDENONE', 'TRIESTE'],
            'Centro Nord': ['BOLOGNA', 'MODENA', 'PARMA', 'PIACENZA', 'RAVENNA',
                            'REGGIO EMILIA', 'FERRARA', 'FORLI', 'RIMINI',
                            'FIRENZE', 'PISA', 'LIVORNO', 'LUCCA', 'MASSA',
                            'PISTOIA', 'PRATO', 'SIENA', 'AREZZO', 'GROSSETO',
                            'PERUGIA', 'TERNI', 'ANCONA', 'ASCOLI PICENO',
                            'FERMO', 'MACERATA', 'PESARO', 'SCANDIANO',
                            'SESTO FIORENTINO'],
            'Centro': ['LATINA', 'FROSINONE', 'RIETI', 'VITERBO',
                       'L\'AQUILA', 'CHIETI', 'PESCARA', 'TERAMO',
                       'CAMPOBASSO', 'ISERNIA', 'CAGLIARI', 'SASSARI',
                       'NUORO', 'ORISTANO', 'ELMAS'],
            'Sud': ['NAPOLI', 'SALERNO', 'AVELLINO', 'BENEVENTO', 'CASERTA',
                    'BARI', 'BRINDISI', 'FOGGIA', 'LECCE', 'TARANTO',
                    'POTENZA', 'MATERA', 'CATANZARO', 'COSENZA',
                    'REGGIO DI CALABRIA', 'VIBO VALENTIA', 'CASTROVILLARI',
                    'SIDERNO', 'SALA CONSILINA'],
            'Sicilia': ['PALERMO', 'CATANIA', 'MESSINA', 'AGRIGENTO',
                        'CALTANISSETTA', 'ENNA', 'RAGUSA', 'SIRACUSA', 'TRAPANI']
        }

        # Cerca la città nelle aree
        for area, citta_list in aree.items():
            if citta.upper() in citta_list:
                return area

        return 'Area non definita'

    # ─── Validazione Pre-Inserimento SAL 7+ ───────────────────────────────────

    def valida_riga_personale_sal7_completa(self, row_data, mesi_info, numero_sal):
        """Valida tutti i dati di una riga personale SAL 7+ PRIMA di iniziare gli inserimenti"""
        errori = []

        # Validazioni obbligatorie SAL 7+
        if not row_data.get('cognome'):
            errori.append("Cognome mancante")

        if not row_data.get('nome'):
            errori.append("Nome mancante")

        if not row_data.get('citta'):
            errori.append("Città/sede mancante")

        if not row_data.get('qualifica'):
            errori.append("Qualifica mancante")


        # Validazione qualifica per SAL 7+ (formato standardizzato)
        qualifica = str(row_data.get('qualifica', '')).strip().upper()
        qualifiche_valide = ['A1', 'A2', 'B', 'C', 'D', 'E', 'DIRIGENTE']
        if qualifica and qualifica not in qualifiche_valide:
            errori.append(f"Qualifica non valida: {qualifica}. Valide: {', '.join(qualifiche_valide)}")

        # Validazione costo orario
        try:
            costo_orario = self.converti_numero_sal7(row_data.get('costo_orario'))
            if costo_orario is not None and costo_orario <= 0:
                errori.append(f"Costo orario non valido: {costo_orario}")
        except Exception as e:
            errori.append(f"Costo orario non convertibile: {e}")

        # Validazione date
        if row_data.get('data_passaggio') and pd.notna(row_data.get('data_passaggio')):
            try:
                self.converti_data_sal7(row_data.get('data_passaggio'))
            except Exception as e:
                errori.append(f"Data passaggio non valida: {e}")

        # Validazione ore mensili SAL 7+
        for i, mese in enumerate(mesi_info):
            col_ore = f'ore_mese_{i + 1}'
            if col_ore in row_data and row_data[col_ore] is not None:
                try:
                    ore = self.converti_numero_sal7(row_data[col_ore])
                    if ore is not None and (ore < 0 or ore > 300):  # Controllo ragionevolezza
                        errori.append(f"Ore {mese['nome']} non ragionevoli: {ore}")
                except Exception as e:
                    errori.append(f"Ore {mese['nome']} non convertibili: {e}")

        # Validazione costi mensili SAL 7+
        for i, mese in enumerate(mesi_info):
            col_costo = f'costo_mese_{i + 1}'
            if col_costo in row_data and row_data[col_costo] is not None:
                try:
                    costo = self.converti_valuta_sal7(row_data[col_costo])
                    if costo is not None and costo < 0:
                        errori.append(f"Costo {mese['nome']} negativo: {costo}")
                except Exception as e:
                    errori.append(f"Costo {mese['nome']} non convertibile: {e}")

        if errori:
            raise ValidazionePersonaleSAL7Error(f"Errori di validazione SAL 7+: {'; '.join(errori)}")

    # ─── Importazione Atomica per Riga SAL 7+ ─────────────────────────────────

    def importa_riga_personale_sal7_atomica(self, row_data, numero_riga, numero_sal, mesi_info):
        """Importa una singola riga personale SAL 7+ in modo completamente atomico"""
        savepoint_name = f"riga_personale_sal7_{numero_riga}_{numero_sal}"

        try:
            # 1. Inizia savepoint per questa riga
            self.cursor.execute(f"SAVEPOINT {savepoint_name}")

            # 2. Validazione completa PRIMA degli inserimenti
            self.valida_riga_personale_sal7_completa(row_data, mesi_info, numero_sal)

            # 3. Inserimento sicuro (ora garantito dalla validazione)
            success = self.inserisci_costo_personale_sal7_sicuro(row_data, numero_sal, mesi_info,numero_riga)

            if not success:
                raise CostoPersonaleError("Inserimento SAL 7+ fallito senza eccezione specifica")

            # 4. Se arrivo qui, tutto OK - rilascio il savepoint
            self.cursor.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            return True

        except Exception as e:
            # 5. Rollback della riga
            try:
                self.cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                self.cursor.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except Error as rollback_error:
                logger.error(f"Errore durante rollback savepoint {savepoint_name}: {rollback_error}")

            # 6. Salva errore in transazione separata
            self.salva_errore_personale_sal7_sicuro(numero_riga, row_data, e, numero_sal)

            # 7. La riga viene scartata
            logger.warning(f"Riga personale SAL 7+ {numero_riga} scartata: {str(e)[:200]}")
            return False

    def salva_errore_personale_sal7_sicuro(self, numero_riga, row_data, errore, numero_sal):
        """Salva errore personale SAL 7+ in una transazione separata per evitare rollback"""
        if not self.error_handler:
            return

        try:
            # Usa una connessione separata per salvare l'errore
            error_connection = mysql.connector.connect(**self.db_config)
            error_cursor = error_connection.cursor()

            # Crea un error handler temporaneo
            temp_error_handler = ImportErrorHandler(error_connection, error_cursor)
            temp_error_handler.session_id = self.error_handler.session_id

            # Prepara i dati della riga per il salvataggio
            dati_riga = {}
            for k, v in row_data.items():
                try:
                    if pd.notna(v):
                        dati_riga[k] = str(v)[:500]  # Limita lunghezza
                    else:
                        dati_riga[k] = None
                except:
                    dati_riga[k] = "Errore conversione"

            # Classifica il tipo di errore per personale SAL 7+
            tipo_errore = self.classifica_errore_personale_sal7(str(errore))

            # Salva l'errore
            temp_error_handler.salva_errore(
                numero_riga=numero_riga,
                nome_foglio=f"SAL_7+_{numero_sal}",
                tipo_errore=tipo_errore,
                messaggio_errore=str(errore)[:1000],  # Limita lunghezza messaggio
                dati_riga=dati_riga,
                tipo_import='personale_sal7'
            )

            error_connection.commit()
            error_connection.close()

        except Exception as save_error:
            logger.error(f"CRITICO: Impossibile salvare errore per riga personale SAL 7+ {numero_riga}: {save_error}")

    def classifica_errore_personale_sal7(self, messaggio_errore):
        """Classifica il tipo di errore per i dati del personale SAL 7+"""
        messaggio_lower = messaggio_errore.lower()


        if 'cognome' in messaggio_lower and ('mancante' in messaggio_lower or 'vuoto' in messaggio_lower):
            return 'cognome_mancante'
        elif 'nome' in messaggio_lower and ('mancante' in messaggio_lower or 'vuoto' in messaggio_lower):
            return 'nome_mancante'
        elif 'qualifica' in messaggio_lower:
            return 'qualifica_non_valida'
        elif 'costo orario' in messaggio_lower:
            return 'costo_orario_invalido'
        elif 'ore' in messaggio_lower and 'convertibile' in messaggio_lower:
            return 'ore_non_convertibili'
        elif 'data' in messaggio_lower and 'valida' in messaggio_lower:
            return 'data_non_valida'
        elif 'foreign key constraint' in messaggio_lower:
            return 'vincolo_integrità'
        elif 'duplicate entry' in messaggio_lower:
            return 'record_duplicato'

        return 'altro'

    def inserisci_costo_personale_sal7_sicuro(self, row, numero_sal, mesi_info,numero_riga):
        """Versione sicura dell'inserimento costo personale per SAL 7+ con gestione standardizzata"""
        # Estrai e valida i dati dalla riga (formato SAL 7+ standardizzato)
        nome = str(row.get('nome', '')).strip()
        cognome = str(row.get('cognome', '')).strip()
        sede = str(row.get('citta', '')).strip()
        qualifica = str(row.get('qualifica', '')).strip()
        costo_orario = self.converti_numero_sal7(row.get('costo_orario'))
        costo_orario_post = self.converti_numero_sal7(row.get('costo_orario_post'))
        data_passaggio = self.converti_data_sal7(row.get('data_passaggio'))
        nuova_qualifica = str(row.get('nuova_qualifica', '')).strip()

        # Usa il costo orario post-passaggio se disponibile (gestione SAL 7+)
        if data_passaggio and costo_orario_post:
            costo_orario_finale = costo_orario_post
            qualifica_finale = nuova_qualifica if nuova_qualifica else qualifica
        else:
            costo_orario_finale = costo_orario
            qualifica_finale = qualifica

        # Recupera gli ID necessari (con validazione robusta per SAL 7+)
        cod_dipendente = self.inserisci_o_recupera_dipendente_sicuro(nome, cognome)
        cod_qualifica = self.inserisci_o_recupera_qualifica_sicuro(qualifica_finale, costo_orario_finale)
        cod_sede = self.inserisci_o_recupera_sede_sicuro(sede)

        # Recupera ID SAL (deve essere >= 7)
        id_sal = self.sal_cache.get(numero_sal)
        if not id_sal:
            raise CostoPersonaleError(f"SAL {numero_sal} non trovato nel database")

        # Inserisci il costo medio unitario per questa qualifica e SAL
        if costo_orario_finale:
            self.inserisci_costo_medio_unitario_sal_sicuro(cod_qualifica, id_sal, costo_orario_finale)

        # CUP e categoria dalla riga (standardizzati per SAL 7+)
        codice_cup = str(row.get('cup', '')).strip() if pd.notna(row.get('cup')) else None
        categoria = "1a - Costo del personale piattaforma multicanale di erogazione servizi per la PA"  # Standardizzato SAL 7+

        # Inserisci CUP se necessario
        if codice_cup:
            self.inserisci_o_recupera_cup_sicuro(codice_cup, categoria)

        # Recupera ID categoria (standardizzata SAL 7+)
        id_categoria = self.categorie_cache.get(categoria)

        # Inserisci i costi per ogni mese del SAL 7+
        records_inseriti = 0

        try:
            # Itera sui mesi del SAL (sempre 4 mesi per SAL 7+)
            for i, mese in enumerate(mesi_info):
                col_ore = f'ore_mese_{i + 1}'
                col_costo = f'costo_mese_{i + 1}'

                # Leggi ore dalla riga
                ore = self.converti_numero_sal7(row.get(col_ore))

                # Se non ci sono ore, passa al mese successivo
                if not ore or ore <= 0:
                    continue

                # Leggi costo dalla riga o calcola dal costo orario
                costo_mese = self.converti_valuta_sal7(row.get(col_costo))
                if not costo_mese and costo_orario_finale:
                    costo_mese = ore * costo_orario_finale

                # Dati del mese
                anno = mese['anno']
                num_mese = mese['numero']
                nome_mese = mese['nome']

                # Verifica se esiste già un record per questo dipendente/mese
                query_check = """SELECT id_costo \
                                 FROM costo_personale
                                 WHERE cod_dipendente = %s \
                                   AND anno = %s \
                                   AND mese = %s \
                                   AND id_sal = %s"""
                self.cursor.execute(query_check, (cod_dipendente, anno, num_mese, id_sal))
                existing = self.cursor.fetchone()

                if existing:
                    # Verifica se i dati sono diversi
                    query_select = """SELECT ore_mese, costo_mese, cod_qualifica, cod_sede, id_sal
                                      FROM costo_personale \
                                      WHERE id_costo = %s"""
                    self.cursor.execute(query_select, (existing[0],))
                    old_data = self.cursor.fetchone()

                    # Se i dati sono identici (incluso SAL), salta l'aggiornamento
                    if (old_data and
                            abs(float(old_data[0]) - ore) < 0.01 and
                            abs(float(old_data[1]) - costo_mese) < 0.01 and
                            old_data[2] == cod_qualifica and
                            old_data[3] == cod_sede and
                            old_data[4] == id_sal):
                        logger.info(
                            f"Costo personale già presente nel DB per {nome} {cognome} - SAL {numero_sal} - {nome_mese}/{anno} (dati identici, skip)")
                        continue

                    # Aggiorna il record esistente
                    query_update = """UPDATE costo_personale
                                      SET ore_mese      = %s, \
                                          costo_mese    = %s,
                                          cod_qualifica = %s, \
                                          cod_sede      = %s,
                                          codice_cup    = %s, \
                                          id_categoria  = %s
                                      WHERE id_costo = %s"""
                    self.cursor.execute(query_update, (ore, costo_mese, cod_qualifica,
                                                       cod_sede, codice_cup, id_categoria,
                                                       existing[0]))
                    logger.debug(f"Aggiornato costo SAL 7+ per {nome} {cognome} - {nome_mese}/{anno}")
                else:
                    # Inserisci nuovo record
                    query_insert = """INSERT INTO costo_personale
                                      (cod_dipendente, cod_qualifica, cod_sede, id_sal,
                                       codice_cup, id_categoria, anno, mese, ore_mese, costo_mese,importo_ammesso,riga_excel)
                                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,%s,%s)"""

                    self.cursor.execute(query_insert, (cod_dipendente, cod_qualifica, cod_sede,
                                                       id_sal, codice_cup, id_categoria,
                                                       anno, num_mese, ore, costo_mese,costo_mese,numero_riga))
                    logger.debug(
                        f"Inserito costo SAL 7+ per {nome} {cognome} - {nome_mese}/{anno}: {ore} ore, €{costo_mese:.2f}")

                records_inseriti += 1

            return True

        except Error as e:
            raise CostoPersonaleError(f"Errore database nell'inserimento costi SAL 7+ per {nome} {cognome}: {e}")

    # ─── Utility e Supporto SAL 7+ ────────────────────────────────────────────

    def _normalize_column_name(self, name: str) -> str:
        """Normalizza il nome di una colonna per il confronto SAL 7+"""
        if not isinstance(name, str):
            return ''
        normalized = re.sub(r'[^\w\s]', '', name).strip().upper()
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized

    def _genera_column_mapping_sal7(self, numero_sal, colonne_excel):
        """Genera mapping dinamico delle colonne ore/costi per SAL 7+ specifico"""
        mesi_info = self.sal_manager.get_mesi_sal(numero_sal)
        mapping = {}

        logger.debug(f"Generazione mapping per SAL 7+ numero {numero_sal}")
        logger.debug(f"Colonne nel file Excel: {colonne_excel}")

        for i, mese in enumerate(mesi_info):
            # Variazioni possibili dei nomi dei mesi per SAL 7+
            variazioni_mese = [
                mese['nome_breve'].upper(),  # SETT, OTT, NOV, DIC
                mese['nome'].upper()[:3],  # SET, OTT, NOV, DIC
                mese['nome'].upper(),  # SETTEMBRE, OTTOBRE
            ]

            # Anno in formato breve
            anno_breve = str(mese['anno'])[2:]

            # Cerca colonne ore (formato SAL 7+: "ORE SETT 24")
            for col in colonne_excel:
                col_clean = str(col).strip()
                col_upper = col_clean.upper()

                for var_mese in variazioni_mese:
                    if f"ORE {var_mese}" in col_upper and anno_breve in col_upper:
                        mapping[col] = f"ore_mese_{i + 1}"
                        logger.debug(f"Mappata colonna ore SAL 7+: '{col}' -> ore_mese_{i + 1}")
                        break

            # Cerca colonne costi (formato SAL 7+: "COSTO SETT 24")
            for col in colonne_excel:
                col_clean = str(col).strip()
                col_upper = col_clean.upper()

                for var_mese in variazioni_mese:
                    if f"COSTO {var_mese}" in col_upper and anno_breve in col_upper:
                        mapping[col] = f"costo_mese_{i + 1}"
                        logger.debug(f"Mappata colonna costo SAL 7+: '{col}' -> costo_mese_{i + 1}")
                        break

        return mapping

    def _find_column_mapping_sal7(self, df_columns: List[str]) -> Dict[str, str]:
        """Trova la mappatura effettiva tra colonne del file e colonne attese per SAL 7+"""
        actual_mapping = {}

        # Mapping base per le colonne standard SAL 7+
        base_mapping = {
            'N°': 'cod_dipendente',
            'COGNOME': 'cognome',
            'NOME': 'nome',
            'SEDE': 'sede',
            'CITTA\'': 'citta',
            'CITTÀ': 'citta',
            'CITTA': 'citta',
            'QUALIFICA': 'qualifica',
            'COSTO MEDIO ORARIO': 'costo_orario',
            'DATA PASSAGGIO LIVELLO': 'data_passaggio',
            'NUOVA QUALIFICA': 'nuova_qualifica',
            'COSTO MEDIO ORARIO (post passaggio)': 'costo_orario_post',
            'SAL': 'sal',
            'Periodo riferimento': 'periodo_riferimento',
            'CUP': 'cup',
            'Categoria di spesa': 'categoria_spesa',
        }

        # Normalizza le chiavi della mappatura
        normalized_map = {self._normalize_column_name(k): v for k, v in base_mapping.items()}

        # Per ogni colonna nel DataFrame
        for col in df_columns:
            normalized_col = self._normalize_column_name(col)

            # Cerca corrispondenza esatta
            if normalized_col in normalized_map:
                actual_mapping[col] = normalized_map[normalized_col]
                logger.debug(f"Mappata colonna SAL 7+ '{col}' -> '{normalized_map[normalized_col]}'")
            # Cerca corrispondenza parziale
            else:
                for norm_key, db_col in normalized_map.items():
                    if norm_key in normalized_col or normalized_col in norm_key:
                        actual_mapping[col] = db_col
                        logger.debug(f"Mappata colonna SAL 7+ '{col}' -> '{db_col}' (corrispondenza parziale)")
                        break

        return actual_mapping

    def valida_file_excel_sal7(self, file_path: str) -> Tuple[bool, Optional[str]]:
        """Valida che il file Excel SAL 7+ sia corretto"""
        try:
            if not os.path.exists(file_path):
                return False, "Il file non esiste"

            valid_extensions = ['.xlsx', '.xls', '.xlsm', '.xlsb', '.csv']
            if not any(file_path.lower().endswith(ext) for ext in valid_extensions):
                return False, f"Formato file non supportato. Formati validi: {', '.join(valid_extensions)}"

            # Prova ad aprire il file
            if file_path.lower().endswith('.csv'):
                df = pd.read_csv(file_path, nrows=5, sep=';')
            else:
                df = pd.read_excel(file_path, nrows=5)

            if df.empty:
                return False, "Il file è vuoto"

            columns = list(df.columns)
            if len(columns) < 5:
                return False, "Il file ha troppo poche colonne"

            return True, None

        except Exception as e:
            return False, f"Errore nella validazione del file: {str(e)}"

    def get_statistiche_database_sal7(self) -> Dict[str, int]:
        """Ottiene le statistiche correnti del database per SAL 7+"""
        stats = {}
        try:
            # Conta dipendenti
            self.cursor.execute("SELECT COUNT(*) FROM dipendenti")
            stats['dipendenti'] = self.cursor.fetchone()[0]

            # Conta qualifiche
            self.cursor.execute("SELECT COUNT(*) FROM qualifica")
            stats['qualifiche'] = self.cursor.fetchone()[0]

            # Conta sedi
            self.cursor.execute("SELECT COUNT(*) FROM localizzazione_sedi")
            stats['sedi'] = self.cursor.fetchone()[0]

            # Conta CUP
            self.cursor.execute("SELECT COUNT(*) FROM codice_unico_progetto")
            stats['cup'] = self.cursor.fetchone()[0]

            # Conta costi personale per SAL 7+
            self.cursor.execute(
                "SELECT COUNT(*) FROM costo_personale cp JOIN sal s ON cp.id_sal = s.id_sal ")
            stats['costi_personale_sal7'] = self.cursor.fetchone()[0]

            # Totale ore SAL 7+
            self.cursor.execute(
                "SELECT COALESCE(SUM(ore_mese), 0) FROM costo_personale cp JOIN sal s ON cp.id_sal = s.id_sal ")
            stats['totale_ore_sal7'] = float(self.cursor.fetchone()[0])

            # Totale costi SAL 7+
            self.cursor.execute(
                "SELECT COALESCE(SUM(costo_mese), 0) FROM costo_personale cp JOIN sal s ON cp.id_sal = s.id_sal ")
            stats['totale_costi_sal7'] = float(self.cursor.fetchone()[0])

        except Exception as e:
            logger.error(f"Errore nel recupero statistiche SAL 7+: {e}")

        return stats

    # ─── Importazione Excel SICURA per SAL 7+ ─────────────────────────────────

    def importa_excel_sal7_sicuro(self, file_path):
        """Versione sicura dell'importazione Excel per SAL 7+ con gestione atomica completa"""
        try:
            logger.info(f"Inizio importazione SICURA file personale SAL 7+: {file_path}")
            self.import_status.start_time = datetime.now()
            self.import_status.update("reading", 10, "Validazione file Excel SAL 7+")

            # Estrai numero SAL dal nome del file (deve essere >= 7)
            numero_sal = self.estrai_numero_sal_da_file_sal7(file_path)

            if numero_sal < 7:
                raise ValueError(f"SAL {numero_sal} non supportato. Questo script supporta solo SAL >= 7")

            logger.info(f"Rilevato SAL numero: {numero_sal}")

            # Ottieni informazioni sul periodo usando SALManager SAL 7+
            data_inizio, data_fine = self.sal_manager.get_periodo_sal(numero_sal)
            mesi_info = self.sal_manager.get_mesi_sal(numero_sal)

            logger.info(f"SAL {numero_sal}: periodo {data_inizio} - {data_fine}")
            logger.info(f"Mesi: {[m['nome'] + ' ' + str(m['anno']) for m in mesi_info]}")

            # Crea sessione di importazione
            id_sal = self.sal_cache.get(numero_sal)
            if not id_sal:
                raise ValidazionePersonaleSAL7Error(f"SAL {numero_sal} non trovato nel database")

            if self.error_handler:
                session_id = self.error_handler.crea_sessione_import(
                    tipo_import='personale_sal7',
                    nome_file=os.path.basename(file_path),
                    id_sal=id_sal,
                    utente=None
                )

            # Ottieni statistiche PRE-importazione
            stats_pre = self.get_statistiche_database_sal7()
            logger.info("=" * 50)
            logger.info("STATISTICHE PRE-IMPORTAZIONE SAL 7+")
            logger.info(f"Dipendenti: {stats_pre.get('dipendenti', 0)}")
            logger.info(f"Qualifiche: {stats_pre.get('qualifiche', 0)}")
            logger.info(f"Sedi: {stats_pre.get('sedi', 0)}")
            logger.info(f"CUP: {stats_pre.get('cup', 0)}")
            logger.info(f"Costi personale SAL 7+: {stats_pre.get('costi_personale_sal7', 0)}")
            logger.info(f"Totale ore SAL 7+: {stats_pre.get('totale_ore_sal7', 0):.2f}")
            logger.info(f"Totale costi SAL 7+: €{stats_pre.get('totale_costi_sal7', 0):,.2f}")
            logger.info("=" * 50)

            # Validazione del file
            is_valid, error_msg = self.valida_file_excel_sal7(file_path)
            if not is_valid:
                raise ValueError(f"File SAL 7+ non valido: {error_msg}")

            self.import_status.update("reading", 20, "Lettura file Excel SAL 7+")

            # Leggi Excel - cerca la riga header corretta (con COGNOME/NOME)
            df = None
            if file_path.lower().endswith('.csv'):
                df = pd.read_csv(file_path, sep=';', header=0)
            else:
                # Colonne chiave che identificano l'header corretto
                colonne_chiave = {'COGNOME', 'NOME', 'SEDE', 'QUALIFICA'}
                best_df = None
                best_score = 0
                best_row = -1

                for header_row in range(10):
                    try:
                        df_test = pd.read_excel(file_path, header=header_row)
                        if len(df_test.columns) <= 5 or df_test.empty:
                            continue

                        # Conta quante colonne chiave sono presenti
                        nomi_upper = {str(c).strip().upper() for c in df_test.columns}
                        score = len(colonne_chiave & nomi_upper)

                        if score > best_score:
                            best_score = score
                            best_df = df_test
                            best_row = header_row

                        # Match perfetto: tutte le colonne chiave trovate
                        if score == len(colonne_chiave):
                            break
                    except Exception:
                        continue

                if best_df is not None:
                    df = best_df
                    logger.info(f"File SAL 7+ letto con successo usando header alla riga {best_row + 1} (score={best_score}/{len(colonne_chiave)})")

            if df is None or df.empty:
                raise ValueError("Impossibile leggere il file Excel SAL 7+")

            self.import_status.update("validating", 30, "Analisi struttura del file SAL 7+")
            logger.info(f"Struttura file SAL 7+: {len(df)} righe, {len(df.columns)} colonne")

            # Pulisci i nomi delle colonne
            df.columns = df.columns.str.strip()

            # Genera mapping colonne dinamico per questo SAL 7+
            column_mapping_dinamico = self._genera_column_mapping_sal7(numero_sal, list(df.columns))

            # Trova mappatura colonne (combina mapping base + dinamico)
            column_mapping = self._find_column_mapping_sal7(list(df.columns))
            column_mapping.update(column_mapping_dinamico)

            if not column_mapping:
                raise ValueError("Nessuna colonna riconosciuta nel file SAL 7+")

            logger.info(f"Colonne mappate SAL 7+: {len(column_mapping)}")
            logger.info(f"Colonne originali Excel: {list(df.columns)}")
            logger.info(f"Mappatura colonne SAL 7+: {column_mapping}")

            # Verifica colonne obbligatorie
            colonne_mappate_target = set(column_mapping.values())
            colonne_obbligatorie = {'cognome', 'nome'}
            mancanti = colonne_obbligatorie - colonne_mappate_target
            if mancanti:
                raise ValueError(
                    f"Colonne obbligatorie non trovate nel file: {mancanti}. "
                    f"Colonne Excel disponibili: {list(df.columns)}. "
                    f"Colonne mappate: {colonne_mappate_target}"
                )

            # Rinomina le colonne secondo la mappatura
            df_mapped = df.rename(columns=column_mapping)

            # Rimuovi colonne duplicate (il partial matching puo' mappare
            # piu' colonne Excel allo stesso target, es. 'costo_orario')
            df_mapped = df_mapped.loc[:, ~df_mapped.columns.duplicated(keep='first')]

            # Rimuovi righe vuote e righe di totale
            df_mapped = df_mapped.dropna(how='all')
            if 'cognome' in df_mapped.columns:
                df_mapped = df_mapped[~df_mapped['cognome'].astype(str).str.upper().isin(['TOTALE', 'TOTAL', 'TOTALI'])]

            # Imposta statistiche
            self.import_status.rows_total = len(df_mapped)
            self.import_status.update("importing", 40, f"Importazione SAL 7+ di {self.import_status.rows_total} righe")

            # Processa ogni riga con importazione atomica
            righe_processate = 0
            righe_con_errori = 0

            for index, row in df_mapped.iterrows():
                numero_riga_excel = index + 3  # Considera header

                try:
                    # Calcola progresso
                    progress = 40 + int((index / len(df_mapped)) * 50)
                    if index % 10 == 0:
                        self.import_status.update(
                            "importing",
                            progress,
                            f"Elaborazione SAL 7+ riga {index + 1}/{len(df_mapped)}"
                        )

                    # Salta righe senza cognome
                    if pd.isna(row.get('cognome')):
                        continue

                    # Prepara i dati della riga
                    row_data = dict(row)

                    # IMPORTAZIONE ATOMICA DELLA RIGA SAL 7+
                    if self.importa_riga_personale_sal7_atomica(row_data, numero_riga_excel, numero_sal, mesi_info):
                        righe_processate += 1
                        self.import_status.rows_imported += 1

                        # Commit ogni 50 righe
                        if righe_processate % 50 == 0:
                            self.connection.commit()
                            logger.info(f"Processate SAL 7+ {righe_processate}/{self.import_status.rows_total} righe")
                    else:
                        righe_con_errori += 1
                        self.import_status.rows_failed += 1

                except Exception as e:
                    # Questo catch non dovrebbe mai attivarsi se importa_riga_personale_sal7_atomica funziona bene
                    logger.error(f"ERRORE CRITICO non gestito nella riga SAL 7+ {numero_riga_excel}: {e}")
                    self.salva_errore_personale_sal7_sicuro(numero_riga_excel, {}, e, numero_sal)
                    righe_con_errori += 1
                    self.import_status.rows_failed += 1

            # Commit finale
            self.connection.commit()
            self.import_status.end_time = datetime.now()

            # Ottieni statistiche POST-importazione
            stats_post = self.get_statistiche_database_sal7()

            # Calcola differenze
            diff_stats = {
                'dipendenti': stats_post.get('dipendenti', 0) - stats_pre.get('dipendenti', 0),
                'qualifiche': stats_post.get('qualifiche', 0) - stats_pre.get('qualifiche', 0),
                'sedi': stats_post.get('sedi', 0) - stats_pre.get('sedi', 0),
                'cup': stats_post.get('cup', 0) - stats_pre.get('cup', 0),
                'costi_personale_sal7': stats_post.get('costi_personale_sal7', 0) - stats_pre.get(
                    'costi_personale_sal7', 0),
                'totale_ore_sal7': stats_post.get('totale_ore_sal7', 0) - stats_pre.get('totale_ore_sal7', 0),
                'totale_costi_sal7': stats_post.get('totale_costi_sal7', 0) - stats_pre.get('totale_costi_sal7', 0)
            }

            # Aggiorna sessione
            if self.error_handler:
                self.error_handler.aggiorna_sessione(
                    totale_righe=self.import_status.rows_total,
                    righe_importate=self.import_status.rows_imported,
                    righe_errore=self.import_status.rows_failed,
                    stato_import='completato'
                )

            # Report finale
            self.import_status.update("completed", 100, "Importazione SAL 7+ completata")

            summary = self.import_status.get_summary()
            logger.info("=" * 50)
            logger.info("IMPORTAZIONE SICURA PERSONALE SAL 7+ COMPLETATA")
            logger.info(f"SAL: {numero_sal}")
            logger.info(f"Periodo: {data_inizio} - {data_fine}")
            logger.info(f"Durata: {summary['duration']:.2f} secondi")
            logger.info(f"Totale righe nel file: {summary['rows_total']}")
            logger.info(f"Righe importate con successo: {summary['rows_imported']}")
            logger.info(f"Righe con errori: {summary['rows_failed']}")
            logger.info(f"Tasso di successo: {summary['success_rate']:.2f}%")
            logger.info("=" * 50)
            logger.info("STATISTICHE POST-IMPORTAZIONE SAL 7+")
            logger.info(f"Dipendenti: {stats_post.get('dipendenti', 0)} (+{diff_stats['dipendenti']})")
            logger.info(f"Qualifiche: {stats_post.get('qualifiche', 0)} (+{diff_stats['qualifiche']})")
            logger.info(f"Sedi: {stats_post.get('sedi', 0)} (+{diff_stats['sedi']})")
            logger.info(f"CUP: {stats_post.get('cup', 0)} (+{diff_stats['cup']})")
            logger.info(
                f"Costi personale SAL 7+: {stats_post.get('costi_personale_sal7', 0)} (+{diff_stats['costi_personale_sal7']})")
            logger.info(
                f"Totale ore SAL 7+: {stats_post.get('totale_ore_sal7', 0):.2f} (+{diff_stats['totale_ore_sal7']:.2f})")
            logger.info(
                f"Totale costi SAL 7+: €{stats_post.get('totale_costi_sal7', 0):,.2f} (+€{diff_stats['totale_costi_sal7']:,.2f})")
            logger.info("=" * 50)

            return True

        except Exception as e:
            logger.error(f"Errore generale nell'importazione sicura personale SAL 7+: {e}")
            self.connection.rollback()
            self.import_status.update("failed", 0, f"Errore: {str(e)}")

            # Marca la sessione come fallita
            if self.error_handler:
                self.error_handler.aggiorna_sessione(stato_import='fallito')

            return False

    def _romano_a_intero(self, s):
        """Converte numero romano in intero (supporta fino a XCIX)."""
        vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}
        result, prev = 0, 0
        for c in reversed(s.upper()):
            v = vals.get(c, 0)
            result += v if v >= prev else -v
            prev = v
        return result

    def estrai_numero_sal_da_file_sal7(self, file_path):
        """Estrae il numero SAL dal nome del file. Supporta cifre arabe (11) e numeri romani (XI)."""
        match = re.search(r'(\d+)\s*SAL', file_path, re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r'\b([IVXLCivxlc]+)\s+SAL\b', file_path, re.IGNORECASE)
        if match:
            numero = self._romano_a_intero(match.group(1))
            if numero > 0:
                return numero
        raise ValueError(f"Impossibile determinare il numero SAL dal file: {file_path}")


# ─── API Client per ricerca e download file ───────────────────────────────────
class PolisAPIClient:
    """Client per l'API di Polis per ricerca e download file"""

    def __init__(self, base_url="https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'accept': 'application/json',
            'User-Agent': 'ImportPersonaleSAL8/1.0'
        })

    def cerca_file_personale(self, cartella_sal: str, query: str = "prospetto costo del personale", limit: int = 100) -> Dict[str, Any]:
        """
        Cerca file del personale per un SAL specifico

        Args:
            cartella_sal: cartella sal
            query: Query di ricerca (default: "prospetto costo del personale")
            limit: Limite risultati (default: 100)

        Returns:
            Risposta dell'API con i risultati della ricerca
        """
        try:
            # Costruisci il path di ricerca basato sul SAL
            path = f"{cartella_sal}"  # Formato path per SAL 8

            # Parametri per la ricerca
            params = {
                'q': query,
                'path': path,
                'limit': limit
            }

            # Effettua la chiamata API
            url = urljoin(self.base_url, '/api/search')
            logger.info(f"Ricerca file API: {url} con parametri {params}")

            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            logger.info(f"API Search Response: {data.get('success', False)}, "
                       f"Risultati: {len(data.get('results', []))}, "
                       f"Tempo: {data.get('responseTime', 0):.2f}s")

            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Errore nella chiamata API di ricerca: {e}")
            raise Exception(f"Errore API ricerca: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"Errore nel parsing JSON della risposta API: {e}")
            raise Exception(f"Errore parsing risposta API: {e}")

    def seleziona_file_excel(self, risultati: List[Dict]) -> Optional[Dict]:
        """
        Seleziona il file Excel più appropriato dai risultati di ricerca

        Args:
            risultati: Lista dei file trovati dall'API

        Returns:
            Il file selezionato o None se nessun file appropriato
        """
        if not risultati:
            return None

        # Priorità: .xlsx > .xls > .csv
        priorita_estensioni = ['.xlsx', '.xls', '.xlsm', '.csv']

        # Filtra solo file Excel/CSV
        file_excel = []
        for file in risultati:
            nome = file.get('name', '').lower()
            if any(nome.endswith(ext) for ext in priorita_estensioni):
                file_excel.append(file)

        if not file_excel:
            logger.warning("Nessun file Excel/CSV trovato nei risultati")
            return None

        # Ordina per priorità estensione e poi per data di modifica
        def priorita_file(file):
            nome = file.get('name', '').lower()

            # Priorità estensione
            for i, ext in enumerate(priorita_estensioni):
                if nome.endswith(ext):
                    priorita_ext = i
                    break
            else:
                priorita_ext = 999

            # Data di modifica (più recente = priorità alta)
            try:
                data_mod = file.get('lastModified', '')
                timestamp = datetime.fromisoformat(data_mod.replace('Z', '+00:00')).timestamp()
                priorita_data = -timestamp  # Negativo per ordine decrescente
            except:
                priorita_data = 0

            return (priorita_ext, priorita_data)

        file_excel.sort(key=priorita_file)
        file_selezionato = file_excel[0]

        logger.info(f"File selezionato: {file_selezionato.get('name')} "
                   f"(dimensione: {file_selezionato.get('size', 0)} bytes, "
                   f"modificato: {file_selezionato.get('lastModified', 'N/A')})")

        return file_selezionato

    def scarica_file(self, file_path: str, destinazione: str = None) -> str:
        """
        Scarica un file dall'API

        Args:
            file_path: Path del file da scaricare (es. "8_SAL_30_04_2025/Prospetto costo del personale 8 SAL.xlsx")
            destinazione: Path locale dove salvare il file (opzionale)

        Returns:
            Path del file scaricato
        """
        try:
            # URL per il download
            base_url = "https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net"
            url = f"{base_url}/api/download/{urllib.parse.quote(file_path, safe='')}"

            logger.info(f"Download file: {url} con path {file_path}")

            # Effettua il download
            response = self.session.get(url, timeout=60, stream=True)
            response.raise_for_status()

            # Determina il nome del file locale
            if destinazione is None:
                nome_file = os.path.basename(file_path)
                # Crea directory temporanea se necessario
                temp_dir = tempfile.mkdtemp(prefix='import_personale_')
                destinazione = os.path.join(temp_dir, nome_file)

            # Scarica il file
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(destinazione, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)

                        # Log progresso ogni MB
                        if downloaded_size % (1024 * 1024) == 0:
                            if total_size > 0:
                                progress = (downloaded_size / total_size) * 100
                                logger.info(f"Download progresso: {progress:.1f}% ({downloaded_size}/{total_size} bytes)")

            logger.info(f"File scaricato con successo: {destinazione} ({downloaded_size} bytes)")
            return destinazione

        except requests.exceptions.RequestException as e:
            logger.error(f"Errore nel download del file: {e}")
            raise Exception(f"Errore download: {e}")
        except IOError as e:
            logger.error(f"Errore nel salvataggio del file: {e}")
            raise Exception(f"Errore salvataggio file: {e}")

    def cerca_e_scarica_file_personale(self, numero_sal: int,cartella_sal: str) -> str:
        """
        Cerca e scarica automaticamente il file del personale per un SAL

        Args:
            numero_sal: Numero del SAL

        Returns:
            Path del file scaricato
        """
        logger.info(f"Ricerca automatica file personale per SAL {numero_sal}")

        # Cerca file
        risultati_ricerca = self.cerca_file_personale(cartella_sal)

        if not risultati_ricerca.get('success', False):
            raise Exception(f"Ricerca API fallita per SAL {numero_sal}")

        risultati = risultati_ricerca.get('results', [])
        if not risultati:
            raise Exception(f"Nessun file trovato per SAL {numero_sal}")

        # Seleziona il file migliore
        file_selezionato = self.seleziona_file_excel(risultati)
        if not file_selezionato:
            raise Exception(f"Nessun file Excel appropriato trovato per SAL {numero_sal}")

        # Scarica il file
        file_path = file_selezionato.get('path')
        if not file_path:
            raise Exception("Path del file non trovato nella risposta API")

        file_locale = self.scarica_file(file_path)
        logger.info(f"File personale SAL {numero_sal} scaricato: {file_locale}")

        return file_locale


# ─── MAIN SICURO per SAL 7+ con API Integration ─────────────────────────────
def main():
    """Funzione principale per l'importazione sicura del personale SAL 7+ con supporto API"""
    import argparse

    # Parser per argomenti da linea di comando
    parser = argparse.ArgumentParser(description='Importa dati del personale SAL 7+ da file Excel o API (VERSIONE CON API)')
    parser.add_argument('file', nargs='?', help='Percorso del file Excel da importare (opzionale se si usa --api)')
    parser.add_argument('--sal', type=int, help='Numero SAL da importare (deve essere >= 7)')
    parser.add_argument('--api', action='store_true', help='Usa API per cercare e scaricare automaticamente il file')
    parser.add_argument('--api-url', default='https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net',
                        help='URL base dell\'API di storage')
    parser.add_argument('--host', default='localhost', help='Host del database MySQL')
    parser.add_argument('--port', type=int, default=3306, help='Porta del database MySQL')
    parser.add_argument('--user', default='root', help='Utente del database')
    parser.add_argument('--password', default=os.getenv('DB_PASSWORD', ''), help='Password del database')
    parser.add_argument('--database', default='polis', help='Nome del database')
    parser.add_argument('--dry-run', action='store_true', help='Esegue in modalità test senza modificare il database')
    # Add this argument to the parser in the main() function
    parser.add_argument('--cartella', type=str, help='Cartella del SAL per la ricerca API')

    args = parser.parse_args()

    # Configurazione database
    DB_CONFIG = {
        'host': args.host,
        'port': args.port,
        'user': args.user,
        'password': args.password,
        'database': args.database
    }

    # Determina il numero SAL
    numero_sal = args.sal if args.sal else 8  # Default SAL 8
    cartella_sal = args.cartella if args.sal else 8

    # Gestione file: API vs file locale
    EXCEL_FILE = None
    file_temporaneo = False

    if args.api:
        # Modalità API: cerca e scarica automaticamente il file
        logger.info("=" * 80)
        logger.info("MODALITÀ API: Ricerca automatica file tramite API")
        logger.info(f"API URL: {args.api_url}")
        logger.info(f"SAL richiesto: {numero_sal}")
        logger.info("=" * 80)

        try:
            # Crea client API
            api_client = PolisAPIClient(base_url=args.api_url)

            # Cerca e scarica il file
            EXCEL_FILE = api_client.cerca_e_scarica_file_personale(numero_sal,cartella_sal)
            file_temporaneo = True

            logger.info(f"✅ File scaricato tramite API: {EXCEL_FILE}")

        except Exception as e:
            logger.error(f"❌ Errore nella ricerca/download tramite API: {e}")
            return 1

    elif args.file:
        # Modalità file esplicito
        EXCEL_FILE = args.file
        if not os.path.exists(EXCEL_FILE):
            logger.error(f"Il file specificato {EXCEL_FILE} non esiste")
            return 1
    else:
        # Modalità ricerca locale (fallback)
        logger.info("Ricerca file locale...")

        if numero_sal:
            possible_files = [
                f'Prospetto costo del personale {numero_sal} SAL.xlsx',
                f'Prospetto costo del personale {numero_sal} SAL.csv',
                f'prospetto_personale_{numero_sal}_sal.xlsx',
                f'personale_{numero_sal}_sal.xlsx'
            ]
        else:
            # Cerca qualsiasi file del personale SAL 7+
            import glob
            pattern_files = glob.glob('*[Pp]rospetto*personale*SAL*.xlsx')
            pattern_files.extend(glob.glob('*[Pp]rospetto*personale*SAL*.csv'))
            pattern_files.extend(glob.glob('*personale*sal*.xlsx'))
            # Filtra solo SAL >= 7
            sal7_files = []
            for f in pattern_files:
                match = re.search(r'(\d+)\s*SAL', f, re.IGNORECASE)
                if match and int(match.group(1)) >= 7:
                    sal7_files.append(f)

            possible_files = sal7_files if sal7_files else [
                'Prospetto costo del personale 8 SAL.xlsx',
                'Prospetto costo del personale 8 SAL.csv',
                'prospetto_personale_8_sal.xlsx',
                'personale_8_sal.xlsx'
            ]

        EXCEL_FILE = None
        for filename in possible_files:
            if os.path.exists(filename):
                EXCEL_FILE = filename
                logger.info(f"Trovato file locale SAL 7+: {filename}")
                break

        if not EXCEL_FILE:
            logger.error(f"File Excel SAL 7+ non trovato localmente.")
            logger.error(f"File cercati: {possible_files}")
            logger.error("Opzioni:")
            logger.error("1. Specifica un file: python import_personale_sal8.py path/to/file.xlsx")
            logger.error("2. Usa l'API: python import_personale_sal8.py --api --sal 8")
            logger.error("3. Usa --sal NUMERO per specificare il SAL (>= 7)")
            return 1

    # Estrai numero SAL dal file se non specificato
    if not args.sal:
        try:
            numero_sal_da_file = None
            match = re.search(r'(\d+)\s*SAL', EXCEL_FILE, re.IGNORECASE)
            if match:
                numero_sal_da_file = int(match.group(1))
                if numero_sal_da_file >= 7:
                    numero_sal = numero_sal_da_file
                    logger.info(f"Numero SAL estratto dal file: {numero_sal}")
                else:
                    logger.error(f"SAL {numero_sal_da_file} non supportato. Questo script supporta solo SAL >= 7")
                    return 1
            else:
                logger.error(f"Impossibile determinare il numero SAL dal nome file: {EXCEL_FILE}")
                logger.error("Usa --sal NUMERO per specificare il SAL (>= 7)")
                return 1
        except Exception as e:
            logger.error(f"Errore nell'estrazione del numero SAL: {e}")
            return 1

    # Validazione SAL
    if numero_sal < 7:
        logger.error(f"SAL {numero_sal} non supportato. Questo script supporta solo SAL >= 7")
        return 1

    # Ottieni date SAL usando SALManager SAL 7+
    sal_manager = SALManagerSAL7()
    data_inizio, data_fine = sal_manager.get_periodo_sal(numero_sal)

    # Mostra informazioni di configurazione
    logger.info("=" * 80)
    logger.info("AVVIO IMPORTAZIONE SICURA PERSONALE SAL 7+ CON API")
    logger.info(f"File: {EXCEL_FILE}")
    logger.info(f"Fonte: {'API' if args.api else 'Locale'}")
    logger.info(f"SAL: {numero_sal} (periodo: {data_inizio} - {data_fine})")
    logger.info(f"Database: {DB_CONFIG['database']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}")
    logger.info(f"Modalità: {'TEST (dry-run)' if args.dry_run else 'PRODUZIONE'}")
    logger.info("Modalità: SICURA (rollback automatico per righe con errori)")
    logger.info("Supporta: Solo SAL >= 7 con formato standardizzato")
    logger.info("=" * 80)

    # Crea importatore sicuro SAL 7+
    importatore = ImportatorePersonaleSAL7(**DB_CONFIG)

    try:
        # Connetti al database
        if not importatore.connetti_db():
            logger.error("Impossibile connettersi al database")
            return 1

        # Se in modalità dry-run, avvia una transazione che verrà rollback
        if args.dry_run:
            logger.info("Modalità DRY-RUN attiva - le modifiche NON saranno salvate")
            importatore.cursor.execute("START TRANSACTION")

        # Carica cache
        importatore.carica_cache()

        # Verifica esistenza SAL
        if numero_sal not in importatore.sal_cache:
            logger.info(f"Inserimento SAL {numero_sal} nel database")
            query = """INSERT INTO sal (numero_sal, data_inizio, data_fine)
                       VALUES (%s, %s, %s)"""
            importatore.cursor.execute(query, (numero_sal, data_inizio, data_fine))
            if not args.dry_run:
                importatore.connection.commit()
            importatore.carica_cache()

        # Verifica e inserisci categoria se necessaria (standardizzata per SAL 7+)
        categoria_personale = "1a - Costo del personale piattaforma multicanale di erogazione servizi per la PA"
        if categoria_personale not in importatore.categorie_cache:
            logger.info("Inserimento categoria personale SAL 7+ nel database")
            importatore.cursor.execute("SELECT MAX(id_categoria) FROM categoria_spesa")
            max_id = importatore.cursor.fetchone()[0]
            nuovo_id = (max_id + 1) if max_id else 1

            query = """INSERT INTO categoria_spesa (id_categoria, descrizione)
                       VALUES (%s, %s)"""
            importatore.cursor.execute(query, (nuovo_id, categoria_personale))
            if not args.dry_run:
                importatore.connection.commit()
            importatore.carica_cache()

        # IMPORTAZIONE SICURA SAL 7+
        success = importatore.importa_excel_sal7_sicuro(EXCEL_FILE)

        # Se in modalità dry-run, fai rollback
        if args.dry_run:
            logger.info("ROLLBACK delle modifiche (modalità dry-run)")
            importatore.connection.rollback()

        if success:
            logger.info("✅ IMPORTAZIONE SICURA PERSONALE SAL 7+ COMPLETATA CON SUCCESSO")
        else:
            logger.error("❌ IMPORTAZIONE SICURA PERSONALE SAL 7+ FALLITA")

        return 0 if success else 1

    except Exception as e:
        logger.error(f"Errore generale: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 1
    finally:
        # Cleanup file temporaneo se scaricato tramite API
        if file_temporaneo and EXCEL_FILE and os.path.exists(EXCEL_FILE):
            try:
                # Rimuovi il file temporaneo e la sua directory
                temp_dir = os.path.dirname(EXCEL_FILE)
                os.remove(EXCEL_FILE)
                if os.path.basename(temp_dir).startswith('import_personale_'):
                    os.rmdir(temp_dir)
                logger.info(f"File temporaneo rimosso: {EXCEL_FILE}")
            except Exception as cleanup_error:
                logger.warning(f"Errore nella rimozione del file temporaneo: {cleanup_error}")

        importatore.chiudi_connessione()


if __name__ == "__main__":
    sys.exit(main())
