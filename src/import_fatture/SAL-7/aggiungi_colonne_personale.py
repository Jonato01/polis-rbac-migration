#!/usr/bin/env python3
"""
Script per aggiungere colonne "Campionamento" e "Risultato controllo"
all'inizio dei file Excel nella riga di intestazione.

Autore: Script generato per progetto SAL-8
Data: Agosto 2025
"""

import pandas as pd
import openpyxl
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import os
import sys
import argparse
import logging
from datetime import datetime


def setup_logging():
    """Configura il sistema di logging"""
    if not os.path.exists('../SAL-8/logs'):
        os.makedirs('../SAL-8/logs')

    log_filename = f'logs/aggiungi_colonne_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)


def trova_riga_intestazione(ws):
    """
    Trova la riga di intestazione nel foglio Excel.
    Cerca righe che contengono colonne tipiche come 'SAL', 'Categoria', etc.
    """
    parole_chiave = [
        'sal', 'categoria', 'spesa', 'accordo', 'fornitore',
        'fattura', 'numero', 'data', 'importo', 'iva'
    ]

    for row_num in range(1, 10):  # Cerca nelle prime 10 righe
        row = ws[row_num]
        celle_con_testo = 0
        corrispondenze = 0

        for cell in row:
            if cell.value and isinstance(cell.value, str):
                celle_con_testo += 1
                testo_minuscolo = cell.value.lower()
                if any(parola in testo_minuscolo for parola in parole_chiave):
                    corrispondenze += 1

        # Se abbiamo almeno 3 celle con testo e almeno 2 corrispondenze
        if celle_con_testo >= 3 and corrispondenze >= 2:
            return row_num

    # Default: assume che l'intestazione sia alla riga 3 (come nel codice di import)
    return 3


def copia_stile_cella(cella_sorgente, cella_destinazione):
    """Copia lo stile da una cella sorgente a una destinazione"""
    try:
        # Copia font
        if hasattr(cella_sorgente, 'font') and cella_sorgente.font:
            cella_destinazione.font = Font(
                name=cella_sorgente.font.name or 'Calibri',
                size=cella_sorgente.font.size or 11,
                bold=cella_sorgente.font.bold or False,
                italic=cella_sorgente.font.italic or False
            )

        # Copia fill/background
        if hasattr(cella_sorgente, 'fill') and cella_sorgente.fill:
            try:
                if hasattr(cella_sorgente.fill, 'start_color') and cella_sorgente.fill.start_color:
                    cella_destinazione.fill = PatternFill(
                        fill_type='solid',
                        start_color=cella_sorgente.fill.start_color.rgb if hasattr(cella_sorgente.fill.start_color, 'rgb') else 'E6E6FA'
                    )
            except:
                # Se fallisce, usa un colore di default
                pass

        # Copia allineamento
        if hasattr(cella_sorgente, 'alignment') and cella_sorgente.alignment:
            cella_destinazione.alignment = Alignment(
                horizontal=cella_sorgente.alignment.horizontal or 'center',
                vertical=cella_sorgente.alignment.vertical or 'center',
                wrap_text=True
            )

        # Copia bordi in modo semplificato
        if hasattr(cella_sorgente, 'border') and cella_sorgente.border:
            try:
                side = Side(style='thin', color='000000')
                cella_destinazione.border = Border(
                    left=side,
                    right=side,
                    top=side,
                    bottom=side
                )
            except:
                # Se fallisce, salta i bordi
                pass

    except Exception as e:
        # Se tutto fallisce, applica uno stile di base
        cella_destinazione.font = Font(bold=True, size=11)
        cella_destinazione.fill = PatternFill(start_color="E6E6FA", end_color="E6E6FA", fill_type="solid")
        cella_destinazione.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def trova_ultima_riga_tabella(ws, riga_intestazione):
    """
    Trova l'ultima riga della tabella nel foglio Excel - si ferma alla prima riga vuota
    """
    ultima_riga_con_dati = riga_intestazione

    # Scansiona riga per riga e fermati alla prima riga completamente vuota
    row_num = riga_intestazione + 1
    while True:
        # Controlla le colonne dalla C in poi (dopo le nostre A e B aggiunte)
        riga_ha_dati = False
        for col_num in range(3, 13):  # Colonne C-L (dopo le nostre A e B aggiunte)
            try:
                cell = ws.cell(row=row_num, column=col_num)
                if cell.value is not None and str(cell.value).strip():
                    riga_ha_dati = True
                    break
            except:
                continue

        if riga_ha_dati:
            ultima_riga_con_dati = row_num
            row_num += 1
        else:
            # Prima riga senza dati trovata - FERMIAMOCI QUI
            break

        # Limite di sicurezza per evitare loop infiniti (molto alto)
        if row_num > riga_intestazione + 50000:
            break

    return ultima_riga_con_dati


