from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
import json
from dotenv import load_dotenv
import PyPDF2
import io
import os

# Carica .env
load_dotenv(r'D:\gitlab\Polis\polis-ocr\.env')

def extract_nota_metodologica_with_multiple_models():
    """Prova diversi modelli Azure per trovare il migliore per l'estrazione delle tabelle"""
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    api_key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")

    print(f" Connessione ad Azure Document Intelligence...")

    client = DocumentAnalysisClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(api_key)
    )

    # PDF path per la Nota metodologica
    pdf_path = r"C:\Users\Renato\OneDrive\Documenti\da caricare\polis\8_SAL_30_04_2025\2025_8 SAL Costo del personale\Nota metodologica_costo personale Polis_update.pdf"

    # Verifica che il file esista
    if not os.path.exists(pdf_path):
        print(f" ERRORE: File non trovato: {pdf_path}")
        return None

    # Lista dei modelli da provare (dal più specifico al più generale)
    models_to_try = [
        ("prebuilt-document", "Modello Document (migliore per tabelle strutturate)"),
        ("prebuilt-layout", "Modello Layout (buono per layout complessi)"),
        ("prebuilt-read", "Modello Read (OCR avanzato con tabelle)")
    ]

    all_results = {}
    best_model = None
    max_tables_found = 0

    for model_name, description in models_to_try:
        print(f"\n Provo con {description}...")

        try:
            with open(pdf_path, "rb") as pdf_file:
                poller = client.begin_analyze_document(model_name, document=pdf_file.read())
                result = poller.result()

            tables_found = len(result.tables) if hasattr(result, 'tables') and result.tables else 0
            print(f" {model_name}: trovate {tables_found} tabelle")

            # Processa le tabelle se trovate
            processed_tables = []
            if tables_found > 0:
                for idx, table in enumerate(result.tables):
                    # Determina la pagina della tabella
                    page_num = 1
                    if hasattr(table, 'bounding_regions') and table.bounding_regions:
                        page_num = table.bounding_regions[0].page_number

                    # Ricostruisci la tabella
                    table_matrix = [['' for _ in range(table.column_count)] for _ in range(table.row_count)]

                    for cell in table.cells:
                        if (cell.row_index < len(table_matrix) and
                            cell.column_index < len(table_matrix[0])):
                            table_matrix[cell.row_index][cell.column_index] = cell.content.strip()

                    table_data = {
                        "tabella_num": idx + 1,
                        "pagina": page_num,
                        "righe": table.row_count,
                        "colonne": table.column_count,
                        "dati": table_matrix,
                        "confidence": getattr(table, 'confidence', None),
                        "modello_usato": model_name
                    }
                    processed_tables.append(table_data)

                    # Mostra anteprima delle prime righe
                    print(f" Tabella {idx + 1} (pag. {page_num}): {table.row_count}x{table.column_count}")
                    if table_matrix:
                        for i, row in enumerate(table_matrix[:2]): # Prime 2 righe
                            row_preview = [str(cell)[:20] + "..." if len(str(cell)) > 20 else str(cell) for cell in row[:3]]
                            print(f" Riga {i + 1}: {row_preview}")

            all_results[model_name] = {
                "modello": model_name,
                "descrizione": description,
                "tabelle_trovate": tables_found,
                "tabelle": processed_tables,
                "successo": True
            }

            # Tieni traccia del modello migliore
            if tables_found > max_tables_found:
                max_tables_found = tables_found
                best_model = model_name

        except Exception as e:
            print(f" ERRORE con {model_name}: {e}")
            all_results[model_name] = {
                "modello": model_name,
                "descrizione": description,
                "tabelle_trovate": 0,
                "errore": str(e),
                "tabelle": [],
                "successo": False
            }

    # Risultati finali
    print(f"\n RIEPILOGO RISULTATI:")
    for model_name, result in all_results.items():
        status = "" if result["successo"] else ""
        print(f"{status} {model_name}: {result['tabelle_trovate']} tabelle")

    if best_model:
        print(f"\n MIGLIOR MODELLO: {best_model} con {max_tables_found} tabelle")

        # Salva risultati del miglior modello
        best_tables = all_results[best_model]["tabelle"]
        output_file = f"azure_nota_metodologica_{best_model.replace('-', '_')}.json"

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(best_tables, f, indent=2, ensure_ascii=False)

        # Salva anche tutti i risultati per confronto
        output_file_all = "azure_nota_metodologica_all_models_comparison.json"
        with open(output_file_all, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)

        print(f" Migliori risultati salvati in: {output_file}")
        print(f" Confronto completo salvato in: {output_file_all}")

        return best_tables
    else:
        print(f"\n Nessun modello è riuscito a estrarre tabelle")
        return None


