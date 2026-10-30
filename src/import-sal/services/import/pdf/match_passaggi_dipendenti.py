#!/usr/bin/env python3
"""
Matching automatico cod_dipendente per passaggi_livello_sal
v2.0 - AI-first + Fuzzy fallback con normalizzazione
"""

import sys
import os
from difflib import SequenceMatcher

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from ocr_common import get_db_config, fetch_mysql_query, execute_single_query


def normalize_text(text):
    """
    Normalizza testo per matching robusto
    
    - Rimuove apostrofi (', ', `)
    - Rimuove accenti (Ã€â†’A, Ãˆâ†’E, etc)
    - Normalizza spazi multipli
    """
    if not text:
        return ""
    
    text = str(text).upper().strip()
    
    # Rimuovi apostrofi e backtick
    text = text.replace("'", "").replace("'", "").replace("`", "")
    
    # Rimuovi accenti comuni italiani
    replacements = {
        "Ã€": "A", "Ã": "A", "Ã‚": "A", "Ãƒ": "A", "Ã„": "A",
        "Ãˆ": "E", "Ã‰": "E", "ÃŠ": "E", "Ã‹": "E",
        "ÃŒ": "I", "Ã": "I", "ÃŽ": "I", "Ã": "I",
        "Ã’": "O", "Ã“": "O", "Ã”": "O", "Ã•": "O", "Ã–": "O",
        "Ã™": "U", "Ãš": "U", "Ã›": "U", "Ãœ": "U",
    }
    
    for old, new in replacements.items():
        text = text.replace(old, new)
    
    # Normalizza spazi multipli
    text = " ".join(text.split())
    
    return text


def smart_match_dipendente(nominativo, dipendenti):
    """
    Match intelligente: AI-first, poi fuzzy fallback con normalizzazione
    
    FLUSSO:
    1. Prova con AI (top 20 candidati piÃ¹ probabili per cognome)
    2. Se AI fallisce â†’ Fuzzy matching con normalizzazione (threshold 60%)
    3. Se fuzzy trova match ambiguo (60-90%) â†’ Chiedi conferma AI
    4. Se niente funziona â†’ return None
    
    Args:
        nominativo: "Fiaschini Luca" o "Luca Fiaschini" o "LUIGI SALVATORE DELLA CARITA'"
        dipendenti: Lista dizionari con cod_dipendente, nome, cognome
    
    Returns:
        Dizionario dipendente matchato oppure None
    """
    if not nominativo or not dipendenti:
        return None
    
    print(f" Matching: {nominativo}")
    
    # ========== FASE 1: AI-FIRST ==========
    print(f"  Phase 1: Asking AI...")
    
    # Estrai parole per trovare candidati intelligenti
    words = normalize_text(nominativo).split()
    
    # Trova candidati che condividono almeno una parola (probabilmente il cognome)
    smart_candidates = []
    for dip in dipendenti:
        nome_norm = normalize_text(dip.get('nome', ''))
        cognome_norm = normalize_text(dip.get('cognome', ''))
        full_norm = f"{nome_norm} {cognome_norm}"
        
        # Se almeno una parola del nominativo Ã¨ nel nome completo del dipendente
        if any(word in full_norm for word in words if len(word) > 2):
            smart_candidates.append({
                'dipendente': dip,
                'similarity': 0, # Non calcolata ancora
                'nome_completo': f"{dip.get('nome', '')} {dip.get('cognome', '')}"
            })
    
    # Se abbiamo candidati intelligenti, usa quelli; altrimenti top 20 generici
    if smart_candidates:
        ai_candidates = smart_candidates[:20] # Max 20 per non sovraccaricare AI
        print(f" Found {len(ai_candidates)} smart candidates for AI")
    else:
        ai_candidates = [
            {
                'dipendente': d,
                'similarity': 0,
                'nome_completo': f"{d.get('nome', '')} {d.get('cognome', '')}"
            }
            for d in dipendenti[:20]
        ]
        print(f" Using top 20 generic candidates for AI")
    
    ai_match = ask_ai_for_match(nominativo, ai_candidates)
    if ai_match:
        print(f"  AI match found: {ai_match['nome_completo']} (ID: {ai_match['dipendente'].get('cod_dipendente')})")
        return ai_match['dipendente']
    
    print(f"  AI could not find match")
    
    # ========== FASE 2: FUZZY MATCHING CON NORMALIZZAZIONE ==========
    print(f" Phase 2: Fuzzy matching with normalization...")
    
    nominativo_norm = normalize_text(nominativo)
    words_norm = [w for w in nominativo_norm.split() if len(w) > 1]
    
    if len(words_norm) < 2:
        print(f"  Not enough words for fuzzy matching")
        return None
    
    fuzzy_candidates = []
    
    # Per ogni dipendente nel DB
    for dip in dipendenti:
        nome_db_norm = normalize_text(dip.get('nome', ''))
        cognome_db_norm = normalize_text(dip.get('cognome', ''))
        
        if not nome_db_norm or not cognome_db_norm:
            continue
        
        # Fuzzy matching con testo normalizzato
        # Prova tutte le combinazioni (nome-cognome, cognome-nome)
        combo1 = f"{words_norm[0]} {words_norm[-1]}" # Es: "LUIGI CARITA"
        combo2 = f"{words_norm[-1]} {words_norm[0]}" # Es: "CARITA LUIGI"
        
        # Se ci sono piÃ¹ di 2 parole, prova anche stringa completa
        if len(words_norm) > 2:
            combo_full = " ".join(words_norm) # Es: "LUIGI SALVATORE DELLA CARITA"
        else:
            combo_full = combo1
        
        db_combo1 = f"{cognome_db_norm} {nome_db_norm}"
        db_combo2 = f"{nome_db_norm} {cognome_db_norm}"
        db_full = f"{nome_db_norm} {cognome_db_norm}"
        
        # Calcola similaritÃ  per tutte le combinazioni
        sim1 = SequenceMatcher(None, combo1, db_combo1).ratio()
        sim2 = SequenceMatcher(None, combo1, db_combo2).ratio()
        sim3 = SequenceMatcher(None, combo2, db_combo1).ratio()
        sim4 = SequenceMatcher(None, combo2, db_combo2).ratio()
        sim5 = SequenceMatcher(None, combo_full, db_full).ratio()
        
        max_similarity = max(sim1, sim2, sim3, sim4, sim5)
        
        # Abbassato threshold a 60% (era 70%)
        if max_similarity > 0.60:
            fuzzy_candidates.append({
                'dipendente': dip,
                'similarity': max_similarity,
                'nome_completo': f"{dip.get('nome', '')} {dip.get('cognome', '')}"
            })
    
    # Ordina candidati per similaritÃ 
    fuzzy_candidates.sort(key=lambda x: x['similarity'], reverse=True)
    
    if not fuzzy_candidates:
        print(f"  No fuzzy candidates found (threshold 60%)")
        return None
    
    # Se c'Ã¨ un candidato con >90% similaritÃ , usalo direttamente
    if fuzzy_candidates[0]['similarity'] >= 0.90:
        best = fuzzy_candidates[0]
        print(f"  Exact fuzzy match: {best['nome_completo']} "
              f"(ID: {best['dipendente'].get('cod_dipendente')}, sim: {best['similarity']:.1%})")
        return best['dipendente']
    
    # Se similaritÃ  60-90%, chiedi conferma AI
    if 0.60 <= fuzzy_candidates[0]['similarity'] < 0.90:
        print(f"  Ambiguous fuzzy match (sim: {fuzzy_candidates[0]['similarity']:.1%}), asking AI for confirmation...")
        top_5 = fuzzy_candidates[:5]
        
        ai_choice = ask_ai_for_match(nominativo, top_5)
        if ai_choice:
            print(f"  AI confirmed: {ai_choice['nome_completo']} "
                  f"(ID: {ai_choice['dipendente'].get('cod_dipendente')})")
            return ai_choice['dipendente']
    
    print(f"  No confident match found for: {nominativo}")
    return None