def correggi_formule_dopo_inserimento(ws, riga_intestazione, ultima_riga, logger):
    """
    Corregge i riferimenti delle formule dopo l'inserimento delle colonne A e B
    Sposta i riferimenti di 2 colonne a destra per compensare l'inserimento
    """
    logger.info("Correzione riferimenti formula dopo inserimento colonne...")

    formule_corrette = 0

    # Scansiona tutte le celle dall'intestazione alla fine dei dati
    for row_num in range(riga_intestazione, ultima_riga + 1):
        for col_num in range(3, 50):  # Controlla colonne dalla C in poi
            try:
                cell = ws.cell(row=row_num, column=col_num)

                # Se la cella contiene una formula
                if cell.value and isinstance(cell.value, str) and cell.value.startswith('='):
                    formula_originale = cell.value
                    formula_corretta = correggi_riferimenti_formula(formula_originale)

                    if formula_corretta != formula_originale:
                        cell.value = formula_corretta
                        formule_corrette += 1

                        if formule_corrette <= 5:  # Log solo le prime 5 per non intasare
                            logger.info(f"Formula corretta in {chr(64+col_num)}{row_num}: {formula_originale} -> {formula_corretta}")

            except Exception as e:
                continue

    logger.info(f"Corrette {formule_corrette} formule")


def correggi_riferimenti_formula(formula):
    """
    Corregge i riferimenti di colonna in una formula spostando di 2 posizioni a destra
    Esempio: =A1+B2 diventa =C1+D2 (perché A,B sono ora le nuove colonne)
    """
    import re

    def sostituisci_riferimento(match):
        col_letter = match.group(1)
        row_number = match.group(2)

        # Converti lettera colonna in numero (A=1, B=2, etc.)
        col_num = 0
        for char in col_letter:
            col_num = col_num * 26 + (ord(char) - ord('A') + 1)

        # Sposta di 2 posizioni a destra (per compensare A,B aggiunte)
        nuovo_col_num = col_num + 2

        # Riconverti in lettere
        nuovo_col_letter = ""
        while nuovo_col_num > 0:
            nuovo_col_num -= 1
            nuovo_col_letter = chr(nuovo_col_num % 26 + ord('A')) + nuovo_col_letter
            nuovo_col_num //= 26

        return nuovo_col_letter + row_number

    # Pattern per trovare riferimenti di cella come A1, B2, AA10, etc.
    pattern = r'\b([A-Z]+)(\d+)\b'
    formula_corretta = re.sub(pattern, sostituisci_riferimento, formula)

    return formula_corretta


