# passaggi_livello.py - OCR per Passaggi di Livello
# v3.1 - Con data_lettera e firma_risorse_umane

import json
import re
from ocr_common import safe_value


def extract_filename_from_url(pdf_url):
    """Estrae il nome file dal path completo"""
    try:
        filename = pdf_url.split('/')[-1]
        return filename if filename else "unknown.pdf"
    except:
        return "unknown.pdf"


def extract_nome_cognome_from_raw_text(raw_text, pdf_filename=None):
    """
    Estrae nome e cognome dal raw_text usando name_extractor avanzato
    
    Returns:
        (nome, cognome) oppure (None, None)
    """
    if not raw_text and not pdf_filename:
        return (None, None)
    
    try:
        # Usa il name_extractor avanzato
        from name_extractor import extract_name_from_raw_text
        
        full_name = extract_name_from_raw_text(raw_text, pdf_filename)
        
        if not full_name:
            print(f"DEBUG - name_extractor: no name found")
            return (None, None)
        
        # Split nome e cognome
        parts = full_name.strip().split()
        
        if len(parts) >= 2:
            # Se primo elemento Ã¨ tutto maiuscolo, Ã¨ probabilmente cognome
            if parts[0].isupper() and len(parts[0]) > 2:
                cognome = parts[0]
                nome = ' '.join(parts[1:])
            else:
                # Altrimenti primo Ã¨ nome
                nome = parts[0]
                cognome = ' '.join(parts[1:])
            
            print(f"DEBUG - name_extractor: {nome} {cognome}")
            return (nome, cognome)
        elif len(parts) == 1:
            print(f"DEBUG - name_extractor: single name {parts[0]}")
            return (None, parts[0])
        
    except ImportError:
        print(f"WARNING - name_extractor not available, using fallback patterns")
        return extract_nome_cognome_fallback(raw_text)
    except Exception as e:
        print(f"ERROR - name_extractor failed: {e}")
        return extract_nome_cognome_fallback(raw_text)
    
    return (None, None)


def extract_nome_cognome_fallback(raw_text):
    """Fallback patterns se name_extractor non disponibile"""
    if not raw_text:
        return (None, None)
    
    # Pattern 1: "Nome COGNOME"
    pattern1 = r'\b([A-Z][a-z]+)\s+([A-Z]{2,}(?:\s+[A-Z]{2,})?)\b'
    match = re.search(pattern1, raw_text)
    if match:
        nome = match.group(1).strip()
        cognome = match.group(2).strip()
        print(f"DEBUG - Fallback Pattern1: {nome} {cognome}")
        return (nome, cognome)
    
    # Pattern 2: COGNOME NOME seguito da keywords
    pattern2 = r'([A-ZÃ€ÃˆÃ‰ÃŒÃ’Ã™]{2,}(?:\s+[A-ZÃ€ÃˆÃ‰ÃŒÃ’Ã™]{2,})*)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*\n'
    match = re.search(pattern2, raw_text)
    if match:
        cognome = match.group(1).strip()
        nome = match.group(2).strip()
        print(f"DEBUG - Fallback Pattern2: {nome} {cognome}")
        return (nome, cognome)
    
    print(f"DEBUG - Fallback: no match found")
    return (None, None)


def extract_sal_from_path(pdf_url):
    """Estrae il numero SAL dal path del PDF"""
    try:
        match = re.search(r'/(\d+)_SAL_', pdf_url)
        if match:
            sal_number = int(match.group(1))
            print(f"DEBUG - Extracted SAL: {sal_number}")
            return sal_number
        else:
            print(f"WARNING - Could not extract SAL from: {pdf_url[:100]}")
            return None
    except Exception as e:
        print(f"ERROR - extracting SAL: {e}")
        return None


def log_passaggi_error(pdf_url, sal, tipo_errore, dettaglio_errore, json_estratto=None):
    """Genera query SQL per inserire errore in passaggi_livello_errori"""
    filename = extract_filename_from_url(pdf_url)
    
    pdf_path_safe = safe_value(pdf_url)
    filename_safe = safe_value(filename)
    sal_safe = sal if sal else "NULL"
    tipo_errore_safe = safe_value(tipo_errore)
    dettaglio_safe = safe_value(dettaglio_errore)
    
    if json_estratto:
        json_trunc = json_estratto[:5000] if len(json_estratto) > 5000 else json_estratto
        json_safe = safe_value(json_trunc)
    else:
        json_safe = "NULL"
    
    query = f"""INSERT INTO passaggi_livello_errori 
        (pdf_path, pdf_filename, sal, tipo_errore, dettaglio_errore, json_estratto)
    VALUES 
        ({pdf_path_safe}, {filename_safe}, {sal_safe}, {tipo_errore_safe}, {dettaglio_safe}, {json_safe})"""
    
    return query


