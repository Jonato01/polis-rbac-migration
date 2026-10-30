"""
name_extractor.py - Estrazione intelligente nominativo da testo raw
v1.0 - Post-processing per quando GPT fallisce
"""

import re
from typing import Optional, Tuple

def extract_name_from_raw_text(raw_text: str, pdf_filename: str = None) -> Optional[str]:
    """
    Estrae il nominativo dal testo raw quando GPT non riesce
    
    Strategie in ordine:
    1. Pattern "Nome COGNOME" prima di "Gentile collega"
    2. Pattern dopo intestazione HR
    3. Pattern vicino a "Sede Centrale", "Digital"
    4. Fallback a filename (ultimo resort)
    
    Args:
        raw_text: Testo estratto da Document Intelligence
        pdf_filename: Nome file PDF (opzionale, per fallback)
    
    Returns:
        Nominativo estratto o None
    """
    
    if not raw_text:
        return extract_name_from_filename(pdf_filename) if pdf_filename else None
    
    # Pulizia testo
    text = raw_text.replace('\r\n', '\n').replace('\r', '\n')
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    print(f"[NAME_EXTRACTOR] Analyzing {len(lines)} lines of text")
    
    # STRATEGIA 1: Cerca prima di "Gentile collega"
    name = extract_before_gentile(lines)
    if name:
        print(f"[NAME_EXTRACTOR] Found via 'Gentile': {name}")
        return name
    
    # STRATEGIA 2: Cerca dopo intestazione HR
    name = extract_after_hr_header(lines)
    if name:
        print(f"[NAME_EXTRACTOR] Found via HR header: {name}")
        return name
    
    # STRATEGIA 3: Cerca vicino a keywords
    name = extract_near_keywords(lines)
    if name:
        print(f"[NAME_EXTRACTOR] Found via keywords: {name}")
        return name
    
    # STRATEGIA 4: Pattern regex generici
    name = extract_via_patterns(text)
    if name:
        print(f"[NAME_EXTRACTOR] Found via regex: {name}")
        return name
    
    # FALLBACK: Filename
    if pdf_filename:
        name = extract_name_from_filename(pdf_filename)
        if name:
            print(f"[NAME_EXTRACTOR] Fallback to filename: {name}")
            return name
    
    print(f"[NAME_EXTRACTOR] No name found")
    return None


def extract_before_gentile(lines: list) -> Optional[str]:
    """Cerca nominativo prima di 'Gentile collega'"""
    try:
        # Trova indice di "Gentile"
        gentile_idx = None
        for i, line in enumerate(lines):
            if 'gentile' in line.lower():
                gentile_idx = i
                break
        
        if gentile_idx is None or gentile_idx < 2:
            return None
        
        # Cerca nelle 10 righe precedenti
        for i in range(max(0, gentile_idx - 10), gentile_idx):
            candidate = lines[i]
            
            # Skip righe con keywords non rilevanti
            if any(kw in candidate.lower() for kw in ['poste italiane', 'viale europa', 'roma', 
                                                        'partita iva', 'codice fiscale', 
                                                        'hr business', 'responsabile',
                                                        'capitale sociale', 'sede legale']):
                continue
            
            # Deve avere almeno 2 parole
            words = candidate.split()
            if len(words) >= 2:
                # Verifica che sembri un nome (lettere, possibile apostrofo/trattino)
                if re.match(r'^[A-Za-zÃ€-Ã¿\'\-\s]+$', candidate):
                    # Non deve essere troppo lungo (max 50 caratteri)
                    if len(candidate) < 50:
                        return candidate.strip()
        
        return None
    except Exception as e:
        print(f"[NAME_EXTRACTOR] Error in extract_before_gentile: {e}")
        return None


def extract_after_hr_header(lines: list) -> Optional[str]:
    """Cerca nominativo dopo intestazione HR"""
    try:
        # Trova intestazione HR
        hr_idx = None
        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in ['hr business', 'risorse umane', 'responsabile']):
                hr_idx = i
                break
        
        if hr_idx is None or hr_idx >= len(lines) - 2:
            return None
        
        # Cerca nelle 10 righe successive
        for i in range(hr_idx + 1, min(hr_idx + 10, len(lines))):
            candidate = lines[i]
            
            # Skip righe sistema/azienda
            if any(kw in candidate.lower() for kw in ['poste italiane', 'viale europa', 
                                                        'digital', 'technology', 'operations',
                                                        'sede centrale', 'sede legale',
                                                        'roma', 'italia', 'nazione']):
                continue
            
            # Deve essere formato nome
            words = candidate.split()
            if 2 <= len(words) <= 4: # Nome Cognome o Nome Secondo Cognome
                if re.match(r'^[A-Za-zÃ€-Ã¿\'\-\s]+$', candidate):
                    if len(candidate) < 50:
                        return candidate.strip()
        
        return None
    except Exception as e:
        print(f"[NAME_EXTRACTOR] Error in extract_after_hr_header: {e}")
        return None


