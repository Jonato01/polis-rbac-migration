import os
import json
import pandas as pd
from openai import OpenAI
from sqlalchemy import create_engine
import string

# ----------------------------
# CONFIGURAZIONE
# ----------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

# Connessione al database MySQL
engine = create_engine(
    os.getenv("DB_SQLALCHEMY_URL", ""))


# ----------------------------
# FUNZIONI DI SUPPORTO
# ----------------------------
def chiedi_ai(prompt):
    """Funzione generica per chiamare l'AI e restituire testo"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    return response.choices[0].message.content.strip()


def excel_col_to_letter(col_num):
    """Converte numero colonna (0-based) in lettera Excel (A, B, C, ...)"""
    result = ""
    while col_num >= 0:
        result = string.ascii_uppercase[col_num % 26] + result
        col_num = col_num // 26 - 1
        if col_num < 0:
            break
    return result


def get_cell_reference(row_idx, col_idx):
    """Restituisce riferimento cella Excel (es. A1, B2, C3)"""
    col_letter = excel_col_to_letter(col_idx)
    return f"{col_letter}{row_idx + 1}"


def create_cell_data_structure(df, max_rows=10, max_cols=20):
    """Crea struttura dati con riferimenti celle precisi"""
    cell_data = {}

    # Limita il numero di righe e colonne per evitare troppi dati
    actual_rows = min(max_rows, len(df))
    actual_cols = min(max_cols, len(df.columns))

    for row_idx in range(actual_rows):
        for col_idx in range(actual_cols):
            cell_ref = get_cell_reference(row_idx, col_idx)
            try:
                value = df.iloc[row_idx, col_idx]
                # Converti in stringa gestendo valori NaN
                if pd.isna(value):
                    cell_data[cell_ref] = ""
                else:
                    cell_data[cell_ref] = str(value).strip()
            except:
                cell_data[cell_ref] = ""

    return cell_data, actual_rows, actual_cols


def trova_header_row_con_celle(df):
    """Trova la riga che contiene gli header e restituisce info con riferimenti celle"""
    keywords = ['regione', 'provincia', 'comune', 'denominazione', 'sito', 'indirizzo', 'avvii', 'conclusioni',
                'cod_imm']

    for row_idx in range(min(10, len(df))):
        row_values = df.iloc[row_idx].astype(str).str.lower()
        matches = sum(1 for keyword in keywords if any(keyword in val for val in row_values))

        if matches >= 3:
            print(f"Header row trovata alla riga {row_idx + 1} (Excel: riga {row_idx + 1})")

            # Crea mapping celle header
            header_cells = {}
            for col_idx, value in enumerate(df.iloc[row_idx]):
                cell_ref = get_cell_reference(row_idx, col_idx)
                header_cells[cell_ref] = str(value).strip() if not pd.isna(value) else ""

            return row_idx, header_cells

    # Se non trova header, restituisce la prima riga
    header_cells = {}
    for col_idx, value in enumerate(df.iloc[0]):
        cell_ref = get_cell_reference(0, col_idx)
        header_cells[cell_ref] = str(value).strip() if not pd.isna(value) else ""

    return 0, header_cells


def genera_mapping_con_celle(df_sample):
    """Genera la mappatura usando riferimenti celle precisi"""

    # Trova header row con riferimenti celle
    header_row_idx, header_cells = trova_header_row_con_celle(df_sample)

    # Crea struttura dati con riferimenti celle
    cell_data, actual_rows, actual_cols = create_cell_data_structure(df_sample)

    print(f"🔍 DEBUG - Header cells: {header_cells}")
    print(f"🔍 DEBUG - Analizzando {actual_rows} righe e {actual_cols} colonne")

    # Prepara dati per l'AI con riferimenti celle precisi
    prompt = f"""
    Devi mappare le celle di un Excel alla tabella MySQL polis.rendicontazione.

    CAMPI TARGET DEL DATABASE:
    - regione (varchar, obbligatorio)
    - provincia (varchar, obbligatorio) 
    - comune (varchar, obbligatorio)
    - denominazione_sito (varchar, opzionale)
    - indirizzo (varchar, opzionale)
    - avvii (varchar, opzionale)
    - conclusioni (varchar, opzionale)
    - cod_imm (varchar, opzionale)

    STRUTTURA EXCEL RILEVATA:
    Header row individuata alla riga: {header_row_idx + 1}

    HEADER CELLS (riga {header_row_idx + 1}):
    {json.dumps(header_cells, indent=2, ensure_ascii=False)}

    DATI COMPLETI DELLE CELLE (prime {actual_rows} righe):
    {json.dumps(cell_data, indent=2, ensure_ascii=False)}

    ISTRUZIONI:
    1. Analizza i contenuti delle celle per identificare quale COLONNA (lettera Excel) corrisponde a quale campo DB
    2. Concentrati sui valori nelle celle header per capire il contenuto di ogni colonna
    3. Verifica i dati nelle righe successive per confermare il contenuto
    4. Mappa le COLONNE (non le singole celle) ai campi del database
    5. Restituisci il mapping usando le LETTERE delle colonne Excel (A, B, C, etc.)

    FORMATO RISPOSTA (SOLO JSON VALIDO):
    {{
        "A": "campo_db_corrispondente",
        "B": "altro_campo_db",
        "C": "altro_campo_db"
    }}

    Esempio: se la colonna A contiene "Regione" nell'header, restituisci "A": "regione"
    """

    mapping_text = chiedi_ai(prompt)

    # Pulisci il testo da eventuali markdown
    mapping_text = mapping_text.strip()
    if mapping_text.startswith("```json"):
        mapping_text = mapping_text[7:]
    if mapping_text.startswith("```"):
        mapping_text = mapping_text[3:]
    if mapping_text.endswith("```"):
        mapping_text = mapping_text[:-3]
    mapping_text = mapping_text.strip()

    try:
        column_mapping = json.loads(mapping_text)
        print(f"✅ Column mapping parsato correttamente: {column_mapping}")

        # Converti mapping colonne in mapping di indici pandas
        pandas_mapping = {}
        for col_letter, db_field in column_mapping.items():
            # Converti lettera colonna in indice numerico
            col_idx = sum((ord(c) - ord('A') + 1) * (26 ** i) for i, c in enumerate(reversed(col_letter.upper()))) - 1
            if col_idx < len(df_sample.columns):
                original_col_name = df_sample.columns[col_idx]
                pandas_mapping[original_col_name] = db_field
                print(f"📍 Colonna {col_letter} (indice {col_idx}, nome '{original_col_name}') → {db_field}")

        return pandas_mapping, column_mapping

    except json.JSONDecodeError as e:
        print(f"❌ Errore parsing JSON mapping: {e}")
        print(f"Testo ricevuto dall'AI:\n{mapping_text}")

        # Fallback: prova a creare mapping manuale
        pandas_mapping = crea_mapping_fallback(df_sample.columns.tolist())
        column_mapping = {}
        print(f"🔄 Usando mapping di fallback: {pandas_mapping}")
        return pandas_mapping, column_mapping


def crea_mapping_fallback(colonne):
    """Crea un mapping di fallback basato su parole chiave"""
    mapping = {}

    mapping_rules = {
        'regione': ['regione', 'reg'],
        'provincia': ['provincia', 'prov'],
        'comune': ['comune', 'city', 'città'],
        'denominazione_sito': ['denominazione', 'sito', 'nome', 'titolo'],
        'indirizzo': ['indirizzo', 'via', 'address'],
        'avvii': ['avvio', 'avvii', 'inizio', 'start', 'data_inizio'],
        'conclusioni': ['conclusione', 'conclusioni', 'fine', 'end', 'data_fine'],
        'cod_imm': ['cod_imm', 'codice', 'code']
    }

    for col in colonne:
        col_lower = str(col).lower()
        for db_field, keywords in mapping_rules.items():
            if any(keyword in col_lower for keyword in keywords):
                mapping[col] = db_field
                break

    return mapping


def normalizza_linea_intervento(linea):
    """Normalizza la linea di intervento ai due valori canonici usando AI con fallback deterministico"""
    if not linea or linea == "Unknown":
        return linea

    # Fallback deterministico: controlla parole chiave
    linea_lower = linea.lower().replace("'", "'").replace("\u2019", "'")
    if 'spazi' in linea_lower or 'italia' in linea_lower:
        return "Spazi Per l'Italia"
    if 'sportello' in linea_lower:
        return "Sportello Unico"

    # Se non riconosciuta, usa AI per classificare
    try:
        prompt = f"""
        Classifica questa linea di intervento POLIS in UNO dei due valori canonici:
        - "Spazi Per l'Italia"
        - "Sportello Unico"

        Valore da classificare: "{linea}"

        Rispondi SOLO con il valore canonico esatto, nient'altro.
        """
        risposta = chiedi_ai(prompt).strip().strip('"').strip("'")
        if "Spazi" in risposta:
            return "Spazi Per l'Italia"
        if "Sportello" in risposta:
            return "Sportello Unico"
    except Exception:
        pass

    return linea


def estrai_info_file(nome_file, path_cartella):
    """Lascia decidere all'AI linea_intervento e id_sal"""
    prompt = f"""
    Analizza il nome file e il path per estrarre informazioni:

    Nome file: "{nome_file}"
    Path cartella: "{path_cartella}"

    Estrai:
    1. linea_intervento: identifica il tipo di intervento e NORMALIZZA al valore canonico.
       I SOLI valori ammessi sono:
       - "Spazi Per l'Italia" (include varianti come: Spazi Italia, Spazi per Italia, Spazi Per I'Italia, SpaziItalia, ecc.)
       - "Sportello Unico"
    2. id_sal: numero del SAL (State Advancement Level) - cerca numeri seguiti da "SAL" o simili

    Esempi:
    - Se vedi "7_SAL" → id_sal = 7
    - Se vedi "Spazi Italia" o "Spazi per Italia" o qualsiasi variante → linea_intervento = "Spazi Per l'Italia"
    - Se vedi "Sportello Unico" → linea_intervento = "Sportello Unico"

    Rispondi SOLO con JSON valido:
    {{
      "linea_intervento": "valore_canonico",
      "id_sal": numero_intero
    }}
    """

    info_text = chiedi_ai(prompt)

    # Pulisci il testo
    info_text = info_text.strip()
    if info_text.startswith("```json"):
        info_text = info_text[7:]
    if info_text.startswith("```"):
        info_text = info_text[3:]
    if info_text.endswith("```"):
        info_text = info_text[:-3]
    info_text = info_text.strip()

    try:
        info = json.loads(info_text)
        print(f"✅ Info file estratte: {info}")
    except json.JSONDecodeError as e:
        print(f"❌ Errore parsing JSON info file: {e}")
        print(f"Testo ricevuto dall'AI:\n{info_text}")

        # Fallback: estrai info manualmente
        id_sal = 7  # default
        linea_intervento = "Unknown"

        # Cerca SAL nel path
        path_parts = path_cartella.lower().split('/')
        for part in path_parts:
            if 'sal' in part:
                numbers = [int(s) for s in part.split('_') if s.isdigit()]
                if numbers:
                    id_sal = numbers[0]
                    break

        # Cerca linea intervento nel nome file o path
        if 'spazi' in nome_file.lower() or 'spazi' in path_cartella.lower():
            linea_intervento = "Spazi Per l'Italia"
        elif 'sportello' in nome_file.lower() or 'sportello' in path_cartella.lower():
            linea_intervento = "Sportello Unico"

        info = {"linea_intervento": linea_intervento, "id_sal": id_sal}
        print(f"🔄 Usando info di fallback: {info}")

    # Normalizza sempre la linea di intervento
    info['linea_intervento'] = normalizza_linea_intervento(info.get('linea_intervento', ''))

    return info


