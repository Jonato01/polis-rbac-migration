# lettere_incarico.py
import os
import sys
import json
import re
import requests
from openai import AzureOpenAI
from dotenv import load_dotenv

# Carica variabili d'ambiente dal file .env
load_dotenv()

# Aggiungi la cartella padre al path per importare ocr_common
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocr_common import *
from matching_utils import find_dipendente_match, normalize_name, parse_italian_date

def extract_name_from_filename(filename):
    """Estrae il nome dal filename del PDF"""
    basename = os.path.basename(filename).replace('.pdf', '').replace('.PDF', '')
    
    # Pattern per estrarre nomi da filename
    patterns = [
        r'([A-Z][a-z]+)\s+([A-Z][a-z]+)', # Apicella Christian
        r'([A-Z]+)[-_\s]+([A-Z]+)', # APICELLA_CHRISTIAN
        r'([A-Za-z]+)[-_\s]+([A-Za-z]+)', # apicella-christian
    ]
    
    for pattern in patterns:
        match = re.search(pattern, basename)
        if match:
            return f"{match.group(1).upper()} {match.group(2).upper()}"
    
    return None

def get_valid_levels_from_db():
    """Recupera livelli validi dalla tabella qualifica"""
    try:
        db_config = get_db_config()
        query = "SELECT des_qualifica FROM qualifica ORDER BY cod_qualifica"
        results = fetch_mysql_query(query, db_config)
        
        if results:
            levels = [row['des_qualifica'] for row in results]
            print(f"Livelli validi dal DB: {levels}")
            return levels
        else:
            print("Nessun livello trovato nel DB, uso fallback")
            return ['A1', 'A2', 'B', 'C', 'D', 'E', 'DIRIGENTE']
    except Exception as e:
        print(f"Errore recupero livelli: {e}")
        return ['A1', 'A2', 'B', 'C', 'D', 'E', 'DIRIGENTE']

def search_names_in_database_enhanced(extracted_name, db_config):
    """
    STRATEGIA INTELLIGENTE: Cerca nel database tutte le possibili corrispondenze
    per ogni parola sia come nome che cognome
    """
    print(f"=== RICERCA DATABASE INTELLIGENTE ===")
    print(f"Nome estratto: '{extracted_name}'")
    
    try:
        # 1. Scomporre in parole (almeno 3 caratteri)
        words = [word.strip() for word in extracted_name.split() if len(word.strip()) > 2]
        print(f"Parole estratte: {words}")
        
        if not words:
            print(f"Nessuna parola valida trovata")
            return []
        
        # 2. Costruisci condizioni OR per ogni parola sia come nome che cognome
        conditions = []
        for word in words:
            conditions.append(f"nome LIKE '%{word}%'")
            conditions.append(f"cognome LIKE '%{word}%'")
            print(f"Cercando '{word}' sia come nome che cognome")
        
        query = f"""
        SELECT cod_dipendente, nome, cognome, 
               CONCAT(cognome, ' ', nome) as nome_completo
        FROM dipendenti 
        WHERE {' OR '.join(conditions)}
        ORDER BY cognome, nome
        """
        
        print(f"Query SQL: {query}")
        print(f"Eseguendo ricerca nel database...")
        
        results = fetch_mysql_query(query, db_config)
        
        if results:
            print(f"Trovati {len(results)} candidati nel database:")
            for i, result in enumerate(results, 1):
                print(f" {i}. {result['nome_completo']} (ID: {result['cod_dipendente']})")
        else:
            print(f"Nessun candidato trovato per '{extracted_name}'")
        
        print(f"=== FINE RICERCA DATABASE ===")
        return results
        
    except Exception as e:
        print(f"Errore ricerca database: {e}")
        return []

