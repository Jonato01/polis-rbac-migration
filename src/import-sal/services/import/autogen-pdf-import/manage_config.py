"""
Utility per gestione configurazione tipi documento
"""
import json
import sys
from typing import Optional

from config.document_types import (
    load_document_types_from_db,
    get_document_config,
    reload_config,
    search_table_by_name
)


def list_document_types():
    """Lista tutti i tipi documento configurati"""
    print("\n" + "=" * 80)
    print("TIPI DOCUMENTO CONFIGURATI")
    print("=" * 80)

    config = load_document_types_from_db()

    if not config:
        print("⚠ Nessun tipo documento configurato")
        return

    print(f"\nTotale: {len(config)} tipi")
    print("\n" + "-" * 80)

    for doc_type, doc_config in config.items():
        print(f"\n📄 {doc_type}: {doc_config['name']}")
        print(f"   Descrizione: {doc_config['description']}")
        print(f"   Tabella: {doc_config['tables']['main']}")
        if doc_config['tables'].get('detail'):
            print(f"   Tabella dettaglio: {doc_config['tables']['detail']}")
        print(f"   Campi: {len(doc_config['fields'])}")
        print(f"   OCR: {doc_config['ocr_strategy']}")
        print(f"   Max pagine: {doc_config['max_pages']}")

        # Hints
        hints = doc_config.get('classification_hints', [])
        if hints:
            print(f"   Hints: {', '.join(hints[:3])}")


def show_document_type(doc_type: str):
    """Mostra dettagli di un tipo documento specifico"""
    print("\n" + "=" * 80)
    print(f"DETTAGLI TIPO DOCUMENTO: {doc_type}")
    print("=" * 80)

    config = get_document_config(doc_type)

    if not config:
        print(f"✗ Tipo documento '{doc_type}' non trovato")
        return

    print(f"\nNome: {config['name']}")
    print(f"Descrizione: {config['description']}")

    print(f"\n📊 Tabelle:")
    print(f"  Principale: {config['tables']['main']}")
    if config['tables'].get('detail'):
        print(f"  Dettaglio: {config['tables']['detail']}")

    print(f"\n🔍 Classificazione:")
    hints = config.get('classification_hints', [])
    print(f"  Hints: {', '.join(hints) if hints else 'Nessuno'}")

    patterns = config.get('filename_patterns', [])
    if patterns:
        print(f"\n📁 Pattern Nome File:")
        for pattern in patterns:
            print(f"  - {pattern}")

    print(f"\n🎯 OCR:")
    print(f"  Strategia: {config['ocr_strategy']}")
    print(f"  Max pagine: {config['max_pages']}")

    if config.get('extraction_notes'):
        print(f"\n📝 Note Estrazione:")
        print(f"  {config['extraction_notes']}")

    print(f"\n🗃️ Campi ({len(config['fields'])}):")
    for field_name, field_config in config['fields'].items():
        required = "✓" if field_config.get('required') else " "
        field_type = field_config.get('type', 'string')
        description = field_config.get('description', '')

        print(f"  [{required}] {field_name} ({field_type})")
        if description:
            print(f"      {description}")

    if config.get('detail_fields'):
        print(f"\n📋 Campi Dettaglio ({len(config['detail_fields'])}):")
        for field_name, field_config in config['detail_fields'].items():
            field_type = field_config.get('type', 'string')
            description = field_config.get('description', '')
            print(f"  - {field_name} ({field_type}): {description}")

    print(f"\n💾 SQL Template:")
    sql = config.get('sql_template', '')
    print(f"  {sql[:200]}...")


def search_tables(keyword: str):
    """Cerca tabelle che matchano una keyword"""
    print("\n" + "=" * 80)
    print(f"RICERCA TABELLE: '{keyword}'")
    print("=" * 80)

    tables = search_table_by_name(keyword)

    if not tables:
        print(f"\n⚠ Nessuna tabella trovata con keyword '{keyword}'")
        return

    print(f"\nTrovate {len(tables)} tabelle:")
    for table in tables:
        print(f"  - {table}")


