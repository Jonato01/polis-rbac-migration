import pandas as pd
import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime
import os
import sys
import re
import argparse
import requests
import urllib.parse
from import_error_handler import ImportErrorHandler


# ─── Eccezioni Custom ──────────────────────────────────────────────────────────
class ImportFattureError(Exception):
    """Eccezione base per errori di importazione fatture"""
    pass


class FornitoreError(ImportFattureError):
    """Errore relativo ai fornitori"""
    pass


class AccordoQuadroError(ImportFattureError):
    """Errore relativo agli accordi quadro"""
    pass


class BuonoConsegnaError(ImportFattureError):
    """Errore relativo ai buoni di consegna"""
    pass


class DocumentoEMError(ImportFattureError):
    """Errore relativo ai documenti EM"""
    pass


class ValidazioneRigaError(ImportFattureError):
    """Errore di validazione della riga"""
    pass


# ─── Configurazione logging ───────────────────────────────────────────────────
def setup_logging():
    """Configura il sistema di logging su file e console"""
    if not os.path.exists('logs'):
        os.makedirs('logs')
    log_filename = f'logs/import_fatture_sal7_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = logging.getLogger('ImportFattureSAL7')
    logger.setLevel(logging.DEBUG)
    # File handler
    fh = logging.FileHandler(log_filename, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    # Formatter
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


logger = setup_logging()


# ─── Classe ImportatoreFattureSAL7 ─────────────────────────────────────────────
class ImportatoreFattureSAL7:
    def __init__(self, host, port, user, password, database):
        """Inizializza la connessione al database e le cache"""
        self.connection = None
        self.cursor = None
        self.db_config = {
            'host': host,
            'port': port,
            'user': user,
            'password': password,
            'database': database
        }
        # Cache per lookup rapido
        self.fornitori_cache = {}
        self.accordi_cache = {}
        self.categorie_cache = {}
        self.sal_cache = {}
        self.classe_cache = {}
        self.documenti_cache = {}  # <numero_documento, id_em>
        self.numero_sal = None  # Sarà sempre >= 7

        # Error handler
        self.error_handler = None

    def connetti_db(self):
        """Stabilisce la connessione al database (cursor buffered)."""
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
        """Chiude cursore e connessione."""
        try:
            if self.cursor:
                self.cursor.close()
            if self.connection and self.connection.is_connected():
                self.connection.close()
                logger.info("Connessione al database chiusa")
        except Error as e:
            logger.error(f"Errore chiudendo la connessione: {e}")

    def carica_cache(self):
        """Carica in memoria le principali tabelle di lookup."""
        try:
            # fornitori
            self.cursor.execute("SELECT id_fornitore, nome FROM fornitori")
            for id_f, nome in self.cursor.fetchall():
                self.fornitori_cache[nome] = id_f

            # accordi_quadro
            self.cursor.execute("SELECT id_accordo, codice FROM accordi_quadro")
            for id_a, codice in self.cursor.fetchall():
                self.accordi_cache[codice] = id_a

            # categoria_spesa
            self.cursor.execute("SELECT id_categoria, descrizione FROM categoria_spesa")
            for id_c, desc in self.cursor.fetchall():
                self.categorie_cache[desc] = id_c

            # sal (solo SAL >= 7)
            self.cursor.execute("SELECT id_sal, numero_sal FROM sal WHERE numero_sal >= 7")
            for id_s, num in self.cursor.fetchall():
                self.sal_cache[num] = id_s

            # classe_cespiti
            self.cursor.execute("SELECT id_classe_cespiti, descrizione FROM classe_cespiti")
            for id_cls, desc in self.cursor.fetchall():
                self.classe_cache[desc] = id_cls

            # documenti_em (cache su numero_documento)
            self.cursor.execute("SELECT id_em, numero_documento FROM documenti_em")
            for id_em, num_doc in self.cursor.fetchall():
                self.documenti_cache[num_doc] = id_em

            logger.info("Cache delle tabelle caricata con successo")
        except Error as e:
            logger.warning(f"[Cache] Tabella mancante o errore: {e}")
            logger.info("Cache delle tabelle caricata (con eventuali warning se mancano tabelle)")

    def converti_percentuale(self, valore):
        """Converte una percentuale (formato italiano o con %) in float."""
        if pd.isna(valore) or valore == '':
            return None
        if isinstance(valore, (int, float)):
            return float(valore)
        s = str(valore).strip().replace(',', '.').replace('%', '')
        try:
            return float(s)
        except:
            logger.warning(f"Impossibile convertire la percentuale: {valore}")
            return None

    def converti_data(self, data_str):
        """Converte una stringa o datetime in oggetto date; altrimenti None."""
        if pd.isna(data_str) or not data_str:
            return None
        if isinstance(data_str, datetime):
            return data_str.date()
        # Se ci sono più date separate da \n, prendi solo la prima
        if isinstance(data_str, str) and '\n' in data_str:
            data_str = data_str.split('\n')[0].strip()
        try:
            return pd.to_datetime(data_str).date()
        except Exception:
            logger.warning(f"Impossibile convertire la data: {data_str}")
            return None

    def converti_numero(self, valore):
        if pd.isna(valore) or valore == '':
            return None
        try:
            return float(valore)
        except:
            logger.warning(f"Impossibile convertire il numero: {valore}")
            return None

    def normalizza_nome_colonna(self, nome):
        """Rimuove newline/spazi multipli, poi minuscolo."""
        if not isinstance(nome, str):
            return ''
        n = re.sub(r'[\r\n\t\u200B]+', ' ', nome)
        n = re.sub(r'\s{2,}', ' ', n)
        return n.strip().lower()

    # ── Metodi "inserisci o recupera" SICURI ────────────────────────────────────────

    def inserisci_o_recupera_fornitore(self, nome_fornitore):
        """Inserisce un nuovo fornitore o restituisce l'ID esistente. SOLLEVA ECCEZIONE se fallisce."""
        if not nome_fornitore or str(nome_fornitore).strip() == '':
            raise FornitoreError("Nome fornitore vuoto o None")

        nome_fornitore = str(nome_fornitore).strip()

        if nome_fornitore in self.fornitori_cache:
            return self.fornitori_cache[nome_fornitore]

        try:
            q = "INSERT INTO fornitori (nome) VALUES (%s)"
            self.cursor.execute(q, (nome_fornitore,))
            id_f = self.cursor.lastrowid
            self.fornitori_cache[nome_fornitore] = id_f
            logger.debug(f"Inserito nuovo fornitore: {nome_fornitore} (ID: {id_f})")
            return id_f
        except mysql.connector.IntegrityError:
            # Prova a recuperare se esiste
            self.cursor.execute("SELECT id_fornitore FROM fornitori WHERE nome = %s", (nome_fornitore,))
            r = self.cursor.fetchone()
            if r:
                id_f = r[0]
                self.fornitori_cache[nome_fornitore] = id_f
                return id_f
            raise FornitoreError(f"Fornitore '{nome_fornitore}' non inseribile e non trovato")
        except Error as e:
            raise FornitoreError(f"Errore database nell'inserimento fornitore '{nome_fornitore}': {e}")

    def inserisci_o_recupera_accordo(self, codice_accordo):
        """Inserisce un nuovo accordo_quadro o restituisce l'ID esistente. SOLLEVA ECCEZIONE se fallisce."""
        if not codice_accordo:
            return None  # Accordo opzionale

        # Pulisci il codice accordo
        codice_accordo = str(codice_accordo).strip()
        if codice_accordo.lower() == 'nan' or not codice_accordo:
            return None

        if codice_accordo in self.accordi_cache:
            return self.accordi_cache[codice_accordo]

        try:
            # Prima prova a vedere se esiste
            self.cursor.execute("SELECT id_accordo FROM accordi_quadro WHERE codice = %s", (codice_accordo,))
            r = self.cursor.fetchone()
            if r:
                id_a = r[0]
                self.accordi_cache[codice_accordo] = id_a
                return id_a

            # Se non esiste, inseriscilo
            q = "INSERT INTO accordi_quadro (codice) VALUES (%s)"
            self.cursor.execute(q, (codice_accordo,))
            id_a = self.cursor.lastrowid
            self.accordi_cache[codice_accordo] = id_a
            logger.debug(f"Inserito nuovo accordo quadro: {codice_accordo} (ID: {id_a})")
            return id_a

        except Error as e:
            raise AccordoQuadroError(f"Errore database nell'inserimento accordo '{codice_accordo}': {e}")

    def inserisci_o_recupera_categoria(self, descrizione_categoria):
        """Inserisce una nuova categoria_spesa o restituisce l'ID esistente. SOLLEVA ECCEZIONE se fallisce."""
        if not descrizione_categoria:
            raise ValidazioneRigaError("Categoria di spesa obbligatoria ma mancante")

        descrizione_categoria = str(descrizione_categoria).strip()

        if descrizione_categoria in self.categorie_cache:
            return self.categorie_cache[descrizione_categoria]

        try:
            q = "INSERT INTO categoria_spesa (descrizione) VALUES (%s)"
            self.cursor.execute(q, (descrizione_categoria,))
            id_c = self.cursor.lastrowid
            self.categorie_cache[descrizione_categoria] = id_c
            logger.debug(f"Inserita nuova categoria: {descrizione_categoria} (ID: {id_c})")
            return id_c
        except mysql.connector.IntegrityError:
            self.cursor.execute("SELECT id_categoria FROM categoria_spesa WHERE descrizione = %s",
                                (descrizione_categoria,))
            r = self.cursor.fetchone()
            if r:
                id_c = r[0]
                self.categorie_cache[descrizione_categoria] = id_c
                return id_c
            raise ValidazioneRigaError(f"Categoria '{descrizione_categoria}' non inseribile e non trovata")
        except Error as e:
            raise ValidazioneRigaError(f"Errore database nell'inserimento categoria '{descrizione_categoria}': {e}")

    def inserisci_o_recupera_bdc(self, numero_bdc, posizione, id_accordo):
        """Inserisce un nuovo buono_consegna o restituisce l'ID esistente. SOLLEVA ECCEZIONE se fallisce."""
        import math

        # Normalize numero_bdc
        if numero_bdc is None or (isinstance(numero_bdc, float) and math.isnan(numero_bdc)) or str(numero_bdc).lower() == 'nan' or str(numero_bdc).strip() == '':
            return None  # BdC opzionale

        # Normalize posizione
        if posizione is None or (isinstance(posizione, float) and math.isnan(posizione)) or str(posizione).lower() == 'nan' or str(posizione).strip() == '':
            posizione = None

        if not id_accordo:
            raise BuonoConsegnaError(f"Impossibile inserire BdC '{numero_bdc}': accordo quadro mancante")

        try:
            s = """SELECT id_bdc
                   FROM buoni_consegna
                   WHERE numero_bdc = %s
                     AND posizione_riferimento {}""".format("IS NULL" if posizione is None else "= %s")
            params = (numero_bdc,) if posizione is None else (numero_bdc, posizione)
            self.cursor.execute(s, params)
            r = self.cursor.fetchone()
            if r:
                return r[0]

            i = """INSERT INTO buoni_consegna
                       (numero_bdc, posizione_riferimento, id_accordo)
                   VALUES (%s, %s, %s)"""
            self.cursor.execute(i, (numero_bdc, posizione, id_accordo))
            id_bdc = self.cursor.lastrowid
            logger.debug(f"Inserito nuovo BdC: {numero_bdc} pos {posizione} (ID: {id_bdc})")
            return id_bdc
        except ValueError as e:
            raise BuonoConsegnaError(f"Posizione BdC non valida per '{numero_bdc}': {e}")
        except Error as e:
            raise BuonoConsegnaError(f"Errore database nell'inserimento BdC '{numero_bdc}': {e}")
    def inserisci_o_recupera_classe(self, descrizione_classe):
        """Inserisce una nuova classe_cespiti o restituisce l'ID esistente. SOLLEVA ECCEZIONE se fallisce."""
        if not descrizione_classe:
            return None  # Classe opzionale

        descrizione_classe = str(descrizione_classe).strip()

        if descrizione_classe in self.classe_cache:
            return self.classe_cache[descrizione_classe]

        try:
            q = "INSERT INTO classe_cespiti (descrizione) VALUES (%s)"
            self.cursor.execute(q, (descrizione_classe,))
            id_cl = self.cursor.lastrowid
            self.classe_cache[descrizione_classe] = id_cl
            logger.debug(f"Inserita nuova classe cespiti: {descrizione_classe} (ID: {id_cl})")
            return id_cl
        except mysql.connector.IntegrityError:
            self.cursor.execute("SELECT id_classe_cespiti FROM classe_cespiti WHERE descrizione = %s",
                                (descrizione_classe,))
            r = self.cursor.fetchone()
            if r:
                id_cl = r[0]
                self.classe_cache[descrizione_classe] = id_cl
                return id_cl
            raise ValidazioneRigaError(f"Classe cespiti '{descrizione_classe}' non inseribile e non trovata")
        except Error as e:
            raise ValidazioneRigaError(f"Errore database nell'inserimento classe cespiti '{descrizione_classe}': {e}")

    def inserisci_o_recupera_documento_em(self, numero_doc, data_reg):
        """Inserisce un nuovo record in documenti_em o restituisce l'ID esistente. SOLLEVA ECCEZIONE se fallisce."""
        # Gestisci il caso in cui numero_doc sia nan o stringa 'nan'
        if not numero_doc or pd.isna(numero_doc) or str(numero_doc).lower() == 'nan':
            return None  # Documento EM opzionale

        # Converti in stringa e pulisci
        numero_doc = str(numero_doc).strip()
        if not numero_doc or numero_doc.lower() == 'nan':
            return None

        # se esiste in cache, restituisco l'id
        if numero_doc in self.documenti_cache:
            return self.documenti_cache[numero_doc]

        try:
            # cerco se esiste già in tabella
            s = "SELECT id_em FROM documenti_em WHERE numero_documento = %s"
            self.cursor.execute(s, (numero_doc,))
            r = self.cursor.fetchone()
            if r:
                id_em = r[0]
                self.documenti_cache[numero_doc] = id_em
                return id_em

            # altrimenti inserisco
            i = "INSERT INTO documenti_em (numero_documento, data_registrazione) VALUES (%s, %s)"
            self.cursor.execute(i, (numero_doc, data_reg))
            id_em = self.cursor.lastrowid
            self.documenti_cache[numero_doc] = id_em
            logger.debug(f"Inserito documento EM: {numero_doc} con ID: {id_em}")
            return id_em
        except Error as e:
            raise DocumentoEMError(f"Errore database nell'inserimento documento EM '{numero_doc}': {e}")

    # ── Validazione Pre-Inserimento ──────────────────────────────────────────────

    def valida_riga_completa(self, row_data):
        """Valida tutti i dati di una riga PRIMA di iniziare gli inserimenti."""
        errori = []

        # Validazioni obbligatorie
        if not row_data.get('Numero fattura del fornitore'):
            errori.append("Numero fattura mancante")

        if not row_data.get('Descrizione Fornitore'):
            errori.append("Descrizione fornitore mancante")

        if not row_data.get('Categoria di spesa'):
            errori.append("Categoria di spesa mancante")

        # Validazione SAL (deve essere >= 7)
        if not self.numero_sal or self.numero_sal < 7:
            errori.append(f"SAL {self.numero_sal} non valido (deve essere >= 7)")

        if self.numero_sal not in self.sal_cache:
            errori.append(f"SAL {self.numero_sal} non trovato nel database")

        # Validazioni numeriche
        try:
            numero_fattura = str(row_data.get('Numero fattura del fornitore', '')).strip()
            if len(numero_fattura) > 100:  # Lunghezza massima colonna numero_fattura
                errori.append(f"Numero fattura troppo lungo: {len(numero_fattura)} caratteri (max 100)")
        except:
            errori.append("Numero fattura non convertibile in stringa")


        # Validazione date
        date_fields = ['Data emissione fattura del fornitore', 'Data registrazione EM', 'Data regolamento',
                       'Data pareggio']
        for field in date_fields:
            if row_data.get(field) and pd.notna(row_data.get(field)):
                try:
                    self.converti_data(row_data.get(field))
                except:
                    errori.append(f"Data non valida in campo '{field}': {row_data.get(field)}")

        # Validazione importi
        importi_fields = ['Imponibile fattura', 'IVA', 'Importo totale fattura', 'Entrata Merce netto IVA',
                          'IVA indetraibile', 'Entrata Merce Lordo IVA indetraibile', 'Importo pagato',
                          'Importo rendicontato']
        for field in importi_fields:
            if row_data.get(field) and pd.notna(row_data.get(field)):
                try:
                    self.converti_numero(row_data.get(field))
                except:
                    errori.append(f"Importo non valido in campo '{field}': {row_data.get(field)}")

        if errori:
            raise ValidazioneRigaError(f"Errori di validazione: {'; '.join(errori)}")

    # ── Importazione Atomica per Riga ──────────────────────────────────────────────

    def importa_riga_atomica(self, row_data, numero_riga, linea_intervento, nome_foglio):
        """Importa una singola riga in modo completamente atomico"""
        savepoint_name = f"riga_{numero_riga}_{nome_foglio.replace(' ', '_')}"

        try:
            # 1. Inizia savepoint per questa riga
            self.cursor.execute(f"SAVEPOINT {savepoint_name}")

            # 2. Validazione completa PRIMA degli inserimenti
            self.valida_riga_completa(row_data)

            # 3. Inserimenti in sequenza (ora sicuri grazie alla validazione)
            id_fattura = self.inserisci_fattura_sicura(row_data, linea_intervento)
            self.inserisci_dettaglio_fattura_sicuro(row_data, id_fattura,numero_riga)
            self.inserisci_pagamento_sicuro(row_data, id_fattura,numero_riga)

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

            # 6. Salva errore in transazione separata (critico!)
            self.salva_errore_riga_sicuro(numero_riga, nome_foglio, row_data, e)

            # 7. La riga viene scartata
            logger.warning(f"Riga {numero_riga} scartata: {str(e)[:200]}")
            return False

    def salva_errore_riga_sicuro(self, numero_riga, nome_foglio, row_data, errore):
        """Salva errore in una transazione separata per evitare rollback"""
        if not self.error_handler:
            return

        try:
            # Usa una connessione separata per salvare l'errore
            error_connection = mysql.connector.connect(**self.db_config)
            error_cursor = error_connection.cursor()

            # Crea un error handler temporaneo con la connessione separata
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

            # Classifica il tipo di errore
            tipo_errore = temp_error_handler.classifica_errore_fatture(str(errore))

            # Salva l'errore
            temp_error_handler.salva_errore(
                numero_riga=numero_riga,
                nome_foglio=nome_foglio,
                tipo_errore=tipo_errore,
                messaggio_errore=str(errore)[:1000],  # Limita lunghezza messaggio
                dati_riga=dati_riga,
                tipo_import='fatture'
            )

            error_connection.commit()
            error_connection.close()

        except Exception as save_error:
            logger.error(f"CRITICO: Impossibile salvare errore per riga {numero_riga}: {save_error}")

    def inserisci_fattura_sicura(self, row, linea_intervento):
        """Versione sicura di inserisci_fattura che solleva eccezioni invece di ritornare None"""
        # 1) fornitore (obbligatorio)
        id_forn = self.inserisci_o_recupera_fornitore(row.get('Descrizione Fornitore'))
        num_registrazione = row.get('Numero registrazione fattura')
        if num_registrazione and isinstance(num_registrazione, str):
            num_registrazione = re.sub(r"\s*\(.*?\)", "", num_registrazione).strip()
        numero_fattura = row.get('Numero fattura del fornitore')
        data_em = self.converti_data(row.get('Data emissione fattura del fornitore'))
        numero_vap = row.get('Numero VAP')
        if numero_vap and isinstance(numero_vap, str):
            numero_vap = re.sub(r"\s*\(.*?\)", "", numero_vap).strip()
        if numero_vap is not None and (
                pd.isna(numero_vap) or str(numero_vap).lower() == 'nan' or str(numero_vap).strip() == ''):
            numero_vap = None
        # Protocolli
        protocollo_fattura = row.get('N° Protocollo Fattura')
        protocollo_integrativo = row.get('N° Protocollo documento integrativo fatture reverse charge')

        # Pulisci i protocolli da spazi e valori nulli
        if protocollo_fattura and pd.notna(protocollo_fattura):
            protocollo_fattura = str(protocollo_fattura).strip()
            if protocollo_fattura.lower() == 'nan' or protocollo_fattura == '':
                protocollo_fattura = None
        else:
            protocollo_fattura = None

        # Gestire il documento EM integrativo se presente
        id_em_integrativo = None
        if protocollo_integrativo and pd.notna(protocollo_integrativo):
            protocollo_integrativo = str(protocollo_integrativo).strip()
            if protocollo_integrativo.lower() != 'nan' and protocollo_integrativo != '':
                id_em_integrativo = self.inserisci_o_recupera_documento_em(protocollo_integrativo, data_em)

        try:
            # 2) verifico se esiste già
            q = """SELECT id_fattura, \
                          imponibile, \
                          iva, \
                          totale, \
                          aliquota_iva, \
                          protocollo_fattura, \
                          id_em_integrativo, \
                          linea_intervento,
                          numero_registrazione
                   FROM fatture
                   WHERE numero_registrazione <=> %s
                     AND numero_fattura <=> %s
                     AND codice_fornitore = %s
                     AND data_emissione <=> %s
                     AND linea_intervento <=> %s
                     AND id_sal = %s"""
            id_sal = self.sal_cache.get(self.numero_sal)
            self.cursor.execute(q, (num_registrazione, numero_fattura, id_forn, data_em, linea_intervento, id_sal))
            r = self.cursor.fetchone()

            if r:
                return r[0]
            else:
                # 3) inserisco nuova fattura
                ins = """INSERT INTO fatture
                         (numero_fattura, data_emissione, codice_fornitore,
                          protocollo_fattura, id_em_integrativo, linea_intervento, numero_registrazione, numero_vap, id_sal)
                         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                self.cursor.execute(ins, (numero_fattura, data_em, id_forn,
                                          protocollo_fattura, id_em_integrativo, linea_intervento, num_registrazione, numero_vap, id_sal))
                id_new = self.cursor.lastrowid
                logger.debug(f"Inserita fattura {numero_fattura} (ID: {id_new})")
                return id_new

        except Error as e:
            raise ImportFattureError(f"Errore database nell'inserimento fattura '{numero_fattura}': {e}")

    def inserisci_dettaglio_fattura_sicuro(self, row, id_fattura,numero_riga):
        """Versione sicura di inserisci_dettaglio_fattura che solleva eccezioni"""
        # 1) categoria (obbligatoria)
        id_cat = self.inserisci_o_recupera_categoria(row.get('Categoria di spesa'))

        # 2) SAL (obbligatorio)
        id_sal = self.sal_cache.get(self.numero_sal)
        if not id_sal:
            raise ValidazioneRigaError(f"SAL {self.numero_sal} non trovato nel database")

        # 3) accordo + buono di consegna (opzionali ma interconnessi)
        id_acc = self.inserisci_o_recupera_accordo(row.get('Accordo Quadro'))
        id_bdc = None
        if id_acc:
            pos = row.get('Posizione riferimento (BdC)')
            id_bdc = self.inserisci_o_recupera_bdc(row.get('Buono di consegna (BdC)'), pos, id_acc)

        # 4) classe_cespiti (opzionale)
        classe_desc = row.get('classe cespiti')
        id_cl = None
        if classe_desc and pd.notna(classe_desc):
            id_cl = self.inserisci_o_recupera_classe(classe_desc)

        # 5) documento EM (opzionale)
        numero_em = row.get('Numero documento EM')
        if numero_em is not None and not pd.isna(numero_em):
            numero_em = str(numero_em).strip()
            if numero_em.lower() == 'nan' or numero_em == '':
                numero_em = None
        else:
            numero_em = None

        data_reg = self.converti_data(row.get('Data registrazione EM'))
        id_em = self.inserisci_o_recupera_documento_em(numero_em, data_reg)

        # 6) valori numerici
        imponibile_nf = self.converti_numero(row.get('Entrata Merce netto IVA'))
        iva_ind = self.converti_numero(row.get('IVA indetraibile'))
        totale_nf = self.converti_numero(row.get('Entrata Merce Lordo IVA indetraibile'))
        imponibile = self.converti_numero(row.get('Imponibile fattura'))
        iva = self.converti_numero(row.get('IVA'))
        # Recupera la percentuale di indetraibilità dal nuovo formato SAL 7+
        perc_ind = self.converti_percentuale(row.get("% indetraibilità dell'IVA (C=A+B)"))

        # 7) COLONNE DSAN (specifiche per SAL 7+)
        attivita_finanziaria = self.converti_percentuale(row.get("ATTIVITA' FINANZIARIA (indetraibile) (A)"))
        attivita_universale = self.converti_percentuale(row.get("ATTIVITA'  UNIVERSALE (indetraibile) (B)"))
        attivita_residuale = self.converti_percentuale(row.get("ATTIVITA' RESIDUALE (detraibile)"))
        aliquota_iva_calc = self.converti_percentuale(row.get("Aliquota IVA indetraibile (C*22%)"))



        try:
            # 9) CERCO se esiste già un record analogo
            conditions = [
                "id_fattura = %s",
                "id_categoria = %s",
                "id_sal = %s",
                "id_bdc = %s"
            ]
            params = [id_fattura, id_cat, id_sal, id_bdc]

            if id_cl is not None:
                conditions.append("id_classe_cespiti = %s")
                params.append(id_cl)
            else:
                conditions.append("id_classe_cespiti IS NULL")

            if id_em is not None:
                conditions.append("id_em = %s")
                params.append(id_em)
            else:
                conditions.append("id_em IS NULL")

            if perc_ind is not None:
                conditions.append("perc_indetraibilita = %s")
                params.append(perc_ind)
            else:
                conditions.append("perc_indetraibilita IS NULL")

            # 10) Se non esiste, inserisco nuovo dettaglio
            ins = """
                  INSERT INTO dettagli_fattura
                  (id_fattura, id_categoria, id_sal, id_bdc, entrata_merce_netto_iva, iva_indetraibile, totale,
                   perc_indetraibilita, id_classe_cespiti, id_em,
                   attivita_finanziaria_indetraibile, attivita_universale_indetraibile,
                   attivita_residuale_detraibile, aliquota_iva_indetraibile_calc,dettagli_fattura.imponibile,dettagli_fattura.iva,riga_excel)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,%s,%s,%s) \
                  """
            self.cursor.execute(ins, (
                id_fattura, id_cat, id_sal, id_bdc, imponibile_nf, iva_ind, totale_nf,
                perc_ind, id_cl, id_em,
                attivita_finanziaria, attivita_universale, attivita_residuale, aliquota_iva_calc,imponibile,iva,numero_riga
            ))
            logger.debug(f"Inserito nuovo dettaglio fattura per fattura {id_fattura}")
            return True

        except Error as e:
            raise ImportFattureError(f"Errore database nell'inserimento dettaglio fattura {id_fattura}: {e}")

    def inserisci_pagamento_sicuro(self, row, id_fattura,numero_riga):
        """Versione sicura di inserisci_pagamento che solleva eccezioni"""
        # Estrai tutti i valori necessari
        pareggio_sap = row.get('Pareggio (SAP S/4)')
        importo_pagato = self.converti_numero(row.get('Importo pagato'))
        importo_rendicontato = self.converti_numero(row.get('Importo rendicontato'))
        data_pagamento = self.converti_data(row.get('Data regolamento'))
        data_pareggio = self.converti_data(row.get('Data pareggio'))
        lista_pagamento = row.get('Lista di pagamento')
        perc_contrib = self.converti_percentuale(row.get('% contribuzione'))
        if perc_contrib is not None and perc_contrib > 1:
            perc_contrib = perc_contrib / 100

        # Ottieni id_sal dalla cache
        id_sal = self.sal_cache.get(self.numero_sal)
        if not id_sal:
            raise ValidazioneRigaError(f"SAL {self.numero_sal} non trovato nel database")

        if lista_pagamento is not None and (pd.isna(lista_pagamento) or str(lista_pagamento).lower() == 'nan'):
            lista_pagamento = None
        pareggio_sap_str = str(pareggio_sap).strip() if pareggio_sap else None

        try:
            # Controllo se il pagamento esiste già usando tutti i campi della INSERT
            # check_query = """
            #               SELECT COUNT(*) \
            #               FROM pagamenti
            #               WHERE id_fattura = %s
            #                 AND COALESCE(id_sal, 0) = COALESCE(%s, 0)
            #                 AND COALESCE(pareggio_sap, '') = COALESCE(%s, '')
            #                 AND COALESCE(importo_pagato, 0) = COALESCE(%s, 0)
            #                 AND COALESCE(importo_rendicontato, 0) = COALESCE(%s, 0)
            #                 AND COALESCE(data_pagamento, '1900-01-01') = COALESCE(%s, '1900-01-01')
            #                 AND COALESCE(data_pareggio, '1900-01-01') = COALESCE(%s, '1900-01-01')
            #                 AND COALESCE(lista_pagamento, '') = COALESCE(%s, '')
            #                 AND COALESCE(percentuale_contributo, 0) = COALESCE(%s, 0) \
            #               """
            # self.cursor.execute(check_query, (
            #     id_fattura, id_sal, pareggio_sap_str, importo_pagato, importo_rendicontato,
            #     data_pagamento, data_pareggio, lista_pagamento, perc_contrib
            # ))
            #
            # count = self.cursor.fetchone()[0]
            #
            # if count > 0:
            #     logger.debug(
            #         f"Pagamento già esistente per fattura {id_fattura}, SAL {self.numero_sal}, salto inserimento")
            #     return False  # Pagamento già esistente

            # Inserisco nuovo pagamento direttamente
            ins = """
                  INSERT INTO pagamenti
                  (id_fattura, id_sal, importo_pagato, importo_rendicontato, data_pagamento, data_pareggio,
                   lista_pagamento, percentuale_contributo, pareggio_sap,importo_ammesso,importo_non_ammesso,riga_excel)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,%s,%s,%s) \
                  """
            self.cursor.execute(ins, (
                id_fattura, id_sal, importo_pagato, importo_rendicontato, data_pagamento,
                data_pareggio, lista_pagamento, perc_contrib, pareggio_sap_str,importo_rendicontato,0,numero_riga
            ))
            id_nuovo_pagamento = self.cursor.lastrowid
            logger.debug(f"Inserito pagamento ID {id_nuovo_pagamento} per fattura {id_fattura}, SAL {self.numero_sal}")
            return True

        except Error as e:
            raise ImportFattureError(f"Errore database nell'inserimento pagamento fattura {id_fattura}: {e}")

    # ── Importazione Foglio SICURA ────────────────────────────────────────────────

    def importa_foglio_excel_sicuro(self, file_path, sheet_name, linea_intervento):
        """Versione sicura dell'importazione foglio con gestione atomica delle righe per SAL 7+"""
        try:
            logger.info(f"Inizio importazione SICURA foglio SAL 7+: {sheet_name}")

            # Leggi l'Excel - per SAL 7+ l'header è tipicamente alla riga 3 (index 2)
            df = pd.read_excel(file_path, sheet_name=sheet_name, header=2)

            if df.empty:
                logger.error(f"Impossibile leggere '{sheet_name}' con header alla riga 3")
                return False

            logger.info(f"Colonne caricate: {len(df.columns)}")

            # Rimuovi righe completamente vuote
            df = df.dropna(how='all')
            df.columns = df.columns.str.replace(r'\s+', ' ', regex=True).str.strip()

            # Crea mapping per SAL 7+ (formato standardizzato)
            column_mapping = self.crea_mapping_colonne_sal7()

            # Trova le colonne effettive nel dataframe
            column_mapping_finale = {}
            for chiave_standard, nomi_possibili in column_mapping.items():
                colonna_trovata = self.trova_colonna_nel_df(df, nomi_possibili)
                if colonna_trovata:
                    column_mapping_finale[colonna_trovata] = chiave_standard
                    logger.debug(f"Mappata colonna '{colonna_trovata}' -> '{chiave_standard}'")

            # Rinomina le colonne
            df_mapped = df.rename(columns=column_mapping_finale)

            logger.info(f"Caricate {len(df_mapped)} righe di dati dal foglio {sheet_name}")

            totale_righe = len(df_mapped)
            righe_processate = 0
            righe_con_errori = 0

            # Processa ogni riga con importazione atomica
            for idx, row in df_mapped.iterrows():
                numero_riga_excel = idx + 4  # +4 perché header è alla riga 3

                try:
                    # Prepara i dati della riga
                    row_data = dict(row)

                    # Salta righe senza numero_fattura
                    if pd.isna(row_data.get('Numero fattura del fornitore')):
                        continue

                    # IMPORTAZIONE ATOMICA DELLA RIGA
                    if self.importa_riga_atomica(row_data, numero_riga_excel, linea_intervento, sheet_name):
                        righe_processate += 1

                        # Commit ogni 50 righe
                        if righe_processate % 50 == 0:
                            self.connection.commit()
                            logger.info(f"Processate {righe_processate}/{totale_righe} righe")
                    else:
                        righe_con_errori += 1

                except Exception as e:
                    # Questo catch non dovrebbe mai attivarsi se importa_riga_atomica funziona bene
                    logger.error(f"ERRORE CRITICO non gestito nella riga {numero_riga_excel}: {e}")
                    self.salva_errore_riga_sicuro(numero_riga_excel, sheet_name, {}, e)
                    righe_con_errori += 1

            # Commit finale
            self.connection.commit()

            logger.info(f"Completato foglio {sheet_name}: {righe_processate} righe OK, {righe_con_errori} errori")
            return righe_processate, righe_con_errori

        except Exception as e:
            logger.error(f"Errore generale nell'importazione del foglio {sheet_name}: {e}")
            self.connection.rollback()
            return 0, 0

    def trova_colonna_nel_df(self, df, nomi_possibili):
        """Trova una colonna nel dataframe usando una lista di nomi possibili."""
        for nome in nomi_possibili:
            nome_norm = self.normalizza_nome_colonna(nome)
            for df_col in df.columns:
                if self.normalizza_nome_colonna(df_col) == nome_norm:
                    return df_col
        return None

    def crea_mapping_colonne_sal7(self):
        """Crea mapping colonne specifico per SAL 7+ con formato standardizzato"""
        return {
            # Colonne standard
            'SAL': ['SAL'],
            'Periodo riferimento': ['Periodo riferimento'],
            'Categoria di spesa': ['Categoria di spesa'],
            'Accordo Quadro': ['Accordo Quadro'],
            'Buono di consegna (BdC)': ['Buono di consegna (BdC)'],
            'Posizione riferimento (BdC)': ['Posizione riferimento (BdC)'],
            'Numero VAP': ['Numero VAP', 'Numero \nVAP'],
            'Numero documento EM': ['Numero documento EM'],
            'Data registrazione EM': ['Data registrazione EM'],
            'Entrata Merce netto IVA': ['Entrata Merce netto IVA'],
            'IVA indetraibile': ['IVA indetraibile'],
            'Entrata Merce Lordo IVA indetraibile': ['Entrata Merce Lordo IVA indetraibile'],
            'Data emissione fattura del fornitore': ['Data emissione fattura del fornitore'],
            'Imponibile fattura': ['Imponibile fattura'],
            'IVA': ['IVA', 'IVA '],
            'Importo totale fattura': ['Importo totale fattura'],
            'Pareggio (SAP S/4)': ['Pareggio (SAP S/4)'],
            'Data pareggio': ['Data pareggio'],
            'Lista di pagamento': ['Lista di pagamento'],
            'Importo pagato': ['Importo pagato'],
            'Importo rendicontato': ['Importo rendicontato'],
            '% contribuzione': ['% contribuzione'],

            # Colonne specifiche SAL 7+
            'Numero fattura del fornitore': [
                'Numero fattura del fornitore',
                'Riferimento fattura'
            ],
            'Descrizione Fornitore': [
                'Descrizione Fornitore',
                'Descrizione fornitore fattura'
            ],
            'Data regolamento': [
                'Data regolamento',
                'Data regolamento fattura'
            ],
            'N° Protocollo Fattura': [
                'N° Protocollo Fattura',
                'N° Protocollo fattura'
            ],
            'N° Protocollo documento integrativo fatture reverse charge': [
                'N° Protocollo documento integrativo fatture reverse charge'
            ],

            # Colonne DSAN (specifiche SAL 7+)
            'classe cespiti': ['classe cespiti'],
            "% indetraibilità dell'IVA (C=A+B)": [
                "% indetraibilità dell'IVA (C=A+B)",
                "% indetraibilità dell'IVA\n(C=A+B)"
            ],
            "ATTIVITA' FINANZIARIA (indetraibile) (A)": ["ATTIVITA' FINANZIARIA (indetraibile) (A)"],
            "ATTIVITA'  UNIVERSALE (indetraibile) (B)": ["ATTIVITA'  UNIVERSALE (indetraibile) (B)"],
            "ATTIVITA' RESIDUALE (detraibile)": [
                "ATTIVITA' RESIDUALE\n (detraibile)",
                "ATTIVITA' RESIDUALE (detraibile)"
            ],
            "Aliquota IVA indetraibile (C*22%)": [
                "Aliquota IVA indetraibile\n (C*22%)",
                "Aliquota IVA indetraibile (C*22%)"
            ]
        }

    # ── Rilevamento fogli con AI ────────────────────────────────────────────────

    def _rileva_fogli_con_ai(self, sheet_names):
        """
        Usa l'AI per determinare quali fogli contengono dati fatture
        e a quale linea di intervento corrispondono.
        Restituisce lista di dict: [{'sheet_name': ..., 'linea_intervento': ...}]
        """
        import json as _json

        prompt = (
            "Dato un file Excel di fatture POLIS con i seguenti fogli:\n"
            f"{sheet_names}\n\n"
            "Determina quali fogli contengono dati di fatture dettagliate e "
            "a quale linea di intervento corrispondono.\n"
            "Le linee di intervento note sono:\n"
            "- 'report SPORTELLOUNICO' per fogli relativi a Sportello Unico\n"
            "- 'report SPAZIITALIA' per fogli relativi a Spazi per l'Italia / Spazi Italia\n\n"
            "Ignora fogli di riepilogo, sintesi o sommario.\n\n"
            "Rispondi SOLO con un JSON array valido:\n"
            '[{"sheet_name": "nome esatto del foglio", "linea_intervento": "valore"}]\n'
            "Se non ci sono fogli di fatture, rispondi con []"
        )

        try:
            # Prova a usare anthropic se disponibile (patched dal container)
            try:
                import anthropic
                client = anthropic.Anthropic()
                response = client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                reply = response.content[0].text.strip()
            except Exception:
                # Fallback: matching fuzzy senza AI
                return self._rileva_fogli_fallback(sheet_names)

            # Pulisci markdown
            if reply.startswith("```json"):
                reply = reply[7:]
            if reply.startswith("```"):
                reply = reply[3:]
            if reply.endswith("```"):
                reply = reply[:-3]
            reply = reply.strip()

            fogli = _json.loads(reply)
            if fogli:
                for f in fogli:
                    logger.info(
                        "AI: foglio '%s' -> linea '%s'",
                        f['sheet_name'], f['linea_intervento'],
                    )
            return fogli

        except Exception as e:
            logger.warning("Rilevamento AI fogli fallito: %s. Uso fallback.", e)
            return self._rileva_fogli_fallback(sheet_names)

    def _rileva_fogli_fallback(self, sheet_names):
        """Fallback: matching case-insensitive per parole chiave."""
        fogli = []
        for name in sheet_names:
            lower = name.lower().replace(" ", "")
            if "sportellounico" in lower or "sportello" in lower:
                fogli.append({
                    'sheet_name': name,
                    'linea_intervento': 'report SPORTELLOUNICO',
                })
                logger.info("Fallback: foglio '%s' -> report SPORTELLOUNICO", name)
            elif "spaziitalia" in lower or "spazi" in lower:
                fogli.append({
                    'sheet_name': name,
                    'linea_intervento': 'report SPAZIITALIA',
                })
                logger.info("Fallback: foglio '%s' -> report SPAZIITALIA", name)
        return fogli

    # ── Importazione Excel SICURA ─────────────────────────────────────────────────

    def importa_excel_sicuro(self, file_path, numero_sal=None):
        """Versione sicura dell'importazione Excel per SAL 7+"""
        try:
            logger.info(f"Inizio importazione SICURA file SAL 7+: {file_path}")

            # Imposta il numero_sal per questa importazione (deve essere >= 7)
            self.numero_sal = numero_sal if numero_sal else 7

            if self.numero_sal < 7:
                logger.error(f"SAL {self.numero_sal} non supportato. Questo script supporta solo SAL >= 7")
                return False

            # Recupera l'id_sal dal numero_sal
            id_sal = self.sal_cache.get(self.numero_sal)
            if not id_sal:
                logger.error(f"SAL {self.numero_sal} non trovato nel database")
                return False

            # Crea sessione di importazione
            if self.error_handler:
                session_id = self.error_handler.crea_sessione_import(
                    tipo_import='fatture',
                    nome_file=os.path.basename(file_path),
                    id_sal=id_sal,
                    utente=None
                )

            # Verifica struttura file
            try:
                excel_file = pd.ExcelFile(file_path)
                logger.info(f"Fogli disponibili: {excel_file.sheet_names}")
            except Exception as e:
                logger.error(f"Errore nella verifica della struttura del file: {e}")
                return False

            # Determina fogli e linee di intervento con AI
            fogli_esistenti = self._rileva_fogli_con_ai(excel_file.sheet_names)

            if not fogli_esistenti:
                logger.error("Nessun foglio da importare trovato nel file Excel")
                return False

            # Importa ogni foglio con il metodo sicuro
            totale_righe_processate = 0
            totale_righe_con_errori = 0

            for foglio in fogli_esistenti:
                logger.info("=" * 60)
                logger.info(f"IMPORTAZIONE SICURA FOGLIO SAL 7+: {foglio['sheet_name']}")
                logger.info("=" * 60)

                righe_proc, righe_err = self.importa_foglio_excel_sicuro(
                    file_path,
                    foglio['sheet_name'],
                    foglio['linea_intervento']
                )

                totale_righe_processate += righe_proc
                totale_righe_con_errori += righe_err

            # Riepilogo finale
            logger.info("=" * 60)
            logger.info("IMPORTAZIONE SICURA SAL 7+ COMPLETATA")
            logger.info(f"SAL importato: {self.numero_sal}")
            logger.info(f"Fogli importati: {len(fogli_esistenti)}")
            logger.info(f"Totale righe processate con successo: {totale_righe_processate}")
            logger.info(f"Totale righe con errori: {totale_righe_con_errori}")
            logger.info(
                f"Tasso di successo: {(totale_righe_processate / (totale_righe_processate + totale_righe_con_errori) * 100):.2f}%" if (
                                                                                                                                                  totale_righe_processate + totale_righe_con_errori) > 0 else "N/A")
            logger.info("=" * 60)

            # Aggiorna stato sessione
            if self.error_handler:
                self.error_handler.aggiorna_sessione(
                    totale_righe=totale_righe_processate + totale_righe_con_errori,
                    righe_importate=totale_righe_processate,
                    righe_errore=totale_righe_con_errori,
                    stato_import='completato'
                )

                # Stampa statistiche errori
                stats = self.error_handler.get_statistiche_errori()
                if stats:
                    logger.info("\n=== STATISTICHE ERRORI ===")
                    for tipo_errore, conteggio, da_correggere, corretti in stats:
                        logger.info(
                            f"{tipo_errore}: {conteggio} totali ({da_correggere} da correggere, {corretti} corretti)")

            return True

        except Exception as e:
            logger.error(f"Errore generale nell'importazione sicura SAL 7+: {e}")
            self.connection.rollback()

            # Marca la sessione come fallita
            if self.error_handler:
                self.error_handler.aggiorna_sessione(stato_import='fallito')

            return False