def get_passaggi_livello_prompt():
    """Restituisce il prompt per l'estrazione dati passaggi di livello in formato JSON"""
    return (
        "Estrai i dati dalla lettera di passaggio di livello/inquadramento e restituisci SOLO un JSON con questa struttura:\n"
        "\n"
        "{\n"
        ' "dipendente": {\n'
        ' "nome": "Mario",\n'
        ' "cognome": "Rossi"\n'
        ' },\n'
        ' "passaggio": {\n'
        ' "data_decorrenza": "2024-01-01",\n'
        ' "data_lettera": "2024-01-15",\n'
        ' "livello_precedente": "C2",\n'
        ' "livello_nuovo": "D1",\n'
        ' "tipo_variazione": "passaggio_livello",\n'
        ' "figura_professionale": "Specialista",\n'
        ' "note": "Aumento merito Euro 2.000,00",\n'
        ' "firma_presente": true\n'
        ' }\n'
        '}\n'
        "\n"
        "ISTRUZIONI SPECIFICHE:\n"
        "- **OBBLIGATORI**: nome, cognome, data_decorrenza, livello_nuovo\n"
        "- **data_lettera**: Cerca pattern 'Roma, 1Â° dicembre 2023' o 'Roma 02/11/2023' nell'intestazione della lettera\n"
        "- **firma_presente**: Cerca firme, sottoscrizioni o nomi in fondo al documento (es: 'Massimo Sidoti', 'Con i migliori auguri' seguiti da firma). Se trovi nome di persona o tracce di firma in fondo, metti true, altrimenti false.\n"
        "- **livello_nuovo**: Estrai il livello (A1, A2, B, C, D, E, DIRIGENTE)\n"
        "- **figura_professionale**: Ruolo tipo 'Specialista', 'Professional', 'Referente Distretto', etc\n"
        "- **note**: Eventuali informazioni aggiuntive su aumenti, indennitÃ , assegni ad personam\n"
        "- Date in formato YYYY-MM-DD\n"
        "- Per campi non trovati usa null (non string 'null')\n"
        "\n"
        'Se il documento NON Ã¨ una lettera di passaggio valida, rispondi: {"error": "not_passaggio_letter"}\n'
        "\n"
        "GENERA SOLO IL JSON SENZA ALCUN TESTO AGGIUNTIVO O MARKDOWN.\n"
    )


