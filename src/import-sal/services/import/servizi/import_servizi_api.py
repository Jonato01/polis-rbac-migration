import pandas as pd
import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime
import os
import sys
import requests
import base64
from io import BytesIO
import argparse
import re
from dotenv import load_dotenv

# Carica variabili d'ambiente
load_dotenv()

# ─── Configurazione logging ───────────────────────────────────────────────────
def setup_logging():
    """Configura il sistema di logging su file e console"""
    if not os.path.exists('logs'):
        os.makedirs('logs')
    log_filename = f'logs/import_servizi_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = logging.getLogger('ImportServizi')
    logger.setLevel(logging.DEBUG)
    
    fh = logging.FileHandler(log_filename, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

logger = setup_logging()

# ─── Classe ImportatoreServiziAPI ─────────────────────────────────────────────
class ImportatoreServiziAPI:
    def __init__(self, host, port, user, password, database, dry_run=False):
        """Inizializza la connessione al database e le cache"""
        self.connection = None
        self.cursor = None
        self.dry_run = dry_run
        self.db_config = {
            'host': host,
            'port': port,
            'user': user,
            'password': password,
            'database': database
        }
        self.api_base_url = "https://polis-coll-api-storage-dec5dpfhhzdrehck.northeurope-01.azurewebsites.net/api"
        
        # Mapping SAL -> mesi del periodo (quadrimestri ciclici)
        self.sal_periodi = {
            3: [5, 6, 7, 8],      # Maggio-Agosto 2023
            4: [9, 10, 11, 12],   # Settembre-Dicembre 2023
            5: [1, 2, 3, 4],      # Gennaio-Aprile 2024
            6: [5, 6, 7, 8],      # Maggio-Agosto 2024
            7: [9, 10, 11, 12],   # Settembre-Dicembre 2024
            8: [1, 2, 3, 4],      # Gennaio-Aprile 2025
            9: [5, 6, 7, 8],      # Maggio-Agosto 2025
            10: [9, 10, 11, 12],  # Settembre-Dicembre 2025
        }

        # Mapping SAL -> anno di riferimento
        self.sal_anno = {
            3: 2023, 4: 2023,
            5: 2024, 6: 2024, 7: 2024,
            8: 2025, 9: 2025, 10: 2025,
        }
        
        # Cache per lookup rapido
        self.sal_cache = {}
        self.servizi_cache = {}
        
        # Statistiche
        self.stats = {
            'servizi_nuovi': 0,
            'servizi_esistenti': 0,
            'dati_inseriti': 0,
            'dati_aggiornati': 0,
            'dati_saltati': 0
        }

    def connetti_db(self):
        """Stabilisce la connessione al database"""
        if self.dry_run:
            logger.info("[DRY-RUN] Simulazione connessione al database")
            return True
            
        try:
            self.connection = mysql.connector.connect(**self.db_config)
            self.cursor = self.connection.cursor(buffered=True)
            logger.info(f"Connessione al database {self.db_config['database']} stabilita")
            return True
        except Error as e:
            logger.error(f"Errore nella connessione al database: {e}")
            return False

    def chiudi_connessione(self):
        """Chiude cursore e connessione"""
        if self.dry_run:
            logger.info("[DRY-RUN] Simulazione chiusura connessione")
            return
            
        try:
            if self.cursor:
                self.cursor.close()
            if self.connection and self.connection.is_connected():
                self.connection.close()
                logger.info("Connessione al database chiusa")
        except Error as e:
            logger.error(f"Errore chiudendo la connessione: {e}")

    def carica_cache(self):
        """Carica in memoria le tabelle di lookup"""
        if self.dry_run:
            logger.info("[DRY-RUN] Simulazione caricamento cache")
            # Cache fittizia per dry-run
            self.sal_cache = {i: i for i in range(3, 11)}
            return
            
        try:
            self.cursor.execute("SELECT id_sal, numero_sal FROM sal")
            for id_s, num in self.cursor.fetchall():
                self.sal_cache[num] = id_s

            self.cursor.execute("SELECT id_servizio, descrizione, canale, id_sal FROM servizi")
            for id_serv, desc, canale, id_sal in self.cursor.fetchall():
                chiave = f"{desc}_{canale}_{id_sal}"
                self.servizi_cache[chiave] = id_serv

            logger.info("Cache delle tabelle caricata con successo")
        except Error as e:
            logger.warning(f"Errore nel caricamento cache: {e}")

    def cerca_file_sal(self, sal_path, query="Report Servizi"):
        """Cerca il file Report Servizi tramite API unified search"""
        try:
            return '9_SAL_31_08_2025/2025_9° SAL Avanzamento tecnico/Report Servizi Polis_mag_ago 25.xlsx'
            url = f"{self.api_base_url}/search/unified"
            params = {
                'q': query,
                'search_type': 'files',
                'path': sal_path,
                'root_only': 'false',
                'exact': 'true',
                'limit': 100
            }
            
            logger.info(f"Ricerca file in {sal_path}...")
            response = requests.get(url, params=params, headers={'accept': 'application/json'})
            response.raise_for_status()
            
            data = response.json()
            
            if not data.get('success') or not data.get('results'):
                logger.error(f"Nessun file trovato per query '{query}' in {sal_path}")
                return None
            
            file_path = data['results'][0]['path']
            logger.info(f"File trovato: {file_path}")
            return file_path
            
        except Exception as e:
            logger.error(f"Errore nella ricerca file: {e}")
            return None

    def scarica_file_excel(self, file_path):
        """Scarica il file Excel tramite API download_base64"""
        try:
            url = f"{self.api_base_url}/download_base64/{file_path}"
            
            logger.info(f"Download file {file_path}...")
            response = requests.get(url, headers={'accept': 'application/json'})
            response.raise_for_status()
            
            data = response.json()
            base64_content = data.get('base64')
            
            if not base64_content:
                logger.error("Contenuto base64 non trovato nella risposta")
                return None
            
            # Decodifica base64 e carica in BytesIO
            excel_bytes = base64.b64decode(base64_content)
            excel_buffer = BytesIO(excel_bytes)
            
            logger.info("File scaricato e decodificato con successo")
            return excel_buffer
            
        except Exception as e:
            logger.error(f"Errore nel download file: {e}")
            return None

    def rileva_mesi_da_excel(self, df, numero_sal):
        """Rileva automaticamente i mesi dalle colonne, filtrando per periodo SAL"""
        mapping_mesi = {
            'gennaio': 1, 'gen': 1,
            'febbraio': 2, 'feb': 2,
            'marzo': 3, 'mar': 3,
            'aprile': 4, 'apr': 4,
            'maggio': 5, 'mag': 5,
            'giugno': 6, 'giu': 6,
            'luglio': 7, 'lug': 7,
            'agosto': 8, 'ago': 8,
            'settembre': 9, 'sett': 9, 'set': 9,
            'ottobre': 10, 'ott': 10,
            'novembre': 11, 'nov': 11,
            'dicembre': 12, 'dic': 12
        }
        
        # Ottieni i mesi del periodo SAL
        mesi_periodo = self.sal_periodi.get(numero_sal)
        if not mesi_periodo:
            logger.error(f"SAL {numero_sal} non ha un periodo definito")
            return {}
        
        logger.info(f"Mesi del periodo SAL {numero_sal}: {mesi_periodo}")
        
        mesi_trovati = {}
        colonne = list(df.columns)
        
        # Cerca nelle colonne dalla 4 in poi (dopo Descrizione, Canale, Misura)
        for idx, col_name in enumerate(colonne[4:], start=4):
            col_lower = str(col_name).lower()
            
            # Salta colonne duplicate (.1, .2, ecc)
            if '.' in str(col_name) and any(char.isdigit() for char in str(col_name).split('.')[-1]):
                continue
            
            for mese_nome, numero in mapping_mesi.items():
                if mese_nome in col_lower:
                    # Importa solo se il mese è nel periodo del SAL
                    if numero in mesi_periodo:
                        mesi_trovati[col_name] = numero
                        logger.info(f"[OK] Colonna '{col_name}' -> Mese {numero} (periodo SAL)")
                    else:
                        logger.debug(f"[--] Colonna '{col_name}' -> Mese {numero} (fuori periodo SAL, ignorata)")
                    break
        
        if not mesi_trovati:
            logger.warning("Nessun mese del periodo SAL trovato")
        else:
            logger.info(f"Totale mesi da importare: {len(mesi_trovati)}")
        
        return mesi_trovati

    def converti_numero(self, valore):
        """Converte un numero (formato italiano) in float"""
        if pd.isna(valore) or valore == '' or str(valore).strip() == '':
            return None
        if isinstance(valore, (int, float)):
            return float(valore)
        
        s = str(valore).strip().replace('.', '').replace(',', '.')
        try:
            return float(s)
        except:
            logger.warning(f"Impossibile convertire: {valore}")
            return None

    def inserisci_o_recupera_servizio(self, descrizione, canale, id_sal, anno=2024):
        """Inserisce un nuovo servizio o restituisce l'ID esistente"""
        if not descrizione:
            return None
        
        chiave = f"{descrizione}_{canale}_{id_sal}"
        
        # Controlla cache
        if chiave in self.servizi_cache:
            self.stats['servizi_esistenti'] += 1
            return self.servizi_cache[chiave]
        
        if self.dry_run:
            fake_id = len(self.servizi_cache) + 1
            self.servizi_cache[chiave] = fake_id
            self.stats['servizi_nuovi'] += 1
            logger.info(f"[DRY-RUN] Nuovo servizio: {descrizione} - {canale} (ID simulato: {fake_id})")
            return fake_id
        
        try:
            query = "SELECT id_servizio FROM servizi WHERE descrizione = %s AND canale = %s AND id_sal = %s"
            self.cursor.execute(query, (descrizione, canale, id_sal))
            result = self.cursor.fetchone()
            
            if result:
                id_servizio = result[0]
                self.servizi_cache[chiave] = id_servizio
                self.stats['servizi_esistenti'] += 1
                return id_servizio
            
            query = "INSERT INTO servizi (descrizione, canale, id_sal, anno) VALUES (%s, %s, %s, %s)"
            self.cursor.execute(query, (descrizione, canale, id_sal, anno))
            id_servizio = self.cursor.lastrowid
            
            self.servizi_cache[chiave] = id_servizio
            self.stats['servizi_nuovi'] += 1
            logger.debug(f"Inserito servizio: {descrizione} (ID: {id_servizio})")
            return id_servizio
            
        except Error as e:
            logger.error(f"Errore inserimento servizio {descrizione}: {e}")
            return None

    def inserisci_servizio_mensile(self, id_servizio, mese, quantita, lordo_iva):
        """Inserisce o aggiorna i dati mensili di un servizio"""
        if not id_servizio or mese is None or quantita is None or lordo_iva is None:
            return False
        
        if quantita == 0 and lordo_iva == 0:
            return True
        
        if self.dry_run:
            self.stats['dati_inseriti'] += 1
            logger.debug(f"[DRY-RUN] Dati mensili: servizio {id_servizio}, mese {mese}, qty={quantita}, €{lordo_iva:.2f}")
            return True
        
        try:
            query = "SELECT id_mensile, quantita, lordo_iva FROM servizi_mensile WHERE id_servizio = %s AND mese = %s"
            self.cursor.execute(query, (id_servizio, mese))
            result = self.cursor.fetchone()
            
            if result:
                id_mensile, old_quantita, old_lordo = result
                
                if abs(float(old_quantita) - quantita) < 0.01 and abs(float(old_lordo) - lordo_iva) < 0.01:
                    self.stats['dati_saltati'] += 1
                    return True
                
                query = "UPDATE servizi_mensile SET quantita = %s, lordo_iva = %s WHERE id_mensile = %s"
                self.cursor.execute(query, (quantita, lordo_iva, id_mensile))
                self.stats['dati_aggiornati'] += 1
                return True
            
            query = "INSERT INTO servizi_mensile (id_servizio, mese, quantita, lordo_iva) VALUES (%s, %s, %s, %s)"
            self.cursor.execute(query, (id_servizio, mese, quantita, lordo_iva))
            self.stats['dati_inseriti'] += 1
            return True
            
        except Error as e:
            logger.error(f"Errore inserimento dati mensili: {e}")
            return False

    def importa_da_sal(self, numero_sal, sal_path, anno=2024):
        """Importa servizi da un SAL specifico tramite API"""
        try:
            logger.info("=" * 70)
            logger.info(f"IMPORTAZIONE SAL {numero_sal} - Path: {sal_path}")
            if self.dry_run:
                logger.info("[DRY-RUN MODE] Nessuna modifica verrà salvata nel database")
            logger.info("=" * 70)
            
            # Verifica esistenza SAL
            id_sal = self.sal_cache.get(numero_sal)
            if not id_sal:
                logger.error(f"SAL {numero_sal} non trovato nel database")
                return False
            
            # Cerca file tramite API
            file_path = self.cerca_file_sal(sal_path)
            if not file_path:
                return False
            
            # Scarica file Excel
            excel_buffer = self.scarica_file_excel(file_path)
            if not excel_buffer:
                return False
            
            # Leggi Excel
            df = pd.read_excel(excel_buffer, header=6)
            
            if df.empty:
                logger.error("File Excel vuoto")
                return False
            
            logger.info(f"Caricate {len(df)} righe dall'Excel")
            
            # Rileva mesi automaticamente per il periodo SAL
            mesi_mappati = self.rileva_mesi_da_excel(df, numero_sal)
            
            if not mesi_mappati:
                logger.error("Impossibile rilevare i mesi dalle colonne")
                return False
            
            # Rimuovi righe vuote
            df = df.dropna(how='all')
            
            # Processa ogni riga
            for idx, row in df.iterrows():
                try:
                    if len(row) < 4:
                        continue
                    
                    descrizione = str(row.iloc[1]) if pd.notna(row.iloc[1]) else ""
                    canale = str(row.iloc[2]) if pd.notna(row.iloc[2]) else ""
                    tipo_misura = str(row.iloc[3]) if pd.notna(row.iloc[3]) else ""
                    
                    if not descrizione or descrizione == 'nan' or len(descrizione) < 5:
                        continue
                    
                    if 'quantità' not in tipo_misura.lower() and 'quantita' not in tipo_misura.lower():
                        continue
                    
                    descrizione_pulita = descrizione.strip()
                    canale_pulito = canale.upper() if canale.upper() in ['SPORTELLO', 'TOTEM'] else 'SPORTELLO'
                    
                    id_servizio = self.inserisci_o_recupera_servizio(descrizione_pulita, canale_pulito, id_sal, anno)
                    if not id_servizio:
                        continue
                    
                    # Processa dati mensili
                    for col_name, mese_num in mesi_mappati.items():
                        if col_name not in row.index:
                            continue
                        
                        valore_numerico = self.converti_numero(row[col_name])
                        
                        if valore_numerico is not None and valore_numerico > 0:
                            self.inserisci_servizio_mensile(id_servizio, mese_num, valore_numerico, valore_numerico)
                    
                except Exception as e:
                    logger.error(f"Errore riga {idx}: {e}")
                    continue
            
            # Commit finale (solo se non dry-run)
            if not self.dry_run:
                self.connection.commit()
                logger.info("Modifiche committate nel database")
            
            # Stampa statistiche
            self.stampa_statistiche()
            
            return True
            
        except Exception as e:
            logger.error(f"Errore generale importazione: {e}")
            import traceback
            traceback.print_exc()
            if not self.dry_run and self.connection:
                self.connection.rollback()
            return False

    def importa_da_file_locale(self, file_path, numero_sal, anno):
        """Importa servizi da un file Excel locale"""
        try:
            logger.info("=" * 70)
            logger.info(f"IMPORTAZIONE SAL {numero_sal} - File: {os.path.basename(file_path)} - Anno: {anno}")
            if self.dry_run:
                logger.info("[DRY-RUN MODE] Nessuna modifica verra' salvata nel database")
            logger.info("=" * 70)

            # Verifica esistenza SAL
            id_sal = self.sal_cache.get(numero_sal)
            if not id_sal:
                logger.error(f"SAL {numero_sal} non trovato nel database")
                return False

            # Leggi Excel locale
            df = pd.read_excel(file_path, header=6)

            if df.empty:
                logger.error("File Excel vuoto")
                return False

            logger.info(f"Caricate {len(df)} righe dall'Excel")

            # Rileva mesi automaticamente per il periodo SAL
            mesi_mappati = self.rileva_mesi_da_excel(df, numero_sal)

            if not mesi_mappati:
                logger.error("Impossibile rilevare i mesi dalle colonne")
                return False

            # Rimuovi righe vuote
            df = df.dropna(how='all')

            # Processa ogni riga
            for idx, row in df.iterrows():
                try:
                    if len(row) < 4:
                        continue

                    descrizione = str(row.iloc[1]) if pd.notna(row.iloc[1]) else ""
                    canale = str(row.iloc[2]) if pd.notna(row.iloc[2]) else ""
                    tipo_misura = str(row.iloc[3]) if pd.notna(row.iloc[3]) else ""

                    if not descrizione or descrizione == 'nan' or len(descrizione) < 5:
                        continue

                    if 'quantità' not in tipo_misura.lower() and 'quantita' not in tipo_misura.lower():
                        continue

                    descrizione_pulita = descrizione.strip()
                    canale_pulito = canale.upper() if canale.upper() in ['SPORTELLO', 'TOTEM'] else 'SPORTELLO'

                    id_servizio = self.inserisci_o_recupera_servizio(descrizione_pulita, canale_pulito, id_sal, anno)
                    if not id_servizio:
                        continue

                    # Processa dati mensili
                    for col_name, mese_num in mesi_mappati.items():
                        if col_name not in row.index:
                            continue

                        valore_numerico = self.converti_numero(row[col_name])

                        if valore_numerico is not None and valore_numerico > 0:
                            self.inserisci_servizio_mensile(id_servizio, mese_num, valore_numerico, valore_numerico)

                except Exception as e:
                    logger.error(f"Errore riga {idx}: {e}")
                    continue

            # Commit finale (solo se non dry-run)
            if not self.dry_run:
                self.connection.commit()
                logger.info("Modifiche committate nel database")

            return True

        except Exception as e:
            logger.error(f"Errore importazione file {file_path}: {e}")
            import traceback
            traceback.print_exc()
            if not self.dry_run and self.connection:
                self.connection.rollback()
            return False

    def importa_tutti_da_cartella(self, cartella):
        """Importa tutti i file Excel Report Servizi dalla cartella"""
        if not os.path.isdir(cartella):
            logger.error(f"Cartella non trovata: {cartella}")
            return False

        files = sorted(os.listdir(cartella))
        excel_files = [f for f in files if f.endswith('.xlsx') and not f.startswith('~$')]

        if not excel_files:
            logger.error(f"Nessun file Excel trovato in {cartella}")
            return False

        logger.info(f"Trovati {len(excel_files)} file Excel in {cartella}")

        risultati = {}
        for fname in excel_files:
            # Estrai numero SAL dal nome file (es. "Report Servizi Polis 3 sal.xlsx" -> 3)
            match = re.search(r'(\d+)\s*sal', fname, re.IGNORECASE)
            if not match:
                logger.warning(f"Impossibile estrarre numero SAL da: {fname}, skip")
                continue

            numero_sal = int(match.group(1))
            anno = self.sal_anno.get(numero_sal, 2024)
            file_path = os.path.join(cartella, fname)

            logger.info(f"\n>>> Elaborazione: {fname} (SAL {numero_sal}, anno {anno})")

            # Reset statistiche per ogni file
            self.stats = {
                'servizi_nuovi': 0,
                'servizi_esistenti': 0,
                'dati_inseriti': 0,
                'dati_aggiornati': 0,
                'dati_saltati': 0
            }

            ok = self.importa_da_file_locale(file_path, numero_sal, anno)
            risultati[numero_sal] = ok

            # Stampa statistiche per questo SAL
            self.stampa_statistiche()

        # Riepilogo finale
        logger.info("\n" + "=" * 70)
        logger.info("RIEPILOGO IMPORTAZIONE BATCH")
        logger.info("=" * 70)
        for sal_num, esito in sorted(risultati.items()):
            stato = "OK" if esito else "ERRORE"
            logger.info(f"  SAL {sal_num}: {stato}")
        logger.info("=" * 70)

        return all(risultati.values())

    def stampa_statistiche(self):
        """Stampa le statistiche finali"""
        logger.info("=" * 70)
        logger.info("STATISTICHE IMPORTAZIONE")
        logger.info("=" * 70)
        logger.info(f"Servizi nuovi:        {self.stats['servizi_nuovi']}")
        logger.info(f"Servizi esistenti:    {self.stats['servizi_esistenti']}")
        logger.info(f"Dati inseriti:        {self.stats['dati_inseriti']}")
        logger.info(f"Dati aggiornati:      {self.stats['dati_aggiornati']}")
        logger.info(f"Dati saltati:         {self.stats['dati_saltati']}")
        logger.info("=" * 70)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Importa servizi da file Excel locali o da API Azure Storage')
    parser.add_argument('--cartella', type=str, help='Cartella con i file Excel dei servizi (importa tutti)')
    parser.add_argument('--file', type=str, help='Singolo file Excel da importare')
    parser.add_argument('--sal', type=int, help='Numero SAL (obbligatorio con --file)')
    parser.add_argument('--dry-run', action='store_true', help='Esegue senza salvare nel database')
    parser.add_argument('--anno', type=int, default=None, help='Anno di riferimento (auto-detect se non specificato)')

    args = parser.parse_args()

    if not args.cartella and not args.file:
        parser.error("Specificare --cartella o --file")

    if args.file and not args.sal:
        parser.error("--sal e' obbligatorio quando si usa --file")

    # Configurazione database
    DB_CONFIG = {
        'host': 'polis-coll-mysql.mysql.database.azure.com',
        'port': 3306,
        'user': 'polisammin',
        'password': r'9DfAv\Y#EK#)x1MBn4Oy',
        'database': 'polis'
    }

    logger.info(f"Configurazione database: {DB_CONFIG['host']} - Database: {DB_CONFIG['database']}")

    importatore = ImportatoreServiziAPI(**DB_CONFIG, dry_run=args.dry_run)

    try:
        if not importatore.connetti_db():
            logger.error("Impossibile connettersi al database")
            return

        importatore.carica_cache()

        if args.cartella:
            importatore.importa_tutti_da_cartella(args.cartella)
        elif args.file:
            anno = args.anno or importatore.sal_anno.get(args.sal, 2024)
            importatore.importa_da_file_locale(args.file, args.sal, anno)
            importatore.stampa_statistiche()

    except Exception as e:
        logger.error(f"Errore generale: {e}")
        import traceback
        traceback.print_exc()
    finally:
        importatore.chiudi_connessione()

if __name__ == "__main__":
    main()