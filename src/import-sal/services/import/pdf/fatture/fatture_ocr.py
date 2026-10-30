# fatture_ocr.py - OCR specifico per Fatture v5.1
# AGGIORNATO: Supporto TUTTI i tipi documento TD* (TD01-TD28)

import json
import re
from ocr_common import *


def extract_sal_from_path(pdf_url):
    """
    Estrae il numero SAL dal path del PDF
    Es: 8_SAL_30_04_2025/... -> 8
    """
    try:
        # Cerca pattern tipo "8_SAL_" nel path
        match = re.search(r'/(\d+)_SAL_', pdf_url)
        if match:
            sal_number = int(match.group(1))
            print(f"DEBUG - Extracted SAL: {sal_number} from path")
            return sal_number
        else:
            print(f"WARNING - Could not extract SAL from path: {pdf_url[:100]}")
            return None
    except Exception as e:
        print(f"ERROR - extracting SAL from path: {e}")
        return None


def extract_filename_from_url(pdf_url):
    """Estrae il nome file dal path completo"""
    try:
        # Prendi tutto dopo l'ultimo /
        filename = pdf_url.split('/')[-1]
        return filename if filename else "unknown.pdf"
    except:
        return "unknown.pdf"


def log_fattura_error(pdf_url, sal, tipo_errore, dettaglio_errore, json_estratto=None):
    """
    Genera query SQL per inserire errore in fatture_pdf_errori
    
    Args:
        pdf_url: Path completo del PDF
        sal: Numero SAL estratto (puo essere None)
        tipo_errore: Tipo errore enum
        dettaglio_errore: Descrizione dettagliata
        json_estratto: JSON parziale estratto (opzionale)
    
    Returns:
        Query SQL INSERT per tabella errori
    """
    filename = extract_filename_from_url(pdf_url)
    
    # Escape stringe per SQL
    pdf_path_safe = safe_value(pdf_url)
    filename_safe = safe_value(filename)
    sal_safe = sal if sal else "NULL"
    tipo_errore_safe = safe_value(tipo_errore)
    dettaglio_safe = safe_value(dettaglio_errore)
    
    # JSON estratto (opzionale, puo essere molto lungo)
    if json_estratto:
        # Limita a 5000 caratteri per non sovraccaricare DB
        json_trunc = json_estratto[:5000] if len(json_estratto) > 5000 else json_estratto
        json_safe = safe_value(json_trunc)
    else:
        json_safe = "NULL"
    
    query = f"""INSERT INTO fatture_pdf_errori 
        (pdf_path, pdf_filename, sal, tipo_errore, dettaglio_errore, json_estratto)
    VALUES 
        ({pdf_path_safe}, {filename_safe}, {sal_safe}, {tipo_errore_safe}, {dettaglio_safe}, {json_safe})"""
    
    return query


