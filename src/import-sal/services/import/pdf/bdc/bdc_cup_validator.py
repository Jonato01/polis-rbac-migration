"""
bdc_cup_validator.py - Validazione CUP a cascata
Pipeline intelligente per correggere CUP estratti male

Strategia:
1. Validazione formato base
2. Regex search nel testo OCR (con filtro anti-parole)
3. Context AI validation con Azure
4. Database lookup parziale
5. Accetta senza CUP (ultima risorsa)
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import json
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from ocr_common import get_connection_pool, get_db_config

load_dotenv()


def is_valid_cup_format(cup: str) -> bool:
    """
    Valida formato CUP italiano con filtro anti-parole
    
    Args:
        cup: Codice CUP da validare
    
    Returns:
        True se formato valido
    """
    if not cup or not isinstance(cup, str):
        return False
    
    cup = cup.upper()
    
    # CUP italiano: 15 caratteri alfanumerici, inizia con lettera
    if not re.match(r'^[A-Z][A-Z0-9]{14}$', cup):
        return False
    
    # IMPORTANTE: Deve contenere ALMENO un numero (evita parole italiane)
    if not any(c.isdigit() for c in cup):
        return False
    
    # Filtro anti-parole comuni italiane (15 lettere)
    parole_invalide = [
        'PREDISPOSIZIONE',
        'INSTALLAZIONE',
        'PROGETTAZIONE',
        'REALIZZAZIONE',
        'MANUTENZIONE',
    ]
    
    if cup in parole_invalide:
        return False
    
    return True


def search_cup_in_text(markdown: str, current_cup: str = None) -> Optional[str]:
    """
    Cerca CUP valido nel testo OCR usando regex con filtro
    
    Args:
        markdown: Testo OCR completo
        current_cup: CUP estratto (per evitare di ritrovare lo stesso)
    
    Returns:
        CUP trovato o None
    """
    print(" Strategia 2: Regex search nel testo...")
    
    # Pattern CUP: lettera + numeri misti (non solo lettere!)
    # Pattern più specifico: lettera iniziale, poi mix con almeno un numero
    patterns = [
        r'\bJ[A-Z0-9]{14}\b', # CUP che inizia con J (comune)
        r'\b[A-Z]\d[A-Z0-9]{13}\b', # Lettera + numero subito dopo
        r'\b[A-Z]{1,3}\d[A-Z0-9]{10,13}\b' # 1-3 lettere, poi numeri
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, markdown.upper())
        
        for match in matches:
            # Valida formato (include filtro anti-parole)
            if is_valid_cup_format(match):
                # Evita di trovare lo stesso CUP invalido
                if current_cup and match in current_cup:
                    continue
                
                print(f" CUP trovato via regex: {match}")
                return match
    
    print(" Nessun CUP trovato via regex")
    return None


def call_azure_for_cup_validation(markdown: str, current_cup: str) -> Optional[str]:
    """
    Chiama Azure OpenAI per validazione CUP
    
    Args:
        markdown: Testo OCR
        current_cup: CUP da validare
        
    Returns:
        CUP corretto o None
    """
    from openai import AzureOpenAI
    
    client = AzureOpenAI(
        api_version="2024-12-01-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    )
    
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
    
    # Limita testo per risparmio token
    text_excerpt = markdown[:3000]
    
    prompt = f"""Sei un esperto di estrazione dati da documenti italiani.

COMPITO: Trova il codice CUP (Codice Unico di Progetto) nel testo.

REGOLE CUP:
- Esattamente 15 caratteri alfanumerici
- Inizia tipicamente con "J" (ma può essere altra lettera)
- DEVE contenere almeno un numero (non solo lettere!)
- Esempio valido: J59J21014170008
- Esempio invalido: PREDISPOSIZIONE (è una parola italiana!)

CUP ESTRATTO (probabilmente errato): {current_cup}

TESTO DEL DOCUMENTO:
{text_excerpt}

ISTRUZIONI:
1. Cerca nel testo un codice di ESATTAMENTE 15 caratteri
2. Controlla che inizi con una lettera maiuscola
3. Verifica che contenga ALMENO un numero
4. NON accettare parole italiane come PREDISPOSIZIONE
5. Se non trovi nulla, rispondi "NOT_FOUND"

Rispondi SOLO con il codice CUP (15 caratteri) o "NOT_FOUND".
NO spiegazioni, NO markdown, SOLO il codice o NOT_FOUND."""
    
    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": "Estrai solo il codice CUP."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=50,
            temperature=0.0
        )
        
        cup_found = response.choices[0].message.content.strip().replace('`', '').replace('"', '').strip()
        
        return cup_found if cup_found != "NOT_FOUND" else None
        
    except Exception as e:
        print(f" Errore Azure OpenAI: {e}")
        return None


def validate_cup_with_context_ai(markdown: str, current_cup: str, attempt: int = 1) -> Optional[str]:
    """
    Chiedi ad Azure OpenAI di trovare CUP nel contesto
    
    Args:
        markdown: Testo OCR completo
        current_cup: CUP estratto (probabilmente invalido)
        attempt: Numero tentativo
    
    Returns:
        CUP corretto o None
    """
    print(f" Strategia 3: Context AI validation (tentativo {attempt}/2)...")
    
    cup_found = call_azure_for_cup_validation(markdown, current_cup)
    
    if not cup_found:
        print(" AI non ha trovato CUP valido")
        return None
    
    # Valida formato (include filtro anti-parole)
    if is_valid_cup_format(cup_found):
        print(f" CUP validato da AI: {cup_found}")
        return cup_found
    else:
        print(f" AI ha risposto ma formato invalido: {cup_found}")
        return None


def search_cup_in_database(partial_cup: str) -> Optional[str]:
    """
    Cerca CUP in database usando ultimi 10 caratteri
    
    Args:
        partial_cup: CUP parziale/invalido
    
    Returns:
        CUP trovato in DB o None
    """
    print("Strategia 4: Database lookup...")
    
    if not partial_cup or len(partial_cup) < 10:
        print(" CUP troppo corto per lookup DB")
        return None
    
    # Prendi ultimi 10 caratteri
    suffix = partial_cup[-10:]
    
    try:
        db_config = get_db_config()
        pool = get_connection_pool(db_config)
        conn = pool.get_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT DISTINCT cup 
            FROM bdc_pdf 
            WHERE cup LIKE %s 
            AND LENGTH(cup) = 15
            LIMIT 5
        """
        
        cursor.execute(query, (f'%{suffix}',))
        results = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        if results:
            cup_found = results[0][0]
            print(f" CUP trovato in DB: {cup_found}")
            
            if len(results) > 1:
                print(f" Trovati {len(results)} match, uso il primo")
            
            return cup_found
        else:
            print(" Nessun match in database")
            return None
            
    except Exception as e:
        print(f" Errore database lookup: {e}")
        return None


def validate_and_correct_cup(
    cup: str,
    markdown: str,
    max_ai_attempts: int = 2
) -> Dict[str, Any]:
    """
    Pipeline completa di validazione/correzione CUP
    
    Args:
        cup: CUP estratto da Azure OpenAI
        markdown: Testo OCR completo
        max_ai_attempts: Numero max tentativi Context AI
    
    Returns:
        Dict con:
        - cup: CUP finale (corretto o originale)
        - valid: True se formato valido
        - method: Metodo usato per correzione
        - confidence: Alta/Media/Bassa
    """
    result = {
        'cup': cup,
        'valid': False,
        'method': 'original',
        'confidence': 'low'
    }
    
    # Step 1: Validazione formato base (con filtro anti-parole)
    if is_valid_cup_format(cup):
        print(f" CUP valido: {cup}")
        result['valid'] = True
        result['confidence'] = 'high'
        return result
    
    print(f" CUP formato invalido: {cup}")
    
    # Step 2: Regex search (con filtro)
    cup_regex = search_cup_in_text(markdown, cup)
    if cup_regex:
        result['cup'] = cup_regex
        result['valid'] = True
        result['method'] = 'regex'
        result['confidence'] = 'high'
        return result
    
    # Step 3: Context AI validation (con retry)
    for attempt in range(1, max_ai_attempts + 1):
        cup_ai = validate_cup_with_context_ai(markdown, cup, attempt)
        if cup_ai:
            result['cup'] = cup_ai
            result['valid'] = True
            result['method'] = f'context_ai_attempt_{attempt}'
            result['confidence'] = 'medium'
            return result
        
        if attempt < max_ai_attempts:
            print(" Retry Context AI...")
    
    # Step 4: Database lookup
    cup_db = search_cup_in_database(cup)
    if cup_db:
        result['cup'] = cup_db
        result['valid'] = True
        result['method'] = 'database'
        result['confidence'] = 'medium'
        return result
    
    # Step 5: Accetta senza CUP valido
    print("Nessuna correzione possibile, accetto risultato originale")
    result['method'] = 'none'
    result['confidence'] = 'low'
    
    return result