#!/usr/bin/env python3
"""
Script per rimuovere emoji da file Python
"""
import os
import re
from pathlib import Path

def remove_emojis(text):
    """Rimuove solo emoji comuni senza toccare indentazione e formattazione"""
    # Lista di emoji comuni da rimuovere
    emojis_to_remove = [
        '', '', '', '', '', '', '', '', '', 
        '', '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', '',
        '', '', '', '', '', '', '', ''
    ]
    
    # Rimuove ogni emoji dalla lista
    for emoji in emojis_to_remove:
        text = text.replace(emoji, '')
    
    # Pattern più sicuro per emoji Unicode rimanenti (esclusi spazi bianchi)
    # Solo emoji veri, non caratteri di controllo o whitespace
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F" # emoticons
        "\U0001F300-\U0001F5FF" # symbols & pictographs
        "\U0001F680-\U0001F6FF" # transport & map
        "\U0001F1E0-\U0001F1FF" # flags
        "\U00002700-\U000027BF" # dingbats
        "\U0001F900-\U0001F9FF" # supplemental symbols
        "\U0001FA00-\U0001FA6F" # chess symbols
        "]+",
        flags=re.UNICODE
    )
    
    text = emoji_pattern.sub('', text)
    
    # Pulisce SOLO spazi multipli consecutivi NON in inizio riga (preserva indentazione)
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        # Preserva indentazione, pulisce solo spazi nel testo
        if line.strip(): # Se la riga ha contenuto
            # Conta spazi iniziali
            indent = len(line) - len(line.lstrip())
            # Pulisce il resto della riga
            content = line[indent:]
            content = re.sub(r' +', ' ', content)
            cleaned_lines.append(' ' * indent + content)
        else:
            cleaned_lines.append(line)
    
    return '\n'.join(cleaned_lines)

def process_file(filepath):
    """Processa un singolo file Python"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            original_content = f.read()
        
        # Rimuove emoji
        cleaned_content = remove_emojis(original_content)
        
        # Se il contenuto è cambiato, salva
        if cleaned_content != original_content:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(cleaned_content)
            return True
        return False
    except Exception as e:
        print(f" Errore processando {filepath}: {e}")
        return False

def main():
    # Directory root (usa la directory del progetto)
    root_dir = Path("./")
    
    print(f"Scansione di: {root_dir}")
    print("=" * 60)
    
    modified_files = []
    total_files = 0
    
    # Scansiona tutti i file .py
    for py_file in root_dir.rglob("*.py"):
        total_files += 1
        if process_file(py_file):
            modified_files.append(py_file)
            print(f" Modificato: {py_file.relative_to(root_dir)}")
    
    print("\n" + "=" * 60)
    print(f"File processati: {total_files}")
    print(f"File modificati: {len(modified_files)}")
    print("=" * 60)
    
    if modified_files:
        print("\nFile modificati:")
        for f in modified_files:
            print(f" - {f.relative_to(root_dir)}")

if __name__ == "__main__":
    main()