def get_fattura_json_prompt():
    """Restituisce il prompt per l'estrazione dati fattura in formato JSON"""
    return (
        "Estrai i dati dal documento fiscale presente nel PDF e restituisci SOLO un JSON con questa struttura:\n"
        "\n"
        "{\n"
        ' "fattura": {\n'
        ' "tipo_documento": "TD01",\n'
        ' "codice_aq": "4490009279",\n'
        ' "cup": "J59J21014170008",\n'
        ' "endorsen": "PP24X00216048",\n'
        ' "numero": "2024/001",\n'
        ' "data": "2024-01-15",\n'
        ' "cedente": "Fornitore SRL",\n'
        ' "piva_cedente": "12345678901",\n'
        ' "committente": "Cliente SPA",\n'
        ' "piva_committente": "09876543210",\n'
        ' "totale_documento": 1220.00,\n'
        ' "imponibile": 1000.00,\n'
        ' "iva": 220.00,\n'
        ' "importo_da_pagare": 1220.00,\n'
        ' "iban": "IT60X0542811101000000123456",\n'
        ' "scadenza": "2024-02-15",\n'
        ' "numero_documento": "2-FE"\n'
        ' },\n'
        ' "righe": [\n'
        ' {\n'
        ' "posizione": 1,\n'
        ' "descrizione": "Servizio di consulenza",\n'
        ' "quantita": 10.00,\n'
        ' "prezzo_unitario": 50.00,\n'
        ' "iva_percentuale": 22.00,\n'
        ' "prezzo_totale": 500.00\n'
        ' }\n'
        ' ]\n'
        '}\n'
        "\n"
        "TIPI DOCUMENTO ACCETTATI (tutti i tipi di fatturazione elettronica):\n"
        "- TD01: Fattura\n"
        "- TD02: Acconto/Anticipo su fattura\n"
        "- TD03: Acconto/Anticipo su parcella\n"
        "- TD04: Nota di credito\n"
        "- TD05: Nota di debito\n"
        "- TD06: Parcella\n"
        "- TD16: Integrazione fattura reverse charge interno\n"
        "- TD17: Integrazione/autofattura per acquisto servizi dall'estero\n"
        "- TD18: Integrazione per acquisto di beni intracomunitari\n"
        "- TD19: Integrazione/autofattura per acquisto di beni ex art.17 c.2 DPR 633/72\n"
        "- TD20: Autofattura per regolarizzazione e integrazione delle fatture\n"
        "- TD21: Autofattura per splafonamento\n"
        "- TD22: Estrazione beni da Deposito IVA\n"
        "- TD23: Estrazione beni da Deposito IVA con versamento IVA\n"
        "- TD24: Fattura differita di cui all'art.21, comma 4, lett. a)\n"
        "- TD25: Fattura differita di cui all'art.21, comma 4, terzo periodo lett. b)\n"
        "- TD26: Cessione di beni ammortizzabili e per passaggi interni\n"
        "- TD27: Fattura per autoconsumo o per cessioni gratuite senza rivalsa\n"
        "- TD28: Acquisti da San Marino con IVA\n"
        "\n"
        "ISTRUZIONI SPECIFICHE:\n"
        "- IMPORTANTE: Estrai SEMPRE il campo 'tipo_documento' dalla sezione 'Tipo Documento' (es: TD01, TD04, TD24)\n"
        "- Il numero fattura/documento e OBBLIGATORIO e deve essere valido\n"
        "- Per NOTE DI CREDITO (TD04): mantieni gli importi POSITIVI, il sistema li gestira come accrediti\n"
        "- Il numero documento e presente nella sezione dati generali documento vicino alla data\n"
        "- I dati di aq corrispondono anche alla dicitura di accordo quadro ed e presente nella tabella\n"
        "- Estrai TUTTI i dati presenti nel documento\n"
        "- Per ogni riga/articolo crea un elemento in 'righe'\n"
        "- Estrai tutti gli importi con precisione decimale\n"
        "- Per campi non trovati usa null (non string 'null')\n"
        "- Date in formato YYYY-MM-DD\n"
        "- Numeri come float, non string\n"
        "- Cerca informazioni su cedente e committente nell'intestazione\n"
        "- Identifica correttamente imponibile, IVA e totale\n"
        "- Per ogni riga identifica: descrizione, quantita, prezzo unitario, IVA%, totale riga\n"
        "\n"
        'Se il documento NON e un documento fiscale valido (non ha Tipo Documento TD*), rispondi: {"error": "not_invoice"}\n'
        "\n"
        "GENERA SOLO IL JSON SENZA ALCUN TESTO AGGIUNTIVO O MARKDOWN.\n"
    )