def extract_with_advanced_table_detection():
    """Versione avanzata che usa tecniche multiple per il rilevamento tabelle"""
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    api_key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")

    client = DocumentAnalysisClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(api_key)
    )

    pdf_path = r"C:\Users\Renato\OneDrive\Documenti\da caricare\polis\8_SAL_30_04_2025\2025_8 SAL Costo del personale\Nota metodologica_costo personale Polis_update.pdf"

    if not os.path.exists(pdf_path):
        print(f" ERRORE: File non trovato: {pdf_path}")
        return None

    print(f" Analisi avanzata con rilevamento tabelle migliorato...")

    try:
        # Usa il modello prebuilt-document con parametri ottimizzati
        with open(pdf_path, "rb") as pdf_file:
            # Analizza l'intero documento
            poller = client.begin_analyze_document(
                "prebuilt-document",
                document=pdf_file.read()
            )
            result = poller.result()

        print(f" Documento analizzato: {len(result.pages)} pagine")

        # Estrai informazioni dettagliate
        all_tables = []
        tables_by_page = {}

        # Processa le tabelle
        if hasattr(result, 'tables') and result.tables:
            print(f" Trovate {len(result.tables)} tabelle totali")

            for idx, table in enumerate(result.tables):
                # Informazioni sulla posizione
                page_num = 1
                bounding_box = None

                if hasattr(table, 'bounding_regions') and table.bounding_regions:
                    page_num = table.bounding_regions[0].page_number
                    bounding_box = {
                        "page": page_num,
                        "polygon": [{"x": p.x, "y": p.y} for p in table.bounding_regions[0].polygon]
                    }

                # Ricostruisci tabella con informazioni dettagliate delle celle
                table_matrix = [['' for _ in range(table.column_count)] for _ in range(table.row_count)]
                cell_details = {}

                for cell in table.cells:
                    if (cell.row_index < len(table_matrix) and
                        cell.column_index < len(table_matrix[0])):

                        content = cell.content.strip()
                        table_matrix[cell.row_index][cell.column_index] = content

                        # Salva dettagli della cella
                        cell_key = f"{cell.row_index},{cell.column_index}"
                        cell_details[cell_key] = {
                            "content": content,
                            "confidence": getattr(cell, 'confidence', None),
                            "row_span": getattr(cell, 'row_span', 1),
                            "column_span": getattr(cell, 'column_span', 1),
                            "is_header": getattr(cell, 'kind', None) == 'header'
                        }

                table_data = {
                    "tabella_num": idx + 1,
                    "pagina": page_num,
                    "righe": table.row_count,
                    "colonne": table.column_count,
                    "dati": table_matrix,
                    "celle_dettagli": cell_details,
                    "bounding_box": bounding_box,
                    "confidence": getattr(table, 'confidence', None)
                }

                all_tables.append(table_data)

                # Organizza per pagina
                if page_num not in tables_by_page:
                    tables_by_page[page_num] = []
                tables_by_page[page_num].append(table_data)

                print(f" Tabella {idx + 1} (pag. {page_num}): {table.row_count}x{table.column_count}")

        # Analizza anche il testo per trovare potenziali tabelle non rilevate
        print(f"\n Analisi del testo per tabelle non rilevate...")
        potential_tables = []

        for page_idx, page in enumerate(result.pages):
            if hasattr(page, 'lines'):
                page_lines = [line.content for line in page.lines]

                # Cerca pattern che potrebbero essere tabelle
                for i, line in enumerate(page_lines):
                    # Pattern comuni per righe di tabelle (separatori, allineamenti, etc.)
                    if (any(sep in line for sep in ['│', '|', '\t']) or
                        len(line.split()) > 3 and
                        any(char.isdigit() for char in line)):

                        potential_tables.append({
                            "pagina": page_idx + 1,
                            "riga": i + 1,
                            "contenuto": line.strip(),
                            "potenziale_tabella": True
                        })

        print(f" Trovate {len(potential_tables)} righe che potrebbero essere tabelle non rilevate")

        # Salva risultati completi
        complete_results = {
            "documento": pdf_path,
            "totale_pagine": len(result.pages),
            "tabelle_rilevate": len(all_tables),
            "tabelle": all_tables,
            "tabelle_per_pagina": tables_by_page,
            "potenziali_tabelle_non_rilevate": potential_tables,
            "metodo_analisi": "advanced_detection"
        }

        # File di output
        output_file = "azure_nota_metodologica_advanced_detection.json"

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(complete_results, f, indent=2, ensure_ascii=False)

        print(f"\n Risultati analisi avanzata salvati in: {output_file}")
        print(f" Totale tabelle rilevate: {len(all_tables)}")

        return all_tables

    except Exception as e:
        print(f" ERRORE durante l'analisi avanzata: {e}")
        return None


if __name__ == "__main__":
    print(" Avvio analisi con modelli multipli per massimizzare il rilevamento delle tabelle...\n")

    # Prima prova con modelli multipli
    result1 = extract_nota_metodologica_with_multiple_models()

    print("\n" + "="*80 + "\n")

    # Poi prova con rilevamento avanzato
    result2 = extract_with_advanced_table_detection()

    if result1 or result2:
        print("\n Analisi completata! Controlla i file JSON generati per vedere tutti i risultati.")
        print(" Confronta i risultati dei diversi modelli per scegliere il migliore per il tuo caso d'uso.")
    else:
        print("\n Nessun modello è riuscito a estrarre tabelle dal documento.")
        print(" Verifica che il PDF contenga effettivamente tabelle strutturate.")
