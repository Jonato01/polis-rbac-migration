import logging
import json
from datetime import datetime

logger = logging.getLogger(__name__)

class ImportErrorHandler:
    """Gestisce gli errori durante l'importazione"""
    
    def __init__(self, connection=None, cursor=None):
        self.connection = connection
        self.cursor = cursor
        self.session_id = None
        self.sal_id = None  # Aggiungo per tenere traccia del SAL ID
        self.errors = []
    
    def crea_sessione_import(self, tipo_import, nome_file, id_sal=None, utente=None):
        """Crea una nuova sessione di import"""
        self.session_id = f"{tipo_import}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.sal_id = id_sal  # Salvo il SAL ID per le notifiche
        logger.info(f"Creata sessione import: {self.session_id}")
        return self.session_id
    
    def salva_errore(self, numero_riga, nome_foglio, tipo_errore, messaggio_errore, dati_riga=None, tipo_import='generic'):
        """Salva un errore nel database o in memoria e crea una notifica"""
        error = {
            'session_id': self.session_id,
            'numero_riga': numero_riga,
            'nome_foglio': nome_foglio,
            'tipo_errore': tipo_errore,
            'messaggio_errore': messaggio_errore,
            'dati_riga': json.dumps(dati_riga) if dati_riga else None,
            'tipo_import': tipo_import,
            'timestamp': datetime.now().isoformat()
        }
        self.errors.append(error)
        logger.warning(f"Errore salvato: Riga {numero_riga} - {tipo_errore}: {messaggio_errore[:100]}")

        # Inserisci notifica nella tabella polis.notifiche se abbiamo una connessione DB
        if self.connection and self.cursor and dati_riga:
            self.inserisci_notifica_errore(numero_riga, nome_foglio, messaggio_errore, dati_riga)

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

            logger.debug(f"Notifica creata per riga {numero_riga} del foglio {nome_foglio}")

        except Exception as e:
            logger.error(f"Errore nell'inserimento notifica per riga {numero_riga}: {e}")

    def crea_messaggio_notifica(self, numero_riga, nome_foglio, messaggio_errore, dati_riga):
        """Crea un messaggio dettagliato per la notifica con i dati della riga Excel"""
        try:
            # Estrai i campi più importanti dalla riga Excel per il personale
            campi_importanti = []

            if 'Nome' in dati_riga and dati_riga['Nome']:
                campi_importanti.append(f"Nome: {dati_riga['Nome']}")

            if 'Cognome' in dati_riga and dati_riga['Cognome']:
                campi_importanti.append(f"Cognome: {dati_riga['Cognome']}")

            if 'Codice Fiscale' in dati_riga and dati_riga['Codice Fiscale']:
                campi_importanti.append(f"CF: {dati_riga['Codice Fiscale']}")

            if 'Qualifica' in dati_riga and dati_riga['Qualifica']:
                campi_importanti.append(f"Qualifica: {dati_riga['Qualifica']}")

            if 'Azienda' in dati_riga and dati_riga['Azienda']:
                campi_importanti.append(f"Azienda: {dati_riga['Azienda']}")

            # Per le fatture, mostra campi diversi
            if 'Descrizione Fornitore' in dati_riga and dati_riga['Descrizione Fornitore']:
                campi_importanti.append(f"Fornitore: {dati_riga['Descrizione Fornitore']}")

            if 'Numero fattura del fornitore' in dati_riga and dati_riga['Numero fattura del fornitore']:
                campi_importanti.append(f"N. Fattura: {dati_riga['Numero fattura del fornitore']}")

            # Costruisci il messaggio
            dati_riga_str = " | ".join(campi_importanti) if campi_importanti else "Dati non disponibili"

            messaggio = f"Errore importazione riga {numero_riga} dell'import del personale: {messaggio_errore}\n\n"

            # Limita la lunghezza del messaggio per evitare problemi con il database
            if len(messaggio) > 1000:
                messaggio = messaggio[:997] + "..."

            return messaggio

        except Exception as e:
            logger.error(f"Errore nella creazione messaggio notifica: {e}")
            return f"Errore importazione riga {numero_riga} del foglio '{nome_foglio}': {messaggio_errore}"

    def aggiorna_sessione(self, totale_righe=None, righe_importate=None, righe_errore=None, stato_import=None):
        """Aggiorna lo stato della sessione"""
        logger.info(f"Aggiornamento sessione {self.session_id}: {stato_import}")
        if totale_righe:
            logger.info(f"  Totale righe: {totale_righe}")
        if righe_importate:
            logger.info(f"  Righe importate: {righe_importate}")
        if righe_errore:
            logger.info(f"  Righe con errore: {righe_errore}")
    
    def get_errors(self):
        """Ritorna tutti gli errori"""
        return self.errors