# ─── API Integration Functions ─────────────────────────────────────────────────
def cerca_file_excel_api(cartella_sal, query="Riepilogo analitico spese sostenute"):
    """
    Cerca file Excel tramite API usando il numero SAL

    Args:
        cartella_sal: cartella del sal
        query: Query di ricerca (default: "Riepilogo analitico spese sostenute")

    Returns:
        dict: Risultato della ricerca con i file trovati
    """
    base_url = "https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net"
    search_endpoint = f"{base_url}/api/search"

    # Costruisci il path di ricerca basato sul numero SAL
    search_path = f"{cartella_sal}"

    params = {
        'q': query,
        'path': search_path,
        'limit': 100
    }

    headers = {
        'accept': 'application/json'
    }

    try:
        logger.info(f"Cercando file Excel per SAL {cartella_sal} tramite API...")
        logger.info(f"URL: {search_endpoint}")
        logger.info(f"Parametri: {params}")

        response = requests.get(search_endpoint, params=params, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()

        if data.get('success'):
            logger.info(f"Ricerca completata in {data.get('responseTime', 0):.2f}s")
            logger.info(f"Trovati {len(data.get('results', []))} file(s)")

            # Filtra solo i file Excel (.xlsx)
            excel_files = [
                f for f in data.get('results', [])
                if f.get('name', '').lower().endswith('.xlsx')
            ]

            if excel_files:
                logger.info(f"File Excel trovati: {len(excel_files)}")
                for f in excel_files:
                    logger.info(f"  - {f.get('name')} ({f.get('size')} bytes)")

                return {
                    'success': True,
                    'files': excel_files,
                    'total_found': len(excel_files)
                }
            else:
                logger.warning("Nessun file Excel (.xlsx) trovato nei risultati")
                return {
                    'success': False,
                    'error': 'Nessun file Excel trovato',
                    'files': []
                }
        else:
            logger.error(f"Ricerca fallita: {data}")
            return {
                'success': False,
                'error': 'Ricerca API fallita',
                'files': []
            }

    except requests.exceptions.Timeout:
        logger.error("Timeout nella ricerca API")
        return {'success': False, 'error': 'Timeout API', 'files': []}
    except requests.exceptions.RequestException as e:
        logger.error(f"Errore nella richiesta API: {e}")
        return {'success': False, 'error': f'Errore API: {e}', 'files': []}
    except Exception as e:
        logger.error(f"Errore imprevisto nella ricerca: {e}")
        return {'success': False, 'error': f'Errore imprevisto: {e}', 'files': []}


def scarica_file_excel_api(file_path, local_filename=None):
    """
    Scarica un file Excel tramite API

    Args:
        file_path: Path del file da scaricare (es. "8_SAL_30_04_2025/Riepilogo analitico spese sostenute 8 SAL.xlsx")
        local_filename: Nome del file locale (opzionale, usa il nome originale se non specificato)

    Returns:
        str: Path del file scaricato o None se errore
    """
    base_url = "https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net"
    download_endpoint = f"{base_url}/api/download/{urllib.parse.quote(file_path, safe='')}"

    headers = {
        'accept': 'application/octet-stream'
    }

    # Determina il nome del file locale
    if not local_filename:
        local_filename = os.path.basename(file_path)

    try:
        logger.info(f"Scaricando file: {file_path}")
        logger.info(f"URL: {download_endpoint}")

        response = requests.get(download_endpoint, headers=headers, timeout=300, stream=True)
        response.raise_for_status()

        # Verifica che il contenuto sia effettivamente un Excel
        content_type = response.headers.get('content-type', '')
        if 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' not in content_type:
            logger.warning(f"Content-Type imprevisto: {content_type}")

        # Scarica il file
        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0

        with open(local_filename, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

                    # Log del progresso ogni MB
                    if downloaded_size % (1024 * 1024) == 0:
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            logger.info(f"Download in corso: {progress:.1f}% ({downloaded_size}/{total_size} bytes)")

        # Verifica che il file sia stato scaricato correttamente
        if os.path.exists(local_filename):
            file_size = os.path.getsize(local_filename)
            logger.info(f"File scaricato con successo: {local_filename} ({file_size} bytes)")

            # Verifica che sia un file Excel valido
            try:
                pd.read_excel(local_filename, nrows=0)
                logger.info("File Excel validato con successo")
                return local_filename
            except Exception as e:
                logger.error(f"Il file scaricato non è un Excel valido: {e}")
                # Rimuovi il file corrotto
                try:
                    os.remove(local_filename)
                except:
                    pass
                return None
        else:
            logger.error("File non trovato dopo il download")
            return None

    except requests.exceptions.Timeout:
        logger.error("Timeout nel download del file")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Errore nel download: {e}")
        return None
    except Exception as e:
        logger.error(f"Errore imprevisto nel download: {e}")
        return None


def trova_e_scarica_excel_sal(numero_sal,cartella_sal:str):
    """
    Funzione principale per trovare e scaricare il file Excel per un SAL specifico

    Args:
        numero_sal: Numero del SAL

    Returns:
        str: Path del file scaricato o None se non trovato/errore
    """
    logger.info(f"Ricerca e download automatico file Excel per SAL {numero_sal}")

    # Cerca il file tramite API
    risultato_ricerca = cerca_file_excel_api(cartella_sal)

    if not risultato_ricerca.get('success'):
        logger.error(f"Ricerca fallita: {risultato_ricerca.get('error')}")
        return None

    files = risultato_ricerca.get('files', [])
    if not files:
        logger.error(f"Nessun file Excel trovato per SAL {numero_sal}")
        return None

    # Trova il file migliore (preferisci quello con nome più specifico)
    file_da_scaricare = None
    nome_target = f"Riepilogo analitico spese sostenute {numero_sal} SAL.xlsx"

    # Prima cerca il file con il nome esatto
    for f in files:
        if f.get('name', '').lower() == nome_target.lower():
            file_da_scaricare = f
            logger.info(f"Trovato file con nome esatto: {f.get('name')}")
            break

    # Se non trovato, prendi il primo file Excel disponibile
    if not file_da_scaricare:
        file_da_scaricare = files[0]
        logger.info(f"Utilizzando il primo file Excel disponibile: {file_da_scaricare.get('name')}")

    # Scarica il file
    file_path = file_da_scaricare.get('path')
    if not file_path:
        logger.error("Path del file non trovato nei risultati API")
        return None

    # Nome del file locale
    local_filename = file_da_scaricare.get('name')

    # Scarica il file
    downloaded_file = scarica_file_excel_api(file_path, local_filename)

    if downloaded_file:
        logger.info(f"✅ File Excel per SAL {numero_sal} scaricato con successo: {downloaded_file}")
        return downloaded_file
    else:
        logger.error(f"❌ Impossibile scaricare il file Excel per SAL {numero_sal}")
        return None


# ─── Configurazione logging ───────────────────────────────────────────────────
def main():
    DB_CONFIG = {
        'host': 'localhost',
        'port': 3306,
        'user': 'root',
        'password': os.getenv('DB_PASSWORD', ''),
        'database': 'polis'
    }
    # DB_CONFIG = {
    #     'host': 'colibri-mysql-flexible.mysql.database.azure.com',
    #     'port': 3306,
    #     'user': 'polisammin',
    #     'database': 'polis',
    # }

    # Parser per gli argomenti
    parser = argparse.ArgumentParser(description='Importa fatture SAL 7+ da Excel nel database (VERSIONE PULITA)')
    parser.add_argument('--sal', type=int, help='Numero del SAL da importare (deve essere >= 7)')
    parser.add_argument('--file', type=str, help='Path del file Excel da importare')
    parser.add_argument('--no-api', action='store_true', help='Disabilita ricerca API e usa solo file locali')
    # Add this argument to the parser in the main() function
    parser.add_argument('--cartella', type=str, help='Cartella del SAL per la ricerca API')

    args = parser.parse_args()

    # Determinazione del numero SAL
    numero_sal = args.sal
    cartella_sal = args.cartella if args.sal else "8_SAL_30_04_2025"

    # Determinazione del file da importare
    EXCEL_FILE = None

    if args.file:
        # File specificato esplicitamente
        EXCEL_FILE = args.file
        if not os.path.exists(EXCEL_FILE):
            logger.error(f"File specificato non trovato: {EXCEL_FILE}")
            return

        # Deduce il numero SAL dal nome del file se non specificato
        if not numero_sal:
            match = re.search(r'(\d+)\s*SAL', EXCEL_FILE, re.IGNORECASE)
            if match:
                numero_sal = int(match.group(1))
                logger.info(f"Numero SAL dedotto dal nome del file: {numero_sal}")
            else:
                logger.error("Impossibile dedurre il numero SAL dal nome del file. Specificare con --sal N")
                return
    else:
        # File non specificato - determina il numero SAL prima
        if not numero_sal:
            # Prova a dedurre il SAL più recente dai file locali
            possible_files = [
                'Riepilogo analitico spese sostenute 10 SAL.xlsx',
                'Riepilogo analitico spese sostenute 9 SAL.xlsx',
                'Riepilogo analitico spese sostenute 8 SAL.xlsx',
                'Riepilogo analitico spese sostenute 7 SAL.xlsx'
            ]
            for fn in possible_files:
                if os.path.exists(fn):
                    match = re.search(r'(\d+)\s*SAL', fn, re.IGNORECASE)
                    if match:
                        numero_sal = int(match.group(1))
                        EXCEL_FILE = fn
                        logger.info(f"Trovato file locale SAL {numero_sal}: {fn}")
                        break

            if not numero_sal:
                logger.error("Nessun numero SAL specificato e nessun file locale trovato.")
                logger.error("Specificare il numero SAL con --sal N")
                return

        # Se non abbiamo ancora un file, prova prima localmente poi API
        if not EXCEL_FILE:
            # Cerca file locale per il SAL specificato
            local_filename = f'Riepilogo analitico spese sostenute {numero_sal} SAL.xlsx'

            if os.path.exists(local_filename):
                EXCEL_FILE = local_filename
                logger.info(f"Trovato file locale per SAL {numero_sal}: {local_filename}")
            elif not args.no_api:
                # File locale non trovato, prova con API
                logger.info(f"File locale non trovato per SAL {numero_sal}, cercando tramite API...")

                downloaded_file = trova_e_scarica_excel_sal(numero_sal,cartella_sal)
                if downloaded_file:
                    EXCEL_FILE = downloaded_file
                    logger.info(f"✅ File scaricato tramite API: {EXCEL_FILE}")
                else:
                    logger.error(f"❌ Impossibile trovare/scaricare file per SAL {numero_sal}")
                    logger.error("Opzioni disponibili:")
                    logger.error("1. Specificare un file esistente con --file")
                    logger.error("2. Assicurarsi che il file sia disponibile nell'API storage")
                    logger.error("3. Usare --no-api per cercare solo file locali")
                    return
            else:
                # API disabilitata e file locale non trovato
                logger.error(f"File locale non trovato per SAL {numero_sal}: {local_filename}")
                logger.error("API disabilitata (--no-api). Specificare un file esistente con --file")
                return

    # Verifica che il SAL sia >= 7
    if numero_sal < 7:
        logger.error(f"SAL {numero_sal} non supportato. Questo script supporta solo SAL >= 7")
        logger.error("Per SAL precedenti al 7, utilizzare lo script legacy appropriato")
        return

    # Verifica finale che il file esista
    if not EXCEL_FILE or not os.path.exists(EXCEL_FILE):
        logger.error(f"File Excel non trovato: {EXCEL_FILE}")
        return

    logger.info("=" * 80)
    logger.info("AVVIO IMPORTAZIONE SAL 7+ PULITA CON INTEGRAZIONE API")
    logger.info(f"File: {EXCEL_FILE}")
    logger.info(f"SAL: {numero_sal}")
    logger.info("Modalità: SICURA con formato standardizzato SAL 7+")
    if not args.no_api:
        logger.info("API: ABILITATA per ricerca automatica file")
    else:
        logger.info("API: DISABILITATA - solo file locali")
    logger.info("=" * 80)

    importatore = ImportatoreFattureSAL7(**DB_CONFIG)
    try:
        if not importatore.connetti_db():
            logger.error("Impossibile connettersi al database")
            return
        importatore.carica_cache()

        # Verifico esistenza SAL, altrimenti lo creo
        if numero_sal not in importatore.sal_cache:
            logger.info(f"Inserimento SAL {numero_sal} nel database")

            # Date per SAL 7+ (ogni SAL copre 4 mesi a partire da settembre 2024)
            # SAL 7: set-dic 2024, SAL 8: gen-apr 2025, SAL 9: mag-ago 2025, ecc.
            from datetime import date
            from dateutil.relativedelta import relativedelta

            # SAL 7 inizia il 1° settembre 2024
            data_inizio_sal7 = date(2024, 9, 1)

            # Calcola l'inizio del SAL corrente
            mesi_dal_sal7 = (numero_sal - 7) * 4
            data_inizio = data_inizio_sal7 + relativedelta(months=mesi_dal_sal7)
            data_fine = data_inizio + relativedelta(months=4, days=-1)

            q = """INSERT INTO sal (numero_sal, data_inizio, data_fine)
                   VALUES (%s, %s, %s)"""
            importatore.cursor.execute(q, (numero_sal, data_inizio, data_fine))
            importatore.connection.commit()
            importatore.carica_cache()

        # IMPORTAZIONE SICURA SAL 7+
        success = importatore.importa_excel_sicuro(EXCEL_FILE, numero_sal=numero_sal)

        if success:
            logger.info("✅ IMPORTAZIONE SAL 7+ COMPLETATA CON SUCCESSO")

            # Cleanup: rimuovi il file scaricato se è stato scaricato tramite API
            if not args.file and not args.no_api and EXCEL_FILE and os.path.exists(EXCEL_FILE):
                try:
                    # Verifica che sia effettivamente un file scaricato (non specificato dall'utente)
                    if not any(EXCEL_FILE.endswith(f) for f in [
                        'Riepilogo analitico spese sostenute 7 SAL.xlsx',
                        'Riepilogo analitico spese sostenute 8 SAL.xlsx',
                        'Riepilogo analitico spese sostenute 9 SAL.xlsx',
                        'Riepilogo analitico spese sostenute 10 SAL.xlsx'
                    ]) or EXCEL_FILE.startswith('Riepilogo analitico spese sostenute'):
                        os.remove(EXCEL_FILE)
                        logger.info(f"File temporaneo rimosso: {EXCEL_FILE}")
                except Exception as e:
                    logger.warning(f"Impossibile rimuovere file temporaneo {EXCEL_FILE}: {e}")
        else:
            logger.error("❌ IMPORTAZIONE SAL 7+ FALLITA")

    except Exception as e:
        logger.error(f"Errore generale: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        importatore.chiudi_connessione()


if __name__ == "__main__":
    main()