def validate_extraction_with_ai_enhanced(data, filename, valid_levels, pdf_text, azure_api_key, attempt=1):
    """Valida estrazione con AI feedback iterativo e ricerca database SEMPRE"""
    
    print(f"=== VALIDAZIONE AI TENTATIVO {attempt} ===")
    print(f"File: {filename}")
    
    # Protezione per pdf_text None
    if pdf_text is None:
        print(f"WARNING: pdf_text è None, uso stringa vuota")
        pdf_text = ""
    
    if attempt > 5:
        print(f"Raggiunto limite tentativi (5) per {filename}")
        return data
    
    extracted_name = data.get('nominativo', '')
    extracted_level = data.get('livello', '')
    
    print(f"Nome estratto: '{extracted_name}'")
    print(f"Livello estratto: '{extracted_level}'")
    
    # SEMPRE ricerca nel database per possibili corrispondenze
    db_config = get_db_config()
    print(f"Avviando ricerca database...")
    db_candidates = search_names_in_database_enhanced(extracted_name, db_config)
    
    # Costruisci info database per l'AI
    db_info = ""
    if db_candidates:
        db_info = f"\nCANDIDATI TROVATI NEL DATABASE:\n"
        for candidate in db_candidates:
            db_info += f"- {candidate['nome_completo']} (ID: {candidate['cod_dipendente']})\n"
        print(f"Invio {len(db_candidates)} candidati all'AI")
    else:
        db_info = "\nNESSUN CANDIDATO TROVATO NEL DATABASE"
        print(f"Nessun candidato da inviare all'AI")
    
    # Verifica AI con focus su database matching
    verification_prompt = f"""
    VERIFICA INTELLIGENTE NOME E DATE NELLA LETTERA DI INCARICO:
    
    Nome estratto: "{extracted_name}"
    Livello estratto: "{extracted_level}"
    {db_info}
    
    TESTO COMPLETO PDF:
    {pdf_text}
    
    COMPITI CRITICI:
    1. Leggi la lettera di incarico e trova il VERO destinatario
    2. Cerca frasi come "Lettera di Incarico di [NOME]", "Sig. [NOME]", "Sig.ra [NOME]"
    3. Controlla se il nome estratto corrisponde al destinatario nel PDF
    4. Se hai candidati dal database, verifica quale corrisponde meglio al PDF
    5. PrioritÃ  ASSOLUTA ai candidati del database se corrispondono al contenuto
    6. Verifica che il livello sia corretto (A1, A2, B, C, D, E, DIRIGENTE)
    7. VERIFICA DATE FORMATO ITALIANO: Le date sono in formato DD/MM/YYYY
       - "01/03/2025" = 1Â° MARZO 2025 = "2025-03-01"
       - "15/12/2024" = 15 DICEMBRE 2024 = "2024-12-15"
       - NON confondere con formato americano MM/DD/YYYY!
    
    STRATEGIA:
    - Se il PDF dice "CHRISTIAN APICELLA" e nel database hai "APICELLA CHRISTIAN", usa quello del database
    - Il formato database Ã¨ sempre COGNOME NOME (es. "APICELLA CHRISTIAN")
    - I livelli devono essere lettere pure (B, C, ecc.) non "B CCNL"
    - Date italiane: DD/MM/YYYY †’ YYYY-MM-DD
    
    Restituisci un JSON con la verifica:
    {{
      "nome_trovato_nel_pdf": true/false,
      "nome_pdf_originale": "Nome come appare nel PDF",
      "nome_corretto": "NOME CORRETTO dal database se diverso",
      "candidato_database_migliore": "Nome dal database se applicabile",
      "livello_valido": true/false,
      "livello_corretto": "LIVELLO CORRETTO se diverso (solo lettera)",
      "date_formato_italiano_verificate": true/false,
      "problemi": ["lista dei problemi trovati"],
      "correzione_necessaria": true/false,
      "spiegazione": "Dettaglio della correzione e ragionamento"
    }}
    """
    
    verification_result = None
    
    # Tentativo 1: Azure OpenAI
    try:
        print(f"Tentando Azure OpenAI...")
        azure_client = AzureOpenAI(
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=azure_api_key,
            api_version="2024-02-15-preview"
        )
        
        print(f"Azure - Chiamando API...")
        verification_response = azure_client.chat.completions.create(
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1"),
            messages=[{"role": "user", "content": verification_prompt}],
            temperature=0.1
        )
        
        print(f"Azure - Risposta ricevuta")
        raw_content = verification_response.choices[0].message.content
        print(f"Azure - Contenuto raw (primi 300 char): '{raw_content[:300]}...'")
        
        if not raw_content or raw_content.strip() == "":
            raise Exception("Azure OpenAI ha restituito contenuto vuoto")
        
        # Estrazione JSON robusta
        # Estrazione JSON robusta - trova JSON valido senza testo extra
        cleaned_content = raw_content.replace("```json", "").replace("```", "").strip()
        start_json = cleaned_content.find('{')

        if start_json == -1:
            raise Exception("Nessun JSON trovato nella risposta")

        # Trova la chiusura del JSON principale contando parentesi
        brace_count = 0
        end_json = start_json
        for i, char in enumerate(cleaned_content[start_json:], start_json):
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_json = i
                    break

        json_content = cleaned_content[start_json:end_json+1]
        print(f"Azure - JSON estratto: '{json_content[:200]}...'")

        verification_result = json.loads(json_content)
        print(f"Azure OpenAI funziona: {verification_result}")
        
    except Exception as azure_error:
        print(f"Azure OpenAI fallito: {azure_error}")
    
    # Se abbiamo una risposta AI valida, processala
    if verification_result:
        # Controlla se ci sono correzioni da applicare anche se l'AI dice correzione_necessaria=false
        nome_corretto = verification_result.get('nome_corretto') or verification_result.get('candidato_database_migliore')
        livello_corretto = verification_result.get('livello_corretto', extracted_level)
        
        # Applica correzioni se il nome o livello suggeriti sono diversi da quelli estratti
        applied_corrections = False
        
        if nome_corretto and nome_corretto != extracted_name:
            data['nominativo'] = nome_corretto
            print(f"Nome corretto: {extracted_name} †’ {data['nominativo']}")
            applied_corrections = True
        
        if not verification_result.get('livello_valido', True) and livello_corretto != extracted_level:
            data['livello'] = livello_corretto
            print(f"Livello corretto: {extracted_level} †’ {data['livello']}")
            applied_corrections = True
        
        if applied_corrections:
            print(f"Correzioni applicate - Rivalidando con tentativo {attempt + 1}...")
            return validate_extraction_with_ai_enhanced(data, filename, valid_levels, pdf_text, azure_api_key, attempt + 1)
        elif verification_result.get('correzione_necessaria', False):
            problemi = verification_result.get('problemi', [])
            spiegazione = verification_result.get('spiegazione', '')
            print(f"Tentativo {attempt} - Problemi AI: {problemi}")
            if spiegazione:
                print(f"Spiegazione AI: {spiegazione}")
            # Ricorsione per altri problemi
            print(f"Rivalidando con tentativo {attempt + 1}...")
            return validate_extraction_with_ai_enhanced(data, filename, valid_levels, pdf_text, azure_api_key, attempt + 1)
        else:
            print(f"Validazione AI OK tentativo {attempt}")
            return data
    else:
        print(f"Entrambe le AI fallite, applico fallback database intelligente...")
        
        # Fallback intelligente con database
        if db_candidates:
            if len(db_candidates) == 1:
                data['nominativo'] = db_candidates[0]['nome_completo']
                print(f"Fallback database (unico candidato): {extracted_name} †’ {data['nominativo']}")
            else:
                # Match migliore basato su parole comuni
                best_candidate = None
                best_score = 0
                for candidate in db_candidates:
                    words_in_extracted = extracted_name.lower().split()
                    words_in_candidate = candidate['nome_completo'].lower().split()
                    score = sum(1 for word in words_in_extracted if any(word in cand_word for cand_word in words_in_candidate))
                    if score > best_score:
                        best_score = score
                        best_candidate = candidate
                
                if best_candidate:
                    data['nominativo'] = best_candidate['nome_completo']
                    print(f"Fallback database (migliore match): {extracted_name} †’ {data['nominativo']}")
        
        # Fallback livelli - pulisci "B CCNL" †’ "B"
        if extracted_level:
            level_clean = extracted_level.split()[0] # Prende solo la prima parola
            if level_clean not in valid_levels:
                simple_fixes = {'8': 'B', '6': 'C', '0': 'O', '5': 'S', '1': 'I'}
                if level_clean in simple_fixes:
                    data['livello'] = simple_fixes[level_clean]
                    print(f"Correzione livello: {extracted_level} †’ {data['livello']}")
            else:
                data['livello'] = level_clean
                if level_clean != extracted_level:
                    print(f"Pulizia livello: {extracted_level} †’ {data['livello']}")
        
        return data