def allarga_colonne_strette(ws, riga_intestazione, ultima_riga, logger, larghezza_minima=10):
    """
    Controlla tutte le colonne della tabella e allarga quelle che sono troppo strette.

    Args:
        ws: Worksheet di openpyxl
        riga_intestazione: Numero della riga di intestazione
        ultima_riga: Ultima riga con dati
        logger: Logger per messaggi
        larghezza_minima: Larghezza minima desiderata per le colonne (default: 10)
    """
    logger.info(f"Controllo larghezza colonne (minimo: {larghezza_minima})")

    colonne_allargate = 0

    try:
        # Determina l'ultima colonna con dati
        ultima_colonna = 1
        for row_num in range(riga_intestazione, min(riga_intestazione + 5, ultima_riga + 1)):
            for col_num in range(1, 50):  # Controlla fino alla colonna AX
                cell = ws.cell(row=row_num, column=col_num)
                if cell.value is not None and str(cell.value).strip():
                    ultima_colonna = max(ultima_colonna, col_num)

        logger.info(f"Ultima colonna con dati identificata: {chr(64 + ultima_colonna)} (colonna {ultima_colonna})")

        # Controlla ogni colonna dalla A all'ultima colonna con dati
        for col_num in range(1, ultima_colonna + 1):
            col_letter = chr(64 + col_num) if col_num <= 26 else chr(64 + ((col_num - 1) // 26)) + chr(64 + ((col_num - 1) % 26) + 1)

            try:
                # Ottieni la larghezza corrente della colonna
                current_width = ws.column_dimensions[col_letter].width

                # Se la larghezza è None o troppo piccola, impostala al minimo
                if current_width is None or current_width < larghezza_minima:
                    # Calcola una larghezza ottimale basata sul contenuto
                    larghezza_ottimale = calcola_larghezza_ottimale(ws, col_num, riga_intestazione, ultima_riga, larghezza_minima)

                    ws.column_dimensions[col_letter].width = larghezza_ottimale
                    colonne_allargate += 1

                    logger.info(f"Colonna {col_letter}: {current_width} -> {larghezza_ottimale}")

            except Exception as e:
                logger.warning(f"Errore nel processare colonna {col_letter}: {e}")
                continue

        logger.info(f"Allargate {colonne_allargate} colonne troppo strette")

    except Exception as e:
        logger.warning(f"Errore generale nell'allargamento colonne: {e}")


def calcola_larghezza_ottimale(ws, col_num, riga_intestazione, ultima_riga, larghezza_minima):
    """
    Calcola la larghezza ottimale per una colonna basandosi sul contenuto.

    Args:
        ws: Worksheet
        col_num: Numero della colonna
        riga_intestazione: Riga di intestazione
        ultima_riga: Ultima riga con dati
        larghezza_minima: Larghezza minima

    Returns:
        Larghezza ottimale calcolata
    """
    max_length = 0

    try:
        # Campiona alcune righe per calcolare la larghezza necessaria
        righe_da_controllare = min(20, ultima_riga - riga_intestazione + 1)
        step = max(1, (ultima_riga - riga_intestazione + 1) // righe_da_controllare)

        for row_num in range(riga_intestazione, ultima_riga + 1, step):
            cell = ws.cell(row=row_num, column=col_num)
            if cell.value is not None:
                content_length = len(str(cell.value))
                max_length = max(max_length, content_length)

        # Calcola larghezza ottimale: lunghezza massima + margine, ma non meno del minimo
        larghezza_ottimale = max(larghezza_minima, min(max_length + 2, 30))  # Max 30 per evitare colonne troppo larghe

        return larghezza_ottimale

    except Exception:
        return larghezza_minima


def applica_filtro_automatico(ws, riga_intestazione, ultima_riga, logger):
    """
    Applica il filtro automatico SOLO alle prime due colonne (A e B)
    """
    try:
        # Definisci il range per il filtro automatico SOLO per le colonne A e B
        # Dal primo angolo (A + riga intestazione) al secondo angolo (B + ultima_riga)
        filter_range = f"A{riga_intestazione}:B{ultima_riga}"

        # Applica il filtro automatico solo alle prime due colonne
        ws.auto_filter.ref = filter_range

        logger.info(f"Filtro automatico applicato SOLO alle prime due colonne: {filter_range}")

    except Exception as e:
        logger.warning(f"Errore nell'applicazione del filtro automatico: {e}")


def pulisci_righe_dopo_tabella(ws, ultima_riga_tabella, logger):
    """
    Cancella TUTTE le righe a partire dalla fine della tabella
    """
    try:
        riga_inizio_pulizia = ultima_riga_tabella + 1
        max_row_foglio = ws.max_row

        logger.info(f"Pulizia TUTTE le righe dalla {riga_inizio_pulizia} alla {max_row_foglio}")

        if riga_inizio_pulizia > max_row_foglio:
            logger.info("Nessuna riga da pulire - la tabella arriva già alla fine del foglio")
            return

        ws.delete_rows(riga_inizio_pulizia, max_row_foglio - riga_inizio_pulizia + 1)
    except Exception as e:
        logger.warning(f"Errore durante la pulizia delle righe: {e}")


def aggiungi_colonne_a_foglio(ws, riga_intestazione, logger):
    """
    Aggiunge le due colonne all'inizio del foglio specificato con formattazione veloce
    INSERENDO nuove colonne e spostando le esistenti verso destra
    """
    logger.info(f"Aggiunta colonne al foglio '{ws.title}' alla riga {riga_intestazione}")
    try:
        ultima_riga_reale = trova_ultima_riga_tabella(ws, riga_intestazione)
        logger.info(f"Ultima riga REALE con dati identificata: {ultima_riga_reale}")

        if ultima_riga_reale < riga_intestazione + 2:
            logger.warning(f"Ultima riga trovata troppo vicina ({ultima_riga_reale}), uso default minimo")
            ultima_riga_reale = riga_intestazione + 10

    except Exception as e:
        logger.warning(f"Errore nel trovare l'ultima riga, uso default: {e}")
        ultima_riga_reale = riga_intestazione + 50
    pulisci_righe_dopo_tabella(ws, ultima_riga_reale, logger)
    # INSERIRE due nuove colonne all'inizio - questo sposta le esistenti verso destra
    ws.insert_cols(1, 2)
    logger.info("Inserite 2 nuove colonne all'inizio - colonne esistenti spostate verso destra")

    # Trova l'ULTIMA RIGA REALE con dati (ora nelle colonne spostate)


    # PULISCI LE RIGHE dopo la fine della tabella


    # CORREGGI LE FORMULE dopo l'inserimento delle colonne
    correggi_formule_dopo_inserimento(ws, riga_intestazione, ultima_riga_reale, logger)

    # Aggiungi i titoli delle nuove colonne nelle colonne A e B (le nuove)
    cella_campionamento = ws.cell(row=riga_intestazione, column=1)  # Nuova Colonna A
    cella_risultato = ws.cell(row=riga_intestazione, column=2)       # Nuova Colonna B

    cella_campionamento.value = "Campionamento"
    cella_risultato.value = "Risultato controllo"

    # Trova una cella di riferimento per copiare lo stile (dalle colonne spostate)
    cella_riferimento = None
    try:
        row = ws[riga_intestazione]
        for cell in row[2:7]:  # Controlla colonne C-G (che erano A-E prima dello spostamento)
            if cell.value:
                cella_riferimento = cell
                break
    except:
        logger.warning("Errore nel trovare cella di riferimento")

    # Definisci gli stili una volta sola
    font_header = Font(bold=True, size=11)
    fill_header = PatternFill(start_color="E6E6FA", end_color="E6E6FA", fill_type="solid")
    alignment_header = Alignment(horizontal="center", vertical="center", wrap_text=True)
    alignment_data = Alignment(horizontal="center", vertical="center")

    border_style = Side(style='thin', color='000000')
    border = Border(
        left=border_style,
        right=border_style,
        top=border_style,
        bottom=border_style
    )

    # Applica stile alle intestazioni
    cella_campionamento.font = font_header
    cella_campionamento.fill = fill_header
    cella_campionamento.alignment = alignment_header
    cella_campionamento.border = border

    cella_risultato.font = font_header
    cella_risultato.fill = fill_header
    cella_risultato.alignment = alignment_header
    cella_risultato.border = border

    logger.info(f"Applicazione formattazione veloce dalle righe {riga_intestazione + 1} a {ultima_riga_reale}")
    logger.info(f"Numero righe da formattare: {ultima_riga_reale - riga_intestazione}")

    # FORMATTAZIONE VELOCE: Applica stili SOLO alle righe con dati reali
    try:
        # Definisci gli intervalli di celle per le due colonne BASATI SULLA REALE ESTENSIONE
        range_colonna_a = f"A{riga_intestazione + 1}:A{ultima_riga_reale}"
        range_colonna_b = f"B{riga_intestazione + 1}:B{ultima_riga_reale}"

        logger.info(f"Intervalli di formattazione: {range_colonna_a} e {range_colonna_b}")

        # Applica bordi e allineamento a tutti gli intervalli in una volta
        for cell_range in [range_colonna_a, range_colonna_b]:
            for row in ws[cell_range]:
                for cell in row:
                    cell.border = border
                    cell.alignment = alignment_data

        logger.info(f"Formattazione applicata con successo a {ultima_riga_reale - riga_intestazione} righe")

    except Exception as e:
        logger.warning(f"Errore nella formattazione veloce, uso metodo alternativo: {e}")
        # Fallback al metodo precedente se la formattazione veloce fallisce
        for row_num in range(riga_intestazione + 1, ultima_riga_reale + 1):
            try:
                cell_a = ws.cell(row=row_num, column=1)
                cell_b = ws.cell(row=row_num, column=2)

                cell_a.border = border
                cell_b.border = border
                cell_a.alignment = alignment_data
                cell_b.alignment = alignment_data
            except:
                continue

    # Imposta larghezza colonne
    try:
        ws.column_dimensions['A'].width = 15
        ws.column_dimensions['B'].width = 18
    except Exception as e:
        logger.warning(f"Errore nell'impostazione larghezza colonne: {e}")

    # APPLICA FILTRO AUTOMATICO alla riga di intestazione
    applica_filtro_automatico(ws, riga_intestazione, ultima_riga_reale, logger)

    # ALLARGA COLONNE TROPPO STRETTE
    allarga_colonne_strette(ws, riga_intestazione, ultima_riga_reale, logger, larghezza_minima=12)

    logger.info(f"Colonne aggiunte con successo")


def processa_file_excel(file_path, fogli_target=None, output_path=None, logger=None):
    """
    Processa un file Excel aggiungendo le colonne richieste

    Args:
        file_path: Path del file Excel di input
        fogli_target: Lista dei nomi dei fogli da processare (None = tutti)
        output_path: Path del file di output (None = crea nuovo file)
        logger: Logger per messaggi
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    logger.info(f"Apertura file: {file_path}")

    try:
        # Apri il workbook con openpyxl per mantenere la formattazione
        wb = openpyxl.load_workbook(file_path)

        # Check if at least one worksheet exists
        if not wb.worksheets:
            logger.error("Il file Excel non contiene alcun foglio di lavoro")
            return False

        fogli_processati = 0

        # Process only the first worksheet
        ws = wb.worksheets[0]
        sheet_name = ws.title

        logger.info(f"Processando solo il primo foglio: '{sheet_name}'")

        # Trova la riga di intestazione
        riga_intestazione = trova_riga_intestazione(ws)
        logger.info(f"Riga intestazione identificata: {riga_intestazione}")

        # Controlla se le colonne esistono già
        prima_cella = ws.cell(row=riga_intestazione, column=1).value
        seconda_cella = ws.cell(row=riga_intestazione, column=2).value

        if (prima_cella == "Campionamento" or
            seconda_cella == "Risultato controllo"):
            logger.warning(f"Foglio '{sheet_name}' sembra già avere le colonne aggiunte, saltato")
        else:
            # Aggiungi le colonne
            aggiungi_colonne_a_foglio(ws, riga_intestazione, logger)
            fogli_processati += 1

        # Determina il nome del file di output
        if output_path:
            output_file = output_path
        else:
            # Crea un nuovo nome file con suffisso
            base_name = os.path.splitext(file_path)[0]
            extension = os.path.splitext(file_path)[1]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"{base_name}_campionato.xlsx"

        wb.save(output_file)

        logger.info(f"File salvato: {output_file}")
        logger.info(f"Fogli processati: {fogli_processati}")

        return True

    except Exception as e:
        logger.error(f"Errore nel processamento del file {file_path}: {e}")
        return False


def main():
    """Funzione principale"""
    parser = argparse.ArgumentParser(
        description='Aggiunge colonne "Campionamento" e "Risultato controllo" ai file Excel'
    )

    parser.add_argument(
        '--file',
        type=str,
        help='Path del file Excel da processare'
    )

    parser.add_argument(
        '--output',
        type=str,
        help='Path del file di output (default: sovrascrivi originale)'
    )

    parser.add_argument(
        '--fogli',
        nargs='+',
        help='Lista dei fogli da processare (default: tutti)'
    )

    parser.add_argument(
        '--backup',
        action='store_true',
        help='Crea backup del file originale prima della modifica'
    )

    args = parser.parse_args()

    # Setup logging
    logger = setup_logging()

    # Determina il file da processare
    if args.file:
        excel_file = args.file
    else:
        # Cerca file Excel di default nella cartella corrente
        possible_files = [
            'Prospetto costo del personale 8 SAL.xlsx'
        ]

        excel_file = None
        for filename in possible_files:
            if os.path.exists(filename):
                excel_file = filename
                break

        if not excel_file:
            logger.error("Nessun file Excel trovato. Specificare con --file")
            logger.error("File cercati:")
            for f in possible_files:
                logger.error(f"  - {f}")
            return

    if not os.path.exists(excel_file):
        logger.error(f"File non trovato: {excel_file}")
        return

    # Crea backup se richiesto
    if args.backup:
        backup_path = f"{excel_file}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        import shutil
        shutil.copy2(excel_file, backup_path)
        logger.info(f"Backup creato: {backup_path}")

    # Processa il file
    logger.info("=" * 60)
    logger.info("AVVIO AGGIUNTA COLONNE EXCEL")
    logger.info(f"File: {excel_file}")
    if args.fogli:
        logger.info(f"Fogli target: {args.fogli}")
    if args.output:
        logger.info(f"Output: {args.output}")
    logger.info("=" * 60)

    success = processa_file_excel(
        file_path=excel_file,
        fogli_target=args.fogli,
        output_path=args.output,
        logger=logger
    )

    if success:
        logger.info("✅ OPERAZIONE COMPLETATA CON SUCCESSO")
    else:
        logger.error("❌ OPERAZIONE FALLITA")


if __name__ == "__main__":
    main()