def ask_ai_for_match(nominativo_pdf, candidates):
    """
    Chiede all'AI di scegliere il match migliore
    
    Args:
        nominativo_pdf: Nome estratto dal PDF
        candidates: Lista dizionari con 'dipendente', 'similarity', 'nome_completo'
    
    Returns:
        Candidato scelto dall'AI oppure None
    """
    try:
        from openai import AzureOpenAI
        
        api_key = os.getenv('AZURE_OPENAI_KEY')
        endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
        deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4o')
        
        if not all([api_key, endpoint, deployment]):
            print("  AI not configured (missing env vars), skipping AI")
            return None
        
        # Prepara lista candidati
        candidates_text = ""
        for i, cand in enumerate(candidates, 1):
            sim_text = f" (similarity: {cand['similarity']:.1%})" if cand['similarity'] > 0 else ""
            candidates_text += f"{i}. {cand['nome_completo']}{sim_text}\n"
        
        prompt = f"""Nel PDF di passaggio livello c'Ã¨ scritto: "{nominativo_pdf}"

Candidati dipendenti nel database:
{candidates_text}

COMPITO: Quale candidato corrisponde ESATTAMENTE al nome nel PDF?

REGOLE:
- Considera possibili typo o errori OCR (es: Flaschini â†’ Fiaschini)
- Considera inversione nome/cognome
- Considera apostrofi/accenti diversi (DELLA CARITA' = DELLA CARITÃ€)
- Considera nomi composti (LUIGI SALVATORE puÃ² essere nome completo)
- Se NESSUNO matcha in modo certo, rispondi "0"

Rispondi SOLO con il numero del candidato (1-{len(candidates)}) oppure 0 se nessuno matcha."""

        client = AzureOpenAI(
            api_key=api_key,
            api_version="2024-02-15-preview",
            azure_endpoint=endpoint
        )
        
        response = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0
        )
        
        answer = response.choices[0].message.content.strip()
        
        # Parse risposta
        try:
            choice_num = int(answer)
            if 1 <= choice_num <= len(candidates):
                return candidates[choice_num - 1]
            else:
                print(f"  AI said: no match (answered {choice_num})")
                return None
        except ValueError:
            print(f"  AI gave invalid answer: {answer}")
            return None
            
    except Exception as e:
        print(f"  AI error: {e}")
        return None


def match_passaggi_livello_dipendenti(sal_number=8, dry_run=False):
    """
    Esegue matching automatico per passaggi_livello_sal
    
    Args:
        sal_number: Numero SAL da processare
        dry_run: Se True, mostra solo cosa farebbe senza modificare DB
    
    Returns:
        Dizionario con statistiche
    """
    print(f"\n{'='*60}")
    print(f"MATCHING AUTOMATICO PASSAGGI LIVELLO - SAL {sal_number}")
    print(f"Version: 2.0 (AI-FIRST + NORMALIZATION FALLBACK)")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")
    
    db_config = get_db_config()
    
    # Step 1: Carica tutti i dipendenti
    print("Loading dipendenti from database...")
    dipendenti = fetch_mysql_query(
        "SELECT cod_dipendente, nome, cognome FROM dipendenti",
        db_config
    )
    
    if not dipendenti:
        print(" ERROR: Could not load dipendenti")
        return {'error': 'no_dipendenti'}
    
    print(f" Loaded {len(dipendenti)} dipendenti\n")
    
    # Step 2: Carica passaggi senza cod_dipendente
    print(f"Loading passaggi_livello_sal without cod_dipendente (SAL {sal_number})...")
    
    query = f"""
        SELECT p.id, p.nome_cognome, p.livello_nuovo, p.data_decorrenza
        FROM passaggi_livello_sal p
        INNER JOIN sal s ON p.id_sal = s.id_sal
        WHERE p.cod_dipendente IS NULL
        AND s.numero_sal = {sal_number}
        ORDER BY p.id
    """
    
    passaggi = fetch_mysql_query(query, db_config)
    
    if not passaggi:
        print(" No passaggi without cod_dipendente found!")
        return {'total': 0, 'matched': 0, 'not_matched': 0}
    
    print(f"Found {len(passaggi)} passaggi to match\n")
    print("="*60)
    
    # Step 3: Match ciascun passaggio
    stats = {
        'total': len(passaggi),
        'matched': 0,
        'not_matched': 0,
        'skipped': 0
    }
    
    for i, passaggio in enumerate(passaggi, 1):
        id_passaggio = passaggio['id']
        nome_cognome = passaggio['nome_cognome']
        
        print(f"\n[{i}/{len(passaggi)}] Processing ID {id_passaggio}")
        
        # Fuzzy + AI matching (AI-first)
        match = smart_match_dipendente(nome_cognome, dipendenti)
        
        if match:
            cod_dipendente = match['cod_dipendente']
            stats['matched'] += 1
            
            if not dry_run:
                # Update database
                update_query = f"""
                    UPDATE passaggi_livello_sal 
                    SET cod_dipendente = {cod_dipendente}
                    WHERE id = {id_passaggio}
                """
                
                success = execute_single_query(update_query, db_config)
                
                if success:
                    print(f" ðŸ’¾ Updated in database")
                else:
                    print(f"  Database update failed")
                    stats['matched'] -= 1
                    stats['skipped'] += 1
            else:
                print(f" ðŸ“ DRY RUN: Would update cod_dipendente to {cod_dipendente}")
        else:
            stats['not_matched'] += 1
            print(f"  No match found - will need manual review")
        
        print("-" * 60)
    
    # Step 4: Summary
    print(f"\n{'='*60}")
    print(f"MATCHING COMPLETED")
    print(f"{'='*60}")
    print(f"Total records: {stats['total']}")
    print(f" Matched: {stats['matched']}")
    print(f" Not matched: {stats['not_matched']}")
    if stats['skipped'] > 0:
        print(f" Skipped: {stats['skipped']}")
    print(f"Success rate: {stats['matched']/stats['total']*100:.1f}%")
    print(f"{'='*60}\n")
    
    return stats


def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Match passaggi_livello_sal with dipendenti (v2.0 AI-FIRST)')
    parser.add_argument('--sal', type=int, default=8, help='SAL number to process')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without updating DB')
    
    args = parser.parse_args()
    
    try:
        stats = match_passaggi_livello_dipendenti(
            sal_number=args.sal,
            dry_run=args.dry_run
        )
        
        if stats.get('error'):
            sys.exit(1)
        
        # Exit code based on success rate
        if stats['matched'] == stats['total']:
            sys.exit(0) # Perfect match
        elif stats['matched'] > 0:
            sys.exit(0) # Partial success
        else:
            sys.exit(1) # No matches
            
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()