import pandas as pd
import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime
import os
import sys
import re
import argparse
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
    log_filename = f'logs/import_fatture_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = logging.getLogger('ImportFatture')
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

# ─── Classe ImportatoreFatture ─────────────────────────────────────────────────
class ImportatoreFatture:
    def __init__(self, host, port, user, password, database):
        """Inizializza la connessione al database e le cache"""
        self.connection = None
        self.cursor     = None
        self.db_config = {
            'host'    : host,
            'port'    : port,
            'user'    : user,
            'password': password,
            'database': database
        }
        # Cache per lookup rapido
        self.fornitori_cache = {}
        self.accordi_cache   = {}
        self.categorie_cache = {}
        self.sal_cache       = {}
        self.classe_cache    = {}
        self.documenti_cache = {}  # <numero_documento, id_em>
        self.numero_sal = None  # Verrà impostato durante l'importazione

        # Aggiungi error handler
        self.error_handler = None

    def connetti_db(self):
        """Stabilisce la connessione al database (cursor buffered)."""
        try:
            self.connection = mysql.connector.connect(**self.db_config)
            self.cursor     = self.connection.cursor(buffered=True)
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

            # sal
            self.cursor.execute("SELECT id_sal, numero_sal FROM sal")
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
        try:
            return pd.to_datetime(data_str).date()
        except Exception:
            logger.warning(f"Impossibile convertire la data: {data_str}")
            return None

    def converti_numero(self, valore):
        """Converte un numero (formato italiano o percentuale) in float."""
        if pd.isna(valore) or valore == '':
            return None
        if isinstance(valore, (int, float)):
            return float(valore)
        s = str(valore).strip().replace('.', '').replace(',', '.').replace('%', '')
        try:
            return float(s)
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
        if not numero_bdc:
            return None  # BdC opzionale
            
        if not id_accordo:
            raise BuonoConsegnaError(f"Impossibile inserire BdC '{numero_bdc}': accordo quadro mancante")
            
        try:
            # Converti posizione in int
            if posizione is not None and pd.notna(posizione):
                if isinstance(posizione, str) and '-' in posizione:
                    raise BuonoConsegnaError(f"Posizione BdC non valida: '{posizione}' (formato non supportato)")
                posizione = int(float(posizione))  # Gestisce anche float
            else:
                posizione = None
                
            s = """SELECT id_bdc FROM buoni_consegna 
                   WHERE numero_bdc = %s AND posizione_riferimento = %s"""
            self.cursor.execute(s, (numero_bdc, posizione))
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
        
        # Validazione SAL
        if not self.numero_sal or self.numero_sal not in self.sal_cache:
            errori.append(f"SAL {self.numero_sal} non valido")
        
        # Validazioni numeriche
        try:
            numero_fattura = str(row_data.get('Numero fattura del fornitore', '')).strip()
            if len(numero_fattura) > 50:  # Lunghezza massima colonna numero_fattura
                errori.append(f"Numero fattura troppo lungo: {len(numero_fattura)} caratteri (max 100)")
        except:
            errori.append("Numero fattura non convertibile in stringa")
        
        # Validazione posizione BdC se presente
        if row_data.get('Posizione riferimento (BdC)'):
            try:
                pos = row_data.get('Posizione riferimento (BdC)')
                if pd.notna(pos):
                    if isinstance(pos, str) and '-' in pos:
                        errori.append(f"Posizione BdC non valida: '{pos}' (formato range non supportato)")
                    else:
                        int(float(pos))  # Prova conversione
            except (ValueError, TypeError):
                errori.append(f"Posizione BdC non convertibile: '{row_data.get('Posizione riferimento (BdC)')}'")
        
        # Validazione date
        date_fields = ['Data emissione fattura del fornitore', 'Data registrazione EM', 'Data regolamento', 'Data pareggio']
        for field in date_fields:
            if row_data.get(field) and pd.notna(row_data.get(field)):
                try:
                    self.converti_data(row_data.get(field))
                except:
                    errori.append(f"Data non valida in campo '{field}': {row_data.get(field)}")
        
        # Validazione importi
        importi_fields = ['Imponibile fattura', 'IVA', 'Importo totale fattura', 'Entrata Merce netto IVA', 
                         'IVA indetraibile', 'Entrata Merce Lordo IVA indetraibile', 'Importo pagato', 'Importo rendicontato']
        for field in importi_fields:
            if row_data.get(field) and pd.notna(row_data.get(field)):
                try:
                    self.converti_numero(row_data.get(field))
                except:
                    errori.append(f"Importo non valido in campo '{field}': {row_data.get(field)}")
        
        if errori:
            raise ValidazioneRigaError(f"Errori di validazione: {'; '.join(errori)}")

    # ── Inserimento Atomico per Riga ──────────────────────────────────────────────

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
            self.inserisci_dettaglio_fattura_sicuro(row_data, id_fattura)
            self.inserisci_pagamento_sicuro(row_data, id_fattura)
            
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
        
        numero_fattura = row.get('Numero fattura del fornitore')
        data_em = self.converti_data(row.get('Data emissione fattura del fornitore'))
        imponibile = self.converti_numero(row.get('Imponibile fattura'))
        iva = self.converti_numero(row.get('IVA'))
        totale = self.converti_numero(row.get('Importo totale fattura'))
        
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
        
        aliquota_iva = None
        if imponibile and iva and imponibile > 0:
            aliquota_iva = round((iva / imponibile) * 100, 2)

        try:
            # 2) verifico se esiste già per questo SAL
            id_sal = self.sal_cache.get(self.numero_sal)
            q = """SELECT id_fattura, imponibile, iva, totale, aliquota_iva, protocollo_fattura, id_em_integrativo, linea_intervento
                FROM fatture
                WHERE numero_fattura = %s AND codice_fornitore = %s AND id_sal = %s"""
            self.cursor.execute(q, (numero_fattura, id_forn, id_sal))
            r = self.cursor.fetchone()

            if r:
                id_f, old_imp, old_iva, old_tot, old_aliq, old_prot, old_em, old_linea = r
                # Controlla se i valori sono cambiati
                cond = (
                    ((imponibile is None and old_imp is None) or (imponibile is not None and old_imp is not None and abs(old_imp - imponibile) < 0.01)) and
                    ((iva is None and old_iva is None) or (iva is not None and old_iva is not None and abs(old_iva - iva) < 0.01)) and
                    ((totale is None and old_tot is None) or (totale is not None and old_tot is not None and abs(old_tot - totale) < 0.01)) and
                    (aliquota_iva == old_aliq) and
                    (protocollo_fattura == old_prot) and
                    (id_em_integrativo == old_em) and
                    (linea_intervento == old_linea)
                )
                if cond:
                    logger.debug(f"Fattura {numero_fattura} già esistente (ID: {id_f})")
                    return id_f

                # altrimenti aggiorno
                u = """UPDATE fatture
                    SET data_emissione = %s, imponibile = %s, iva = %s, aliquota_iva = %s, totale = %s,
                        protocollo_fattura = %s, id_em_integrativo = %s, linea_intervento = %s
                    WHERE id_fattura = %s"""
                self.cursor.execute(u, (data_em, imponibile, iva, aliquota_iva, totale,
                                    protocollo_fattura, id_em_integrativo, linea_intervento, id_f))
                logger.debug(f"Aggiornata fattura {numero_fattura} (ID: {id_f})")
                return id_f

            # 3) inserisco nuova fattura (nuova per questo SAL)
            ins = """INSERT INTO fatture
                    (numero_fattura, data_emissione, codice_fornitore, imponibile, iva, aliquota_iva, totale,
                    protocollo_fattura, id_em_integrativo, linea_intervento, id_sal)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
            self.cursor.execute(ins, (numero_fattura, data_em, id_forn, imponibile, iva, aliquota_iva, totale,
                                    protocollo_fattura, id_em_integrativo, linea_intervento, id_sal))
            id_new = self.cursor.lastrowid
            logger.debug(f"Inserita fattura {numero_fattura} (ID: {id_new})")
            return id_new

        except Error as e:
            raise ImportFattureError(f"Errore database nell'inserimento fattura '{numero_fattura}': {e}")

    def inserisci_dettaglio_fattura_sicuro(self, row, id_fattura):
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
            p_int = int(float(pos)) if pd.notna(pos) else None
            id_bdc = self.inserisci_o_recupera_bdc(row.get('Buono di consegna (BdC)'), p_int, id_acc)

        # 4) classe_cespiti (opzionale)
        classe_desc = row.get('Classe cespiti')
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
        
        # Recupera la percentuale di indetraibilità
        perc_ind = self.converti_percentuale(row.get("% indetraibilità dell'IVA (C=A+B)"))
        if perc_ind is None:
            perc_ind = self.converti_percentuale(row.get("%IVA INDETRAIBILE"))
        
        # 7) NUOVE COLONNE DSAN (solo se presenti nel file)
        attivita_finanziaria = None
        attivita_universale = None
        attivita_residuale = None
        aliquota_iva_calc = None
        
        # Verifica se le colonne DSAN esistono
        if "ATTIVITA' FINANZIARIA (indetraibile) (A)" in row:
            attivita_finanziaria = self.converti_percentuale(row.get("ATTIVITA' FINANZIARIA (indetraibile) (A)"))
        if "ATTIVITA'  UNIVERSALE (indetraibile) (B)" in row:
            attivita_universale = self.converti_percentuale(row.get("ATTIVITA'  UNIVERSALE (indetraibile) (B)"))
        if "ATTIVITA' RESIDUALE (detraibile)" in row:
            attivita_residuale = self.converti_percentuale(row.get("ATTIVITA' RESIDUALE (detraibile)"))
        if "Aliquota IVA indetraibile (C*22%)" in row:
            aliquota_iva_calc = self.converti_percentuale(row.get("Aliquota IVA indetraibile (C*22%)"))

        # 8) CALCOLO importo_ammesso e importo_non_ammesso
        importo_ammesso = None
        importo_non_ammesso = None

        if imponibile_nf is not None:
            if perc_ind is not None and perc_ind > 0:
                importo_ammesso = round(imponibile_nf * (1 - (perc_ind / 100.0)), 2)
                importo_non_ammesso = round(imponibile_nf - importo_ammesso, 2)
            else:
                importo_ammesso = round(imponibile_nf, 2)
                importo_non_ammesso = 0.0

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

            select_q = f"""
                SELECT id_voce, imponibile, iva, totale, perc_indetraibilita, importo_ammesso, importo_non_ammesso,
                       attivita_finanziaria_indetraibile, attivita_universale_indetraibile, 
                       attivita_residuale_detraibile, aliquota_iva_indetraibile_calc
                FROM dettagli_fattura
                WHERE {' AND '.join(conditions)}
            """
            self.cursor.execute(select_q, tuple(params))
            r = self.cursor.fetchone()

            if r:
                # Controlla se i valori sono identici
                (id_voce, old_imp, old_iva, old_tot, old_perc, old_amm, old_non, 
                 old_fin, old_univ, old_res, old_aliq_calc) = r
                
                def valori_uguali(val1, val2, soglia=0.01):
                    if val1 is None and val2 is None:
                        return True
                    if val1 is None or val2 is None:
                        return False
                    return abs(val1 - val2) < soglia
                
                cond_valori = (
                    valori_uguali(imponibile_nf, old_imp) and
                    valori_uguali(iva_ind, old_iva) and
                    valori_uguali(totale_nf, old_tot) and
                    valori_uguali(perc_ind, old_perc) and
                    valori_uguali(importo_ammesso, old_amm) and
                    valori_uguali(importo_non_ammesso, old_non) and
                    valori_uguali(attivita_finanziaria, old_fin) and
                    valori_uguali(attivita_universale, old_univ) and
                    valori_uguali(attivita_residuale, old_res) and
                    valori_uguali(aliquota_iva_calc, old_aliq_calc)
                )
                if cond_valori:
                    logger.debug(f"Dettaglio fattura ID {id_voce} già identico")
                    return True
                
                # altrimenti aggiorno
                u = """
                    UPDATE dettagli_fattura
                    SET imponibile = %s, iva = %s, totale = %s, perc_indetraibilita = %s,
                        id_classe_cespiti = %s, id_em = %s, importo_ammesso = %s, importo_non_ammesso = %s,
                        attivita_finanziaria_indetraibile = %s, attivita_universale_indetraibile = %s,
                        attivita_residuale_detraibile = %s, aliquota_iva_indetraibile_calc = %s
                    WHERE id_voce = %s
                """
                self.cursor.execute(u, (
                    imponibile_nf, iva_ind, totale_nf, perc_ind, id_cl, id_em,
                    importo_ammesso, importo_non_ammesso, attivita_finanziaria,
                    attivita_universale, attivita_residuale, aliquota_iva_calc, id_voce
                ))
                logger.debug(f"Aggiornato dettaglio fattura ID {id_voce}")
                return True

            # 10) Se non esiste, inserisco nuovo dettaglio
            ins = """
                INSERT INTO dettagli_fattura
                    (id_fattura, id_categoria, id_sal, id_bdc, imponibile, iva, totale,
                     perc_indetraibilita, id_classe_cespiti, id_em, importo_ammesso, importo_non_ammesso,
                     attivita_finanziaria_indetraibile, attivita_universale_indetraibile,
                     attivita_residuale_detraibile, aliquota_iva_indetraibile_calc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            self.cursor.execute(ins, (
                id_fattura, id_cat, id_sal, id_bdc, imponibile_nf, iva_ind, totale_nf,
                perc_ind, id_cl, id_em, importo_ammesso, importo_non_ammesso,
                attivita_finanziaria, attivita_universale, attivita_residuale, aliquota_iva_calc
            ))
            logger.debug(f"Inserito nuovo dettaglio fattura per fattura {id_fattura}")
            return True

        except Error as e:
            raise ImportFattureError(f"Errore database nell'inserimento dettaglio fattura {id_fattura}: {e}")

    def inserisci_pagamento_sicuro(self, row, id_fattura):
        """Versione sicura di inserisci_pagamento che solleva eccezioni"""
        # Verifica se esiste un pareggio
        pareggio_sap = row.get('Pareggio (SAP S/4)')
        if pd.isna(pareggio_sap) or not pareggio_sap:
            logger.debug(f"Nessun pareggio SAP per fattura ID {id_fattura}")
            return True  # Pagamento opzionale

        # Estrai tutti i valori necessari
        importo_pagato = self.converti_numero(row.get('Importo pagato'))
        importo_rendicontato = self.converti_numero(row.get('Importo rendicontato'))
        data_pagamento = self.converti_data(row.get('Data regolamento'))
        data_pareggio = self.converti_data(row.get('Data pareggio'))
        lista_pagamento = row.get('Lista di pagamento')
        perc_contrib = self.converti_numero(row.get('% contribuzione'))
        if perc_contrib is not None and perc_contrib > 1:
            perc_contrib = perc_contrib / 100

        pareggio_sap_str = str(pareggio_sap).strip() if pareggio_sap else None

        try:
            # Cerco se esiste già un pagamento
            s = """
                SELECT id_pagamento, importo_pagato, importo_rendicontato, percentuale_contributo, lista_pagamento
                FROM pagamenti
                WHERE id_fattura = %s AND pareggio_sap = %s
            """
            self.cursor.execute(s, (id_fattura, pareggio_sap_str))
            r = self.cursor.fetchone()
            
            if r:
                id_pag, old_ip, old_ir, old_pc, old_lista = r
                
                def valori_uguali(val1, val2, soglia=0.01):
                    if val1 is None and val2 is None:
                        return True
                    if val1 is None or val2 is None:
                        return False
                    if isinstance(val1, str) and isinstance(val2, str):
                        return val1.strip() == val2.strip()
                    return abs(float(val1) - float(val2)) < soglia
                    
                cond = (
                    valori_uguali(importo_pagato, old_ip) and
                    valori_uguali(importo_rendicontato, old_ir) and
                    valori_uguali(perc_contrib, old_pc) and
                    valori_uguali(lista_pagamento, old_lista)
                )
                if cond:
                    logger.debug(f"Pagamento ID {id_pag} già identico")
                    return True
                
                # altrimenti aggiorno
                u = """
                    UPDATE pagamenti
                    SET importo_pagato = %s, importo_rendicontato = %s, data_pagamento = %s,
                        data_pareggio = %s, lista_pagamento = %s, percentuale_contributo = %s
                    WHERE id_pagamento = %s
                """
                self.cursor.execute(u, (
                    importo_pagato, importo_rendicontato, data_pagamento,
                    data_pareggio, lista_pagamento, perc_contrib, id_pag
                ))
                logger.debug(f"Aggiornato pagamento ID {id_pag}")
                return True

            # inserisco nuovo pagamento
            id_sal = self.sal_cache.get(self.numero_sal)
            ins = """
                INSERT INTO pagamenti
                    (id_fattura, id_sal, importo_pagato, importo_rendicontato, data_pagamento, data_pareggio,
                     lista_pagamento, percentuale_contributo, pareggio_sap)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            self.cursor.execute(ins, (
                id_fattura, id_sal, importo_pagato, importo_rendicontato, data_pagamento,
                data_pareggio, lista_pagamento, perc_contrib, pareggio_sap_str
            ))
            id_nuovo_pagamento = self.cursor.lastrowid
            logger.debug(f"Inserito pagamento ID {id_nuovo_pagamento}")
            return True

        except Error as e:
            raise ImportFattureError(f"Errore database nell'inserimento pagamento fattura {id_fattura}: {e}")

    # ── Importazione Foglio SICURA ────────────────────────────────────────────────

    def importa_foglio_excel_sicuro(self, file_path, sheet_name, linea_intervento):
        """Versione sicura dell'importazione foglio con gestione atomica delle righe"""
        try:
            logger.info(f"Inizio importazione SICURA foglio: {sheet_name}")

            # Leggo i dati del foglio
            # Provo a leggere l’intestazione su riga 3 (index 2) e, in seconda battuta, su riga 4 (index 3)
            df = None
            for hdr in (2, 3):
                try:
                    df = pd.read_excel(file_path, sheet_name=sheet_name, header=hdr)
                    # controllo rapido: deve contenere le colonne chiave
                    if {'Numero fattura del fornitore', 'Imponibile fattura', '% indetraibilità dell’IVA (C=A+B)'}.issubset(df.columns):
                        logger.info(f"Lettura foglio '{sheet_name}' con header in riga {hdr+1}")
                        break
                except Exception:
                    continue

            if df is None or df.empty:
                logger.error(f"Impossibile leggere '{sheet_name}' con header 2 o 3")
                return False

            logger.info(f"Colonne caricate: {len(df.columns)}")

            # Rimuovo righe completamente vuote
            df = df.dropna(how='all')
            df.columns = df.columns.str.replace(r'\s+', ' ', regex=True).str.strip()

            # Crea mapping flessibile
            mapping_flessibile = self.crea_mapping_colonne_flessibile()
            
            # Trova le colonne effettive nel dataframe
            column_mapping = {}
            for chiave_standard, nomi_possibili in mapping_flessibile.items():
                colonna_trovata = self.trova_colonna_nel_df(df, nomi_possibili)
                column_mapping[chiave_standard] = colonna_trovata
                if colonna_trovata:
                    logger.debug(f"Mappata colonna '{chiave_standard}' -> '{colonna_trovata}'")

            logger.info(f"Caricate {len(df)} righe di dati dal foglio {sheet_name}")

            totale_righe = len(df)
            righe_processate = 0
            righe_con_errori = 0

            # Processa ogni riga con importazione atomica
            for idx, row in df.iterrows():
                numero_riga_excel = idx + 4  # +4 perché header è alla riga 3
                
                try:
                    # Prepara i dati della riga
                    row_data = {}
                    for key, mapped in column_mapping.items():
                        if mapped and mapped in row.index:
                            row_data[key] = row[mapped]
                        else:
                            row_data[key] = None

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

    # ── Metodi di supporto (mantenuti uguali) ──────────────────────────────────────

    def trova_colonna_nel_df(self, df, nomi_possibili):
        """Trova una colonna nel dataframe usando una lista di nomi possibili."""
        for nome in nomi_possibili:
            nome_norm = self.normalizza_nome_colonna(nome)
            for df_col in df.columns:
                if self.normalizza_nome_colonna(df_col) == nome_norm:
                    return df_col
        return None
        
    def crea_mapping_colonne_flessibile(self):
        """Crea un mapping flessibile che gestisce nomi diversi tra i fogli."""
        return {
            # Colonne con nomi identici
            'SAL': ['SAL'],
            'Periodo riferimento': ['Periodo riferimento'],
            'Categoria di spesa': ['Categoria di spesa'],
            'Accordo Quadro': ['Accordo Quadro'],
            'Buono di consegna (BdC)': ['Buono di consegna (BdC)'],
            'Posizione riferimento (BdC)': ['Posizione riferimento (BdC)'],
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
            
            # Colonne con nomi diversi tra i fogli
            'Numero fattura del fornitore': [
                'Numero fattura del fornitore',
                'Riferimento fattura',
                'Numero fattura',
                'Numero fattura del fornitore'
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
            
            # Colonne opzionali
            'Classe cespiti': ['Classe cespiti', 'classe cespiti'],
            "%IVA INDETRAIBILE": ["%IVA INDETRAIBILE"],
            "% indetraibilità dell'IVA (C=A+B)": [
                "% indetraibilità dell'IVA (C=A+B)",
                "% indetraibilità dell'IVA \n(C=A+B)"
            ],
            
            # Colonne DSAN
            "ATTIVITA' FINANZIARIA (indetraibile) (A)": ["ATTIVITA' FINANZIARIA (indetraibile) (A)"],
            "ATTIVITA'  UNIVERSALE (indetraibile) (B)": ["ATTIVITA'  UNIVERSALE (indetraibile) (B)"],
            "ATTIVITA' RESIDUALE (detraibile)": ["ATTIVITA' RESIDUALE\n (detraibile)", "ATTIVITA' RESIDUALE (detraibile)"],
            "Aliquota IVA indetraibile (C*22%)": ["Aliquota IVA indetraibile\n (C*22%)", "Aliquota IVA indetraibile (C*22%)"]
        }

    # ── Importazione Excel SICURA ─────────────────────────────────────────────────

    def importa_excel_sicuro(self, file_path, numero_sal=None):
        """Versione sicura dell'importazione Excel con gestione atomica completa"""
        try:
            logger.info(f"Inizio importazione SICURA file: {file_path}")
            
            # Imposta il numero_sal per questa importazione
            self.numero_sal = numero_sal if numero_sal else 7

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

            # Definisci fogli da importare
            fogli_mapping_specifico = {
               5: [
                   {'sheet_name': 'report SPORTUNICO',    'linea_intervento': 'report SPORTELLOUNICO'},
                   {'sheet_name': 'report SPAZIITA',      'linea_intervento': 'report SPAZIITALIA'}
               ],
               6: [
                   {'sheet_name': 'report SPORTUNICO',    'linea_intervento': 'report SPORTELLOUNICO'},
                   {'sheet_name': 'report SPAZIITA',      'linea_intervento': 'report SPAZIITALIA'}
               ],
               7: [
                   {'sheet_name': 'report SPORTELLOUNICO','linea_intervento': 'report SPORTELLOUNICO'},
                   {'sheet_name': 'report SPAZIITALIA',   'linea_intervento': 'report SPAZIITALIA'}
               ]
            }
            
            fogli_da_importare = fogli_mapping_specifico.get(self.numero_sal, [])
            
            # Fallback generico se non trova fogli specifici
            if not any(f['sheet_name'] in excel_file.sheet_names for f in fogli_da_importare):
                logger.info("Mapping specifico non trovato, provo ricerca generica...")
                fogli_da_importare = []
                
                patterns = [
                    ('SPORTUNICO', 'report SPORTUNICO'),
                    ('SPORTELLOUNICO', 'report SPORTELLOUNICO'),
                    ('SPAZIITA', 'report SPAZIITA'),
                    ('SPAZIITALIA', 'report SPAZIITALIA')
                ]
                
                for sheet in excel_file.sheet_names:
                    sheet_upper = sheet.upper()
                    for pattern, linea in patterns:
                        if pattern in sheet_upper:
                            fogli_da_importare.append({
                                'sheet_name': sheet,
                                'linea_intervento': linea
                            })
                            break

            # Controllo esistenza fogli
            fogli_esistenti = []
            for foglio in fogli_da_importare:
                if foglio['sheet_name'] in excel_file.sheet_names:
                    fogli_esistenti.append(foglio)
                    logger.info(f"Foglio trovato: {foglio['sheet_name']}")
                else:
                    logger.warning(f"Foglio non trovato: {foglio['sheet_name']}")

            if not fogli_esistenti:
                logger.error("Nessun foglio da importare trovato nel file Excel")
                return False

            # Importa ogni foglio con il metodo sicuro
            totale_righe_processate = 0
            totale_righe_con_errori = 0

            for foglio in fogli_esistenti:
                logger.info("=" * 60)
                logger.info(f"IMPORTAZIONE SICURA FOGLIO: {foglio['sheet_name']}")
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
            logger.info("IMPORTAZIONE SICURA COMPLETATA")
            logger.info(f"SAL importato: {self.numero_sal}")
            logger.info(f"Fogli importati: {len(fogli_esistenti)}")
            logger.info(f"Totale righe processate con successo: {totale_righe_processate}")
            logger.info(f"Totale righe con errori: {totale_righe_con_errori}")
            logger.info(f"Tasso di successo: {(totale_righe_processate/(totale_righe_processate+totale_righe_con_errori)*100):.2f}%" if (totale_righe_processate+totale_righe_con_errori) > 0 else "N/A")
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
                        logger.info(f"{tipo_errore}: {conteggio} totali ({da_correggere} da correggere, {corretti} corretti)")
            
            return True

        except Exception as e:
            logger.error(f"Errore generale nell'importazione sicura: {e}")
            self.connection.rollback()
            
            # Marca la sessione come fallita
            if self.error_handler:
                self.error_handler.aggiorna_sessione(stato_import='fallito')
            
            return False

# ─── MAIN SICURO ───────────────────────────────────────────────────────────────
def main():
    DB_CONFIG = {
        'host': '172.29.0.49',
        'port': 3306,
        'user': 'root',
        'password': os.getenv('DB_PASSWORD', ''),
        'database': 'polis'
    }

    # Parser per gli argomenti
    parser = argparse.ArgumentParser(description='Importa fatture da Excel nel database (VERSIONE SICURA)')
    parser.add_argument('--sal', type=int, help='Numero del SAL da importare (dedotto dal nome file se non specificato)')
    parser.add_argument('--file', type=str, help='Path del file Excel da importare')
    parser.add_argument('--sicuro', action='store_true', default=True, help='Usa il metodo di importazione sicuro (default)')
    args = parser.parse_args()

    # Determinazione del file da importare
    if args.file:
        EXCEL_FILE = args.file
    else:
        # File di default basati sul SAL
        if args.sal:
            file_mapping = {
                5: 'Riepilogo analitico spese sostenute 5 SAL.xlsx',
                7: 'Riepilogo analitico spese sostenute 7 SAL.xlsx'
            }
            EXCEL_FILE = file_mapping.get(args.sal)
        else:
            # Cerca un file che corrisponde al pattern
            possible_files = [
                'Riepilogo analitico spese sostenute 5 SAL.xlsx',
                'Riepilogo analitico spese sostenute 7 SAL.xlsx'
            ]
            for fn in possible_files:
                if os.path.exists(fn):
                    EXCEL_FILE = fn
                    break
            else:
                EXCEL_FILE = None
    
    if not EXCEL_FILE or not os.path.exists(EXCEL_FILE):
        logger.error(f"File Excel non trovato: {EXCEL_FILE}")
        logger.error("Uso: python import_fatture_sicuro.py [--sal N] [--file path_file_excel]")
        return

    # Deduce il numero SAL dal nome del file se non specificato
    numero_sal = args.sal
    if not numero_sal:
        # Cerca pattern nel nome del file: "... N SAL..."
        import re
        match = re.search(r'(\d+)\s*SAL', EXCEL_FILE, re.IGNORECASE)
        if match:
            numero_sal = int(match.group(1))
            logger.info(f"Numero SAL dedotto dal nome del file: {numero_sal}")
        else:
            logger.error("Impossibile dedurre il numero SAL dal nome del file. Specificare con --sal N")
            return
    
    logger.info("=" * 80)
    logger.info("AVVIO IMPORTAZIONE SICURA FATTURE")
    logger.info(f"File: {EXCEL_FILE}")
    logger.info(f"SAL: {numero_sal}")
    logger.info("Modalità: SICURA (rollback automatico per righe con errori)")
    logger.info("=" * 80)

    importatore = ImportatoreFatture(**DB_CONFIG)
    try:
        if not importatore.connetti_db():
            logger.error("Impossibile connettersi al database")
            return
        importatore.carica_cache()

        # Verifico esistenza SAL, altrimenti lo creo
        if numero_sal not in importatore.sal_cache:
            logger.info(f"Inserimento SAL {numero_sal} nel database")
            
            # Date basate sul SAL
            date_mapping = {
                5: ('2024-01-01', '2024-04-30'),
                7: ('2024-09-01', '2024-12-31')
            }
            data_inizio, data_fine = date_mapping.get(numero_sal, ('2024-01-01', '2024-12-31'))
            
            q = """INSERT INTO sal (numero_sal, data_inizio, data_fine) 
                   VALUES (%s, %s, %s)"""
            importatore.cursor.execute(q, (numero_sal, data_inizio, data_fine))
            importatore.connection.commit()
            importatore.carica_cache()

        # IMPORTAZIONE SICURA
        success = importatore.importa_excel_sicuro(EXCEL_FILE, numero_sal=numero_sal)
        
        if success:
            logger.info("✅ IMPORTAZIONE SICURA COMPLETATA CON SUCCESSO")
        else:
            logger.error("❌ IMPORTAZIONE SICURA FALLITA")

    except Exception as e:
        logger.error(f"Errore generale: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        importatore.chiudi_connessione()

if __name__ == "__main__":
    main()