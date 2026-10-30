import json
from datetime import datetime
import logging

class ImportErrorHandler:
    """Gestisce il salvataggio e recupero degli errori di importazione nel database"""
    
    def __init__(self, connection, cursor):
        self.connection = connection
        self.cursor = cursor
        self.session_id = None
        self.logger = logging.getLogger('ImportErrorHandler')
        self.sal_id = None  # Aggiungo per tenere traccia del SAL ID

    def crea_sessione_import(self, tipo_import, nome_file, id_sal, utente=None):
        """Crea una nuova sessione di importazione e restituisce l'ID"""
        try:
            query = """
                INSERT INTO import_sessions 
                (id_sal, tipo_import, nome_file, utente, stato_import) 
                VALUES (%s, %s, %s, %s, 'in_corso')
            """
            self.cursor.execute(query, (id_sal, tipo_import, nome_file, utente))
            self.session_id = self.cursor.lastrowid
            self.sal_id = id_sal  # Salvo il SAL ID per le notifiche
            self.connection.commit()
            self.logger.info(f"Creata sessione import ID: {self.session_id} per SAL ID: {id_sal}")
            return self.session_id
        except Exception as e:
            self.logger.error(f"Errore creazione sessione: {e}")
            return None
    
    def salva_errore(self, numero_riga, nome_foglio, tipo_errore, messaggio_errore, dati_riga, tipo_import=None):
        """Salva un errore di importazione nel database e crea una notifica"""
        if not self.session_id:
            self.logger.error("Nessuna sessione di import attiva")
            return False
        
        try:
            # Converti i dati della riga in JSON
            dati_json = json.dumps(dati_riga, ensure_ascii=False, default=str)
            
            query = """
                INSERT INTO import_errors 
                (id_session, tipo_import, numero_riga, nome_foglio, tipo_errore, messaggio_errore, dati_riga) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            
            # Se tipo_import non è fornito, recuperalo dalla sessione
            if not tipo_import and self.session_id:
                self.cursor.execute("SELECT tipo_import FROM import_sessions WHERE id_session = %s", (self.session_id,))
                result = self.cursor.fetchone()
                tipo_import = result[0] if result else 'unknown'
            
            self.cursor.execute(query, (
                self.session_id,
                tipo_import,
                numero_riga,
                nome_foglio,
                tipo_errore,
                messaggio_errore,
                dati_json
            ))
            
            # Inserisci notifica nella tabella polis.notifiche
            self.inserisci_notifica_errore(numero_riga, nome_foglio, messaggio_errore, dati_riga)

            return True
            
        except Exception as e:
            self.logger.error(f"Errore nel salvataggio errore: {e}")
            return False
    
    def inserisci_notifica_errore(self, numero_riga, nome_foglio, messaggio_errore, dati_riga):
        """Inserisce una notifica nella tabella polis.notifiche per la riga fallita"""
        try:
            # Prepara il messaggio della notifica con i dettagli della riga Excel
            messaggio_notifica = self.crea_messaggio_notifica(numero_riga, nome_foglio, messaggio_errore, dati_riga)

            query_notifica = """
                INSERT INTO polis.notifiche 
                (sal_id, messaggio, tipo, letta, user_id) 
                VALUES (%s, %s, %s, %s, %s)
            """

            self.cursor.execute(query_notifica, (
                self.sal_id,
                messaggio_notifica,
                'validation',
                0,  # letta = false
                1   # user_id = 1 come richiesto
            ))

            self.logger.debug(f"Notifica creata per riga {numero_riga} del foglio {nome_foglio}")

        except Exception as e:
            self.logger.error(f"Errore nell'inserimento notifica per riga {numero_riga}: {e}")

    def crea_messaggio_notifica(self, numero_riga, nome_foglio, messaggio_errore, dati_riga):
        """Crea un messaggio dettagliato per la notifica con i dati della riga Excel"""
        try:
            # Estrai i campi più importanti dalla riga Excel
            campi_importanti = []

            # Per le fatture, mostra i campi chiave
            if 'Descrizione Fornitore' in dati_riga and dati_riga['Descrizione Fornitore']:
                campi_importanti.append(f"Fornitore: {dati_riga['Descrizione Fornitore']}")

            if 'Numero fattura del fornitore' in dati_riga and dati_riga['Numero fattura del fornitore']:
                campi_importanti.append(f"N. Fattura: {dati_riga['Numero fattura del fornitore']}")

            if 'Data emissione fattura del fornitore' in dati_riga and dati_riga['Data emissione fattura del fornitore']:
                campi_importanti.append(f"Data: {dati_riga['Data emissione fattura del fornitore']}")

            if 'Importo fattura' in dati_riga and dati_riga['Importo fattura']:
                campi_importanti.append(f"Importo: {dati_riga['Importo fattura']}")

            # Per il personale, mostra campi diversi
            if 'Nome' in dati_riga and dati_riga['Nome']:
                campi_importanti.append(f"Nome: {dati_riga['Nome']}")

            if 'Cognome' in dati_riga and dati_riga['Cognome']:
                campi_importanti.append(f"Cognome: {dati_riga['Cognome']}")

            if 'Codice Fiscale' in dati_riga and dati_riga['Codice Fiscale']:
                campi_importanti.append(f"CF: {dati_riga['Codice Fiscale']}")

            # Costruisci il messaggio
            dati_riga_str = " | ".join(campi_importanti) if campi_importanti else "Dati non disponibili"

            messaggio = f"Errore alla riga {numero_riga}  del foglio '{nome_foglio}' delle spese: {messaggio_errore}\n\n"

            # Limita la lunghezza del messaggio per evitare problemi con il database
            if len(messaggio) > 1000:
                messaggio = messaggio[:997] + "..."

            return messaggio

        except Exception as e:
            self.logger.error(f"Errore nella creazione messaggio notifica: {e}")
            return f"Errore importazione riga {numero_riga} del foglio '{nome_foglio}': {messaggio_errore}"

    def aggiorna_sessione(self, totale_righe=None, righe_importate=None, righe_errore=None, stato_import=None):
        """Aggiorna i contatori della sessione di importazione"""
        if not self.session_id:
            return False
        
        try:
            updates = []
            params = []
            
            if totale_righe is not None:
                updates.append("totale_righe = %s")
                params.append(totale_righe)
            
            if righe_importate is not None:
                updates.append("righe_importate = %s")
                params.append(righe_importate)
            
            if righe_errore is not None:
                updates.append("righe_errore = %s")
                params.append(righe_errore)
            
            if stato_import is not None:
                updates.append("stato_import = %s")
                params.append(stato_import)
            
            if updates:
                query = f"UPDATE import_sessions SET {', '.join(updates)} WHERE id_session = %s"
                params.append(self.session_id)
                self.cursor.execute(query, params)
                self.connection.commit()
            
            return True
            
        except Exception as e:
            self.logger.error(f"Errore aggiornamento sessione: {e}")
            return False
    
    def classifica_errore_fatture(self, messaggio_errore):
        """Classifica il tipo di errore per le fatture"""
        if 'invalid literal for int()' in messaggio_errore:
            if '10-20' in messaggio_errore:
                return 'posizione_bdc_invalida'
        elif 'Data too long for column' in messaggio_errore:
            if 'numero_fattura' in messaggio_errore:
                return 'numero_fattura_lungo'
        elif 'foreign key constraint fails' in messaggio_errore:
            if 'accordi_quadro' in messaggio_errore:
                return 'accordo_quadro_mancante'
            elif 'documenti_em' in messaggio_errore:
                return 'documento_em_mancante'
            elif 'fornitori' in messaggio_errore:
                return 'fornitore_mancante'
        elif 'Fornitore non trovato' in messaggio_errore:
            return 'fornitore_non_trovato'
        
        return 'altro'
    
    def get_errori_sessione(self, session_id=None):
        """Recupera tutti gli errori di una sessione"""
        if not session_id:
            session_id = self.session_id
        
        query = """
            SELECT 
                id_error,
                numero_riga,
                nome_foglio,
                tipo_errore,
                messaggio_errore,
                dati_riga,
                stato_correzione,
                data_errore
            FROM import_errors
            WHERE id_session = %s
            ORDER BY numero_riga
        """
        
        self.cursor.execute(query, (session_id,))
        return self.cursor.fetchall()
    
    def get_statistiche_errori(self, session_id=None):
        """Ottiene statistiche sugli errori di una sessione"""
        if not session_id:
            session_id = self.session_id
        
        query = """
            SELECT 
                tipo_errore,
                COUNT(*) as conteggio,
                COUNT(CASE WHEN stato_correzione = 'da_correggere' THEN 1 END) as da_correggere,
                COUNT(CASE WHEN stato_correzione = 'corretto' THEN 1 END) as corretti
            FROM import_errors
            WHERE id_session = %s
            GROUP BY tipo_errore
        """
        
        self.cursor.execute(query, (session_id,))
        return self.cursor.fetchall()
    
    def marca_errore_corretto(self, id_error, note=None, corretto_da=None):
        """Marca un errore come corretto"""
        query = """
            UPDATE import_errors 
            SET stato_correzione = 'corretto',
                note_correzione = %s,
                corretto_da = %s,
                data_correzione = NOW()
            WHERE id_error = %s
        """
        
        self.cursor.execute(query, (note, corretto_da, id_error))
        self.connection.commit()
        return self.cursor.rowcount > 0