def extract_near_keywords(lines: list) -> Optional[str]:
    """Cerca nominativo vicino a keywords specifiche"""
    try:
        keywords = ['digital', 'technology', 'operations', 'sede centrale', 'sede legale']
        
        for i, line in enumerate(lines):
            # Se trovo keyword
            if any(kw in line.lower() for kw in keywords):
                # Guarda riga precedente
                if i > 0:
                    candidate = lines[i - 1]
                    
                    # Verifica che sembri un nome
                    words = candidate.split()
                    if 2 <= len(words) <= 4:
                        if re.match(r'^[A-Za-zÃ€-Ã¿\'\-\s]+$', candidate):
                            # Non deve contenere keywords aziendali
                            if not any(kw in candidate.lower() for kw in ['poste', 'italiane', 
                                                                           'viale', 'roma']):
                                if len(candidate) < 50:
                                    return candidate.strip()
        
        return None
    except Exception as e:
        print(f"[NAME_EXTRACTOR] Error in extract_near_keywords: {e}")
        return None


def extract_via_patterns(text: str) -> Optional[str]:
    """Cerca pattern specifici con regex"""
    try:
        # Pattern: Nome COGNOME (prima lettera maiuscola + TUTTO MAIUSCOLO)
        # Es: "Roberta CASTAGNOZZI"
        pattern = r'\b([A-Z][a-z]+)\s+([A-Z]{2,}(?:\s+[A-Z]{2,})?)\b'
        matches = re.findall(pattern, text)
        
        if matches:
            # Prendi prima occorrenza
            nome, cognome = matches[0]
            full_name = f"{nome} {cognome}".strip()
            
            # Verifica che non sia parte di intestazione
            if not any(kw in full_name.lower() for kw in ['poste italiane', 'spa']):
                return full_name
        
        return None
    except Exception as e:
        print(f"[NAME_EXTRACTOR] Error in extract_via_patterns: {e}")
        return None


def extract_name_from_filename(filename: str) -> Optional[str]:
    """
    Estrae nome da filename come ultima risorsa
    
    Esempi:
    - "Castagnozzi 01.12.2023.pdf" â†’ "Castagnozzi"
    - "IT-Chiese-firmato.pdf" â†’ "Chiese"
    - "Dolce Claudio_C_Firmato.pdf" â†’ "Dolce Claudio"
    """
    try:
        if not filename:
            return None
        
        # Rimuovi estensione
        name_part = filename.replace('.pdf', '').replace('.PDF', '')
        
        # Rimuovi suffissi comuni
        name_part = re.sub(r'[-_]firmato$', '', name_part, flags=re.IGNORECASE)
        name_part = re.sub(r'^IT[-_]', '', name_part, flags=re.IGNORECASE)
        
        # Pattern 1: "Cognome Nome" o "Nome Cognome" (con spazio)
        # Es: "Dolce Claudio_C_Firmato" â†’ "Dolce Claudio"
        parts = re.split(r'[-_\s]+', name_part)
        
        # Filtra parti che sembrano date o codici
        valid_parts = []
        for part in parts:
            # Skip se sembra data (es: "01.12.2023")
            if re.match(r'\d{2}\.\d{2}\.\d{4}', part):
                continue
            # Skip se Ã¨ singola lettera (es: "C")
            if len(part) == 1:
                continue
            # Skip se Ã¨ tutto numeri
            if part.isdigit():
                continue
            # Aggiungi
            valid_parts.append(part)
        
        # Se abbiamo 1-2 parti valide, Ã¨ probabilmente il nome
        if 1 <= len(valid_parts) <= 2:
            return ' '.join(valid_parts)
        
        # Altrimenti prendi prima parte
        if valid_parts:
            return valid_parts[0]
        
        return None
    except Exception as e:
        print(f"[NAME_EXTRACTOR] Error in extract_name_from_filename: {e}")
        return None


def test_extractor():
    """Test function"""
    
    # Test case 1: Testo Castagnozzi
    text1 = """
    CA/Risorse Umane e Organizzazione
    HR Business Partner MIPA e DTO
    Il Responsabile
    
    Roberta CASTAGNOZZI
    Digital, Technology & Operations
    Sede Centrale
    
    Roma, 1Â° dicembre 2023
    
    Gentile collega,
    siamo lieti di comunicarLe...
    """
    
    result1 = extract_name_from_raw_text(text1)
    print(f"Test 1: {result1}") # Deve dare "Roberta CASTAGNOZZI"
    
    # Test case 2: Filename fallback
    result2 = extract_name_from_raw_text("", "IT-Chiese-firmato.pdf")
    print(f"Test 2: {result2}") # Deve dare "Chiese"
    
    # Test case 3: Filename con data
    result3 = extract_name_from_raw_text("", "Castagnozzi 01.12.2023.pdf")
    print(f"Test 3: {result3}") # Deve dare "Castagnozzi"


if __name__ == "__main__":
    test_extractor()