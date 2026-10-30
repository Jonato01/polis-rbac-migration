# aq_ocr.py - OCR specifico per Accordi Quadro (AQ)

import json
from ocr_common import *

def get_aq_json_prompt(file_name=None):
    """Restituisce il prompt per l'estrazione dati AQ in formato JSON

    Args:
        file_name: Nome del file PDF (opzionale, per estrarre codice AQ dal nome)
    """

    # Fetch categorie di spesa dal database
    db_config = get_db_config()
    try:
        from mysql.connector import pooling
        pool = pooling.MySQLConnectionPool(
            pool_name="polis_pool_aq",
            pool_size=1,
            pool_reset_session=True,
            host=db_config['host'],
            port=db_config['port'],
            user=db_config['user'],
            password=db_config['password'],
            database=db_config['database']
        )
        connection = pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT DISTINCT codice_categoria, descrizione FROM categoria_spesa")
        categorie = cursor.fetchall()
        cursor.close()
        connection.close()
        
        categorie_text = "\n".join([f"- {c['codice_categoria']}: {c['descrizione']}" for c in categorie])
    except Exception as e:
        print(f"Errore fetch categorie: {e}")
        categorie_text = "Categorie non disponibili"
    
    # Aggiungi info sul nome file se disponibile
    file_name_info = ""
    if file_name:
        file_name_info = f"""
    
    NOME FILE PDF: {file_name}
    IMPORTANTE: Il codice AQ completo (es. 4490009557) potrebbe essere presente nel nome del file.
    Nel documento il codice AQ potrebbe essere scritto in modo errato con delle 'x' (es. AQ n° 449000xxxx).
    Se nel documento il codice AQ è mascherato o incompleto, USA IL CODICE AQ COMPLETO DAL NOME DEL FILE.
    """

    return f"""
    Estrai i dati dall'Accordo Quadro presente nel PDF e restituisci SOLO un JSON con questa struttura:
    
    {{
      "aq": {{
          "codice": "2A",
          "codice_aq": "4490012492",
          "cig": "A04F71A30C",
          "oggetto_aq": "Descrizione completa dell'oggetto dell'Accordo Quadro",
          "fornitore_nome": "RTI: E-Shore S.r.l. (mandataria), Loginet S.r.l., DP Service Energy S.r.l.",
          "data_stipula": "2024-03-26",
          "data_scadenza": "2033-03-26",
          "importo_totale": 8400000.00,
          "cup": "J59J21014170008",
          "durata_mesi": 108
      }}
    }}
    {file_name_info}
    CATEGORIE DI SPESA DISPONIBILI:
    {categorie_text}
    
    ISTRUZIONI SPECIFICHE:
    - Estrai i dati presenti nell'Accordo Quadro in UN SOLO record JSON (NON un array)
    - Il CIG di stipula è OBBLIGATORIO
    - IMPORTANTE: il campo codice_aq deve essere una STRINGA con tutte le cifre (es. "4490012492"), NON un numero, per evitare troncamenti
    - IMPORTANTE: Se nel documento il codice AQ è mascherato (es. 449000xxxx), estrai il codice completo dal nome del file
    - Per RTI, includi tutti i fornitori nel campo fornitore_nome
    - Analizza il contenuto per identificare la categoria di spesa più appropriata
    - Estrai l'oggetto completo dall'Art. 1 o simile
    - Date in formato YYYY-MM-DD
    - Importi come numeri decimali senza simboli
    - Per campi non trovati usa null
    - la data di stipula è uguale a quella di firma elettronica in ultima pagina del documento pdf se ce ne sono di più prendi sempre quella più al fondo
    - la data di scadenza è calcolata aggiungendo la durata alla data di stipula
    - il campo "codice" è il codice identificativo interno che può essere una lettera o una combinazione di lettere e numeri (es. 2A, 2B, 3, ecc.)
    - il campo "codice" va dedotto dal contesto del documento non è mai presente esplicitamente, deduci il codice in base alle categorie di spesa che ti ho fornito, il codice è sempre obbligatorio che ci sia
    Se il documento non è un Accordo Quadro valido, rispondi: {{"error": "not_aq"}}
    
    GENERA SOLO IL JSON SENZA ALCUN TESTO AGGIUNTIVO O MARKDOWN.
    """