def json_to_fatture_queries(json_data, pdf_url):
    """
    Converte JSON fattura in query SQL con INSERT
    v5.1: Gestione completa errori + supporto TUTTI i tipi TD*
    
    Returns:
        Tupla (queries_success, query_error)
        - queries_success: Lista query per inserimento fattura (se successo)
        - query_error: Query per inserimento errore (se fallimento)
    """
    try:
        print("=== FATTURE_OCR VERSION: 5.1-ALL-TD-TYPES ===")
        print(f"DEBUG - Processing JSON: {json_data[:300]}...")
        print(f"DEBUG - PDF URL: {pdf_url}")
        
        filename = extract_filename_from_url(pdf_url)

        # ========== VALIDAZIONE 1: Estrazione SAL ==========
        sal = extract_sal_from_path(pdf_url)
        if not sal:
            error_msg = f"Impossibile estrarre SAL dal path: {pdf_url[:200]}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_fattura_error(
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
            
            error_query = log_fattura_error(
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
            
            tipo_err = "pdf_corrotto" if error_type == "not_invoice" else "ocr_fallito"
            error_query = log_fattura_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore=tipo_err,
                dettaglio_errore=f"OCR error: {error_type}",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        # ========== VALIDAZIONE 4: Struttura JSON ==========
        if "fattura" not in data:
            error_msg = "JSON non contiene sezione 'fattura'"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_fattura_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="json_malformato",
                dettaglio_errore="Missing 'fattura' section in JSON",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        fattura = data["fattura"]
        numero_fattura = fattura.get('numero')
        protocollo = fattura.get('endorsen')

        # ========== VALIDAZIONE 5: Numero Fattura ==========
        if not numero_fattura or numero_fattura == "null" or str(numero_fattura).strip() == "":
            error_msg = f"Numero fattura mancante o NULL: {numero_fattura}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_fattura_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="numero_fattura_mancante",
                dettaglio_errore=f"Numero fattura vuoto o NULL: {numero_fattura}",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        # ========== VALIDAZIONE 6: Protocollo ==========
        if not protocollo or protocollo == "null" or str(protocollo).strip() == "":
            error_msg = f"Protocollo mancante o NULL: {protocollo}"
            print(f"ERRORE: {error_msg}")
            
            error_query = log_fattura_error(
                pdf_url=pdf_url,
                sal=sal,
                tipo_errore="protocollo_mancante",
                dettaglio_errore=f"Protocollo (endorsen) vuoto o NULL: {protocollo}",
                json_estratto=json_data[:5000]
            )
            return (None, error_query)

        # ========== TUTTO OK: Genera query INSERT ==========
        righe = data.get("righe", [])
        tipo_documento = fattura.get('tipo_documento', 'TD01') # Default TD01 se mancante
        
        print(f"OK VALIDAZIONE - SAL: {sal}, Numero: {numero_fattura}, Protocollo: {protocollo}, Tipo: {tipo_documento}, Righe: {len(righe)}")

        queries = []

        # INSERT fattura principale (ora con pdf_path e tipo_documento)
        query_fattura = f"""INSERT INTO fatture_pdf (
            pdf_path, sal, numero, tipo_documento, data, cedente, piva_cedente, committente, piva_committente,
            totale_documento, imponibile, iva, importo_da_pagare, iban, scadenza,
            protocollo, versione_duplicato, cup, codice_aq, numero_documento
        )
        SELECT 
            {safe_value(pdf_url)} as pdf_path,
            {sal} as sal,
            {safe_value(fattura.get('numero'))} as numero,
            {safe_value(tipo_documento)} as tipo_documento,
            {safe_value(fattura.get('data'), 'date')} as data,
            {safe_value(fattura.get('cedente'))} as cedente,
            {safe_value(fattura.get('piva_cedente'))} as piva_cedente,
            {safe_value(fattura.get('committente'))} as committente,
            {safe_value(fattura.get('piva_committente'))} as piva_committente,
            {safe_value(fattura.get('totale_documento'), 'decimal')} as totale_documento,
            {safe_value(fattura.get('imponibile'), 'decimal')} as imponibile,
            {safe_value(fattura.get('iva'), 'decimal')} as iva,
            {safe_value(fattura.get('importo_da_pagare'), 'decimal')} as importo_da_pagare,
            {safe_value(fattura.get('iban'))} as iban,
            {safe_value(fattura.get('scadenza'), 'date')} as scadenza,
            {safe_value(fattura.get('endorsen'))} as protocollo,
            COALESCE((SELECT MAX(f2.versione_duplicato) + 1 
                    FROM fatture_pdf f2 
                    WHERE f2.sal = {sal} AND f2.protocollo = {safe_value(protocollo)}), 0) as versione_duplicato,
            {safe_value(fattura.get('cup'))} as cup,
            {safe_value(fattura.get('codice_aq'))} as codice_aq,
            {safe_value(fattura.get('numero_documento'))} as numero_documento"""

        queries.append(query_fattura)

        # DELETE righe esistenti
        if righe:
            query_delete = f"""DELETE r FROM righe_fattura_pdf r
INNER JOIN fatture_pdf f ON r.id_fattura = f.id_fattura
WHERE f.sal = {sal} AND f.protocollo = {safe_value(protocollo)}"""
            
            queries.append(query_delete)

            # INSERT righe
            for riga in righe:
                query_riga = f"""INSERT INTO righe_fattura_pdf (
                    id_fattura, posizione, descrizione, quantita, prezzo_unitario,
                    iva_percentuale, prezzo_totale
                ) SELECT 
                    f.id_fattura,
                    {safe_value(riga.get('posizione'), 'int')},
                    {safe_value(riga.get('descrizione'))},
                    {safe_value(riga.get('quantita'), 'decimal')},
                    {safe_value(riga.get('prezzo_unitario'), 'decimal')},
                    {safe_value(riga.get('iva_percentuale'), 'decimal')},
                    {safe_value(riga.get('prezzo_totale'), 'decimal')}
                FROM fatture_pdf f
                WHERE f.sal = {sal} AND f.protocollo = {safe_value(protocollo)}
                ORDER BY f.id_fattura DESC
                LIMIT 1"""

                queries.append(query_riga)

        print(f"DEBUG - Generated {len(queries)} query SQL SUCCESS")
        return (queries, None) # Successo: queries OK, nessun errore

    except Exception as e:
        # ========== ERRORE GENERICO NON GESTITO ==========
        error_msg = f"Exception non gestita: {str(e)}"
        print(f"ERRORE CRITICO: {error_msg}")
        import traceback
        print(traceback.format_exc())
        
        sal = extract_sal_from_path(pdf_url) if pdf_url else None
        
        error_query = log_fattura_error(
            pdf_url=pdf_url,
            sal=sal,
            tipo_errore="altro",
            dettaglio_errore=f"Exception: {str(e)[:500]}",
            json_estratto=json_data[:5000] if 'json_data' in locals() else None
        )
        return (None, error_query)