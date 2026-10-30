#!/usr/bin/env python3
"""
Script per processare PDF di timesheet:
1. Converte PDF in immagini
2. Rileva e corregge rotazioni usando Azure OpenAI
3. Estrae dati dipendente (nome, categoria, ore per mese) usando Azure OpenAI
4. Trova codice dipendente nel database
5. Salva risultati in JSON
"""

import os
import sys
import json
import base64
import mysql.connector
import requests
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
import fitz # PyMuPDF
from PIL import Image
import io
from openai import AzureOpenAI
from dotenv import load_dotenv
import argparse
import logging
from difflib import SequenceMatcher
import uuid
import threading
import glob

# Carica variabili d'ambiente
load_dotenv()

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('../pdf_timesheet_processor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class PDFTimesheetProcessor:
    def __init__(self):
        """Inizializza il processore con configurazione Azure OpenAI e database"""
        self.azure_openai_client = AzureOpenAI(
            api_key=os.getenv('AZURE_OPENAI_KEY'),
            api_version="2024-02-01",
            azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT')
        )

        # Modello da utilizzare (assicurati che sia disponibile nel tuo deployment)
        self.model_name = "gpt-4o" # o "gpt-4-vision-preview" se disponibile

        # Genera ID univoco per questo processo/thread per evitare conflitti file
        self.process_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        self.temp_dir = Path("../temp_images")
        self.temp_dir.mkdir(exist_ok=True)
        
        logger.info(f"[PROCESS_ID] {self.process_id} - Inizializzato processore")

        # Configurazione database per ricerca dipendenti
        self.db_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'root'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'polis'),
            'charset': 'utf8mb4'
        }
        
        # API URLs per download PDF
        self.azure_import_path_url = os.getenv('AZURE_IMPORT_PATH_URL')
        self.azure_search_in_folder_url = os.getenv('AZURE_SEARCH_IN_FOLDER_URL')
        self.azure_download_url = os.getenv('AZURE_DOWNLOAD_URL')
        self.request_timeout = 30
        
        # Inizializza database
        self.init_database()

    def month_name_to_number(self, month_name: str) -> int:
        """Converte nome mese italiano in numero"""
        month_map = {
            'gennaio': 1, 'gen': 1,
            'febbraio': 2, 'feb': 2,
            'marzo': 3, 'mar': 3,
            'aprile': 4, 'apr': 4,
            'maggio': 5, 'mag': 5,
            'giugno': 6, 'giu': 6,
            'luglio': 7, 'lug': 7,
            'agosto': 8, 'ago': 8,
            'settembre': 9, 'set': 9,
            'ottobre': 10, 'ott': 10,
            'novembre': 11, 'nov': 11,
            'dicembre': 12, 'dic': 12
        }
        
        month_lower = month_name.lower().strip()
        return month_map.get(month_lower, 0)

    def init_database(self):
        """Crea tabella pdf_timesheet se non esiste"""
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pdf_timesheet (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    nome_cognome VARCHAR(255) NOT NULL,
                    cod_dipendente INT,
                    categoria_dipendente VARCHAR(10) NOT NULL,
                    numero_sal VARCHAR(50),
                    mese INT NOT NULL,
                    anno INT NOT NULL,
                    totale_ore_mese DECIMAL(8,2) NOT NULL,
                    firma_presente BOOLEAN DEFAULT FALSE,
                    data_estrazione TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    nome_file VARCHAR(255) COMMENT 'Nome del file PDF processato',
                    path_file VARCHAR(500) COMMENT 'Path completo del file PDF',
                    INDEX idx_dipendente (cod_dipendente),
                    INDEX idx_mese_anno (mese, anno),
                    INDEX idx_sal (numero_sal),
                    INDEX idx_nome_file (nome_file),
                    FOREIGN KEY (cod_dipendente) REFERENCES dipendenti(cod_dipendente) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            
            conn.commit()
            cursor.close()
            conn.close()
            logger.info("Database pdf_timesheet inizializzato")
            
        except Exception as e:
            logger.error(f"Errore inizializzazione database: {e}")
            # Non fare raise, continua anche senza DB

    def get_pdf_list(self) -> List[str]:
        """Ottiene lista PDF da API Azure"""
        try:
            response = requests.get(
                f"{self.azure_import_path_url}?type=timesheet_firmati",
                timeout=self.request_timeout
            )
            response.raise_for_status()
            
            data = response.json()
            if not data.get('success'):
                return []
            
            import_paths = data.get('import_path', [])
            if not isinstance(import_paths, list):
                import_paths = [import_paths] if import_paths else []
            
            all_files = []
            for path in import_paths:
                import urllib.parse
                encoded_folder = urllib.parse.quote(path, safe='')
                search_url = f"{self.azure_search_in_folder_url}?folder={encoded_folder}&recursive=true"
                
                resp = requests.get(search_url, timeout=self.request_timeout)
                if resp.status_code == 200:
                    search_data = resp.json()
                    if search_data.get('success') and search_data.get('files'):
                        pdf_files = [f['path'] for f in search_data['files'] if f['path'].lower().endswith('.pdf')]
                        all_files.extend(pdf_files)
            
            logger.info(f"Trovati {len(all_files)} PDF dall'API")
            return all_files
            
        except Exception as e:
            logger.error(f"Errore ricerca PDF da API: {e}")
            return []

    def fetch_pdf_from_api(self, pdf_path: str) -> Optional[bytes]:
        """Scarica PDF dall'API Azure"""
        try:
            url = f"{self.azure_download_url}/{pdf_path}"
            response = requests.get(url, timeout=self.request_timeout)
            response.raise_for_status()
            
            data = response.json()
            if data.get('success') and data.get('base64'):
                return base64.b64decode(data['base64'])
            return None
            
        except Exception as e:
            logger.error(f"Errore download PDF da API: {e}")
            return None

    def pdf_bytes_to_images(self, pdf_bytes: bytes, dpi: int = 150, file_process_id: str = None) -> List[str]:
        """Converte PDF bytes in immagini (per PDF da API)

        Args:
            pdf_bytes: Bytes del PDF
            dpi: Risoluzione delle immagini (default 150 per ridurre dimensione e evitare limite 20MB Azure)
            file_process_id: ID univoco per questo file (evita race conditions tra worker paralleli)
        """
        logger.info("Convertendo PDF bytes in immagini")

        # Usa file_process_id se fornito, altrimenti fallback a self.process_id (backward compatibility)
        process_id = file_process_id or self.process_id

        pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")
        image_paths = []

        for page_num in range(len(pdf_document)):
            page = pdf_document.load_page(page_num)

            # Converti pagina in immagine
            mat = fitz.Matrix(dpi/72, dpi/72)
            pix = page.get_pixmap(matrix=mat)

            # Salva immagine con process_id per evitare conflitti tra thread
            image_path = self.temp_dir / f"page_{process_id}_{page_num + 1}.png"
            pix.save(str(image_path))
            image_paths.append(str(image_path))

            logger.info(f"Creata immagine per pagina {page_num + 1}: {image_path}")

        pdf_document.close()
        return image_paths
    

    def extract_year_from_data(self, extracted_data: Dict[str, Any]) -> int:
        """Estrae l'anno con strategia di fallback"""
        
        # 1. PRIORITÃ€: Anno dai dati estratti AI
        if 'anno' in extracted_data and extracted_data['anno']:
            return int(extracted_data['anno'])

    def save_to_database(self, extracted_data: Dict[str, Any], nome_file: str = None, path_file: str = None) -> int:
        """Salva dati estratti nel database - una riga per ogni mese"""
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor()
            

            saved_count = 0
            # ESTRAI ANNO DINAMICAMENTE
            anno = self.extract_year_from_data(extracted_data)
            logger.info(f"Anno determinato: {anno}")
            nome_cognome = extracted_data.get('nominativo', '')
            cod_dipendente = extracted_data.get('cod_dipendente')
            numero_sal = extracted_data.get('numero_sal') # Nuovo campo SAL
            ore_per_mese = extracted_data.get('ore_per_mese', {})
            categoria_per_mese = extracted_data.get('categoria_per_mese', {})
            firme_per_mese = extracted_data.get('firme_per_mese', {})
            
            # Validazione continuità mesi - controlla gap sospetti
            if ore_per_mese:
                mesi_estratti = [self.month_name_to_number(m) for m in ore_per_mese.keys()]
                mesi_estratti.sort()

                # Controlla gap nella sequenza
                if len(mesi_estratti) > 1:
                    min_mese = mesi_estratti[0]
                    max_mese = mesi_estratti[-1]
                    mesi_attesi = list(range(min_mese, max_mese + 1))
                    mesi_mancanti = [m for m in mesi_attesi if m not in mesi_estratti]

                    if mesi_mancanti:
                        mesi_mancanti_nomi = []
                        month_map_reverse = {1: 'gennaio', 2: 'febbraio', 3: 'marzo', 4: 'aprile', 5: 'maggio',
                                           6: 'giugno', 7: 'luglio', 8: 'agosto', 9: 'settembre',
                                           10: 'ottobre', 11: 'novembre', 12: 'dicembre'}
                        for m in mesi_mancanti:
                            mesi_mancanti_nomi.append(month_map_reverse.get(m, str(m)))

                        logger.warning(f"[MONTH CONTINUITY] Gap rilevato nei mesi estratti da '{nome_file}'")
                        logger.warning(f"[MONTH CONTINUITY] Mesi estratti: {', '.join([month_map_reverse.get(m, str(m)) for m in mesi_estratti])}")
                        logger.warning(f"[MONTH CONTINUITY] Mesi MANCANTI nel range: {', '.join(mesi_mancanti_nomi)}")
                        logger.warning(f"[MONTH CONTINUITY] VERIFICARE se questi mesi sono effettivamente assenti dal PDF o se sono stati saltati dall'OCR")

            # Una riga per ogni mese trovato
            for mese_nome, ore_totali in ore_per_mese.items():
                # SKIP mesi con ore null/None (l'AI a volte inventa mesi inesistenti)
                if ore_totali is None:
                    logger.warning(f"Skippato mese '{mese_nome}' con ore=null (non presente nel PDF)")
                    continue
                
                mese_numero = self.month_name_to_number(mese_nome)
                
                if mese_numero == 0:
                    logger.warning(f"Mese non riconosciuto: {mese_nome}")
                    continue
                
                categoria = categoria_per_mese.get(mese_nome, 'A2') # Default A2
                firma_presente = firme_per_mese.get(mese_nome, False)
                
                cursor.execute("""
                    INSERT INTO pdf_timesheet 
                    (nome_cognome, cod_dipendente, categoria_dipendente,
                     numero_sal, mese, anno, totale_ore_mese, firma_presente,
                     nome_file, path_file)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    nome_cognome,
                    cod_dipendente,
                    categoria,
                    numero_sal,
                    mese_numero,
                    anno, # Anno fisso
                    float(ore_totali),
                    firma_presente,
                    nome_file,
                    path_file
                ))
                
                saved_count += 1
                logger.info(f"Salvato nel DB: {nome_cognome} - SAL {numero_sal} - Mese {mese_numero} ({mese_nome}) - {ore_totali} ore - Categoria {categoria}")
            
            conn.commit()
            cursor.close()
            conn.close()
            
            logger.info(f"Salvate {saved_count} righe nel database")
            return saved_count
            
        except Exception as e:
            logger.error(f"Errore salvataggio database: {e}")
            return 0


    def calculate_similarity(self, str1: str, str2: str) -> float:
        """Calcola similarità tra due stringhe (0-1)"""
        return SequenceMatcher(None, str1.upper(), str2.upper()).ratio()

    def validate_months_from_filename(self, extracted_data: Dict[str, Any], filename: str) -> None:
        """Valida mesi estratti contro hints dal filename (es: Mag_Ago25 = Maggio-Agosto)"""
        try:
            logger.info(f"[MONTH VALIDATION] Validazione mesi da filename: {filename}")

            # Mappa abbreviazioni mesi italiani
            month_abbr_map = {
                'gen': 'gennaio', 'feb': 'febbraio', 'mar': 'marzo', 'apr': 'aprile',
                'mag': 'maggio', 'giu': 'giugno', 'lug': 'luglio', 'ago': 'agosto',
                'set': 'settembre', 'ott': 'ottobre', 'nov': 'novembre', 'dic': 'dicembre'
            }

            filename_lower = filename.lower()

            # Cerca pattern come "Mag_Ago" o "gen_apr" nel filename
            found_months = []
            for abbr, full_name in month_abbr_map.items():
                if abbr in filename_lower:
                    found_months.append(full_name)

            if len(found_months) >= 2:
                # Assume primo mese = inizio range, ultimo mese = fine range
                first_month = found_months[0]
                last_month = found_months[-1]

                first_num = self.month_name_to_number(first_month)
                last_num = self.month_name_to_number(last_month)

                if first_num > 0 and last_num > 0:
                    expected_months = []
                    month_map_reverse = {1: 'gennaio', 2: 'febbraio', 3: 'marzo', 4: 'aprile', 5: 'maggio',
                                       6: 'giugno', 7: 'luglio', 8: 'agosto', 9: 'settembre',
                                       10: 'ottobre', 11: 'novembre', 12: 'dicembre'}

                    for m in range(first_num, last_num + 1):
                        expected_months.append(month_map_reverse.get(m, ''))

                    logger.info(f"[MONTH VALIDATION] Filename suggerisce range: {first_month} - {last_month} ({', '.join(expected_months)})")

                    # Controlla mesi estratti
                    ore_per_mese = extracted_data.get('ore_per_mese', {})
                    extracted_month_names = list(ore_per_mese.keys())

                    logger.info(f"[MONTH VALIDATION] Mesi estratti da PDF: {', '.join(extracted_month_names)}")

                    # Trova mesi mancanti
                    missing_months = [m for m in expected_months if m not in extracted_month_names]

                    if missing_months:
                        logger.warning(f"[MONTH VALIDATION] ATTENZIONE: Mesi attesi ma NON estratti: {', '.join(missing_months)}")
                        logger.warning(f"[MONTH VALIDATION] Filename: {filename}")
                        logger.warning(f"[MONTH VALIDATION] POSSIBILE OCR MISS - Verificare manualmente il PDF")
                    else:
                        logger.info(f"[MONTH VALIDATION] OK - Tutti i mesi attesi sono stati estratti")

        except Exception as e:
            logger.warning(f"[MONTH VALIDATION] Errore validazione: {e}")

    def extract_name_from_filename(self, filename: str) -> Optional[str]:
        """Estrae nome dal filename con supporto per cognomi composti (DE FALCO, DI MARCO, etc.)"""
        try:
            logger.info(f"[FILENAME HINT] Inizio estrazione da: {filename}")
            
            # Pulisce prefissi comuni e estensione
            name_part = filename.replace('.pdf', '').replace('TM_POLIS_', '').replace('TM_', '')
            
            # Normalizza spazi multipli
            name_part = ' '.join(name_part.split())
            
            # Separa sia per underscore CHE per spazio per isolare tutte le parti
            # Sostituisco _ con spazio e poi splitto
            name_part = name_part.replace('_', ' ')
            parts = name_part.split()
            
            name_parts = []
            months = ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic']
            # Particelle di cognomi italiani composti
            surname_particles = ['de', 'di', 'del', 'della', 'degli', 'delle', 'da', 'dal', 'dalla', 'van', 'von', 'la', 'le']

            i = 0
            while i < len(parts):
                part = parts[i].strip()
                if not part:
                    i += 1
                    continue
                    
                part_lower = part.lower()
                
                # Se troviamo un mese, STOP - tutto quello che segue non è parte del nome
                # Match esatto (gen, mar, etc.) O mese+numeri (gen25, mar24, etc.)
                is_month = (part_lower in months) or any(
                    part_lower.startswith(month) and len(part_lower) > len(month) and part_lower[len(month):].isdigit()
                    for month in months
                )
                if is_month:
                    break

                # Salta numeri, mix numero-lettere, "polis", parole troppo corte (eccetto particelle)
                is_number = part_lower.isdigit() or any(char.isdigit() for char in part_lower)
                is_too_short = len(part) <= 2 and part_lower not in surname_particles
                is_polis = part_lower in ['polis', '']

                if is_number or is_too_short or is_polis:
                    i += 1
                    continue

                # Se è una particella di cognome E c'è una parola successiva valida, uniscile
                if part_lower in surname_particles and i + 1 < len(parts):
                    next_part = parts[i + 1].strip()
                    next_part_lower = next_part.lower()

                    # Verifica che la prossima parte non sia un mese/numero/invalid
                    # Match esatto mese O mese+numeri (gen25, mar24, etc.), NON sottostringa
                    next_is_month = (next_part_lower in months) or any(
                        next_part_lower.startswith(month) and len(next_part_lower) > len(month) and next_part_lower[len(month):].isdigit()
                        for month in months
                    )
                    next_is_valid = (
                        next_part and
                        not next_is_month and
                        not next_part_lower.isdigit() and
                        not any(char.isdigit() for char in next_part_lower) and
                        len(next_part) > 2
                    )

                    if next_is_valid:
                        # Unisci particella + cognome (es: "DE" + "FALCO" -> "DE FALCO")
                        combined = f"{part} {next_part}"
                        name_parts.append(combined)
                        i += 2  # Salta entrambe le parti
                        continue

                # Parte valida normale
                name_parts.append(part)
                i += 1

            if name_parts:
                # Prendi massimo 3 parti per supportare "DE FALCO LUIGI" (cognome composto + nome)
                # o "ROSSI MARIO" (cognome semplice + nome)
                # Non limitiamo più a [:3] ma prendiamo tutto quello che abbiamo raccolto prima del primo mese
                result = ' '.join(name_parts).upper()
                logger.info(f"[FILENAME HINT] Nome estratto: '{result}'")
                return result
            
            logger.warning(f"[FILENAME HINT] Nessun nome valido estratto da filename")
            return None
        except Exception as e:
            logger.warning(f"[FILENAME HINT] Errore estrazione: {e}")
            return None

    def find_employee_by_name(self, nome_cognome: str, filename: str) -> Tuple[Optional[int], Optional[str]]:
        """Trova dipendente con matching intelligente multi-livello"""
        if not nome_cognome:
            return None, None
            
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor(dictionary=True)
            
            # LIVELLO 1: Match esatto
            cursor.execute("""
                SELECT cod_dipendente, nome, cognome FROM dipendenti 
                WHERE CONCAT(UPPER(cognome), ' ', UPPER(nome)) = UPPER(%s)
                   OR CONCAT(UPPER(nome), ' ', UPPER(cognome)) = UPPER(%s)
                LIMIT 1
            """, (nome_cognome, nome_cognome))
            
            result = cursor.fetchone()
            if result:
                nome_corretto = f"{result['cognome']} {result['nome']}"
                logger.info(f"[MATCH EXACT] {nome_corretto} (ID: {result['cod_dipendente']})")
                cursor.close()
                conn.close()
                return result['cod_dipendente'], nome_corretto
            
            # LIVELLO 2: Filename hint (estrazione anticipata per validazione)
            filename_hint = self.extract_name_from_filename(filename)
            
            # LIVELLO 2A: Se abbiamo filename hint, proviamo match intelligente
            if filename_hint:
                logger.info(f"[FILENAME HINT] {filename_hint}")
                
                # Verifica discrepanza tra filename e OCR
                hint_ocr_similarity = self.calculate_similarity(filename_hint, nome_cognome)
                if hint_ocr_similarity < 0.5:
                    logger.warning(f"[DISCREPANCY ALERT] Filename hint '{filename_hint}' molto diverso da OCR '{nome_cognome}' (similarity: {hint_ocr_similarity:.2f})")
                    logger.warning(f"[DISCREPANCY ALERT] Priorizzo filename hint per matching")

                # Strategia: cerca candidati che matchano il filename hint
                hint_parts = filename_hint.split()
                for hint_part in hint_parts:
                    if len(hint_part) >= 3:
                        cursor.execute("""
                            SELECT cod_dipendente, nome, cognome FROM dipendenti
                            WHERE UPPER(cognome) LIKE %s OR UPPER(nome) LIKE %s
                            LIMIT 20
                        """, (f"%{hint_part}%", f"%{hint_part}%"))
                        
                        candidates = cursor.fetchall()
                        if candidates:
                            best_match = None
                            best_score = 0.0
                            best_source = None

                            # Calcola similarity con ENTRAMBI: filename hint E nome estratto
                            for candidate in candidates:
                                full_name = f"{candidate['cognome']} {candidate['nome']}"
                                
                                # Score con filename hint
                                score_hint = self.calculate_similarity(filename_hint, full_name)
                                # Score con nome estratto OCR
                                score_ocr = self.calculate_similarity(nome_cognome, full_name)
                                
                                # Usa lo score MIGLIORE dei due (filename o OCR)
                                if score_hint > score_ocr:
                                    score = score_hint
                                    source = "filename"
                                else:
                                    score = score_ocr
                                    source = "ocr"

                                if score > best_score:
                                    best_score = score
                                    best_match = candidate
                                    best_source = source

                            # Soglia più bassa (0.60) quando filename hint è coinvolto per prioritizzare filename
                            if best_match and best_score >= 0.60:
                                nome_corretto = f"{best_match['cognome']} {best_match['nome']}"
                                
                                # Log dettagliato per debug
                                score_hint_best = self.calculate_similarity(filename_hint, nome_corretto)
                                score_ocr_best = self.calculate_similarity(nome_cognome, nome_corretto)
                                
                                if score_hint_best != score_ocr_best:
                                    logger.info(f"[FILENAME CORRECTION] OCR '{nome_cognome}' -> '{nome_corretto}' (hint score: {score_hint_best:.2f}, OCR score: {score_ocr_best:.2f}, source: {best_source})")
                                else:
                                    logger.info(f"[MATCH FILENAME+SIMILARITY] {nome_corretto} (ID: {best_match['cod_dipendente']}, score: {best_score:.2f})")
                                
                                cursor.close()
                                conn.close()
                                return best_match['cod_dipendente'], nome_corretto
            
            # LIVELLO 3: Similarity search globale (fallback)
            logger.info(f"[SIMILARITY SEARCH] Cerco match per: {nome_cognome}")
            cursor.execute("SELECT cod_dipendente, nome, cognome FROM dipendenti")
            all_dipendenti = cursor.fetchall()
            
            best_match = None
            best_score = 0.0
            
            for dipendente in all_dipendenti:
                full_name = f"{dipendente['cognome']} {dipendente['nome']}"
                score = self.calculate_similarity(nome_cognome, full_name)
                
                if score > best_score:
                    best_score = score
                    best_match = dipendente
            
            cursor.close()
            conn.close()
            
            # Soglia standard per similarity globale
            if best_match and best_score >= 0.75:
                nome_corretto = f"{best_match['cognome']} {best_match['nome']}"
                logger.info(f"[MATCH SIMILARITY] {nome_corretto} (ID: {best_match['cod_dipendente']}, score: {best_score:.2f})")
                return best_match['cod_dipendente'], nome_corretto
            
            logger.warning(f"[NO MATCH] Nessun dipendente trovato per: {nome_cognome} (best score: {best_score:.2f})")
            return None, nome_cognome
            
        except Exception as e:
            logger.error(f"Errore ricerca dipendente: {e}")
            return None, nome_cognome

    def determine_final_employee_name(self, extracted_name: str, filename: str) -> Tuple[str, Optional[int]]:
        """Determina nome finale dipendente usando AI e feedback filename"""
        try:
            filename_name = self.extract_name_from_filename(filename)
            
            logger.info(f"Determinazione nome finale:")
            logger.info(f" - Nome estratto: {extracted_name}")
            logger.info(f" - Nome da filename: {filename_name}")
            
            # Prompt AI per correzione nome
            prompt = f"""
Determina il NOME CORRETTO del dipendente considerando entrambe le fonti.

NOME ESTRATTO DAL PDF: {extracted_name}
NOME DAL FILENAME: {filename_name}
FILENAME COMPLETO: {filename}

REGOLE IMPORTANTI:
1. Il filename Ã¨ PIÃ™ AFFIDABILE dell'OCR
2. Se c'Ã¨ contraddizione, prioritÃ  al filename
3. Correggi errori OCR evidenti usando il filename
4. Il nome deve essere "COGNOME NOME"

ESEMPIO: Se OCR dice "ANCHINI FABIO" ma filename contiene "ANICHINI", 
il nome corretto Ã¨ "ANICHINI FABIO"

RISPOSTA (SOLO JSON):
{{
  "nome_finale": "COGNOME NOME corretto",
  "correzioni_applicate": "descrizione correzioni se fatte"
}}
"""

            response = self.azure_openai_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0
            )
            
            response_text = response.choices[0].message.content.strip()
            
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end]
            
            result = json.loads(response_text)
            nome_finale = result.get('nome_finale', extracted_name)
            
            if result.get('correzioni_applicate'):
                logger.info(f"Correzioni AI: {result['correzioni_applicate']}")
            
            # Trova dipendente nel database
            cod_dipendente, nome_corretto = self.find_employee_by_name(nome_finale, filename)
            
            logger.info(f"Nome finale determinato: {nome_corretto} (ID: {cod_dipendente})")
            return nome_corretto, cod_dipendente
            
        except Exception as e:
            logger.error(f"Errore determinazione nome finale: {e}")
            # Fallback: usa il nome estratto e cerca comunque nel DB
            cod_dipendente, nome_corretto = self.find_employee_by_name(extracted_name, filename)
            return nome_corretto, cod_dipendente

    def pdf_to_images(self, pdf_path: str, dpi: int = 150, file_process_id: str = None) -> List[str]:
        """
        Converte PDF in immagini

        Args:
            pdf_path: Percorso del file PDF
            dpi: Risoluzione delle immagini (default 150 per ridurre dimensione e evitare limite 20MB Azure)
            file_process_id: ID univoco per questo file (evita race conditions tra worker paralleli)

        Returns:
            Lista dei percorsi delle immagini create
        """
        logger.info(f"Convertendo PDF in immagini: {pdf_path}")

        # Usa file_process_id se fornito, altrimenti fallback a self.process_id (backward compatibility)
        process_id = file_process_id or self.process_id

        pdf_document = fitz.open(pdf_path)
        image_paths = []

        for page_num in range(len(pdf_document)):
            page = pdf_document.load_page(page_num)

            # Converti pagina in immagine
            mat = fitz.Matrix(dpi/72, dpi/72) # Matrice di trasformazione per DPI
            pix = page.get_pixmap(matrix=mat)

            # Salva immagine con process_id per evitare conflitti tra thread
            image_path = self.temp_dir / f"page_{process_id}_{page_num + 1}.png"
            pix.save(str(image_path))
            image_paths.append(str(image_path))

            logger.info(f"Creata immagine per pagina {page_num + 1}: {image_path}")

        pdf_document.close()
        return image_paths

    def encode_image_to_base64(self, image_path: str) -> str:
        """Codifica immagine in base64 per Azure OpenAI"""
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    def detect_and_correct_rotation(self, image_path: str) -> str:
        """
        Rileva se l'immagine Ã¨ ruotata e la corregge se necessario

        Args:
            image_path: Percorso dell'immagine

        Returns:
            Percorso dell'immagine corretta
        """
        logger.info(f"Rilevando rotazione per: {image_path}")

        # Codifica immagine
        base64_image = self.encode_image_to_base64(image_path)

        # Prompt per rilevare rotazione
        rotation_prompt = """
        Analizza questa immagine di un timesheet/foglio presenze e determina se Ã¨ ruotata.
        
        Rispondi SOLO con un numero che indica i gradi di rotazione necessari per raddrizzare l'immagine:
        - 0: se l'immagine Ã¨ giÃ  dritta
        - 90: se l'immagine Ã¨ ruotata di 90 gradi in senso orario (testo leggibile ruotando di 90Â° antiorario)
        - 180: se l'immagine Ã¨ capovolta
        - 270: se l'immagine Ã¨ ruotata di 90 gradi in senso antiorario (testo leggibile ruotando di 90Â° orario)
        
        Considera che il testo dovrebbe essere leggibile normalmente (orizzontale, da sinistra a destra).
        
        Rispondi SOLO con il numero (0, 90, 180, o 270).
        """

        try:
            response = self.azure_openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": rotation_prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=10,
                temperature=0
            )

            rotation_degrees = int(response.choices[0].message.content.strip())
            logger.info(f"Rotazione rilevata: {rotation_degrees} gradi")

            # Se non serve rotazione, restituisci il percorso originale
            if rotation_degrees == 0:
                return image_path

            # Applica rotazione
            corrected_path = self._rotate_image(image_path, rotation_degrees)
            return corrected_path

        except Exception as e:
            logger.error(f"Errore nel rilevamento rotazione: {e}")
            return image_path # Restituisci immagine originale in caso di errore

    def _rotate_image(self, image_path: str, degrees: int) -> str:
        """Ruota l'immagine del numero di gradi specificato"""
        logger.info(f"Ruotando immagine di {degrees} gradi")

        with Image.open(image_path) as img:
            # Ruota l'immagine (PIL ruota in senso antiorario)
            rotated_img = img.rotate(-degrees, expand=True)

            # Salva immagine ruotata
            corrected_path = image_path.replace('.png', '_corrected.png')
            rotated_img.save(corrected_path)

        return corrected_path

    def extract_timesheet_data(self, image_paths: List[str], filename: str) -> Dict[str, Any]:
        """
        Estrae dati dal timesheet usando Azure OpenAI

        Args:
            image_paths: Lista dei percorsi delle immagini corrette
            filename: Nome del file per log

        Returns:
            Dizionario con i dati estratti
        """
        logger.info("Estraendo dati dal timesheet")

        # Prepara le immagini per l'analisi
        image_contents = []
        for i, image_path in enumerate(image_paths):
            base64_image = self.encode_image_to_base64(image_path)
            image_contents.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64_image}"
                }
            })

        # Prompt per estrazione dati - solo dal PDF
        extraction_prompt = """
        Analizza queste immagini di un timesheet/foglio presenze e estrai le seguenti informazioni:

        ESTRAI:
        1. NOMINATIVO del dipendente (nome e cognome completo):
           - Cerca la label "Nominativo:" nell'intestazione della pagina
           - Il nome si trova subito DOPO la parola "Nominativo:"
           - È scritto in MAIUSCOLO (es. "DEL MONTE MATTEO", "DE FALCO LUIGI")
           - NON confondere con altri nomi che potrebbero apparire nel documento
           - Questo campo è presente in OGNI pagina del documento
           
           IMPORTANTE PER IL NOMINATIVO:
           - Fai MOLTA attenzione alle lettere simili: C vs G, B vs D, I vs L, O vs Q, E vs F
           - Cognomi COMPOSTI italiani sono comuni: "DE FALCO", "DI MARCO", "DEL MONTE", "DELLA VALLE"
           - Se vedi "GIULI" controlla se potrebbe essere "CIUOLI"
           - Se vedi "SCANDINO" controlla se potrebbe essere "BERARDINO"
           - Formato sempre: COGNOME NOME (in maiuscolo), es: "DE FALCO LUIGI" NON "EVANGELISTA MATTEO"
           - Se hai dubbi, ricontrolla attentamente quella zona dell'immagine su TUTTE le pagine
           - VERIFICA SU PIÙ PAGINE: se il nominativo sembra incoerente tra pagine, usa la versione più chiara
        2. ANNO di riferimento (es. 2024, 2025) - cerca nelle intestazioni o date
        3. CATEGORIA di appartenenza del dipendente per ogni MESE (es. A1, A2, B, C, D, E, DIRIGENTE, etc.)
        4. SAL NUMERO - cerca "SAL" seguito da un numero
        5. ORE LAVORATE per ogni MESE presente nel documento
        6. PRESENZA FIRMA per ogni MESE - controlla se dopo ogni tabella mensile Ã¨ presente una firma

        IMPORTANTE - ESTRAZIONE ORE MENSILI:
        Per ogni mese, devi estrarre SOLO il valore dalla riga "TOTALE ORE MESE":
        - Cerca la riga etichettata esattamente come "TOTALE ORE MESE" (ultima riga della tabella)
        - Prendi il valore nella colonna "Totale ore" (ultima colonna a destra)
        - Questo Ã¨ il valore PIÃ™ GRANDE nella colonna perchÃ© somma tutte le attivitÃ 
        - NON prendere valori dalle righe intermedie come:
          * "AttivitÃ  progetto Sviluppo"
          * "TOT ore progetto" 
          * "Altri progetti finanziati"
          * "AttivitÃ  ordinaria"
          * "Ferie"
          * "Assenza (malattia, permessi etc)"
        - SOLO dalla riga "TOTALE ORE MESE"

        Il nominativo Ã¨ generalmente presente nella prima pagina nell'intestazione del documento.
        La categoria, le ore lavorate e le firme possono essere distribuite su piÃ¹ pagine e mesi.
        IMPORTANTE: La categoria del dipendente puÃ² variare da mese a mese, quindi estrai la categoria specifica per ogni mese.
        Per le firme, cerca dopo ogni tabella mensile segni di scrittura a mano, firme, scarabocchi o qualsiasi segno.

        REGOLA CRITICA - NON INVENTARE MESI, MA NON SALTARLI:
        - Includi SOLO i mesi che vedi EFFETTIVAMENTE nel PDF
        - NON aggiungere mesi con valore null se non sono presenti nel documento
        - ATTENZIONE: I mesi potrebbero NON essere sequenziali (es: "Maggio, Giugno, Luglio, Agosto" o "Gennaio, Marzo, Maggio")
        - SCANSIONA TUTTE LE PAGINE: una tabella per "Maggio" potrebbe essere sulla prima pagina, poi "Giugno" sulla seconda, etc.
        - Cerca intestazioni mese in ITALIANO: "Gennaio", "Febbraio", "Marzo", "Aprile", "MAGGIO", "Giugno", "Luglio", "Agosto", etc.
        - Se vedi "Maggio" o "Mag" in qualsiasi pagina, DEVI includerlo nel JSON
        - Se vedi solo Gennaio, Febbraio e Aprile, NON includere Marzo nel JSON
        - Ogni mese nel JSON DEVE corrispondere a una tabella/sezione presente nel PDF
        - ESEMPIO: Se il PDF contiene "Maggio", "Giugno", "Luglio", "Agosto" estrai tutti e 4 i mesi (non solo Giugno-Agosto)

        Restituisci i dati in formato JSON con questa struttura:
        {{
            "nominativo": "Nome Cognome del dipendente",
            "numero_sal": "numero_sal",
            "anno": "anno recuperato", 
            "categoria_per_mese": {{
                "gennaio": "categoria_gennaio",
                "febbraio": "categoria_febbraio",
                "aprile": "categoria_aprile"
            }},
            "ore_per_mese": {{
                "gennaio": numero_ore,
                "febbraio": numero_ore,
                "aprile": numero_ore
            }},
            "firme_per_mese": {{
                "gennaio": true/false,
                "febbraio": true/false,
                "aprile": true/false
            }},
            "totale_ore": numero_totale_ore
        }}

        IMPORTANTE: L'esempio sopra mostra gennaio/febbraio/aprile, ma tu devi estrarre SOLO i mesi presenti nel PDF.
        Se non riesci a trovare alcune informazioni per un mese ESISTENTE, usa null per quel valore specifico.
        Per le categorie, cerca indicazioni specifiche per ogni mese (potrebbero essere in tabelle, intestazioni mensili, o note).
        Per le firme, usa true se Ã¨ presente una firma per quel mese, false se non Ã¨ presente, null se non riesci a determinarlo.
        
        Rispondi SOLO con il JSON, senza testo aggiuntivo.
        """

        try:
            # Prepara il messaggio con tutte le immagini
            message_content = [{"type": "text", "text": extraction_prompt}]
            message_content.extend(image_contents)

            response = self.azure_openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "user",
                        "content": message_content
                    }
                ],
                max_tokens=2000,
                temperature=0
            )

            # Estrai e parsifica la risposta JSON
            response_text = response.choices[0].message.content.strip()
            logger.info(f"Risposta Azure OpenAI: {response_text}")

            # Rimuovi eventuali markdown code blocks
            if response_text.startswith('```json'):
                response_text = response_text[7:]
            if response_text.endswith('```'):
                response_text = response_text[:-3]

            extracted_data = json.loads(response_text)

            # Fix 3: Gestisci il caso in cui Azure risponda con un array invece di un singolo oggetto
            if isinstance(extracted_data, list):
                logger.warning(f"[JSON ARRAY] Azure ha risposto con un array di {len(extracted_data)} elementi invece di un singolo oggetto")
                if extracted_data:
                    # Prendi il primo elemento valido
                    logger.info(f"[JSON ARRAY] Uso il primo elemento dell'array")
                    extracted_data = extracted_data[0]
                else:
                    # Array vuoto - ritorna errore
                    logger.error(f"[JSON ARRAY] Array vuoto ricevuto da Azure")
                    extracted_data = {"error": "Array vuoto ricevuto da Azure"}

            return extracted_data

        except json.JSONDecodeError as e:
            logger.error(f"Errore nel parsing JSON: {e}")
            logger.error(f"Risposta ricevuta: {response_text}")
            return {
                "error": "Errore nel parsing della risposta",
                "raw_response": response_text
            }
        except Exception as e:
            logger.error(f"Errore nell'estrazione dati: {e}")
            return {"error": str(e)}

    def process_pdf(self, pdf_path: str, output_path: Optional[str] = None, is_local: bool = True, dry_run: bool = False) -> Dict[str, Any]:
        """
        Processa un PDF completo (locale o da API)

        Args:
            pdf_path: Percorso del file PDF o path API
            output_path: Percorso del file JSON di output (opzionale)
            is_local: True se file locale, False se da API
            dry_run: Se True, non salva nel database (solo estrazione e JSON)

        Returns:
            Dizionario con i risultati
        """
        logger.info(f"Iniziando processamento PDF: {pdf_path} ({'locale' if is_local else 'da API'})")
        filename = Path(pdf_path).name

        # GENERA ID UNIVOCO PER QUESTO FILE (non riutilizzare self.process_id)
        # Fix per race conditions: ogni PDF ha il suo ID univoco per evitare conflitti tra worker paralleli
        file_process_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        logger.info(f"[FILE_PROCESS_ID] {file_process_id} - Processing: {filename}")

        try:
            # 1. Ottieni immagini (da file locale o API) - passa file_process_id
            if is_local:
                image_paths = self.pdf_to_images(pdf_path, file_process_id=file_process_id)
            else:
                # Scarica PDF dall'API
                pdf_bytes = self.fetch_pdf_from_api(pdf_path)
                if not pdf_bytes:
                    return {"error": f"Impossibile scaricare PDF da API: {pdf_path}"}
                image_paths = self.pdf_bytes_to_images(pdf_bytes, file_process_id=file_process_id)

            # 2. Rileva e correggi rotazioni
            corrected_image_paths = []
            for image_path in image_paths:
                corrected_path = self.detect_and_correct_rotation(image_path)
                corrected_image_paths.append(corrected_path)

            # 3. Estrai dati dal timesheet (passa anche il filename per hints)
            extracted_data = self.extract_timesheet_data(corrected_image_paths, filename)

            # 3a. Valida mesi estratti contro filename hint
            self.validate_months_from_filename(extracted_data, filename)

            if "error" in extracted_data:
                logger.error(f"Errore estrazione: {extracted_data['error']}")
                return {"error": extracted_data["error"]}

            # 4. Trova codice dipendente dal nome estratto
            nome_estratto = extracted_data.get('nominativo', '')
            logger.info(f"Nome estratto dal PDF: {nome_estratto}")
            
            # Cerca dipendente (la funzione gestisce internamente filename hint + fallback)
            cod_dipendente, nome_corretto = self.find_employee_by_name(nome_estratto, filename)
            
            if cod_dipendente is None:
                logger.warning(f"[MATCH FAILED] Nessun match trovato per: {nome_estratto}")
            
            # Aggiorna i dati estratti
            extracted_data['nominativo'] = nome_corretto or nome_estratto
            extracted_data['cod_dipendente'] = cod_dipendente
            
            logger.info(f"Dati finali: nominativo={extracted_data['nominativo']}, cod_dipendente={extracted_data.get('cod_dipendente')}")

            # 4a. Salva nel database (se non dry-run)
            if extracted_data.get('ore_per_mese'):
                if dry_run:
                    logger.info("DRY-RUN mode: saltato salvataggio database")
                    db_saved = 0
                else:
                    db_saved = self.save_to_database(extracted_data, nome_file=filename, path_file=pdf_path)
                    logger.info(f"Righe salvate nel database: {db_saved}")
            else:
                logger.warning("Nessun dato ore per mese trovato, skip salvataggio DB")
                db_saved = 0

            # 5. Aggiungi metadati (inclusi nome_file e path_file)
            result = {
                "processed_at": datetime.now().isoformat(),
                "total_pages": len(image_paths),
                "nome_file": filename,
                "path_file": pdf_path,
                "dry_run": dry_run,
                "db_rows_saved": db_saved,
                "data": extracted_data
            }

            # 6. Salva risultati JSON
            if output_path is None:
                pdf_name = Path(pdf_path).stem.replace('/', '_').replace('\\', '_')
                output_path = f"timesheet_result_{pdf_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            logger.info(f"Risultati salvati in: {output_path}")

            # 7. Pulizia file temporanei usando file_process_id specifico
            try:
                self.cleanup_temp_images(file_process_id)
            except Exception as e:
                logger.warning(f"Errore cleanup file temporanei: {e}")

            return result

        except Exception as e:
            logger.error(f"Errore nel processamento PDF: {e}")
            # Cleanup anche in caso di errore usando file_process_id specifico
            try:
                self.cleanup_temp_images(file_process_id)
            except:
                pass
            return {"error": str(e)}

    def process_batch_from_api(self, limit: int = 3, dry_run: bool = False) -> Dict[str, Any]:
        """Processa un batch di PDF dall'API"""
        logger.info(f"Avvio processamento batch da API (limite: {limit}, dry_run: {dry_run})")
        
        # Ottieni lista PDF
        pdf_list = self.get_pdf_list()
        if not pdf_list:
            logger.error("Nessun PDF trovato dall'API")
            return {"error": "Nessun PDF trovato"}
        
        # Limita per test
        if limit > 0:
            pdf_list = pdf_list[:limit]
            logger.info(f"Limitato a {limit} PDF per test")
        
        # Statistiche
        results = {
            "batch_started": datetime.now().isoformat(),
            "total_found": len(pdf_list),
            "processed": 0,
            "success": 0,
            "failed": 0,
            "results": []
        }
        
        # Processa ogni PDF
        for i, pdf_path in enumerate(pdf_list, 1):
            logger.info(f"\n[{i}/{len(pdf_list)}] " + "="*60)
            logger.info(f"Processando: {Path(pdf_path).name}")
            
            try:
                result = self.process_pdf(pdf_path, is_local=False, dry_run=dry_run)
                results["processed"] += 1
                
                if "error" in result:
                    results["failed"] += 1
                    logger.error(f"FALLITO [{i}/{len(pdf_list)}]: {result['error']}")
                else:
                    results["success"] += 1
                    logger.info(f"COMPLETATO [{i}/{len(pdf_list)}]: {Path(pdf_path).name}")
                
                results["results"].append({
                    "pdf_path": pdf_path,
                    "success": "error" not in result,
                    "result": result
                })
                
            except Exception as e:
                results["failed"] += 1
                logger.error(f"ERRORE [{i}/{len(pdf_list)}]: {e}")
                results["results"].append({
                    "pdf_path": pdf_path,
                    "success": False,
                    "error": str(e)
                })
        
        # Report finale
        results["batch_completed"] = datetime.now().isoformat()
        success_rate = (results["success"] / results["processed"] * 100) if results["processed"] > 0 else 0
        
        logger.info(f"\n" + "="*60)
        logger.info(f"BATCH COMPLETATO!")
        logger.info(f"PDF processati: {results['processed']}")
        logger.info(f"Successi: {results['success']}")
        logger.info(f"Fallimenti: {results['failed']}")
        logger.info(f"Tasso successo: {success_rate:.1f}%")
        
        # Salva report batch
        batch_report = f"batch_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(batch_report, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Report batch salvato in: {batch_report}")
        
        return results

    def cleanup_temp_images(self, file_process_id: str = None):
        """Rimuove file temporanei di questo file usando il file_process_id

        Args:
            file_process_id: ID univoco del file da pulire (se None usa self.process_id per backward compatibility)
        """
        try:
            # Usa file_process_id se fornito, altrimenti self.process_id (backward compatibility)
            process_id = file_process_id or self.process_id

            pattern = str(self.temp_dir / f"page_{process_id}_*.png")
            files_to_remove = glob.glob(pattern)

            for file_path in files_to_remove:
                try:
                    os.remove(file_path)
                    logger.debug(f"[CLEANUP] Rimosso: {file_path}")
                except Exception as e:
                    logger.warning(f"[CLEANUP] Errore rimozione {file_path}: {e}")

            if files_to_remove:
                logger.info(f"[CLEANUP] {len(files_to_remove)} file temporanei rimossi per process_id {process_id}")
        except Exception as e:
            logger.error(f"[CLEANUP] Errore generale: {e}")