def json_to_aq_queries(json_data, pdf_url=None, codice_aq_fallback=None):
    """Converte JSON AQ in query SQL con UPSERT

    Args:
        json_data: JSON string con i dati estratti dal PDF
        pdf_url: URL/path del PDF (usato anche per estrarre codice AQ dal nome file)
        codice_aq_fallback: Codice AQ da usare se non presente nel JSON (es. dalla ricerca)
    """
    try:
        print("=== AQ_OCR VERSION: 2025-01-18-SINGLE-RECORD ===")
        print(f"DEBUG - Processing JSON: {json_data[:300]}...")
        if codice_aq_fallback:
            print(f"DEBUG - Codice AQ fallback disponibile: {codice_aq_fallback}")

        clean_json = json_data.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)

        if "error" in data:
            print(f"JSON contiene errore: {data['error']}")
            return None

        if "aq" not in data:
            print("JSON non contiene sezione 'aq'")
            return None

        # Supporta sia oggetto singolo che array (per retrocompatibilità)
        aq_data = data["aq"]
        if isinstance(aq_data, list):
            # Se è un array, prendi solo il primo elemento
            if len(aq_data) == 0:
                print("Array 'aq' vuoto")
                return None
            aq = aq_data[0]
            print(f"DEBUG - JSON contiene array con {len(aq_data)} elementi, uso solo il primo")
        else:
            # Oggetto singolo (nuovo formato)
            aq = aq_data
            print("DEBUG - JSON contiene oggetto singolo (formato corretto)")

        # Usa codice_aq dal JSON, se null/None usa il fallback
        codice_aq = aq.get('codice_aq')

        # Verifica se il codice_aq è valido (non nullo, non vuoto, non mascherato con 'x')
        codice_aq_str = str(codice_aq) if codice_aq else ''
        is_invalid = (
            codice_aq is None or
            codice_aq == '' or
            str(codice_aq).lower() == 'null' or
            'x' in codice_aq_str.lower()  # Mascherato tipo 449000xxxx
        )

        if is_invalid:
            if codice_aq_fallback:
                print(f"DEBUG - codice_aq '{codice_aq}' non valido/mascherato, uso fallback: {codice_aq_fallback}")
                codice_aq = codice_aq_fallback
            else:
                print(f"DEBUG - codice_aq '{codice_aq}' non valido e nessun fallback disponibile")

        cig = aq.get('cig')

        # UPSERT Accordo Quadro - una sola query
        query_aq = f"""INSERT INTO pdf_aq (
                codice, codice_aq, cig, oggetto_aq,
                fornitore_nome, data_stipula, data_scadenza,
                importo_totale, cup, durata_mesi
            ) VALUES (
                {safe_value(aq.get('codice'))},
                {safe_value(codice_aq, 'int')},
                {safe_value(cig)},
                {safe_value(aq.get('oggetto_aq'))},
                {safe_value(aq.get('fornitore_nome'))},
                {safe_value(aq.get('data_stipula'), 'date')},
                {safe_value(aq.get('data_scadenza'), 'date')},
                {safe_value(aq.get('importo_totale'), 'decimal')},
                {safe_value(aq.get('cup'))},
                {safe_value(aq.get('durata_mesi'), 'int')}
            ) ON DUPLICATE KEY UPDATE
                oggetto_aq = VALUES(oggetto_aq),
                fornitore_nome = VALUES(fornitore_nome),
                data_stipula = VALUES(data_stipula),
                data_scadenza = VALUES(data_scadenza),
                importo_totale = VALUES(importo_totale),
                cup = VALUES(cup),
                durata_mesi = VALUES(durata_mesi),
                codice = VALUES(codice)"""

        print(f"DEBUG - Query AQ generata con successo per codice {aq.get('codice')}, codice_aq {codice_aq}")

        return [query_aq]  # Ritorna lista con una sola query

    except json.JSONDecodeError as e:
        print(f"Errore parsing JSON: {e}")
        print(f"JSON problematico: {json_data[:200]}...")
        return None
    except Exception as e:
        print(f"Errore conversione JSON to SQL: {e}")
        return None

def ensure_aq_table_structure():
    """Verifica e aggiorna la struttura della tabella pdf_aq"""
    db_config = get_db_config()
    
    # Query per verificare le colonne esistenti
    check_columns_query = """
    SELECT COLUMN_NAME 
    FROM INFORMATION_SCHEMA.COLUMNS 
    WHERE TABLE_SCHEMA = DATABASE() 
    AND TABLE_NAME = 'pdf_aq'
    """
    
    try:
        from mysql.connector import pooling
        pool = pooling.MySQLConnectionPool(
            pool_name="polis_pool_check",
            pool_size=1,
            pool_reset_session=True,
            host=db_config['host'],
            port=db_config['port'],
            user=db_config['user'],
            password=db_config['password'],
            database=db_config['database']
        )
        connection = pool.get_connection()
        cursor = connection.cursor()
        
        cursor.execute(check_columns_query)
        existing_columns = [row[0] for row in cursor.fetchall()]
        
        # Definisci le colonne necessarie
        required_columns = {
            'fornitore_nome': 'VARCHAR(500)',
            'data_stipula': 'DATE',
            'data_scadenza': 'DATE',
            'importo_totale': 'DECIMAL(15,2)',
            'cup': 'VARCHAR(20)',
            'durata_mesi': 'INT'
        }
        
        # Aggiungi le colonne mancanti
        for col_name, col_type in required_columns.items():
            if col_name not in existing_columns:
                alter_query = f"ALTER TABLE pdf_aq ADD COLUMN {col_name} {col_type}"
                try:
                    cursor.execute(alter_query)
                    connection.commit()
                    print(f"âœ“ Aggiunta colonna {col_name}")
                except Exception as e:
                    if "Duplicate column" not in str(e):
                        print(f"âœ— Errore aggiungendo {col_name}: {e}")
        
        cursor.close()
        connection.close()
        print("âœ“ Struttura tabella pdf_aq verificata")
        
    except Exception as e:
        print(f"âœ— Errore verifica struttura tabella: {e}")
 