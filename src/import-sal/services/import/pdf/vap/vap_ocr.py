# vap_ocr.py - OCR specifico per Verbali di Accettazione Provvisoria (VAP)

import json
from ocr_common import *

def get_vap_json_prompt():
    """Restituisce il prompt per l'estrazione dati VAP in formato JSON"""
    return (
        "Estrai i dati dal Verbale di Accettazione Provvisoria (VAP) presente nel PDF e restituisci SOLO un JSON con questa struttura:\n"
        "\n"
        "{\n"
        ' "vap": {\n'
        ' "vap_numero": 123456,\n'
        ' "funzione": "Funzione/Ruolo",\n'
        ' "utente_compilatore": "Nome Compilatore",\n'
        ' "fornitore": "Nome Fornitore SRL",\n'
        ' "codice_fornitore": "FOR123",\n'
        ' "oda_bc": "ODA123",\n'
        ' "accordo_quadro": "AQ123",\n'
        ' "protocollo_acquisti_contratto": "PROT123",\n'
        ' "protocollo_acquisti_proroga": "PROT456",\n'
        ' "validita_da": "2025-01-01",\n'
        ' "validita_a": "2025-12-31",\n'
        ' "scadenza_contratto": "2025-12-31",\n'
        ' "scadenza_proroga_rinnovo": "2026-06-30",\n'
        ' "data_consegna_servizi": "2025-06-15",\n'
        ' "data_ultima_consegna_da": "2025-06-01",\n'
        ' "data_ultima_consegna_a": "2025-06-30",\n'
        ' "descrizione_fornitura": "Descrizione della fornitura",\n'
        ' "rapporto_fornitura": "Dettagli rapporto",\n'
        ' "rti": "RTI info",\n'
        ' "subappalto": "Subappalto info",\n'
        ' "fornitore_subappalto": "Nome Subappaltatore",\n'
        ' "consegna_finale": "SI",\n'
        ' "svincolo_trattenuta": "NO",\n'
        ' "importo_imponibile": 10000.50,\n'
        ' "importo_aggiuntivo": 500.25,\n'
        ' "importo_totale": 10500.75,\n'
        ' "approvatore_1": "Nome Approvatore 1",\n'
        ' "data_approvazione_1": "2025-07-01",\n'
        ' "approvatore_2": "Nome Approvatore 2",\n'
        ' "data_approvazione_2": "2025-07-02",\n'
        ' "approvatore_3": "Nome Approvatore 3",\n'
        ' "data_approvazione_3": "2025-07-03",\n'
        ' "direttore_esecuzione": "Nome Direttore",\n'
        ' "data_direttore_esecuzione": "2025-07-04",\n'
        ' "resp_procedimento": "Nome Responsabile",\n'
        ' "data_resp_procedimento": "2025-07-05",\n'
        ' "bloccato_pagamento": "NO",\n'
        ' "liquidabile": "SI"\n'
        ' },\n'
        ' "posizioni": [\n'
        ' {\n'
        ' "oda": "ODA123",\n'
        ' "posizione": "0010",\n'
        ' "importo": 5000.25,\n'
        ' "quantita_em": 2,\n'
        ' "importo_liq": 4000.25,\n'
        ' "importo_bloc": 500.00,\n'
        ' "importo_assest": 3500.25,\n'
        ' "quantita_assest": 1,\n'
        ' "importo_su_assest": 3000.00,\n'
        ' "quantita_su_assest": 1,\n'
        ' "testo_posizione": "Descrizione della posizione"\n'
        ' }\n'
        ' ]\n'
        '}\n'
        "\n"
        "ISTRUZIONI SPECIFICHE:\n"
        "- Estrai TUTTI i dati presenti nel Verbale VAP\n"
        "- Il numero VAP è OBBLIGATORIO (vap_numero)\n"
        "- Per ogni posizione/riga crea un elemento in 'posizioni'\n"
        "- I campi SI/NO devono essere esattamente 'SI' o 'NO'\n"
        "- Cerca informazioni su approvatori e date nelle firme\n"
        "- Estrai tutti gli importi con precisione decimale\n"
        "- Per campi non trovati usa null (non string 'null')\n"
        "- Date in formato YYYY-MM-DD\n"
        "- Numeri come float/int, non string\n"
        "\n"
        'Se il documento non è un VAP valido, rispondi: {"error": "not_vap"}\n'
        "\n"
        "GENERA SOLO IL JSON SENZA ALCUN TESTO AGGIUNTIVO O MARKDOWN.\n"
    )

def json_to_vap_queries(json_data, pdf_url=None):
    """Converte JSON VAP in query SQL con DELETE + INSERT"""
    try:
        print("=== VAP_OCR VERSION: 2024-12-09-REFACTORED ===")
        print(f"DEBUG - Processing JSON: {json_data[:300]}...")

        clean_json = json_data.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)

        if "error" in data:
            print(f"JSON contiene errore: {data['error']}")
            return None

        if "vap" not in data:
            print("JSON non contiene sezione 'vap'")
            return None

        vap = data["vap"]
        vap_numero = vap.get('vap_numero')
        if not vap_numero or vap_numero == "null" or str(vap_numero).strip() == "":
            print(f"ERRORE: vap_numero mancante o NULL ({vap_numero}), skipping PDF")
            return None

        posizioni = data.get("posizioni", [])
        print(f"DEBUG - VAP Numero: {vap_numero}, Posizioni: {len(posizioni)}")

        queries = []

        # UPSERT VAP principale
        query_vap = f"""INSERT INTO vap_pdf (
            vap_numero, funzione, utente_compilatore,
            fornitore, codice_fornitore,
            oda_bc, accordo_quadro, protocollo_acquisti_contratto, protocollo_acquisti_proroga,
            validita_da, validita_a, scadenza_contratto, scadenza_proroga_rinnovo,
            data_consegna_servizi, data_ultima_consegna_da, data_ultima_consegna_a,
            descrizione_fornitura, rapporto_fornitura, rti, subappalto, fornitore_subappalto,
            consegna_finale, svincolo_trattenuta,
            importo_imponibile, importo_aggiuntivo, importo_totale,
            approvatore_1, data_approvazione_1, approvatore_2, data_approvazione_2,
            approvatore_3, data_approvazione_3, direttore_esecuzione, data_direttore_esecuzione,
            resp_procedimento, data_resp_procedimento,
            bloccato_pagamento, liquidabile
        ) VALUES (
            {safe_value(vap_numero, 'int')},
            {safe_value(vap.get('funzione'))},
            {safe_value(vap.get('utente_compilatore'))},
            {safe_value(vap.get('fornitore'))},
            {safe_value(vap.get('codice_fornitore'))},
            {safe_value(vap.get('oda_bc'))},
            {safe_value(vap.get('accordo_quadro'))},
            {safe_value(vap.get('protocollo_acquisti_contratto'))},
            {safe_value(vap.get('protocollo_acquisti_proroga'))},
            {safe_value(vap.get('validita_da'), 'date')},
            {safe_value(vap.get('validita_a'), 'date')},
            {safe_value(vap.get('scadenza_contratto'), 'date')},
            {safe_value(vap.get('scadenza_proroga_rinnovo'), 'date')},
            {safe_value(vap.get('data_consegna_servizi'), 'date')},
            {safe_value(vap.get('data_ultima_consegna_da'), 'date')},
            {safe_value(vap.get('data_ultima_consegna_a'), 'date')},
            {safe_value(vap.get('descrizione_fornitura'))},
            {safe_value(vap.get('rapporto_fornitura'))},
            {safe_value(vap.get('rti'))},
            {safe_value(vap.get('subappalto'))},
            {safe_value(vap.get('fornitore_subappalto'))},
            {safe_value(vap.get('consegna_finale'), 'enum')},
            {safe_value(vap.get('svincolo_trattenuta'), 'enum')},
            {safe_value(vap.get('importo_imponibile'), 'decimal')},
            {safe_value(vap.get('importo_aggiuntivo'), 'decimal')},
            {safe_value(vap.get('importo_totale'), 'decimal')},
            {safe_value(vap.get('approvatore_1'))},
            {safe_value(vap.get('data_approvazione_1'), 'date')},
            {safe_value(vap.get('approvatore_2'))},
            {safe_value(vap.get('data_approvazione_2'), 'date')},
            {safe_value(vap.get('approvatore_3'))},
            {safe_value(vap.get('data_approvazione_3'), 'date')},
            {safe_value(vap.get('direttore_esecuzione'))},
            {safe_value(vap.get('data_direttore_esecuzione'), 'date')},
            {safe_value(vap.get('resp_procedimento'))},
            {safe_value(vap.get('data_resp_procedimento'), 'date')},
            {safe_value(vap.get('bloccato_pagamento'), 'enum')},
            {safe_value(vap.get('liquidabile'), 'enum')}
        ) ON DUPLICATE KEY UPDATE
            fornitore={safe_value(vap.get('fornitore'))},
            importo_totale={safe_value(vap.get('importo_totale'), 'decimal')},
            funzione={safe_value(vap.get('funzione'))},
            descrizione_fornitura={safe_value(vap.get('descrizione_fornitura'))}"""

        queries.append(query_vap)

        # DELETE + INSERT posizioni usando subquery per l'ID
        if posizioni:
            # Cancella le posizioni esistenti usando l'ID del VAP
            query_delete = f"DELETE FROM vap_pdf_posizioni WHERE vap_numero = (SELECT id FROM vap_pdf WHERE vap_numero = {safe_value(vap_numero, 'int')})"
            queries.append(query_delete)

            for posizione in posizioni:
                # Usa subquery per ottenere l'ID nella foreign key
                query_posizione = f"""INSERT INTO vap_pdf_posizioni (
                    vap_numero, oda, posizione,
                    importo, quantita_em, importo_liq, importo_bloc,
                    importo_assest, quantita_assest, importo_su_assest, quantita_su_assest,
                    testo_posizione
                ) VALUES (
                    (SELECT id FROM vap_pdf WHERE vap_numero = {safe_value(vap_numero, 'int')}),
                    {safe_value(posizione.get('oda'))},
                    {safe_value(posizione.get('posizione'))},
                    {safe_value(posizione.get('importo'), 'decimal')},
                    {safe_value(posizione.get('quantita_em'), 'int')},
                    {safe_value(posizione.get('importo_liq'), 'decimal')},
                    {safe_value(posizione.get('importo_bloc'), 'decimal')},
                    {safe_value(posizione.get('importo_assest'), 'decimal')},
                    {safe_value(posizione.get('quantita_assest'), 'int')},
                    {safe_value(posizione.get('importo_su_assest'), 'decimal')},
                    {safe_value(posizione.get('quantita_su_assest'), 'int')},
                    {safe_value(posizione.get('testo_posizione'))}
                ) ON DUPLICATE KEY UPDATE
                    importo={safe_value(posizione.get('importo'), 'decimal')},
                    testo_posizione={safe_value(posizione.get('testo_posizione'))},
                    importo_liq={safe_value(posizione.get('importo_liq'), 'decimal')}"""

                queries.append(query_posizione)

        print(f"DEBUG - Generate {len(queries)} query SQL (1 UPSERT vap + 1 DELETE + {len(posizioni)} INSERT posizioni)")
        return "; ".join(queries)

    except json.JSONDecodeError as e:
        print(f"Errore parsing JSON: {e}")
        print(f"JSON problematico: {json_data[:200]}...")
        return None
    except Exception as e:
        print(f"Errore conversione JSON to SQL: {e}")
        return None

 