def importa_excel(path_file, path_cartella):
    """Importa un singolo Excel nel DB"""
    print(f"\n{'=' * 60}")
    print(f">>> Importazione file: {os.path.basename(path_file)}")
    print(f"{'=' * 60}")

    # Estrai info dal nome file + path usando AI
    info = estrai_info_file(os.path.basename(path_file), path_cartella)
    linea = info.get("linea_intervento")
    id_sal = info.get("id_sal")

    try:
        # Leggi Excel - prova diversi metodi per gestire file corrotti
        print("📖 Lettura file Excel...")
        df = pd.read_excel(path_file, engine='openpyxl')
        print(f"✅ File letto con successo: {df.shape[0]} righe, {df.shape[1]} colonne")

    except Exception as e:
        print(f"❌ Errore lettura Excel: {e}")
        try:
            # Prova con xlrd per file .xls
            df = pd.read_excel(path_file, engine='xlrd')
            print(f"✅ File letto con xlrd: {df.shape[0]} righe, {df.shape[1]} colonne")
        except Exception as e2:
            print(f"❌ Errore anche con xlrd: {e2}")
            return 0

    # Mostra info preliminari con riferimenti celle
    print(f"📊 Dimensioni: {df.shape[0]} righe x {df.shape[1]} colonne")
    print(f"📊 Range celle: A1 fino a {excel_col_to_letter(df.shape[1] - 1)}{df.shape[0]}")

    # Genera mapping dinamico con riferimenti celle
    print("\n🔄 Generazione mapping colonne con riferimenti celle...")
    pandas_mapping, column_mapping = genera_mapping_con_celle(df)

    if column_mapping:
        print(f"📋 Column mapping (Excel): {column_mapping}")
    print(f"📋 Pandas mapping: {pandas_mapping}")

    if not pandas_mapping:
        print("⚠️ Nessun mapping trovato - skip file")
        return 0

    # Applica mapping e rimuovi colonne che fanno riferimento a None
    df_renamed = df.rename(columns=pandas_mapping)
    target_columns = [col for col in pandas_mapping.values() if col in df_renamed.columns and col is not None]

    if not target_columns:
        print("⚠️ Nessuna colonna target trovata dopo mapping - skip file")
        return 0

    df_db = df_renamed[target_columns].copy()
    print(f"📋 Colonne mappate disponibili: {df_db.columns.tolist()}")

    # Trova e rimuovi righe header
    header_row_idx, _ = trova_header_row_con_celle(df_db)
    if header_row_idx > 0:
        df_db = df_db.iloc[header_row_idx + 1:].copy()
        print(f"🔄 Rimossi header dalla riga Excel {header_row_idx + 1}")

    # Pulizia dati
    print("\n🧹 Pulizia dati...")

    # Rimuovi righe completamente vuote
    initial_rows = len(df_db)
    df_db = df_db.dropna(how='all')
    print(f"🗑️ Rimosse {initial_rows - len(df_db)} righe completamente vuote")

    # Salta la prima riga di dati importati solo se contiene header
    if len(df_db) > 0:
        # Verifica se la prima riga contiene header
        first_row_values = df_db.iloc[0].astype(str).str.lower()
        header_keywords = ['regione', 'provincia', 'comune', 'denominazione', 'sito', 'indirizzo', 'avvii', 'conclusioni', 'cod_imm']

        # Conta quante parole chiave di header sono presenti nella prima riga
        header_matches = sum(1 for keyword in header_keywords if any(keyword in val for val in first_row_values))

        if header_matches >= 2:  # Se trova almeno 2 parole chiave tipiche degli header
            df_db = df_db.iloc[1:].copy()
            print(f"🔄 Saltata la prima riga perché contiene header della tabella")
        else:
            print(f"🔄 Prima riga mantenuta - non sembra contenere header")

    # Rimuovi righe dove le colonne principali sono tutte vuote
    main_columns = ['regione', 'provincia', 'comune']
    existing_main_columns = [col for col in main_columns if col in df_db.columns]

    if existing_main_columns:
        before_cleanup = len(df_db)
        df_db = df_db.dropna(subset=existing_main_columns, how='all')
        print(f"🗑️ Rimosse {before_cleanup - len(df_db)} righe con campi principali vuoti")

    # Pulisci e converti valori
    for col in df_db.columns:
        if col in ['regione', 'provincia', 'comune', 'denominazione_sito', 'indirizzo', 'cod_imm']:
            # Converti a stringa, sostituisci 'nan' e pulisci spazi
            df_db[col] = df_db[col].fillna('').astype(str).replace('nan', '').str.strip()



    # Aggiungi colonne extra
    df_db['id_sal'] = id_sal
    df_db['linea_itervento'] = linea

    # Controllo finale
    if len(df_db) == 0:
        print("⚠️ Nessun dato rimasto dopo la pulizia - skip import")
        return 0

    print(f"\n📤 Pronto per import: {len(df_db)} righe")
    print(f"📋 Colonne finali: {df_db.columns.tolist()}")

    # Mostra anteprima dati finali
    print(f"\n👀 Anteprima dati da importare:")
    print(df_db.head(2).to_string())

    # Salva mapping per riferimento futuro
    mapping_info = {
        "file": os.path.basename(path_file),
        "excel_column_mapping": column_mapping,
        "pandas_mapping": pandas_mapping,
        "target_columns": target_columns,
        "header_row": header_row_idx + 1,
        "total_rows_imported": len(df_db)
    }

    mapping_file = f"mapping_{os.path.splitext(os.path.basename(path_file))[0]}.json"
    with open(mapping_file, 'w', encoding='utf-8') as f:
        json.dump(mapping_info, f, indent=2, ensure_ascii=False)
    print(f"💾 Mapping salvato in {mapping_file}")

    # Scrivi nel DB
    try:
        df_db.to_sql("rendicontazione", con=engine, schema="polis", if_exists="append", index=False)
        print(f"\n✅ IMPORT COMPLETATO: {len(df_db)} righe caricate nel database!")
        return len(df_db)

    except Exception as e:
        print(f"\n❌ ERRORE durante l'import nel database: {e}")

        # Salva file di debug
        debug_file = f"debug_import_{os.path.basename(path_file)}.csv"
        df_db.to_csv(debug_file, index=False)
        print(f"💾 Dati salvati in {debug_file} per debug")
        raise


# ----------------------------
# MAIN
# ----------------------------
if __name__ == "__main__":
    cartella = r"C:\Users\Renato\OneDrive\Documenti\da caricare\polis\6° SAL al 31 Agosto 2024\6 SAL_relazione tecnica"

    print("🚀 Avvio processo di import Excel con riferimenti celle precisi")
    print(f"📂 Cartella: {cartella}")

    files_processed = 0

    for file in os.listdir(cartella):
        if ("rendicont" in file.lower()) and (file.endswith(".xlsx") or file.endswith(".xls")):
            print(f"\n📄 Trovato file: {file}")
            importa_excel(os.path.join(cartella, file), cartella)
            files_processed += 1

    print(f"\n🏁 Processo completato! Files processati: {files_processed}")