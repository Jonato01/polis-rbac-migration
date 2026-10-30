import pandas as pd
import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime
import os
import sys
import re

# ─── Configurazione logging ───────────────────────────────────────────────────
def setup_logging():
    """Configura il sistema di logging su file e console"""
    if not os.path.exists('logs'):
        os.makedirs('logs')
    log_filename = f'logs/import_servizi_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = logging.getLogger('ImportServizi')
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

# ─── Classe ImportatoreServizi ─────────────────────────────────────────────────
class ImportatoreServizi:
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
        self.sal_cache = {}
        self.servizi_cache = {}  # cache per evitare duplicati

    def connetti_db(self):
        """Stabilisce la connessione al database (cursor buffered)."""
        try:
            self.connection = mysql.connector.connect(**self.db_config)
            self.cursor = self.connection.cursor(buffered=True)
            logger.info(f"Connessione al database {self.db_config['database']} stabilita con successo")
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
            # SAL
            self.cursor.execute("SELECT id_sal, numero_sal FROM sal")
            for id_s, num in self.cursor.fetchall():
                self.sal_cache[num] = id_s

            # Servizi esistenti
            self.cursor.execute("SELECT id_servizio, descrizione, canale, id_sal FROM servizi")
            for id_serv, desc, canale, id_sal in self.cursor.fetchall():
                chiave = f"{desc}_{canale}_{id_sal}"
                self.servizi_cache[chiave] = id_serv

            logger.info("Cache delle tabelle caricata con successo")
        except Error as e:
            logger.warning(f"[Cache] Errore nel caricamento cache: {e}")

    def converti_numero(self, valore):
        """Converte un numero (formato italiano) in float."""
        if pd.isna(valore) or valore == '' or str(valore).strip() == '':
            return None
        if isinstance(valore, (int, float)):
            return float(valore)
        
        # Gestione stringhe: rimuove punti (migliaia) e converte virgola in punto
        s = str(valore).strip().replace('.', '').replace(',', '.')
        try:
            return float(s)
        except:
            logger.warning(f"Impossibile convertire il numero: {valore}")
            return None

    def inserisci_o_recupera_servizio(self, descrizione, canale, id_sal, anno=2024):
        """Inserisce un nuovo servizio o restituisce l'ID esistente."""
        if not descrizione:
            return None
        
        # Crea chiave per cache
        chiave = f"{descrizione}_{canale}_{id_sal}"
        
        # Controlla cache
        if chiave in self.servizi_cache:
            return self.servizi_cache[chiave]
        
        try:
            # Verifica se esiste già
            query = """SELECT id_servizio FROM servizi 
                      WHERE descrizione = %s AND canale = %s AND id_sal = %s"""
            self.cursor.execute(query, (descrizione, canale, id_sal))
            result = self.cursor.fetchone()
            
            if result:
                id_servizio = result[0]
                self.servizi_cache[chiave] = id_servizio
                return id_servizio
            
            # Inserisci nuovo servizio
            query = """INSERT INTO servizi (descrizione, canale, id_sal, anno) 
                      VALUES (%s, %s, %s, %s)"""
            self.cursor.execute(query, (descrizione, canale, id_sal, anno))
            id_servizio = self.cursor.lastrowid
            
            self.servizi_cache[chiave] = id_servizio
            logger.debug(f"Inserito nuovo servizio: {descrizione} - {canale} (ID: {id_servizio})")
            return id_servizio
            
        except Error as e:
            logger.error(f"Errore nell'inserimento servizio {descrizione}: {e}")
            return None

    def inserisci_servizio_mensile(self, id_servizio, mese, quantita, lordo_iva):
        """Inserisce o aggiorna i dati mensili di un servizio."""
        if not id_servizio or mese is None or quantita is None or lordo_iva is None:
            print(f"❌ PARAMETRI MANCANTI: id_servizio={id_servizio}, mese={mese}, quantita={quantita}, lordo_iva={lordo_iva}")
            return False
        
        # Se quantita o lordo_iva sono 0, non inserire
        if quantita == 0 and lordo_iva == 0:
            print(f"⚠️ VALORI ZERO: quantita={quantita}, lordo_iva={lordo_iva}")
            return True
        
        try:
            # Verifica se esiste già
            query = """SELECT id_mensile, quantita, lordo_iva FROM servizi_mensile 
                      WHERE id_servizio = %s AND mese = %s"""
            self.cursor.execute(query, (id_servizio, mese))
            result = self.cursor.fetchone()
            
            if result:
                id_mensile, old_quantita, old_lordo = result
                
                # Se i valori sono identici, salta
                if (abs(float(old_quantita) - quantita) < 0.01 and 
                    abs(float(old_lordo) - lordo_iva) < 0.01):
                    print(f"✅ GIÀ ESISTENTE: servizio {id_servizio}, mese {mese}")
                    return True
                
                # Aggiorna
                query = """UPDATE servizi_mensile 
                          SET quantita = %s, lordo_iva = %s 
                          WHERE id_mensile = %s"""
                self.cursor.execute(query, (quantita, lordo_iva, id_mensile))
                print(f"🔄 AGGIORNATO: servizio {id_servizio}, mese {mese}")
                return True
            
            # Inserisci nuovo record mensile
            query = """INSERT INTO servizi_mensile (id_servizio, mese, quantita, lordo_iva) 
                      VALUES (%s, %s, %s, %s)"""
            self.cursor.execute(query, (id_servizio, mese, quantita, lordo_iva))
            print(f"✅ INSERITO: servizio {id_servizio}, mese {mese}: {quantita} unità, €{lordo_iva:.2f}")
            return True
            
        except Error as e:
            print(f"❌ ERRORE DB: {e}")
            logger.error(f"Errore nell'inserimento dati mensili: {e}")
            return False

    def get_numero_mese(self, nome_colonna):
        """Estrae il numero del mese dal nome della colonna"""
        mapping_mesi = {
            'sett': 9, 'set': 9, 'settembre': 9,
            'ott': 10, 'ottobre': 10,
            'nov': 11, 'novembre': 11,
            'dic': 12, 'dicembre': 12
        }
        
        nome_lower = nome_colonna.lower()
        for mese_nome, numero in mapping_mesi.items():
            if mese_nome in nome_lower:
                return numero
        
        return None

    def importa_excel(self, file_path):
        """Importa il file Excel dei servizi con logica SEMPLIFICATA."""
        try:
            logger.info(f"Inizio importazione file Excel: {file_path}")
            
            # Verifica che il SAL 7 esista
            id_sal_7 = self.sal_cache.get(7)
            if not id_sal_7:
                logger.error("SAL 7 non trovato nel database")
                return False
            
            # Leggi l'Excel con header=6 (riga 7)
            df = pd.read_excel(file_path, header=6)
            
            if df.empty:
                logger.error("File Excel vuoto o non leggibile")
                return False
            
            logger.info(f"Caricate {len(df)} righe dal file Excel")
            logger.info(f"Colonne trovate: {list(df.columns)}")
            
            # DEBUG: mostra le prime righe
            print("=== PRIME RIGHE DEL DATAFRAME ===")
            for i in range(min(5, len(df))):
                row = df.iloc[i]
                print(f"Riga {i}: {list(row.values)}")
            print("===================================")
            
            # Rimuovi righe vuote
            df = df.dropna(how='all')
            print(f"Righe dopo dropna: {len(df)}")
            
            # Trova le colonne dei mesi
            colonne_mesi = {
                'Settembre': 9,
                'Ottobre': 10,
                'Novembre': 11,
                'Dicembre': 12
            }
            
            servizi_inseriti = 0
            dati_mensili_inseriti = 0
            
            # Processa ogni riga
            for idx, row in df.iterrows():
                try:
                    print(f"\n--- RIGA {idx} ---")
                    
                    # Accesso sicuro alle colonne
                    if len(row) < 4:
                        print("❌ Riga troppo corta")
                        continue
                    
                    codice = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
                    descrizione = str(row.iloc[1]) if pd.notna(row.iloc[1]) else ""
                    canale = str(row.iloc[2]) if pd.notna(row.iloc[2]) else ""
                    tipo_misura = str(row.iloc[3]) if pd.notna(row.iloc[3]) else ""
                    
                    print(f"Codice: '{codice}'")
                    print(f"Descrizione: '{descrizione}'")
                    print(f"Canale: '{canale}'")
                    print(f"Tipo: '{tipo_misura}'")
                    
                    # Salta righe non valide
                    if not descrizione or descrizione == 'nan' or len(descrizione) < 5:
                        print("❌ Descrizione non valida")
                        continue
                    
                    # Solo righe con "Quantità"
                    if 'quantità' not in tipo_misura.lower() and 'quantita' not in tipo_misura.lower():
                        print(f"❌ Non è una riga di quantità: '{tipo_misura}'")
                        continue
                    
                    # Pulisci descrizione
                    descrizione_pulita = descrizione.strip()
                    canale_pulito = canale.upper() if canale.upper() in ['SPORTELLO', 'TOTEM'] else 'SPORTELLO'
                    
                    print(f"✅ PROCESSO: '{descrizione_pulita}' - {canale_pulito}")
                    
                    # Inserisci servizio
                    id_servizio = self.inserisci_o_recupera_servizio(descrizione_pulita, canale_pulito, id_sal_7, 2024)
                    if not id_servizio:
                        print(f"❌ Errore inserimento servizio")
                        continue
                    
                    print(f"✅ ID SERVIZIO: {id_servizio}")
                    
                    # Processa i dati mensili
                    for nome_colonna, numero_mese in colonne_mesi.items():
                        if nome_colonna not in row.index:
                            print(f"⚠️ Colonna {nome_colonna} non trovata")
                            continue
                        
                        valore_grezzo = row[nome_colonna]
                        print(f"  {nome_colonna} (mese {numero_mese}): '{valore_grezzo}' (tipo: {type(valore_grezzo)})")
                        
                        valore_numerico = self.converti_numero(valore_grezzo)
                        print(f"  Convertito: {valore_numerico}")
                        
                        if valore_numerico is not None and valore_numerico > 0:
                            if self.inserisci_servizio_mensile(id_servizio, numero_mese, valore_numerico, valore_numerico):
                                dati_mensili_inseriti += 1
                        else:
                            print(f"  ❌ Valore non valido: {valore_numerico}")
                    
                    servizi_inseriti += 1
                    
                    # Commit ogni 5 righe per test
                    if servizi_inseriti % 5 == 0:
                        self.connection.commit()
                        print(f"💾 COMMIT dopo {servizi_inseriti} servizi")
                
                except Exception as e:
                    print(f"❌ ERRORE nella riga {idx}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue
            
            # Commit finale
            self.connection.commit()
            
            logger.info("=" * 50)
            logger.info("IMPORTAZIONE EXCEL COMPLETATA")
            logger.info(f"Servizi elaborati: {servizi_inseriti}")
            logger.info(f"Dati mensili inseriti: {dati_mensili_inseriti}")
            logger.info("=" * 50)
            
            return True
            
        except Exception as e:
            logger.error(f"Errore generale nell'importazione Excel: {e}")
            import traceback
            traceback.print_exc()
            self.connection.rollback()
            return False

# ─── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    DB_CONFIG = {
        'host': '172.29.0.49',
        'port': 3306,
        'user': 'root',
        'password': os.getenv('DB_PASSWORD', ''),
        'database': 'polis'
    }

    # File possibili da cercare
    possible_files = [
        'Report Servizi Polis_sett_dic 24.csv',
        'servizi_polis_7_sal.csv',
        'report_servizi.csv',
        'Report Servizi Polis_sett_dic 24.xlsx',
        'servizi_polis_7_sal.xlsx',
        'report_servizi.xlsx'
    ]
    
    FILE_SERVIZI = None
    for filename in possible_files:
        if os.path.exists(filename):
            FILE_SERVIZI = filename
            logger.info(f"Trovato file: {filename}")
            break
    
    # Controlla argomenti da linea di comando
    if len(sys.argv) > 1:
        FILE_SERVIZI = sys.argv[1]
    
    if not FILE_SERVIZI or not os.path.exists(FILE_SERVIZI):
        logger.error(f"File dei servizi non trovato. Cercati: {possible_files}")
        logger.error("Uso: python import_servizi.py [path_file_servizi]")
        return

    importatore = ImportatoreServizi(**DB_CONFIG)
    try:
        if not importatore.connetti_db():
            logger.error("Impossibile connettersi al database")
            return
            
        importatore.carica_cache()

        # Verifica esistenza SAL 7, altrimenti lo crea
        if 7 not in importatore.sal_cache:
            logger.info("Inserimento SAL 7 nel database")
            query = """INSERT INTO sal (numero_sal, data_inizio, data_fine) 
                      VALUES (7, '2024-09-01', '2024-12-31')"""
            importatore.cursor.execute(query)
            importatore.connection.commit()
            importatore.carica_cache()

        # Importa solo Excel per ora
        if FILE_SERVIZI.lower().endswith(('.xlsx', '.xls')):
            importatore.importa_excel(FILE_SERVIZI)
        else:
            logger.error("Per ora supporto solo Excel")

    except Exception as e:
        logger.error(f"Errore generale: {e}")
    finally:
        importatore.chiudi_connessione()

if __name__ == "__main__":
    main()