def show_fields(doc_type: str):
    """Mostra solo i campi di un tipo documento"""
    config = get_document_config(doc_type)

    if not config:
        print(f"✗ Tipo documento '{doc_type}' non trovato")
        return

    print("\n" + "=" * 80)
    print(f"CAMPI: {doc_type} ({config['name']})")
    print("=" * 80)

    print(f"\nTabella: {config['tables']['main']}")
    print(f"Campi totali: {len(config['fields'])}")

    # Campi obbligatori
    required_fields = [
        name for name, conf in config['fields'].items()
        if conf.get('required')
    ]

    if required_fields:
        print(f"\n✓ Campi Obbligatori ({len(required_fields)}):")
        for field_name in required_fields:
            field_config = config['fields'][field_name]
            print(f"  - {field_name} ({field_config.get('type')})")
            print(f"    {field_config.get('description', '')}")

    # Campi opzionali
    optional_fields = [
        name for name, conf in config['fields'].items()
        if not conf.get('required')
    ]

    if optional_fields:
        print(f"\n○ Campi Opzionali ({len(optional_fields)}):")
        for field_name in optional_fields:
            field_config = config['fields'][field_name]
            print(f"  - {field_name} ({field_config.get('type')})")


def generate_prompt(doc_type: str):
    """Genera e mostra il prompt di estrazione"""
    print("\n" + "=" * 80)
    print(f"PROMPT ESTRAZIONE: {doc_type}")
    print("=" * 80)

    from agents.importers import get_importer

    importer = get_importer(doc_type)

    if not importer:
        print(f"✗ Importer non trovato per '{doc_type}'")
        return

    prompt = importer.get_extraction_prompt()

    print(f"\nLunghezza: {len(prompt)} caratteri")
    print("\n" + "-" * 80)
    print(prompt)
    print("-" * 80)


def main():
    """Entry point"""
    if len(sys.argv) < 2:
        print("""
Uso: python manage_config.py <comando> [argomenti]

Comandi disponibili:

  list                    Lista tutti i tipi documento
  show <TIPO>            Mostra dettagli tipo documento
  fields <TIPO>          Mostra solo i campi
  prompt <TIPO>          Mostra prompt estrazione
  search <KEYWORD>       Cerca tabelle per keyword
  reload                 Ricarica configurazione da DB

Esempi:

  python manage_config.py list
  python manage_config.py show AQ
  python manage_config.py fields FATTURA
  python manage_config.py prompt BDC
  python manage_config.py search fattur
  python manage_config.py reload
        """)
        sys.exit(1)

    command = sys.argv[1].lower()

    try:
        if command == 'list':
            list_document_types()

        elif command == 'show':
            if len(sys.argv) < 3:
                print("✗ Specificare tipo documento: python manage_config.py show AQ")
                sys.exit(1)
            show_document_type(sys.argv[2].upper())

        elif command == 'fields':
            if len(sys.argv) < 3:
                print("✗ Specificare tipo documento: python manage_config.py fields AQ")
                sys.exit(1)
            show_fields(sys.argv[2].upper())

        elif command == 'prompt':
            if len(sys.argv) < 3:
                print("✗ Specificare tipo documento: python manage_config.py prompt AQ")
                sys.exit(1)
            generate_prompt(sys.argv[2].upper())

        elif command == 'search':
            if len(sys.argv) < 3:
                print("✗ Specificare keyword: python manage_config.py search fattur")
                sys.exit(1)
            search_tables(sys.argv[2])

        elif command == 'reload':
            print("\n⟳ Ricaricamento configurazione da database...")
            reload_config()
            print("✓ Configurazione ricaricata")

            from agents.importers import clear_importer_cache
            clear_importer_cache()
            print("✓ Cache importer svuotata")

            list_document_types()

        else:
            print(f"✗ Comando sconosciuto: {command}")
            print("Usa 'python manage_config.py' per vedere l'help")
            sys.exit(1)

    except Exception as e:
        print(f"\n✗ Errore: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

