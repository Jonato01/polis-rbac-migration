"""
Modulo DB per aggiornare la tabella sal_upload.
Include retry con backoff e recovery check.
"""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pymysql
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_SSL

ROME_TZ = ZoneInfo('Europe/Rome')

# Retry config
MAX_DB_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # secondi: 2, 4, 8


def now_rome():
    return datetime.now(ROME_TZ)


def get_db_connection():
    """Crea connessione al DB MySQL con retry."""
    params = {
        'host': DB_HOST,
        'port': DB_PORT,
        'user': DB_USER,
        'password': DB_PASSWORD,
        'database': DB_NAME,
        'charset': 'utf8mb4',
        'connect_timeout': 10,
    }
    if DB_SSL:
        params['ssl'] = {'ssl': True}

    last_err = None
    for attempt in range(1, MAX_DB_RETRIES + 1):
        try:
            conn = pymysql.connect(**params)
            if attempt > 1:
                logging.warning(f">>> DB: connessione riuscita al tentativo {attempt}")
            return conn
        except Exception as e:
            last_err = e
            if attempt < MAX_DB_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                logging.warning(f">>> DB: connessione fallita (tentativo {attempt}/{MAX_DB_RETRIES}): {e}")
                logging.warning(f">>> DB: retry tra {wait}s...")
                time.sleep(wait)

    logging.warning(f">>> DB: connessione fallita dopo {MAX_DB_RETRIES} tentativi: {last_err}")
    raise last_err


def update_sal_upload_stato(zip_blob_name: str, stato: str, extra_fields: dict = None) -> bool:
    """
    Aggiorna lo stato di un sal_upload cercandolo per zip_blob_name.
    Ritenta automaticamente in caso di errore DB.

    Returns:
        True se aggiornamento riuscito, False altrimenti.
        Rilancia l'eccezione se tutti i retry falliscono.
    """
    last_err = None
    for attempt in range(1, MAX_DB_RETRIES + 1):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            set_parts = ["stato = %s"]
            params = [stato]

            if extra_fields:
                for key, value in extra_fields.items():
                    if value == 'NOW()':
                        set_parts.append(f"{key} = %s")
                        params.append(now_rome())
                    else:
                        set_parts.append(f"{key} = %s")
                        params.append(value)

            set_clause = ", ".join(set_parts)
            params.append(zip_blob_name)

            sql = f"UPDATE sal_upload SET {set_clause} WHERE zip_blob_name = %s"
            cursor.execute(sql, params)
            conn.commit()

            rows_affected = cursor.rowcount
            if rows_affected > 0:
                logging.warning(f">>> DB: sal_upload aggiornato → stato={stato} (blob={zip_blob_name})")
            else:
                logging.warning(f">>> DB: ATTENZIONE nessun sal_upload trovato per blob={zip_blob_name}")

            return rows_affected > 0

        except Exception as e:
            last_err = e
            logging.warning(f">>> DB ERRORE (tentativo {attempt}/{MAX_DB_RETRIES}): {type(e).__name__}: {e}")
            if attempt < MAX_DB_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                logging.warning(f">>> DB: retry update tra {wait}s...")
                time.sleep(wait)
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    # Tutti i retry falliti: rilancia per bloccare il flusso
    logging.warning(f">>> DB: update FALLITO dopo {MAX_DB_RETRIES} tentativi per blob={zip_blob_name}")
    raise last_err


def set_sal_upload_error(zip_blob_name: str, error_message: str) -> bool:
    """Imposta stato ERROR con messaggio. Non rilancia eccezioni."""
    try:
        return update_sal_upload_stato(
            zip_blob_name,
            'ERROR',
            {'error_message': error_message[:500]}  # tronca messaggi lunghi
        )
    except Exception as e:
        logging.warning(f">>> DB: impossibile impostare ERROR per {zip_blob_name}: {e}")
        return False


def get_sal_upload_by_blob(zip_blob_name: str) -> dict | None:
    """
    Recupera id, numero_sal e normalized_folder dal sal_upload per il blob dato.
    Serve per costruire il messaggio da inviare alla coda import-excel.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT id, numero_sal, normalized_folder FROM sal_upload WHERE zip_blob_name = %s",
            (zip_blob_name,)
        )
        return cursor.fetchone()
    except Exception as e:
        logging.warning(f">>> DB: errore query sal_upload per blob={zip_blob_name}: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def get_stuck_uploads() -> list:
    """
    Recovery: trova sal_upload in stato UPLOADED (blob processato ma DB non aggiornato).
    Ritorna lista di dict con id e zip_blob_name.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        sql = """
            SELECT id, zip_blob_name, uploaded_at
            FROM sal_upload
            WHERE stato = 'UPLOADED'
              AND uploaded_at < DATE_SUB(NOW(), INTERVAL 5 MINUTE)
        """
        cursor.execute(sql)
        rows = cursor.fetchall()
        return list(rows)

    except Exception as e:
        logging.warning(f">>> DB: errore query recovery: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def get_sal_upload_by_blob(zip_blob_name: str) -> dict | None:
    """
    Recupera i dati di un sal_upload dal zip_blob_name.
    Usato dalla Azure Function dopo l'unzip per ottenere upload_id e numero_sal
    necessari per il messaggio sulla coda Service Bus.
    
    Returns:
        dict con id, numero_sal, normalized_folder oppure None se non trovato.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("""
            SELECT id, numero_sal, normalized_folder, folder_name
            FROM sal_upload
            WHERE zip_blob_name = %s
        """, (zip_blob_name,))
        
        row = cursor.fetchone()
        return row

    except Exception as e:
        logging.warning(f">>> DB: errore get_sal_upload_by_blob per {zip_blob_name}: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass
