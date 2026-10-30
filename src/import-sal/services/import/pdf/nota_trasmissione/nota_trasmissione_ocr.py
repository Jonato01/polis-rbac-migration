# nota_trasmissione_ocr.py - OCR per Nota di Trasmissione Rendicontazione SAL
# Version 2.0 - Refactored per architettura standard con Azure OpenAI

import json
import re
from ocr_common import safe_value

def get_nota_trasmissione_json_prompt():
    """Restituisce il prompt per l'estrazione dati da Nota di Trasmissione"""
    return (
        "Estrai i dati dalla Nota di Trasmissione presente nel PDF e restituisci SOLO un JSON con questa struttura:\n"
        "\n"
        "{\n"
        ' "nota_trasmissione": {\n'
        ' "numero_sal": 8,\n'
        ' "data_inizio_periodo": "2025-01-01",\n'
        ' "data_fine_periodo": "2025-04-30"\n'
        ' },\n'
        ' "linee_intervento": [\n'
        ' {\n'
        ' "linea_intervento": "Linea 1 - Descrizione",\n'
        ' "cup": "J59J21014170008",\n'
        ' "rendicontato": 125000.50,\n'
        ' "contributo_competenza": 100000.40\n'
        ' }\n'
        ' ]\n'
        '}\n'
        "\n"
        "ISTRUZIONI SPECIFICHE:\n"
        "- Estrai TUTTE le linee di intervento presenti nella tabella di rendicontazione\n"
        "- numero_sal Ã¨ OBBLIGATORIO (es: 8 per '8Â° SAL')\n"
        "- data_fine_periodo Ã¨ OBBLIGATORIA (formato YYYY-MM-DD)\n"
        "- data_inizio_periodo Ã¨ OPZIONALE (puÃ² essere null se non presente)\n"
        "- Per ogni linea di intervento estrai:\n"
        " * linea_intervento: Nome/descrizione della linea\n"
        " * cup: Codice CUP se presente, altrimenti null\n"
        " * rendicontato: Importo rendicontato (numero decimale)\n"
        " * contributo_competenza: Contributo di competenza (numero decimale)\n"
        "- NON includere righe di TOTALE\n"
        "- Importi come float con decimali (es: 125000.50)\n"
        "- Per campi non trovati usa null (non string 'null')\n"
        "- Date in formato YYYY-MM-DD\n"
        "- Numeri come float, non string\n"
        "\n"
        'Se il documento non Ã¨ una Nota di Trasmissione valida, rispondi: {"error": "not_nota_trasmissione"}\n'
        "\n"
        "GENERA SOLO IL JSON SENZA ALCUN TESTO AGGIUNTIVO O MARKDOWN.\n"
    )


def json_to_nota_trasmissione_queries(json_data, pdf_url=None):
    """
    Converte JSON Nota Trasmissione in query SQL INSERT
    
    Args:
        json_data: JSON string con dati estratti
        
    Returns:
        SQL query string o None
    """
    try:
        print("=== NOTA_TRASMISSIONE_OCR VERSION: 2024-11-04-REFACTORED ===")
        print(f"DEBUG - Processing JSON: {json_data[:300]}...")
        
        # Pulisci JSON da markdown
        clean_json = json_data.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        # Controlla errori
        if "error" in data:
            print(f"JSON contiene errore: {data['error']}")
            return None
        
        # Valida struttura
        if "nota_trasmissione" not in data:
            print("JSON non contiene sezione 'nota_trasmissione'")
            return None
        
        if "linee_intervento" not in data:
            print("JSON non contiene sezione 'linee_intervento'")
            return None
        
        nota = data["nota_trasmissione"]
        linee = data["linee_intervento"]
        
        # Valida campi obbligatori
        numero_sal = nota.get("numero_sal")
        data_inizio = nota.get("data_inizio_periodo")
        data_fine = nota.get("data_fine_periodo")
        
        if not numero_sal:
            print("ERROR - numero_sal mancante")
            return None
        
        if not data_fine:
            print("ERROR - data_fine_periodo mancante (obbligatoria)")
            return None
        
        # data_inizio Ã¨ opzionale (puÃ² essere NULL nel DB)
        if not linee or len(linee) == 0:
            print("ERROR - nessuna linea di intervento trovata")
            return None
        
        if data_inizio:
            print(f"DEBUG - SAL: {numero_sal}, Periodo: {data_inizio} - {data_fine}")
        else:
            print(f"DEBUG - SAL: {numero_sal}, Periodo fine: {data_fine} (data_inizio NULL)")
        print(f"DEBUG - Linee intervento: {len(linee)}")
        
        # Genera query SQL per ogni linea
        all_queries = []
        
        for idx, linea in enumerate(linee, 1):
            linea_intervento = linea.get("linea_intervento", "")
            cup = linea.get("cup")
            rendicontato = linea.get("rendicontato", 0)
            contributo_competenza = linea.get("contributo_competenza", 0)
            
            # Validazione
            if not linea_intervento:
                print(f"WARNING - Linea {idx}: linea_intervento vuota, skip")
                continue
            
            # Converti valori
            try:
                rendicontato = float(rendicontato) if rendicontato else 0.0
                contributo_competenza = float(contributo_competenza) if contributo_competenza else 0.0
            except (ValueError, TypeError):
                print(f"WARNING - Linea {idx}: valori numerici non validi, skip")
                continue
            
            # Genera SQL INSERT
            query = f"""INSERT INTO pdf_rendicontazione_polis_sal 
(numero_sal, data_inizio_periodo, data_fine_periodo, linea_intervento, cup, rendicontato, `contributo di competenza`)
VALUES (
{numero_sal},
{safe_value(data_inizio, 'date')},
{safe_value(data_fine, 'date')},
{safe_value(linea_intervento)},
{safe_value(cup) if cup else 'NULL'},
{rendicontato},
{contributo_competenza}
)"""
            
            all_queries.append(query)
            print(f" âœ… Linea {idx}: {linea_intervento[:50]}... (â‚¬{rendicontato:,.2f})")
        
        if not all_queries:
            print("ERROR - Nessuna query generata")
            return None
        
        # Combina tutte le query
        final_query = ";\n".join(all_queries) + ";"
        
        print(f"DEBUG - Generate {len(all_queries)} INSERT queries")
        return final_query
        
    except json.JSONDecodeError as e:
        print(f"ERROR - Invalid JSON: {e}")
        return None
    except Exception as e:
        print(f"ERROR - Processing failed: {e}")
        import traceback
        traceback.print_exc()
        return None

 