def get_lettere_incarico_json_prompt():
    """Prompt for lettere incarico extraction"""
    return """
    Estrai i dati dalla lettera di incarico e restituisci SOLO un JSON:
    
    {
      "nominativo": "COGNOME NOME",
      "livello": "A1/A2/B/C/D/E/DIRIGENTE",
      "data_inizio": "YYYY-MM-DD",
      "data_lettera": "YYYY-MM-DD",
      "firma_responsabile": true
    }
    
    ISTRUZIONI CRITICHE:
    - Il nominativo Ã¨ nella intestazione "Lettera di Incarico di [NOME]"
    - Il livello Ã¨ in "Livello: [X] CCNL" - estrai solo la lettera/numero (B, C, A1, etc.)
    - Data inizio: cerca "dal DD/MM/YYYY" o "a far data dal DD/MM/YYYY"
    - Data lettera: cerca "CITTÃ€, DD/MM/YYYY" in fondo al documento
    - Firma: cerca presenza di firme o "Il Responsabile" 
    - FORMATO DATE ITALIANO: Le date sono in formato DD/MM/YYYY (giorno/mese/anno)
    - Converti in formato YYYY-MM-DD per il JSON
    
    ESEMPI FORMATO DATE ITALIANO:
    - "Livello: B CCNL di Poste Italiane" †’ "B"
    - "dal 01/01/2025 al 31/12/2026" †’ "2025-01-01" (1Â° gennaio)
    - "SESTO FIORENTINO, 02/01/2025" †’ "2025-01-02" (2 gennaio)
    - "PERUGIA, 01/03/2025" †’ "2025-03-01" (1Â° marzo, NON 2 gennaio!)
    
    ATTENZIONE: 01/03/2025 = 1Â° MARZO 2025 = 2025-03-01
    
    GENERA SOLO IL JSON.
    """