def json_to_passaggi_queries(json_data, pdf_url, raw_text=None):
    """
    Converte JSON passaggi livello in query SQL con INSERT
    v3.1: Con data_lettera e firma_risorse_umane
    
    Args:
        json_data: JSON string from GPT
        pdf_url: URL del PDF
        raw_text: Raw text extracted by Document Intelligence
    
    Returns:
        Tupla (queries_success, query_error)
    """
    try:
        print("=== PASSAGGI_LIVELLO VERSION: 3.1-WITH-DATA-LETTERA-FIRMA ===")
        print(f"DEBUG - Processing JSON: {json_data[:300]}...")
        print(f"DEBUG - PDF URL: {pdf_url}")
        print(f"DEBUG - Raw text available: {len(raw_text) if raw_text else 0} chars")
        
        filename = extract_filename_from_url(pdf_url)

        # ========== VALIDAZIONE 1: Estrazione SAL ==========
        sal = extract_sal_from_path(pdf_url)
        if not sal:
            error_msg = f"Impossibile estrarre SAL dal path: {pdf_url[:200]}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_passaggi_error(
                pdf_url=pdf_url,
                sal=None,
                tipo_errore="altro",
                dettaglio_errore=f"SAL extraction failed: {error_msg}",
                json_estratto=None
            )
            return (None, error_query)

        # ========== VALIDAZIONE 2: Parsing JSON ==========
        try:
            clean_json = json_data.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_json)
        except json.JSONDecodeError as e:
            error_msg = f"JSON malformato: {str(e)}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_passaggi_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="json_malformato",
                dettaglio_errore=f"JSONDecodeError: {str(e)}",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        # ========== VALIDAZIONE 3: Errore OCR ==========
        if "error" in data:
            error_type = data.get('error', 'unknown')
            error_msg = f"OCR ha restituito errore: {error_type}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_passaggi_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="ocr_fallito",
                dettaglio_errore=f"OCR error: {error_type}",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        # ========== VALIDAZIONE 4: Struttura JSON ==========
        if "dipendente" not in data or "passaggio" not in data:
            error_msg = "JSON non contiene sezioni 'dipendente' e 'passaggio'"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_passaggi_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="json_malformato",
                dettaglio_errore="Missing 'dipendente' or 'passaggio' sections",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        dipendente = data["dipendente"]
        passaggio = data["passaggio"]

        # ========== EXTRACTION INTELLIGENTE con raw_text ==========
        nome = dipendente.get('nome')
        cognome = dipendente.get('cognome')
        
        if (not nome or not cognome) and raw_text:
            print("DEBUG - GPT didn't extract nome/cognome, trying advanced name_extractor...")
            filename_for_extraction = extract_filename_from_url(pdf_url)
            nome_extracted, cognome_extracted = extract_nome_cognome_from_raw_text(raw_text, filename_for_extraction)
            
            if nome_extracted and cognome_extracted:
                nome = nome or nome_extracted
                cognome = cognome or cognome_extracted
                print(f"Extracted from advanced extraction: {nome} {cognome}")
            elif cognome_extracted and not nome_extracted:
                cognome = cognome or cognome_extracted
                print(f"Only cognome extracted: {cognome}, nome still missing")

        # ========== VALIDAZIONE 5: Dati Obbligatori ==========
        data_decorrenza = passaggio.get('data_decorrenza')
        
        if not nome or not cognome:
            error_msg = f"Nome/Cognome mancanti: nome={nome}, cognome={cognome}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_passaggi_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="dati_mancanti",
                dettaglio_errore=f"Nome o cognome mancanti: {error_msg}",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)
        
        if not data_decorrenza:
            error_msg = "Data di decorrenza mancante"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_passaggi_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="dati_mancanti",
                dettaglio_errore="Data decorrenza mancante o NULL",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        # ========== TUTTO OK: Genera query INSERT ==========
        print(f"VALIDAZIONE OK - SAL: {sal}, Nome: {nome} {cognome}, Decorrenza: {data_decorrenza}")

        queries = []
        
        # Concatena nome e cognome
        nome_cognome = f"{nome} {cognome}"
        
        # Prepara figura_professionale (combina figura_professionale + note se presenti)
        figura_prof_value = passaggio.get('figura_professionale', '')
        note_value = passaggio.get('note', '')
        
        if figura_prof_value and note_value:
            figura_prof_combined = f"{figura_prof_value}. {note_value}"
        elif figura_prof_value:
            figura_prof_combined = figura_prof_value
        elif note_value:
            figura_prof_combined = note_value
        else:
            figura_prof_combined = ''
        
        # Tronca a 500 caratteri
        figura_prof = figura_prof_combined[:497] + '...' if len(figura_prof_combined) > 500 else figura_prof_combined
        
        # data_lettera
        data_lettera = passaggio.get('data_lettera')
        
        # firma_risorse_umane (da boolean a tinyint)
        firma_presente = passaggio.get('firma_presente', False)
        firma_risorse_umane = 1 if firma_presente else 0

        # INSERT nella tabella esistente passaggi_livello_sal
        query_passaggio = f"""INSERT INTO passaggi_livello_sal (
            nome_cognome,
            livello_nuovo,
            figura_professionale,
            data_decorrenza,
            data_lettera,
            firma_risorse_umane,
            id_sal,
            pdf_path,
            pdf_filename
        )
        SELECT
            {safe_value(nome_cognome)} as nome_cognome,
            {safe_value(passaggio.get('livello_nuovo'))} as livello_nuovo,
            {safe_value(figura_prof)} as figura_professionale,
            {safe_value(data_decorrenza, 'date')} as data_decorrenza,
            {safe_value(data_lettera, 'date')} as data_lettera,
            {firma_risorse_umane} as firma_risorse_umane,
            s.id_sal,
            {safe_value(pdf_url)} as pdf_path,
            {safe_value(filename)} as pdf_filename
        FROM sal s
        WHERE s.numero_sal = {sal}
        LIMIT 1"""

        queries.append(query_passaggio)

        print(f"DEBUG - Generated {len(queries)} query SQL SUCCESS")
        return (queries, None)

    except Exception as e:
        error_msg = f"Exception non gestita: {str(e)}"
        print(f"ERRORE CRITICO: {error_msg}")
        import traceback
        print(traceback.format_exc())
        
        sal = extract_sal_from_path(pdf_url) if pdf_url else None
        
        error_query = log_passaggi_error(
            pdf_url=pdf_url,
            sal=sal,
            tipo_errore="altro",
            dettaglio_errore=f"Exception: {str(e)[:500]}",
            json_estratto=json_data[:5000] if 'json_data' in locals() else None
        )
        return (None, error_query)