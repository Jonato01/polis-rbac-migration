# common/matching_utils.py
from fuzzywuzzy import fuzz
from fuzzywuzzy import process
import re

def normalize_name(name):
    """Normalize name for comparison"""
    if not name:
        return ""
    
    # Convert to uppercase
    normalized = name.upper().strip()
    
    # Remove multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    
    # Remove special characters
    normalized = re.sub(r'[^\w\s]', '', normalized)
    
    return normalized

def find_dipendente_match(nome_pdf, dipendenti_db, threshold=85):
    """
    Find best match for employee name
    
    Args:
        nome_pdf: Name from PDF
        dipendenti_db: List of dict with cod_dipendente, nome, cognome
        threshold: Minimum similarity score (0-100)
    
    Returns:
        dict with cod_dipendente and confidence score, or None
    """
    if not nome_pdf or not dipendenti_db:
        return None
    
    nome_normalized = normalize_name(nome_pdf)
    
    # Create full names from DB
    db_names = []
    for dip in dipendenti_db:
        full_name = f"{dip.get('cognome', '')} {dip.get('nome', '')}".strip()
        db_names.append({
            'full_name': full_name,
            'normalized': normalize_name(full_name),
            'cod_dipendente': dip['cod_dipendente']
        })
    
    # Try exact match first
    for db_name in db_names:
        if db_name['normalized'] == nome_normalized:
            return {
                'cod_dipendente': db_name['cod_dipendente'],
                'confidence': 100.0,
                'matched_name': db_name['full_name']
            }
    
    # Try fuzzy matching
    choices = [(db_name['normalized'], db_name) for db_name in db_names]
    best_match = process.extractOne(
        nome_normalized, 
        choices,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold
    )
    
    if best_match:
        matched_name, db_name_dict, score = best_match[0], best_match[2][1], best_match[1]
        return {
            'cod_dipendente': db_name_dict['cod_dipendente'],
            'confidence': score,
            'matched_name': db_name_dict['full_name']
        }
    
    return None

def extract_livello_from_text(text):
    """Extract level (A1, A2, B, C, D, etc.) from text"""
    if not text:
        return None
    
    # Pattern for levels
    pattern = r'\b([A-E](?:[12])?|DIRIGENTE)\b'
    match = re.search(pattern, text.upper())
    
    if match:
        livello = match.group(1)
        return 'DIRIGENTE' if livello == 'DIRIGENTE' else livello
    
    return None

def parse_italian_date(date_str):
    """Parse Italian date format to YYYY-MM-DD"""
    if not date_str:
        return None
    
    months = {
        'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
        'maggio': '05', 'giugno': '06', 'luglio': '07', 'agosto': '08',
        'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12'
    }
    
    # Try DD/MM/YYYY format
    match = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', date_str)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    
    # Try DD mese YYYY format
    for month_name, month_num in months.items():
        pattern = fr'(\d{{1,2}})\s+{month_name}\s+(\d{{4}})'
        match = re.search(pattern, date_str.lower())
        if match:
            day, year = match.groups()
            return f"{year}-{month_num}-{day.zfill(2)}"
    
    return None