def json_to_lettere_queries(json_data, filename="", pdf_text=""):
    """Convert JSON to SQL queries with AI validation SEMPRE ATTIVA"""
    
    # Protezione per pdf_text None
    if pdf_text is None:
        print(f"WARNING: pdf_text è None in json_to_lettere_queries, uso stringa vuota")
        pdf_text = ""
    
    # Estrai nome file dal path se disponibile
    # Estrai nome file dal path o dall'output del log
    original_filename = filename
    if not original_filename and pdf_text:
        # Cerca "Processing PDF: [URL]" nel testo
        import re
        processing_match = re.search(r'Processing PDF: .+/([^/]+\.pdf)', pdf_text)
        if processing_match:
            original_filename = processing_match.group(1)
        else:
            # Fallback: cerca qualsiasi .pdf nell'output
            pdf_match = re.search(r'([^/\s]+\.pdf)', pdf_text)
            if pdf_match:
                original_filename = pdf_match.group(1)
            else:
                original_filename = "unknown.pdf"
    elif not original_filename:
        original_filename = "unknown.pdf"

    print(f"Nome file estratto: '{original_filename}'")
    
    print(f"=== DEBUG PROCESSING LETTERE INCARICO ===")
    print(f"File: {original_filename}")
    print(f"JSON ricevuto (primi 200 char): {json_data[:200]}...")
    
    try:
        clean_json = json_data.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        print(f"Dati estratti inizialmente:")
        print(f" - Nome: {data.get('nominativo', 'N/A')}")
        print(f" - Livello: {data.get('livello', 'N/A')}")
        print(f" - Data inizio: {data.get('data_inizio', 'N/A')}")
        print(f" - Data lettera: {data.get('data_lettera', 'N/A')}")
        
        if "error" in data:
            print(f"Errore nei dati estratti: {data}")
            return None
        
        # Ottieni livelli validi dal database
        valid_levels = get_valid_levels_from_db()
        
        # SEMPRE applica validazione AI con ricerca database
        azure_api_key = os.getenv('AZURE_OPENAI_KEY')
        azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
        azure_deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT')
        
        if azure_api_key and azure_endpoint and azure_deployment:
            print(f"Azure OpenAI configurato - Attivando validazione AI SEMPRE...")
            data = validate_extraction_with_ai_enhanced(data, original_filename, valid_levels, pdf_text, azure_api_key)
        else:
            print(f"Azure OpenAI non configurato - Applico solo validazione database...")
            # Anche senza API key, fai almeno la ricerca database
            db_config = get_db_config()
            db_candidates = search_names_in_database_enhanced(data.get('nominativo', ''), db_config)
            if db_candidates and len(db_candidates) == 1:
                data['nominativo'] = db_candidates[0]['nome_completo']
                print(f"Correzione database senza AI: †’ {data['nominativo']}")
        
        print(f"Dati FINALI dopo validazione:")
        print(f" - Nome: {data.get('nominativo', 'N/A')}")
        print(f" - Livello: {data.get('livello', 'N/A')}")
        
        # Get employee match
        dipendenti = fetch_mysql_query(
            "SELECT cod_dipendente, nome, cognome FROM dipendenti",
            get_db_config()
        )
        
        match = find_dipendente_match(data['nominativo'], dipendenti)
        print(f"Employee matching: {match}")
        
        firma_responsabile = 1 if data.get('firma_responsabile', False) else 0
        
        # Determina numero_sal basato su data_inizio
        db_config = get_db_config()
        numero_sal = get_sal_number_by_date(data.get('data_inizio'), db_config)

        query = f"""
        INSERT INTO pdf_lettere_incarico (
            cod_dipendente, nome_cognome, livello,
            data_inizio, data_lettera, firma_responsabile,
            numero_sal
        ) VALUES (
            {match['cod_dipendente'] if match else 'NULL'},
            {safe_value(data['nominativo'])},
            {safe_value(data.get('livello'))},
            {safe_value(data.get('data_inizio'), 'date')},
            {safe_value(data.get('data_lettera'), 'date')},
            {firma_responsabile},
            {numero_sal}
        ) ON DUPLICATE KEY UPDATE
            livello={safe_value(data.get('livello'))},
            data_inizio={safe_value(data.get('data_inizio'), 'date')},
            data_lettera={safe_value(data.get('data_lettera'), 'date')},
            numero_sal={numero_sal}
        """
        
        print(f"FINALE: {data['nominativo']}: livello {data.get('livello', '?')}")
        print(f" Data inizio: {data.get('data_inizio', 'N/A')}")
        print(f" SAL assegnato: {numero_sal}")
        if match:
            print(f" Matched to cod: {match['cod_dipendente']} ({match.get('confidence', 0):.0f}%)")
        
        print(f"=== FINE DEBUG PROCESSING ===")
        return query
        
    except Exception as e:
        print(f"Errore in json_to_lettere_queries: {e}")
        return None

def get_sal_number_by_date(data_inizio, db_config):
    """
    Determina il numero_sal corretto chiamando l'API per lettere_incarico
    """
    try:
        api_url = os.getenv('AZURE_IMPORT_PATH_URL', "https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/sal/import-path")
        params = {"type": "lettere_incarico"}

        print(f"Chiamando API per SAL lettere_incarico: {api_url}")
        response = requests.get(api_url, params=params, timeout=10)

        if response.status_code == 200:
            api_data = response.json()
            print(f"Risposta API: {api_data}")

            if api_data.get('success', False):
                sal_number = api_data.get('sal_number')
                sal_name = api_data.get('sal_name', 'N/A')
                print(f"SAL ottenuto da API: {sal_number} ({sal_name})")
                return sal_number
            else:
                print(f"API ha restituito success=false")
                return 8 # Fallback
        else:
            print(f"Errore API: status {response.status_code}")
            return 8 # Fallback

    except Exception as e:
        print(f"Errore nel chiamare l'API SAL: {e}")
        return 8 # Fallback in caso di errore